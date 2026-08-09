# Blender add-on to import and export 3MF files.
# Copyright (C) 2025 Jack (modernization for Blender 4.2+)
# This add-on is free software; you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either version 2 of the License, or (at your option) any later
# version.
# This add-on is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with this program; if not, write to the Free
# Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# <pep8 compliant>

"""
Public API for programmatic 3MF import and export.

These entry points let other Blender addons, CLI scripts, and headless
automation workflows import or export 3MF files *without* going through
Blender operator invocation (``bpy.ops``).  They build the appropriate
context objects, run the same pipeline code as the operators, and return
lightweight result dataclasses.

Quick start::

    from io_mesh_3mf.api import import_3mf, export_3mf

    # Import
    result = import_3mf("/path/to/model.3mf", import_materials="PAINT")
    print(result.status, result.num_loaded, result.objects)

    # Export
    result = export_3mf(
        "/path/to/output.3mf",
        use_orca_format="AUTO",
        use_selection=True,
    )
    print(result.status, result.num_written)

Inspect without importing::

    from io_mesh_3mf.api import inspect_3mf

    info = inspect_3mf("/path/to/model.3mf")
    print(info.unit, info.num_objects, info.num_triangles_total)
    for obj in info.objects:
        print(obj["name"], obj["num_vertices"], obj["num_triangles"])

Batch operations::

    from io_mesh_3mf.api import batch_import

    results = batch_import(["/a.3mf", "/b.3mf"], import_materials="PAINT")
    for r in results:
        print(r.status, r.num_loaded)

Building blocks for custom workflows::

    from io_mesh_3mf.api import colors, types, segmentation, units
"""

from __future__ import annotations

import os
import xml.etree.ElementTree
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import bpy  # type: ignore

from .common.annotations import Annotations
from .common.constants import (
    MATERIAL_NAMESPACE,
    MODEL_MIMETYPE,
    MODEL_NAMESPACES,
    RELS_MIMETYPE,
    SUPPORTED_EXTENSIONS,
)
from .common.extensions import ExtensionManager
from .common.logging import debug, error, warn
from .common.metadata import Metadata, MetadataEntry
from .common.units import (
    blender_to_metre,
    export_unit_scale,
    threemf_to_metre,
)

# ═══════════════════════════════════════════════════════════════════════════
# API Version & Registry
# ═══════════════════════════════════════════════════════════════════════════
#
# Discovery strategies (from simplest to most robust):
#
# 1. Direct import — if you can import this module, the API is available:
#
#        try:
#            from io_mesh_3mf.api import import_3mf, export_3mf
#        except ImportError:
#            import_3mf = export_3mf = None
#
# 2. Driver-namespace lookup — works after register() has been called:
#
#        threemf_api = bpy.app.driver_namespace.get("io_mesh_3mf")
#
# 3. Standalone helper — copy threemf_discovery.py into your addon:
#
#        from .threemf_discovery import get_threemf_api
#        api = get_threemf_api()  # None if not installed

#: API version following semantic versioning (MAJOR.MINOR.PATCH).
#: - MAJOR: Breaking changes to existing functions/signatures
#: - MINOR: New features, backward-compatible
#: - PATCH: Bug fixes only
API_VERSION = (1, 3, 2)

#: Human-readable version string
API_VERSION_STRING = ".".join(str(v) for v in API_VERSION)

#: Capability flags for feature detection. Other addons can check these
#: to determine what functionality is available without version parsing.
API_CAPABILITIES = frozenset(
    {
        "import",  # import_3mf() available
        "export",  # export_3mf() available
        "inspect",  # inspect_3mf() available
        "batch",  # batch_import/batch_export available
        "callbacks",  # on_progress, on_warning, on_object_created
        "target_collection",  # import to specific collection
        "orca_format",  # Orca/BambuStudio export format
        "prusa_format",  # PrusaSlicer export format
        "paint_mode",  # MMU paint segmentation
        "project_template",  # Custom Orca project template
        "object_settings",  # Per-object Orca settings
        "building_blocks",  # colors, types, segmentation sub-namespaces
        "global_scale",  # Scale multiplier parameter (import & export)
        "compression",  # Configurable ZIP compression level (export)
        "thumbnail",  # Thumbnail generation (export)
        "use_components",  # Component instancing for linked duplicates (export)
        "auto_smooth",  # Auto smooth-by-angle on import
        "subdivision_depth",  # Paint segmentation subdivision depth control
        "flatten_hierarchy",  # Option to flatten parented meshes into top-level build items (export)
        "modifier_parts",  # Orca/BambuStudio modifier part subtypes (import, export, inspect)
        "slicer_profile",  # Named slicer profile selection (export)
    }
)

#: Registry key in bpy.app.driver_namespace
_REGISTRY_KEY = "io_mesh_3mf"

# Tracks whether unregister() explicitly disabled the API for this session.
# Prevents is_available() from re-registering after the addon is disabled.
_explicitly_disabled = False


def _register_api() -> None:
    """Register this API module in bpy.app.driver_namespace for discovery."""
    global _explicitly_disabled
    import sys

    _explicitly_disabled = False
    bpy.app.driver_namespace[_REGISTRY_KEY] = sys.modules[__name__]
    debug(f"Registered 3MF API v{API_VERSION_STRING} in driver_namespace")


def _unregister_api() -> None:
    """Remove the API from bpy.app.driver_namespace."""
    global _explicitly_disabled
    _explicitly_disabled = True
    bpy.app.driver_namespace.pop(_REGISTRY_KEY, None)


def is_available() -> bool:
    """Check if the 3MF API is loaded and available.

    If you can import this function, the underlying code is available.
    This call ensures the module is registered in
    ``bpy.app.driver_namespace`` so that :func:`get_api` and the
    standalone discovery helper (:mod:`~io_mesh_3mf.threemf_discovery`)
    can find it.

    Returns ``False`` only when the addon has been explicitly disabled
    via ``unregister()`` in the current session.

    :return: True if the API is ready to use.

    Example::

        from io_mesh_3mf.api import is_available
        if is_available():
            print("3MF API is ready")
    """
    # Fast path — already registered.
    if _REGISTRY_KEY in bpy.app.driver_namespace:
        return True
    # Addon was explicitly disabled this session (unregister called).
    if _explicitly_disabled:
        return False
    # driver_namespace was lost (e.g. Blender restart cleared it, or
    # module-level auto-register ran before Blender was ready).  Re-register
    # — safe because if we're executing this code the module IS loaded.
    try:
        _register_api()
    except Exception:  # noqa: BLE001
        return False
    return _REGISTRY_KEY in bpy.app.driver_namespace


def get_api():
    """Get the 3MF API module.

    Ensures the module is registered in ``bpy.app.driver_namespace``
    first (handles Blender restart / load-order edge cases), then
    returns it.

    :return: The ``io_mesh_3mf.api`` module, or ``None`` if the addon
             is disabled.
    :rtype: module | None

    Example::

        from io_mesh_3mf.api import get_api
        api = get_api()
        if api:
            result = api.import_3mf("/model.3mf")
    """
    if is_available():
        return bpy.app.driver_namespace.get(_REGISTRY_KEY)
    return None


def has_capability(capability: str) -> bool:
    """Check if a specific API capability is available.

    Use this for forward-compatible feature detection instead of version
    checks. New capabilities may be added in minor versions.

    :param capability: Capability name (e.g., "paint_mode", "batch").
    :return: True if the capability is supported.

    Example::

        from io_mesh_3mf.api import has_capability
        if has_capability("object_settings"):
            # Safe to use object_settings parameter
            result = export_3mf(path, object_settings={...})
    """
    return capability in API_CAPABILITIES


def check_version(minimum: tuple[int, int, int]) -> bool:
    """Check if the API version meets a minimum requirement.

    :param minimum: tuple of (major, minor, patch) minimum version.
    :return: True if API_VERSION >= minimum.

    Example::

        from io_mesh_3mf.api import check_version
        if check_version((1, 2, 0)):
            # Use features added in v1.2.0
            ...
    """
    return API_VERSION >= minimum


# Auto-register when this module is imported (deferred to first use for safety)
try:
    _register_api()
except Exception:  # noqa: BLE001
    debug("API auto-register deferred: Blender not fully initialized")


__all__ = [
    "API_CAPABILITIES",
    "API_VERSION",
    "API_VERSION_STRING",
    "ExportResult",
    "ImportResult",
    "InspectResult",
    "batch_export",
    "batch_import",
    "check_version",
    "colors",
    "components",
    "export_3mf",
    "extensions",
    "get_api",
    "has_capability",
    "import_3mf",
    "inspect_3mf",
    "is_available",
    "metadata",
    "segmentation",
    "types",
    "units",
    "xml_tools",
]


# ═══════════════════════════════════════════════════════════════════════════
# Result dataclasses
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ImportResult:
    """Return value from :func:`import_3mf`.

    Attributes:
        status: ``"FINISHED"`` on success, ``"CANCELLED"`` on failure.
        num_loaded: Number of objects successfully imported.
        objects: list of ``bpy.types.Object`` instances created during import.
        warnings: Accumulated warning messages (if any).
    """

    status: str = "FINISHED"
    num_loaded: int = 0
    objects: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    """Return value from :func:`export_3mf`.

    Attributes:
        status: ``"FINISHED"`` on success, ``"CANCELLED"`` on failure.
        num_written: Number of objects written to the archive.
        filepath: Absolute path of the written ``.3mf`` file.
        warnings: Accumulated warning messages (if any).
    """

    status: str = "FINISHED"
    num_written: int = 0
    filepath: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class InspectResult:
    """Return value from :func:`inspect_3mf`.

    A lightweight summary of a 3MF archive's contents, extracted *without*
    creating any Blender objects or materials.

    Attributes:
        status: ``"OK"`` on success, ``"ERROR"`` on failure.
        error_message: Human-readable error string when ``status == "ERROR"``.
        unit: The unit declared in the model file (``"millimeter"`` etc.).
        metadata: Top-level ``<metadata>`` key/value pairs from the model.
        objects: Per-object summary dicts with keys:

            - ``"id"`` — resource ID string
            - ``"name"`` — object name (or ``""`` if unnamed)
            - ``"type"`` — object type attribute (``"model"`` / ``"solidsupport"`` / …)
            - ``"num_vertices"`` — vertex count
            - ``"num_triangles"`` — triangle count
            - ``"num_components"`` — number of component references
            - ``"has_materials"`` — whether face materials are present
            - ``"has_segmentation"`` — whether MMU paint segmentation is present

        materials: Per-material-group summary dicts with keys:

            - ``"id"`` — resource ID string
            - ``"type"`` — ``"basematerials"`` | ``"colorgroup"`` | ``"texture2dgroup"``
            - ``"count"`` — number of entries in the group

        textures: Per-texture summary dicts with keys:

            - ``"id"`` — resource ID string
            - ``"path"`` — internal archive path
            - ``"contenttype"`` — MIME type string

        extensions_used: set of namespace URIs for extensions referenced
            in the model's ``requiredextensions`` / ``recommendedextensions``.
        vendor_format: Detected slicer vendor format (``"orca"`` / ``None``).
        archive_files: list of all file paths inside the ZIP archive.
        num_objects: Total number of ``<object>`` resources.
        num_triangles_total: Sum of all triangle counts across objects.
        num_vertices_total: Sum of all vertex counts across objects.
        part_subtypes: Per-part subtype info from ``model_settings.config``
            (Orca/BambuStudio only).  Each entry is a dict with keys:

            - ``"part_id"`` — part ID string
            - ``"subtype"`` — ``"normal_part"`` | ``"modifier_part"`` |
              ``"support_enforcer"`` | ``"support_blocker"``
            - ``"name"`` — part name (or ``""`` if unnamed)

        warnings: Accumulated warnings during inspection.
    """

    status: str = "OK"
    error_message: str = ""
    unit: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    objects: list[dict] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)
    textures: list[dict] = field(default_factory=list)
    extensions_used: set[str] = field(default_factory=set)
    vendor_format: str | None = None
    archive_files: list[str] = field(default_factory=list)
    num_objects: int = 0
    num_triangles_total: int = 0
    num_vertices_total: int = 0
    part_subtypes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Callback type aliases (for documentation clarity)
# ═══════════════════════════════════════════════════════════════════════════

# Called with (percentage: int 0-100, message: str)
ProgressCallback = Callable[[int, str], None]
# Called with (warning_message: str)
WarningCallback = Callable[[str], None]
# Called with (blender_object, resource_id: str) after each object is built
ObjectCreatedCallback = Callable[..., None]


# ═══════════════════════════════════════════════════════════════════════════
# inspect_3mf  — read-only archive inspection (no Blender objects created)
# ═══════════════════════════════════════════════════════════════════════════


def inspect_3mf(filepath: str) -> InspectResult:
    """Inspect a 3MF file without importing anything into Blender.

    Opens the archive, parses the XML model file(s), and returns a
    summary of objects, materials, textures, metadata, and extensions.
    No Blender objects, meshes, or materials are created.

    :param filepath: Path to the ``.3mf`` file.
    :return: :class:`InspectResult` with archive metadata and statistics.

    Example::

        info = inspect_3mf("model.3mf")
        if info.status == "OK":
            for obj in info.objects:
                print(f"{obj['name']}: {obj['num_triangles']} tris")
    """
    from .common.constants import MODEL_DEFAULT_UNIT

    filepath = os.path.abspath(filepath)
    result = InspectResult()

    # --- Open archive -------------------------------------------------------
    try:
        archive = zipfile.ZipFile(filepath, "r")
    except (zipfile.BadZipFile, OSError) as e:
        result.status = "ERROR"
        result.error_message = f"Unable to read archive: {e}"
        return result

    result.archive_files = archive.namelist()

    # --- Find model files ---------------------------------------------------
    # Look for [Content_Types].xml to resolve MIME types, but fall back to
    # scanning for *.model files if the content-types file is missing.
    model_paths: list[str] = []
    for name in result.archive_files:
        lower = name.lower()
        if lower.endswith(".model"):
            model_paths.append(name)

    if not model_paths:
        result.status = "ERROR"
        result.error_message = "No .model files found in archive"
        archive.close()
        return result

    # --- Parse each model file ----------------------------------------------
    for model_path in model_paths:
        try:
            with archive.open(model_path) as f:
                tree = xml.etree.ElementTree.ElementTree(file=f)
        except xml.etree.ElementTree.ParseError as e:
            result.warnings.append(f"Malformed XML in {model_path}: {e}")
            continue

        root = tree.getroot()

        # Unit.
        if not result.unit:
            result.unit = root.attrib.get("unit", MODEL_DEFAULT_UNIT)

        # Top-level metadata.
        for meta_node in root.iterfind("./3mf:metadata", MODEL_NAMESPACES):
            name = meta_node.attrib.get("name", "")
            if name:
                result.metadata[name] = meta_node.text or ""

        # Extensions referenced.
        for attr_key in ("requiredextensions", "recommendedextensions"):
            ext_str = root.attrib.get(attr_key, "")
            if ext_str:
                resolved = _resolve_prefixes(root, ext_str)
                result.extensions_used.update(resolved)

        # Vendor detection (lightweight — check namespace presence).
        from .import_3mf.slicer import detect_vendor

        detected = detect_vendor(root)
        if detected and result.vendor_format is None:
            result.vendor_format = detected

        # ---- Materials / textures ------------------------------------------
        _inspect_materials(root, result)
        _inspect_textures(root, result)

        # ---- Objects -------------------------------------------------------
        for obj_node in root.iterfind("./3mf:resources/3mf:object", MODEL_NAMESPACES):
            obj_id = obj_node.attrib.get("id", "")
            obj_name = obj_node.attrib.get("name", "")
            obj_type = obj_node.attrib.get("type", "model")

            # Count vertices.
            vert_nodes = obj_node.findall(
                "./3mf:mesh/3mf:vertices/3mf:vertex", MODEL_NAMESPACES
            )
            num_verts = len(vert_nodes)

            # Count triangles.
            tri_nodes = obj_node.findall(
                "./3mf:mesh/3mf:triangles/3mf:triangle", MODEL_NAMESPACES
            )
            num_tris = len(tri_nodes)

            # Count components.
            comp_nodes = obj_node.findall(
                "./3mf:components/3mf:component", MODEL_NAMESPACES
            )
            num_components = len(comp_nodes)

            # Check for materials (pid/pindex on object or any triangle).
            has_materials = "pid" in obj_node.attrib
            if not has_materials:
                for tri in tri_nodes[:1]:
                    if "pid" in tri.attrib:
                        has_materials = True
                        break

            # Check for segmentation (slic3rpe or Orca paint_color).
            has_seg = False
            for tri in tri_nodes[:1]:
                if tri.attrib.get(
                    "{http://schemas.slic3r.org/3mf/2017/06}mmu_segmentation"
                ):
                    has_seg = True
                    break
                if tri.attrib.get("paint_color"):
                    has_seg = True
                    break

            obj_summary = {
                "id": obj_id,
                "name": obj_name,
                "type": obj_type,
                "num_vertices": num_verts,
                "num_triangles": num_tris,
                "num_components": num_components,
                "has_materials": has_materials,
                "has_segmentation": has_seg,
            }
            result.objects.append(obj_summary)
            result.num_triangles_total += num_tris
            result.num_vertices_total += num_verts

    result.num_objects = len(result.objects)

    # --- Parse model_settings.config for part subtypes (Orca/BambuStudio) ---
    config_path = "Metadata/model_settings.config"
    if config_path in result.archive_files:
        try:
            with archive.open(config_path) as f:
                config_root = xml.etree.ElementTree.fromstring(f.read().decode("UTF-8"))
            for obj_elem in config_root.findall(".//object"):
                for part_elem in obj_elem.findall("part"):
                    part_id = part_elem.get("id", "")
                    subtype = part_elem.get("subtype", "normal_part")
                    part_name = ""
                    for meta in part_elem.findall("metadata"):
                        if meta.get("key") == "name":
                            part_name = meta.get("value", "")
                            break
                    result.part_subtypes.append(
                        {
                            "part_id": part_id,
                            "subtype": subtype,
                            "name": part_name,
                        }
                    )
        except Exception as e:  # noqa: BLE001
            result.warnings.append(f"Could not parse model_settings.config: {e}")

    archive.close()
    return result


# ═══════════════════════════════════════════════════════════════════════════
# import_3mf
# ═══════════════════════════════════════════════════════════════════════════


def import_3mf(
    filepath: str,
    *,
    global_scale: float = 1.0,
    import_materials: str = "MATERIALS",
    reuse_materials: bool = True,
    import_location: str = "KEEP",
    origin_to_geometry: str = "KEEP",
    grid_spacing: float = 0.1,
    auto_smooth: bool = False,
    auto_smooth_angle: float = 0.5236,
    paint_uv_method: str = "SMART",
    paint_texture_size: int = 0,
    scene_unit: str = "KEEP",
    target_collection: str | None = None,
    on_progress: ProgressCallback | None = None,
    on_warning: WarningCallback | None = None,
    on_object_created: ObjectCreatedCallback | None = None,
) -> ImportResult:
    """Import a 3MF file into the current Blender scene.

    This is the headless/programmatic counterpart to the ``Import3MF``
    operator.  It skips UI-specific behaviour (progress bars, camera zoom,
    paint popups) but runs the exact same import pipeline.

    :param filepath: Path to the ``.3mf`` file to import.
    :param global_scale: Scale multiplier (default 1.0).
    :param import_materials: ``"MATERIALS"`` | ``"PAINT"`` | ``"NONE"``.
    :param reuse_materials: Reuse existing Blender materials by name/color.
    :param import_location: ``"ORIGIN"`` | ``"CURSOR"`` | ``"KEEP"`` | ``"GRID"``.
    :param origin_to_geometry: ``"KEEP"`` | ``"CENTER"`` | ``"BOTTOM"``.
    :param grid_spacing: Spacing between objects in grid layout mode.
    :param auto_smooth: Apply Smooth by Angle modifier to imported objects.
    :param auto_smooth_angle: Maximum angle (radians) for smooth shading
        (default 0.5236 = 30 degrees).
    :param paint_uv_method: ``"SMART"`` (default) or ``"LIGHTMAP"``.
        Smart UV groups adjacent faces; Lightmap gives each face unique space.
    :param paint_texture_size: Override texture resolution (0 = auto).
    :param scene_unit: set the Blender scene unit after import.
        ``"KEEP"`` (default) leaves scene units unchanged.
        ``"FILE"`` sets units to match the unit declared in the 3MF file.
        Any Blender length unit identifier (e.g. ``"MILLIMETERS"``, ``"INCHES"``) sets it directly.
    :param target_collection: Name of an existing Blender collection to place
        imported objects into.  If *None*, objects are added to the active
        collection.  If the named collection does not exist it will be created
        and linked to the scene.
    :param on_progress: optional ``(percentage: int, message: str)`` callback.
    :param on_warning: optional ``(message: str)`` callback fired for each warning.
    :param on_object_created: optional callback fired after each Blender
        object is built.  Receives ``(blender_object, resource_id)`` arguments.
    :return: :class:`ImportResult` with status, loaded count, and object list.
    """
    from .import_3mf import archive as archive_mod  # noqa: I001
    from .import_3mf import builder as builder_mod
    from .import_3mf import geometry as geometry_mod
    from .import_3mf.context import ImportContext, ImportOptions
    from .import_3mf.materials import (
        extract_textures_from_archive as _extract_textures_impl,
        read_composite_materials as _read_composite_impl,
        read_materials as _read_materials_impl,
        read_multiproperties as _read_multiproperties_impl,
        read_pbr_metallic_properties as _read_pbr_metallic_impl,
        read_pbr_specular_properties as _read_pbr_specular_impl,
        read_pbr_texture_display_properties as _read_pbr_texture_display_impl,
        read_pbr_translucent_properties as _read_pbr_translucent_impl,
        read_texture_groups as _read_texture_groups_impl,
        read_textures as _read_textures_impl,
        store_passthrough_materials as _store_passthrough_impl,
    )
    from .import_3mf.scene import apply_grid_layout
    from .import_3mf.slicer import (
        detect_vendor,
        read_all_slicer_colors,
    )

    filepath = os.path.abspath(filepath)
    result = ImportResult()
    warnings_list = result.warnings

    if on_progress:
        on_progress(0, "Starting import…")

    # Build context (no operator).
    options = ImportOptions(
        global_scale=global_scale,
        import_materials=import_materials,
        reuse_materials=reuse_materials,
        import_location=import_location,
        origin_to_geometry=origin_to_geometry,
        grid_spacing=grid_spacing,
        auto_smooth=auto_smooth,
        auto_smooth_angle=auto_smooth_angle,
        paint_uv_method=paint_uv_method,
        paint_texture_size=paint_texture_size,
    )
    ctx = ImportContext(options=options, operator=None)

    # Wire up warning callback (intercept ctx.safe_report for WARNING level).
    if on_warning is not None:
        _original_safe_report = ctx.safe_report

        def _intercepted_safe_report(level, message):
            _original_safe_report(level, message)
            if "WARNING" in level:
                on_warning(message)
                warnings_list.append(message)

        ctx.safe_report = _intercepted_safe_report  # type: ignore[assignment]

    scene_metadata = Metadata()
    scene_metadata.retrieve(bpy.context.scene)
    del scene_metadata["Title"]
    annotations_obj = Annotations()
    annotations_obj.retrieve()

    # Switch to object mode, deselect everything.
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
    if bpy.ops.object.select_all.poll():
        bpy.ops.object.select_all(action="DESELECT")

    # --- Collection targeting -----------------------------------------------
    original_collection = bpy.context.view_layer.active_layer_collection
    if target_collection is not None:
        col = bpy.data.collections.get(target_collection)
        if col is None:
            col = bpy.data.collections.new(target_collection)
            bpy.context.scene.collection.children.link(col)
        # Find the layer collection wrapper for this collection.
        layer_col = _find_layer_collection(
            bpy.context.view_layer.layer_collection,
            col,
        )
        if layer_col is not None:
            bpy.context.view_layer.active_layer_collection = layer_col

    ctx.current_archive_path = filepath

    if on_progress:
        on_progress(5, "Reading archive…")

    # --- Read archive -------------------------------------------------------
    try:
        files_by_content_type = archive_mod.read_archive(ctx, filepath)
    except Exception as e:  # noqa: BLE001
        error(f"Failed to read archive {filepath}: {e}")
        result.status = "CANCELLED"
        # Restore collection.
        bpy.context.view_layer.active_layer_collection = original_collection
        return result

    # If no model files were found, the archive is unreadable or invalid.
    if not files_by_content_type.get(MODEL_MIMETYPE):
        error(f"No model files found in archive: {filepath}")
        result.status = "CANCELLED"
        bpy.context.view_layer.active_layer_collection = original_collection
        return result

    # Relationships & content types.
    for rels_file in files_by_content_type.get(RELS_MIMETYPE, []):
        annotations_obj.add_rels(rels_file)
    annotations_obj.add_content_types(files_by_content_type)
    archive_mod.must_preserve(ctx, files_by_content_type, annotations_obj)

    # Stash slicer config files for round-trip export.
    archive_mod.stash_slicer_configs(ctx, filepath)

    if on_progress:
        on_progress(15, "Parsing model files…")

    # --- Parse model files --------------------------------------------------
    for model_file in files_by_content_type.get(MODEL_MIMETYPE, []):
        try:
            document = xml.etree.ElementTree.ElementTree(file=model_file)
        except xml.etree.ElementTree.ParseError as e:
            error(f"3MF document is malformed: {e}")
            warnings_list.append(f"Malformed XML: {e}")
            continue
        if document is None:
            continue
        root = document.getroot()

        # Vendor detection.
        if ctx.options.import_materials != "NONE":
            ctx.vendor_format = detect_vendor(root)
        else:
            ctx.vendor_format = None

        # Extension activation.
        _activate_extensions_api(ctx, root)

        # Apply requested scene unit before computing scale.
        context = bpy.context
        _apply_scene_unit_api(context, root, scene_unit)

        # Unit scale.
        scale_unit = _import_unit_scale(context, root, global_scale)

        # Reset per-model resource dictionaries.
        ctx.resource_objects = {}
        ctx.resource_materials = {}
        ctx.resource_textures = {}
        ctx.resource_texture_groups = {}
        ctx.orca_filament_colors = {}
        ctx.object_default_extruders = {}
        ctx.part_subtypes = {}
        ctx.part_groups = {}
        ctx.part_metadata = {}
        ctx.wrapper_metadata = {}
        ctx.part_names = {}

        if on_progress:
            on_progress(20, "Reading filament colours…")

        # Read filament colours (single archive open, priority order).
        read_all_slicer_colors(ctx, filepath)

        # Read modifier part subtypes from Orca/BambuStudio model_settings.
        if ctx.vendor_format == "orca":
            from .import_3mf.slicer.colors import read_orca_part_subtypes

            read_orca_part_subtypes(ctx, filepath)

        # Metadata.
        for metadata_node in root.iterfind("./3mf:metadata", MODEL_NAMESPACES):
            if "name" not in metadata_node.attrib:
                continue
            name = metadata_node.attrib["name"]
            preserve_str = metadata_node.attrib.get("preserve", "0")
            preserve = preserve_str != "0" and preserve_str.lower() != "false"
            datatype = metadata_node.attrib.get("type", "")
            value = metadata_node.text
            scene_metadata[name] = MetadataEntry(
                name=name,
                preserve=preserve,
                datatype=datatype,
                value=value,
            )

        if on_progress:
            on_progress(30, "Reading materials…")

        # Materials.
        if ctx.options.import_materials != "NONE":
            material_ns = {"m": MATERIAL_NAMESPACE}
            pbr_metallic = _read_pbr_metallic_impl(ctx, root, material_ns)
            pbr_specular = _read_pbr_specular_impl(ctx, root, material_ns)
            pbr_translucent = _read_pbr_translucent_impl(ctx, root, material_ns)
            _read_pbr_texture_display_impl(ctx, root, material_ns)

            display_properties = {}
            display_properties.update(pbr_metallic)
            display_properties.update(pbr_specular)
            display_properties.update(pbr_translucent)

            _read_materials_impl(ctx, root, material_ns, display_properties)
            _read_textures_impl(ctx, root, material_ns)
            _read_texture_groups_impl(ctx, root, material_ns, display_properties)
            _read_composite_impl(ctx, root, material_ns)
            _read_multiproperties_impl(ctx, root, material_ns)

        # Extract textures.
        _extract_textures_impl(ctx, filepath)

        if on_progress:
            on_progress(45, "Reading geometry…")

        # Objects.
        geometry_mod.read_objects(ctx, root)

        if on_progress:
            on_progress(60, "Building Blender objects…")

        # Build items (pass progress_callback through if available).
        builder_mod.build_items(ctx, root, scale_unit, progress_callback=on_progress)

    # Fire on_object_created for each built object.
    if on_object_created is not None:
        for obj in ctx.imported_objects:
            on_object_created(obj, str(getattr(obj, "name", "")))

    # Store scene data.
    scene_metadata.store(bpy.context.scene)
    annotations_obj.store()
    _store_passthrough_impl(ctx)

    # Grid layout.
    if ctx.options.import_location == "GRID":
        apply_grid_layout(ctx.imported_objects, ctx.options.grid_spacing)

    # Restore original collection.
    bpy.context.view_layer.active_layer_collection = original_collection

    result.num_loaded = ctx.num_loaded
    result.objects = list(ctx.imported_objects)
    result.status = "FINISHED"

    if on_progress:
        on_progress(100, "Import complete")

    debug(f"API: Imported {ctx.num_loaded} objects from {filepath}")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# export_3mf
# ═══════════════════════════════════════════════════════════════════════════


def export_3mf(
    filepath: str,
    *,
    objects=None,
    use_selection: bool = False,
    export_hidden: bool = False,
    skip_disabled: bool = True,
    global_scale: float = 1.0,
    use_mesh_modifiers: bool = True,
    coordinate_precision: int = 9,
    compression_level: int = 3,
    use_orca_format: str = "AUTO",
    use_components: bool = True,
    flatten_hierarchy: bool = False,
    mmu_slicer_format: str = "ORCA",
    subdivision_depth: int = 7,
    slicer_profile: str = "NONE",
    thumbnail_mode: str = "AUTO",
    thumbnail_resolution: int = 256,
    thumbnail_image: str = "",
    project_template: str | None = None,
    object_settings: dict | None = None,
    on_progress: ProgressCallback | None = None,
    on_warning: WarningCallback | None = None,
    show_progress_window: bool = False,
    progress_mode: str = "NONE",
) -> ExportResult:
    """Export Blender objects to a 3MF file.

    This is the headless/programmatic counterpart to the ``Export3MF``
    operator.  It skips UI-specific behaviour by default, but the browser
    progress card can be requested via *show_progress_window*.

    :param filepath: Destination path for the ``.3mf`` file.
    :param objects: Explicit list of ``bpy.types.Object`` to export.
        If *None*, falls back to ``use_selection`` logic or all scene objects.
    :param use_selection: Export selected objects only (ignored when *objects* is given).
    :param export_hidden: Include hidden objects.
    :param skip_disabled: Skip objects disabled for rendering (camera icon)
        and objects in excluded/hidden collections (default *True*).
    :param global_scale: Scale multiplier (default 1.0).
    :param use_mesh_modifiers: Apply modifiers before exporting.
    :param coordinate_precision: Decimal precision for vertex coordinates.
    :param compression_level: ZIP deflate compression level (0–9, default 3).
        0 = no compression (fastest, largest), 9 = max compression (slowest,
        smallest). 3 balances speed and file size.
    :param use_orca_format: ``"AUTO"`` | ``"STANDARD"`` | ``"PAINT"``.
        ``AUTO`` (default) detects materials and paint data, choosing the
        best exporter automatically.  ``STANDARD`` always uses the
        spec-compliant StandardExporter with proper component instancing.
        ``PAINT`` forces segmentation export.  When *project_template* or
        *object_settings* is provided, the Orca exporter is used
        automatically even in ``AUTO`` mode.
    :param use_components: Use component instances for linked duplicates.
    :param flatten_hierarchy: When *True*, child meshes parented to an Empty
        are written directly as top-level build items with their combined
        world transform instead of wrapped in a component container.  Some
        printing services reject files that use 3MF ``<component>``
        references; enable this to maximize compatibility.  Default *False*
        because it discards the parent–child hierarchy.
    :param mmu_slicer_format: ``"ORCA"`` | ``"PRUSA"`` (only relevant when
        *use_orca_format* is ``"PAINT"``).
    :param subdivision_depth: Maximum recursive subdivision depth for paint
        segmentation (4–10, default 7). Higher = finer detail but slower.
    :param slicer_profile: Name of a saved slicer profile to embed in the
        archive (e.g. ``"My Bambu X1C Profile"``), or ``"NONE"`` (default)
        to omit profile data.  The profile must exist in the addon's slicer
        profile storage.  Only applied by the Orca and Prusa exporters.
    :param thumbnail_mode: ``"AUTO"`` (render clean preview), ``"CUSTOM"``
        (use *thumbnail_image*), or ``"NONE"`` (no thumbnail).
    :param thumbnail_resolution: Width and height in pixels for AUTO mode
        (default 256).
    :param thumbnail_image: Absolute path to an image file for CUSTOM mode.
    :param project_template: Absolute path to a JSON file to use as the Orca
        ``project_settings.config`` instead of the built-in template.  The
        addon loads this file, patches ``filament_colour`` and resizes
        filament arrays to match the export, then writes it to the archive.
        If the file does not exist or is invalid JSON, a warning is logged
        and the built-in template is used as a fallback.  Only relevant for
        Orca/BambuStudio exports (``mmu_slicer_format="ORCA"``).
    :param object_settings: Per-object Orca Slicer setting overrides.
        A dict mapping ``bpy.types.Object`` instances to dicts of
        ``{setting_key: value_string}`` pairs.  These are written as
        ``<metadata>`` entries in ``model_settings.config`` so that Orca
        applies different print settings to individual objects.  Keys are
        passed through without validation — any valid Orca setting key
        (e.g. ``"layer_height"``, ``"wall_loops"``, ``"sparse_infill_speed"``)
        is accepted.  Objects not present in this dict use project defaults.

        Example::

            object_settings={
                supports_obj: {
                    "layer_height": "0.12",
                    "wall_loops": "2",
                    "sparse_infill_speed": "50",
                },
                # other objects use project defaults
            }

    :param on_progress: optional ``(percentage: int, message: str)`` callback.
    :param on_warning: optional ``(message: str)`` callback for warnings.
    :param show_progress_window: Deprecated, has no effect.
    :param progress_mode: Controls whether to show the in-viewport progress bar.

        - ``"NONE"`` (default) — no indicator.
        - ``"AUTO"`` | ``"VIEWPORT"`` — show the in-viewport bar based on
          operation size.

        The *on_progress* callback fires regardless of this setting.
    :return: :class:`ExportResult` with status, written count, and filepath.
    """
    from .export_3mf.archive import create_archive
    from .export_3mf.components import collect_mesh_objects
    from .export_3mf.context import ExportContext, ExportOptions
    from .export_3mf.geometry import check_non_manifold_geometry
    from .export_3mf.orca import OrcaExporter
    from .export_3mf.prusa import PrusaExporter
    from .export_3mf.standard import StandardExporter

    filepath = os.path.abspath(filepath)
    result = ExportResult(filepath=filepath)

    # ── Resolve progress mode ─────────────────────────────────────────────────
    # progress_mode takes precedence; show_progress_window=True is legacy AUTO.
    _raw_mode = (
        progress_mode
        if progress_mode != "NONE"
        else ("AUTO" if show_progress_window else "NONE")
    )

    _pw = None
    if _raw_mode != "NONE":
        from .progress import PHASES, ProgressReporter, get_progress_mode as _gpm  # noqa: I001

        _filename = os.path.basename(filepath)
        _resolved_mode = _gpm(
            "export", tri_count=999_999, has_paint=True, thumbnail_render=False
        ) if _raw_mode == "AUTO" else _raw_mode

        _pw = ProgressReporter(_resolved_mode)
        _pw.start(
            bpy.context,
            "export",
            _filename,
            phases=PHASES["export"],
            can_cancel=False,
        )
        # Wrap on_progress so both the caller's callback and the reporter update.
        _caller_on_progress = on_progress

        def _on_progress_wrapped(pct: int, msg: str) -> None:
            _phases = PHASES["export"]
            _n = len(_phases)
            _phase_idx = min(int(pct / 100 * _n), _n - 1)
            _pw.update(pct / 100, _phase_idx, msg)
            if _caller_on_progress is not None:
                _caller_on_progress(pct, msg)

        on_progress = _on_progress_wrapped

    if on_progress:
        on_progress(0, "Starting export…")

    options = ExportOptions(
        use_selection=use_selection,
        export_hidden=export_hidden,
        include_disabled=not skip_disabled,
        global_scale=global_scale,
        use_mesh_modifiers=use_mesh_modifiers,
        coordinate_precision=coordinate_precision,
        compression_level=compression_level,
        use_orca_format=use_orca_format,
        use_components=use_components,
        flatten_hierarchy=flatten_hierarchy,
        mmu_slicer_format=mmu_slicer_format,
        subdivision_depth=subdivision_depth,
        slicer_profile=slicer_profile,
        thumbnail_mode=thumbnail_mode,
        thumbnail_resolution=thumbnail_resolution,
        thumbnail_image=thumbnail_image,
    )
    ctx = ExportContext(
        options=options,
        operator=None,
        filepath=filepath,
        extension_manager=ExtensionManager(),
    )

    # Wire the progress window into the context so internal _progress_update()
    # calls (inside the exporter) drive the browser card live.
    if _pw is not None:
        ctx.progress = _pw

    # Wire up custom project template path.
    if project_template is not None:
        ctx.project_template_path = os.path.abspath(project_template)

    # Wire up per-object setting overrides (convert Object keys to name strings).
    if object_settings is not None:
        for obj, settings_dict in object_settings.items():
            obj_name = str(obj.name)
            ctx.object_settings[obj_name] = {
                str(k): str(v) for k, v in settings_dict.items()
            }

    # Wire up warning callback.
    if on_warning is not None:
        _original_safe_report = ctx.safe_report

        def _intercepted_safe_report(level, message):
            _original_safe_report(level, message)
            if "WARNING" in level:
                on_warning(message)
                result.warnings.append(message)

        ctx.safe_report = _intercepted_safe_report  # type: ignore[assignment]

    if on_progress:
        on_progress(10, "Creating archive…")

    # Create archive.
    archive = create_archive(filepath, ctx.safe_report, ctx.options.compression_level)
    if archive is None:
        if _pw is not None:
            _pw.finish()
        result.status = "CANCELLED"
        return result

    # Determine objects to export.
    context = bpy.context
    if objects is not None:
        blender_objects = objects
        # The caller explicitly chose these objects — bypass visibility and
        # render-disable filters so the exporter doesn't silently skip them.
        options.export_hidden = True
        options.include_disabled = True
    elif use_selection:
        blender_objects = context.selected_objects
        mesh_objects = collect_mesh_objects(
            blender_objects, export_hidden=True, include_disabled=True
        )
        if not mesh_objects:
            error("Export cancelled: No mesh objects in selection")
            result.status = "CANCELLED"
            return result
    else:
        blender_objects = context.scene.objects

    if on_progress:
        on_progress(20, "Checking geometry…")

    # Non-manifold check.
    # Use collect_mesh_objects to walk into Empty hierarchies (e.g. when
    # the caller passes a parent Empty grouping several mesh children).
    mesh_objects = collect_mesh_objects(
        blender_objects,
        export_hidden=options.export_hidden,
        include_disabled=options.include_disabled,
    )
    if mesh_objects:
        non_manifold = check_non_manifold_geometry(mesh_objects, use_mesh_modifiers)
        if non_manifold:
            msg = f"Non-manifold geometry detected in: {non_manifold[0]}"
            warn(msg)
            result.warnings.append("Exported geometry contains non-manifold issues.")
            if on_warning:
                on_warning(msg)

    if on_progress:
        on_progress(30, "Writing 3MF data…")

    scale = export_unit_scale(context, global_scale)

    # Check if any mesh has materials assigned.
    # Must check EVALUATED objects because Geometry Nodes "Set Material"
    # nodes only create material slots on the evaluated depsgraph copy.
    # We detect ANY material (not just multi-material) because slicers
    # like Orca/BambuStudio ignore core-spec <basematerials> and only
    # read the Orca-style colorgroup/paint_color attributes written by
    # OrcaExporter.
    has_materials = False
    if mesh_objects and use_mesh_modifiers:
        depsgraph = context.evaluated_depsgraph_get()
        for obj in mesh_objects:
            eval_obj = obj.evaluated_get(depsgraph)
            if len(eval_obj.material_slots) >= 1:
                has_materials = True
                break
    elif mesh_objects:
        has_materials = any(len(obj.material_slots) >= 1 for obj in mesh_objects)

    # Dispatch to exporter.
    try:  # must keep _pw.finish() in finally
        try:
            if use_orca_format == "PAINT":
                if mmu_slicer_format == "ORCA":
                    exporter = OrcaExporter(ctx)
                else:
                    if ctx.project_template_path or ctx.object_settings:
                        warn(
                            "project_template and object_settings are Orca-specific "
                            "features and will be ignored for PrusaSlicer export"
                        )
                    exporter = PrusaExporter(ctx)
            elif use_orca_format == "STANDARD":
                # Explicit standard mode — always spec-compliant
                debug("API: Standard mode requested, using StandardExporter")
                exporter = StandardExporter(ctx)
            else:
                # AUTO mode
                if ctx.project_template_path or ctx.object_settings:
                    exporter = OrcaExporter(ctx)
                else:
                    # Check for MMU paint textures
                    has_paint = any(
                        obj.data.get("3mf_is_paint_texture")
                        for obj in mesh_objects
                        if obj.type == "MESH" and obj.data is not None
                    )
                    if has_paint:
                        debug(
                            "API AUTO: paint textures detected — promoting to PAINT mode"
                        )
                        ctx.options.use_orca_format = "PAINT"
                        if mmu_slicer_format == "ORCA":
                            exporter = OrcaExporter(ctx)
                        else:
                            exporter = PrusaExporter(ctx)
                    elif has_materials:
                        debug("API AUTO: materials detected, using OrcaExporter")
                        exporter = OrcaExporter(ctx)
                    else:
                        exporter = StandardExporter(ctx)

            status_set = exporter.execute(context, archive, blender_objects, scale)
            result.status = next(iter(status_set)) if status_set else "FINISHED"
        except Exception as e:  # noqa: BLE001
            error(f"Export failed: {e}")
            result.status = "CANCELLED"
            result.warnings.append(str(e))
            return result
    finally:
        if _pw is not None:
            _pw.finish()
        ctx.progress = None

    result.num_written = ctx.num_written

    if on_progress:
        on_progress(100, "Export complete")

    debug(f"API: Exported {ctx.num_written} objects to {filepath}")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# batch_import / batch_export
# ═══════════════════════════════════════════════════════════════════════════


def batch_import(
    filepaths: Sequence[str],
    *,
    on_progress: ProgressCallback | None = None,
    on_warning: WarningCallback | None = None,
    on_object_created: ObjectCreatedCallback | None = None,
    **import_kwargs,
) -> list[ImportResult]:
    """Import multiple 3MF files in sequence with per-file error isolation.

    Each file is imported independently — a failure in one file does not
    prevent the others from being processed.  All keyword arguments
    supported by :func:`import_3mf` can be passed via ``**import_kwargs``
    and will be applied to every file.

    :param filepaths: Sequence of ``.3mf`` file paths to import.
    :param on_progress: optional global progress callback.  Receives
        ``(percentage, message)`` where percentage spans 0-100 across
        *all* files.
    :param on_warning: Warning callback forwarded to each :func:`import_3mf` call.
    :param on_object_created: Object-created callback forwarded to each call.
    :param import_kwargs: Keyword arguments forwarded to :func:`import_3mf`.
    :return: list of :class:`ImportResult`, one per input file (same order).

    Example::

        results = batch_import(
            ["a.3mf", "b.3mf", "c.3mf"],
            import_materials="PAINT",
            target_collection="Imports",
        )
        total = sum(r.num_loaded for r in results)
        print(f"Imported {total} objects total")
    """
    results: list[ImportResult] = []
    total = len(filepaths)

    for idx, fp in enumerate(filepaths):
        # Per-file progress wrapper.
        file_progress: ProgressCallback | None = None
        if on_progress:
            base_pct = int((idx / total) * 100)
            span_pct = int(100 / total) if total else 100

            def _file_progress(pct: int, msg: str, _base=base_pct, _span=span_pct, _idx=idx):
                overall = _base + int(pct * _span / 100)
                on_progress(min(overall, 100), f"[{_idx + 1}/{total}] {msg}")

            file_progress = _file_progress

        try:
            r = import_3mf(
                fp,
                on_progress=file_progress,
                on_warning=on_warning,
                on_object_created=on_object_created,
                **import_kwargs,
            )
        except Exception as e:  # noqa: BLE001
            error(f"batch_import: Failed on {fp}: {e}")
            r = ImportResult(status="CANCELLED", warnings=[str(e)])
        results.append(r)

    if on_progress:
        on_progress(100, "Batch import complete")

    return results


def batch_export(
    items: Sequence[tuple[str, list | None]],
    *,
    on_progress: ProgressCallback | None = None,
    on_warning: WarningCallback | None = None,
    **export_kwargs,
) -> list[ExportResult]:
    """Export multiple 3MF files in sequence with per-file error isolation.

    Each item is a ``(filepath, objects)`` tuple.  If *objects* is ``None``,
    the export falls back to the ``use_selection`` / all-scene logic from
    :func:`export_3mf`.

    :param items: Sequence of ``(filepath, objects_or_None)`` tuples.
    :param on_progress: optional global progress callback.
    :param on_warning: Warning callback forwarded to each :func:`export_3mf` call.
    :param export_kwargs: Keyword arguments forwarded to :func:`export_3mf`.
    :return: list of :class:`ExportResult`, one per item (same order).

    Example::

        cubes = [o for o in bpy.data.objects if "Cube" in o.name]
        spheres = [o for o in bpy.data.objects if "Sphere" in o.name]
        results = batch_export([
            ("cubes.3mf", cubes),
            ("spheres.3mf", spheres),
        ], use_orca_format="AUTO")
    """
    results: list[ExportResult] = []
    total = len(items)

    for idx, (fp, objs) in enumerate(items):
        file_progress: ProgressCallback | None = None
        if on_progress:
            base_pct = int((idx / total) * 100)
            span_pct = int(100 / total) if total else 100

            def _file_progress(pct: int, msg: str, _base=base_pct, _span=span_pct, _idx=idx):
                overall = _base + int(pct * _span / 100)
                on_progress(min(overall, 100), f"[{_idx + 1}/{total}] {msg}")

            file_progress = _file_progress

        try:
            r = export_3mf(
                fp,
                objects=objs,
                on_progress=file_progress,
                on_warning=on_warning,
                **export_kwargs,
            )
        except Exception as e:  # noqa: BLE001
            error(f"batch_export: Failed on {fp}: {e}")
            r = ExportResult(status="CANCELLED", filepath=fp, warnings=[str(e)])
        results.append(r)

    if on_progress:
        on_progress(100, "Batch export complete")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _find_layer_collection(
    layer_collection,
    target_collection,
):
    """Recursively find the LayerCollection wrapping *target_collection*."""
    if layer_collection.collection == target_collection:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, target_collection)
        if found is not None:
            return found
    return None


def _resolve_prefixes(root, prefixes_str: str) -> set[str]:
    """Resolve extension prefix strings to namespace URIs."""
    from .common.constants import PRODUCTION_NAMESPACE

    if not prefixes_str:
        return set()
    prefix_to_ns = {}
    for attr_name, attr_value in root.attrib.items():
        if attr_name.startswith("{"):
            continue
        if attr_name.startswith("xmlns:"):
            prefix_to_ns[attr_name[6:]] = attr_value
    known = {
        "p": PRODUCTION_NAMESPACE,
        "m": "http://schemas.microsoft.com/3dmanufacturing/material/2015/02",
        "slic3rpe": "http://schemas.slic3r.org/3mf/2017/06",
    }
    prefix_to_ns.update({k: v for k, v in known.items() if k not in prefix_to_ns})
    resolved: set[str] = set()
    for prefix in prefixes_str.split():
        prefix = prefix.strip()
        if not prefix:
            continue
        if prefix in prefix_to_ns:
            resolved.add(prefix_to_ns[prefix])
        else:
            resolved.add(prefix)
    return resolved


def _inspect_materials(root, result: InspectResult) -> None:
    """Scan material resources for inspect_3mf without creating Blender data."""
    mat_ns = {"m": MATERIAL_NAMESPACE}

    # basematerials
    for bm_node in root.iterfind(
        "./3mf:resources/m:basematerials", {**MODEL_NAMESPACES, **mat_ns}
    ):
        mat_id = bm_node.attrib.get("id", "")
        bases = bm_node.findall("m:base", mat_ns)
        result.materials.append(
            {
                "id": mat_id,
                "type": "basematerials",
                "count": len(bases),
            }
        )

    # colorgroups
    for cg_node in root.iterfind(
        "./3mf:resources/m:colorgroup", {**MODEL_NAMESPACES, **mat_ns}
    ):
        cg_id = cg_node.attrib.get("id", "")
        colors = cg_node.findall("m:color", mat_ns)
        result.materials.append(
            {
                "id": cg_id,
                "type": "colorgroup",
                "count": len(colors),
            }
        )

    # texture2dgroups
    for tg_node in root.iterfind(
        "./3mf:resources/m:texture2dgroup", {**MODEL_NAMESPACES, **mat_ns}
    ):
        tg_id = tg_node.attrib.get("id", "")
        coords = tg_node.findall("m:tex2coord", mat_ns)
        result.materials.append(
            {
                "id": tg_id,
                "type": "texture2dgroup",
                "count": len(coords),
            }
        )


def _inspect_textures(root, result: InspectResult) -> None:
    """Scan texture resources for inspect_3mf."""
    mat_ns = {"m": MATERIAL_NAMESPACE}
    for tex_node in root.iterfind(
        "./3mf:resources/m:texture2d", {**MODEL_NAMESPACES, **mat_ns}
    ):
        tex_id = tex_node.attrib.get("id", "")
        result.textures.append(
            {
                "id": tex_id,
                "path": tex_node.attrib.get("path", ""),
                "contenttype": tex_node.attrib.get("contenttype", ""),
            }
        )


def _apply_scene_unit_api(
    context: bpy.types.Context,
    root: xml.etree.ElementTree.Element,
    scene_unit: str,
) -> None:
    """Apply the scene_unit setting to context.scene.unit_settings."""
    from .import_3mf.operator import _BLENDER_UNIT_SYSTEM, _THREEMF_TO_BLENDER_UNIT

    if scene_unit == "KEEP":
        return
    from .common.constants import MODEL_DEFAULT_UNIT

    if scene_unit == "FILE":
        threemf_unit = root.attrib.get("unit", MODEL_DEFAULT_UNIT)
        target = _THREEMF_TO_BLENDER_UNIT.get(threemf_unit, "MILLIMETERS")
    else:
        target = scene_unit
    system = _BLENDER_UNIT_SYSTEM.get(target, "METRIC")
    context.scene.unit_settings.system = system
    context.scene.unit_settings.length_unit = target
    context.scene.unit_settings.scale_length = 1.0
    debug(f"API: Scene units set to {target} ({system})")


def _import_unit_scale(
    context: bpy.types.Context,
    root: xml.etree.ElementTree.Element,
    global_scale: float,
) -> float:
    """Calculate unit scale exactly like the Import3MF operator."""
    from .common.constants import MODEL_DEFAULT_UNIT

    scale = global_scale
    blender_unit_to_metre = context.scene.unit_settings.scale_length
    if blender_unit_to_metre == 0:
        blender_unit = context.scene.unit_settings.length_unit
        blender_unit_to_metre = blender_to_metre[blender_unit]

    threemf_unit = root.attrib.get("unit", MODEL_DEFAULT_UNIT)
    threemf_unit_to_metre = threemf_to_metre[threemf_unit]
    scale *= threemf_unit_to_metre / blender_unit_to_metre
    return scale


def _activate_extensions_api(
    ctx,
    root: xml.etree.ElementTree.Element,
) -> None:
    """Activate extensions on ctx.extension_manager (no operator needed)."""
    for attr_key in ("requiredextensions", "recommendedextensions"):
        ext_str = root.attrib.get(attr_key, "")
        if ext_str:
            resolved = _resolve_prefixes(root, ext_str)
            for ns in resolved:
                if ns in SUPPORTED_EXTENSIONS:
                    ctx.extension_manager.activate(ns)
                    debug(f"API: Activated extension: {ns}")


# ═══════════════════════════════════════════════════════════════════════════
# Building-block re-exports
# ═══════════════════════════════════════════════════════════════════════════
#
# These sub-namespace imports expose the common building blocks so addon
# developers and CLI scripts can access them through ``api.*`` without
# needing to know the internal package layout.
#
#   from io_mesh_3mf.api import colors
#   r, g, b = colors.hex_to_rgb("#CC3319")
#
#   from io_mesh_3mf.api import types
#   obj = types.ResourceObject(vertices=[], triangles=[], ...)
#
#   from io_mesh_3mf.api import segmentation
#   tree = segmentation.decode_segmentation_string("A3F0")

from .common import colors  # noqa: I001 -- sorted correctly; inline comments confuse ruff
from .common import extensions  # ExtensionManager, Extension, MATERIALS_EXTENSION, ...
from .common import metadata  # Metadata, MetadataEntry
from .common import segmentation  # SegmentationDecoder, SegmentationEncoder, ...
from .common import types  # ResourceObject, Component, ResourceMaterial, ...
from .common import units  # blender_to_metre, threemf_to_metre, import_unit_scale, ...
from .common import xml as xml_tools  # parse_transformation, format_transformation, ...
from .export_3mf import components  # detect_linked_duplicates, ComponentGroup, ...
