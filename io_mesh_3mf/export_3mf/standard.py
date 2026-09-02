# Blender add-on to import and export 3MF files.
# Copyright (C) 2020 Ghostkeeper
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
Standard 3MF exporter.

Exports spec-compliant 3MF files with optional basematerials, texture2dgroup,
PBR display properties, and triangle sets.  This is the default exporter used
when the user does not select Orca or PrusaSlicer paint-segmentation output.
"""

from __future__ import annotations

import collections
import re
import time
import xml.etree.ElementTree
import zipfile
from typing import List, Set, Tuple, TYPE_CHECKING

import bpy
import mathutils

from ..common.constants import MODEL_NAMESPACE, MODEL_LOCATION
from ..common.extensions import TRIANGLE_SETS_EXTENSION, MATERIALS_EXTENSION
from ..common.logging import debug, timing_debug, warn
from ..common.metadata import Metadata
from ..common.xml import format_transformation

from .archive import write_core_properties
from .components import collect_mesh_objects, detect_linked_duplicates, should_use_components
from .geometry import (
    write_vertices, write_triangles, write_passthrough_triangles,
    write_metadata, get_raw_geometry, clear_raw_geometry,
)
from .materials import (
    write_materials,
    get_triangle_color,
    detect_textured_materials,
    detect_pbr_textured_materials,
    write_textures_to_archive,
    write_texture_relationships,
    write_texture_resources,
    write_pbr_textures_to_archive,
    write_pbr_texture_display_properties,
    write_passthrough_materials,
    write_passthrough_textures_to_archive,
)
from .thumbnail import write_thumbnail
from .triangle_sets import write_triangle_sets

if TYPE_CHECKING:
    from .context import ExportContext


_CLARK_RE = re.compile(r"\{([^}]*)\}(.*)")

# These namespace URIs are implicitly declared in XML and must never have an
# explicit xmlns:... declaration added by the streaming writer.
_IMPLICIT_NS = frozenset({
    "http://www.w3.org/XML/1998/namespace",   # xml:
    "http://www.w3.org/2000/xmlns/",          # xmlns:
})


def _stream_model_to_file(f, root: xml.etree.ElementTree.Element) -> None:
    """Write the 3MF model XML to a binary file-like object *f* without using
    ElementTree's Python-level serialiser.

    ElementTree's ``write()`` walks every DOM node in Python — for a 2M-triangle
    mesh that means 3–4 M recursive Python calls taking 15–20 s.  This writer
    replaces that by:
      - Serialising only the small structural elements (resources, materials,
        metadata, build items) through a fast recursive string builder.
      - For ``<mesh>`` elements whose geometry was prepared by ``write_vertices``
        and ``write_triangles``, injecting the pre-built raw XML strings
        (stored as ``_raw_vertices_xml`` and ``_raw_triangles_xml`` on the
        element) directly, bypassing the DOM entirely.

    Namespace declarations are derived from ElementTree's registered namespace
    map (populated by ``register_namespace()`` calls earlier in the export).
    """
    # Build uri→prefix from ElementTree's internal registry.
    # _namespace_map stores {uri: prefix}, so copy it directly.
    try:
        from xml.etree.ElementTree import _namespace_map  # type: ignore[attr-defined]
        uri_to_prefix: dict = dict(_namespace_map)
    except (ImportError, AttributeError):
        uri_to_prefix = {MODEL_NAMESPACE: ""}

    def _qname(tag: str) -> str:
        """Convert ``{uri}local`` Clark notation to a serialisable qualified name."""
        m = _CLARK_RE.match(tag)
        if not m:
            return tag
        uri, local = m.group(1), m.group(2)
        prefix = uri_to_prefix.get(uri)
        if prefix:
            return f"{prefix}:{local}"
        return local  # default namespace — no prefix

    def _attr_escape(v: str) -> str:
        return v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")

    def _text_escape(v: str) -> str:
        return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _collect_ns(elem, seen: set) -> set:
        """Collect all namespace URIs referenced by tags and attributes."""
        m = _CLARK_RE.match(elem.tag or "")
        if m:
            seen.add(m.group(1))
        for k in elem.attrib:
            m = _CLARK_RE.match(k)
            if m:
                seen.add(m.group(1))
        for child in elem:
            _collect_ns(child, seen)
        return seen

    def _write_element(elem, is_root: bool = False) -> None:
        tag_str = _qname(elem.tag)

        attr_parts: list = []

        if is_root:
            # Emit namespace declarations on the root element.
            ns_uris = _collect_ns(elem, set())
            for uri in sorted(ns_uris):
                if uri in _IMPLICIT_NS:
                    continue
                prefix = uri_to_prefix.get(uri)
                if prefix is None:
                    continue
                if prefix == "":
                    attr_parts.append(f'xmlns="{uri}"')
                else:
                    attr_parts.append(f'xmlns:{prefix}="{uri}"')

        for k, v in elem.attrib.items():
            attr_parts.append(f'{_qname(k)}="{_attr_escape(v)}"')

        attr_str = (" " + " ".join(attr_parts)) if attr_parts else ""

        raw_v, raw_t = get_raw_geometry(elem)
        has_content = raw_v is not None or len(elem) > 0 or elem.text

        if has_content:
            f.write(f"<{tag_str}{attr_str}>".encode("utf-8"))
            if elem.text:
                f.write(_text_escape(elem.text).encode("utf-8"))
            if raw_v is not None:
                # Geometry written as pre-built strings — the main speedup.
                f.write(raw_v.encode("utf-8"))
                if raw_t is not None:
                    f.write(raw_t.encode("utf-8"))
                clear_raw_geometry(elem)
            # Always recurse into DOM children (handles triangle_sets,
            # metadatagroup, and passthrough <triangles> from write_passthrough_triangles).
            for child in elem:
                _write_element(child)
            f.write(f"</{tag_str}>".encode("utf-8"))
        else:
            f.write(f"<{tag_str}{attr_str}/>".encode("utf-8"))

        if elem.tail:
            f.write(_text_escape(elem.tail).encode("utf-8"))

    f.write(b"<?xml version='1.0' encoding='UTF-8'?>\n")
    _write_element(root, is_root=True)


def _is_object_excluded(obj: bpy.types.Object, ctx) -> bool:
    """Return True if *obj* should be excluded from export.

    Checks (in order):
    1. **Viewport visibility** — ``visible_get()`` accounts for the per-object
       eye icon, collection visibility toggles, and view-layer excludes.
       Skipped when ``export_hidden`` is *True*.
    2. **Render disabled** — the camera-icon on the object
       (``obj.hide_render``).  Included when ``include_disabled`` is *True*.
    """
    if not ctx.options.export_hidden and not obj.visible_get():
        return True
    if not ctx.options.include_disabled and obj.hide_render:
        return True
    return False


class BaseExporter:
    """Base class for format-specific exporters."""

    def __init__(self, ctx: ExportContext):
        """
        Initialize with reference to the export context.

        :param ctx: The ExportContext with settings and state.
        """
        self.ctx = ctx

    def attr(self, name: str) -> str:
        """
        Get attribute name, optionally with namespace prefix.

        In Orca/PrusaSlicer mode, attributes should not have namespace prefixes.
        In standard 3MF mode with default_namespace, they need the prefix.
        """
        if (
            self.ctx.options.use_orca_format in ("PAINT", "AUTO")
            or self.ctx.options.mmu_slicer_format == "PRUSA"
        ):
            return name
        return f"{{{MODEL_NAMESPACE}}}{name}"

    @staticmethod
    def _find_paint_texture(original_object: bpy.types.Object):
        """Return the paint texture image for *original_object*, or ``None``.

        Checks the ``3mf_is_paint_texture`` flag before scanning material
        nodes so non-paint objects are skipped cheaply.
        """
        mesh_data = original_object.data
        if not (
            "3mf_is_paint_texture" in mesh_data
            and mesh_data["3mf_is_paint_texture"]
        ):
            return None
        for mat_slot in original_object.material_slots:
            if mat_slot.material and mat_slot.material.use_nodes:
                for node in mat_slot.material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        return node.image
        return None

    def _extract_auxiliary_segmentation(
        self,
        original_object: bpy.types.Object,
        eval_object: bpy.types.Object,
        mesh: bpy.types.Mesh,
        layer_type: str,
        subdivided_mesh=None,
    ) -> dict:
        """Extract seam or support segmentation strings from a paint texture.

        Uses the same segmentation codec as color paint but with a fixed
        2-state palette (enforce / block) plus a background state (auto).

        :param original_object: The original (non-evaluated) Blender object.
        :param eval_object: The evaluated Blender object (with modifiers applied).
        :param mesh: The mesh with loop_triangles already calculated.
        :param layer_type: ``"SEAM"`` or ``"SUPPORT"``.
        :return: Dict mapping loop_triangle index -> hex segmentation string.
        """
        from .segmentation import texture_to_segmentation
        from ..paint.helpers import (
            _layer_flag_key, _layer_colors_key, _layer_uv_name,
            _layer_colors, LAYER_BACKGROUND,
        )

        original_mesh = original_object.data
        flag_key = _layer_flag_key(layer_type)

        if not (flag_key in original_mesh and original_mesh[flag_key]):
            return {}

        uv_name = _layer_uv_name(layer_type)
        bg, enforce, block = _layer_colors(layer_type)

        # Find the paint texture for this layer
        paint_texture = None
        image_suffix = f"_{uv_name}"

        # Try by naming convention first
        target_name = f"{original_mesh.name}{image_suffix}"
        paint_texture = bpy.data.images.get(target_name)

        # Fallback: scan material nodes for matching label/name
        if not paint_texture:
            for mat_slot in original_object.material_slots:
                if mat_slot.material and mat_slot.material.use_nodes:
                    for node in mat_slot.material.node_tree.nodes:
                        if node.type == "TEX_IMAGE" and node.image:
                            if (node.label == uv_name
                                    or node.name == uv_name
                                    or image_suffix in node.image.name):
                                paint_texture = node.image
                                break
                    if paint_texture:
                        break

        if not paint_texture:
            debug(f"  No {layer_type} paint texture found for export")
            return {}

        # Build extruder_colors mapping so _build_state_map produces correct
        # states:  0-based idx -> RGB tuple.
        #   idx 0 -> enforce (ext_num 1 -> state 1)
        #   idx 1 -> block   (ext_num 2 -> state 2)
        #   idx 2 -> bg      (ext_num 3 -> state 0 via default_extruder=3)
        extruder_colors = {
            0: list(enforce),
            1: list(block),
            2: list(LAYER_BACKGROUND),
        }

        debug(f"  Exporting {layer_type} paint texture '{paint_texture.name}'")

        try:
            seg_strings = texture_to_segmentation(
                eval_object,
                paint_texture,
                extruder_colors,
                default_extruder=3,
                max_depth=self.ctx.options.subdivision_depth,
                mesh=subdivided_mesh,
            )
            debug(
                f"  Generated {len(seg_strings)} {layer_type} segmentation strings"
            )
            return seg_strings
        except Exception as e:
            debug(f"  WARNING: Failed to export {layer_type} segmentation: {e}")
            import traceback
            traceback.print_exc()
            return {}


class StandardExporter(BaseExporter):
    """Exports standard 3MF files (core spec with optional basematerials and triangle sets)."""

    def execute(
        self,
        context: bpy.types.Context,
        archive: zipfile.ZipFile,
        blender_objects,
        global_scale: float,
    ) -> Set[str]:
        """
        Standard 3MF export (non-Orca mode).

        Uses core 3MF spec with optional basematerials and triangle sets.
        """
        ctx = self.ctx

        from .segmentation import clear_export_state_cache
        clear_export_state_cache()

        # Register all active extension namespaces with ElementTree
        ctx.extension_manager.register_namespaces(xml.etree.ElementTree)

        # Register MODEL_NAMESPACE as the default namespace (empty prefix)
        xml.etree.ElementTree.register_namespace("", MODEL_NAMESPACE)

        # Create model root element
        root = xml.etree.ElementTree.Element(f"{{{MODEL_NAMESPACE}}}model")
        root.set("unit", "millimeter")
        root.set("xml:lang", "en-US")

        scene_metadata = Metadata()
        scene_metadata.retrieve(bpy.context.scene)
        write_metadata(root, scene_metadata, ctx.options.use_orca_format)

        resources_element = xml.etree.ElementTree.SubElement(
            root, f"{{{MODEL_NAMESPACE}}}resources"
        )

        # Resolve all mesh objects recursively (descends into nested empties)
        # Used for material scanning; write_objects handles hierarchy itself.
        all_mesh_objects = collect_mesh_objects(
            blender_objects,
            export_hidden=ctx.options.export_hidden,
            include_disabled=ctx.options.include_disabled,
        )

        (
            ctx.material_name_to_index,
            ctx.next_resource_id,
            ctx.material_resource_id,
            basematerials_element,
        ) = write_materials(
            resources_element,
            all_mesh_objects,
            ctx.options.use_orca_format,
            ctx.vertex_colors,
            ctx.next_resource_id,
        )

        # Accumulate all texture relationships across PBR, standard, and
        # passthrough pipelines, then write them once at the end to avoid
        # each call overwriting the previous rels file.
        all_texture_rels = {}

        # Detect PBR textured materials FIRST — these use pbmetallictexturedisplayproperties
        pbr_textured_materials = detect_pbr_textured_materials(all_mesh_objects)

        if pbr_textured_materials and basematerials_element is not None:
            for mat_name, pbr_info in pbr_textured_materials.items():
                if pbr_info.get("roughness") or pbr_info.get("metallic"):
                    ctx.pbr_material_names.add(mat_name)

            if ctx.pbr_material_names:
                debug(f"Detected PBR textured materials: {list(ctx.pbr_material_names)}")
                ctx.extension_manager.activate(MATERIALS_EXTENSION.namespace)

                pbr_image_to_path = write_pbr_textures_to_archive(
                    archive, pbr_textured_materials
                )

                if pbr_image_to_path:
                    all_texture_rels.update(pbr_image_to_path)

                    material_to_display_props, ctx.next_resource_id = (
                        write_pbr_texture_display_properties(
                            resources_element,
                            pbr_textured_materials,
                            pbr_image_to_path,
                            ctx.next_resource_id,
                            basematerials_element,
                        )
                    )
                    debug(
                        f"Created PBR display properties for {len(material_to_display_props)} materials"
                    )

        # Detect and export textured materials — including PBR materials.
        # PBR materials need a texture2dgroup for UV coordinate data;
        # pbmetallictexturedisplayproperties are display hints only.
        textured_materials = detect_textured_materials(all_mesh_objects)
        ctx.texture_groups = {}

        if textured_materials:
            debug(
                f"Detected {len(textured_materials)} textured materials"
            )
            ctx.extension_manager.activate(MATERIALS_EXTENSION.namespace)

            image_to_path = write_textures_to_archive(
                archive, textured_materials
            )
            all_texture_rels.update(image_to_path)

            ctx.texture_groups, ctx.next_resource_id = write_texture_resources(
                resources_element,
                textured_materials,
                image_to_path,
                ctx.next_resource_id,
                ctx.options.coordinate_precision,
            )
            debug(f"Created {len(ctx.texture_groups)} texture groups")

            # If every basematerial is now covered by a texture2dgroup, the
            # basematerials element (and its linked PBR display properties)
            # become dead weight that can confuse consumers.  Remove them so
            # the exported file mirrors the consortium reference samples.
            if (
                basematerials_element is not None
                and ctx.material_name_to_index
                and set(ctx.material_name_to_index.keys()) <= set(textured_materials.keys())
            ):
                # Find and remove the PBR display properties element linked
                # via displaypropertiesid before removing basematerials.
                display_props_id = basematerials_element.get("displaypropertiesid")
                if display_props_id:
                    for child in list(resources_element):
                        if child.get("id") == display_props_id:
                            resources_element.remove(child)
                            debug("Removed unused PBR display properties "
                                  f"(id={display_props_id})")
                            break

                resources_element.remove(basematerials_element)
                basematerials_element = None
                debug("Removed unused basematerials — all faces use texture2dgroup")

        # Write passthrough texture images to the archive BEFORE writing XML references
        passthrough_image_paths = write_passthrough_textures_to_archive(archive)
        if passthrough_image_paths:
            for path in passthrough_image_paths:
                all_texture_rels[path] = f"/{path}"

        # Write all texture relationships in a single call
        if all_texture_rels:
            write_texture_relationships(archive, all_texture_rels)

        # Write passthrough materials (compositematerials, multiproperties, etc.)
        ctx.next_resource_id, passthrough_written, ctx.passthrough_id_remap = (
            write_passthrough_materials(resources_element, ctx.next_resource_id)
        )
        if passthrough_written:
            ctx.extension_manager.activate(MATERIALS_EXTENSION.namespace)

        ctx._progress_update(5, "Writing objects...", phase=1)
        _t_objects = time.perf_counter()
        self.write_objects(root, resources_element, blender_objects, global_scale)
        timing_debug("StandardExporter.write_objects TOTAL", (time.perf_counter() - _t_objects) * 1000)

        # Re-register namespaces now that all extensions have been activated.
        # The initial registration (above) runs before material detection, so
        # extensions activated during detection would get auto-prefixed (ns0,
        # ns1, ...) instead of the correct short prefixes (m, t, etc.).
        ctx.extension_manager.register_namespaces(xml.etree.ElementTree)
        xml.etree.ElementTree.register_namespace("", MODEL_NAMESPACE)

        # Declare required extensions on the model root element so consumers
        # know they must support these namespaces to render the model.
        required_ext_string = ctx.extension_manager.get_required_extensions_string()
        if required_ext_string:
            root.set("requiredextensions", required_ext_string)

        ctx._progress_update(95, "Writing model XML...")
        _t_xml = time.perf_counter()
        with archive.open(MODEL_LOCATION, "w", force_zip64=True) as f:
            _stream_model_to_file(f, root)
        timing_debug("XML serialise + write to archive", (time.perf_counter() - _t_xml) * 1000)

        write_core_properties(archive)
        write_thumbnail(archive, ctx, list(blender_objects))

        ctx._progress_update(100, "Finalizing export...")
        return ctx.finalize_export(archive)

    def _compute_centering_offset(
        self,
        blender_objects: List[bpy.types.Object],
        scale_matrix: mathutils.Matrix,
    ) -> mathutils.Matrix:
        """Return a translation that moves the collective bounding box to XY center, Z=0."""
        all_x, all_y, all_z = [], [], []
        for obj in blender_objects:
            if _is_object_excluded(obj, self.ctx) or obj.type != "MESH":
                continue
            m = scale_matrix @ obj.matrix_world
            for corner in obj.bound_box:
                v = m @ mathutils.Vector(corner)
                all_x.append(v.x)
                all_y.append(v.y)
                all_z.append(v.z)
        if not all_x:
            return mathutils.Matrix.Identity(4)
        return mathutils.Matrix.Translation((
            -(min(all_x) + max(all_x)) / 2,
            -(min(all_y) + max(all_y)) / 2,
            -min(all_z),
        ))

    def write_objects(
        self,
        root: xml.etree.ElementTree.Element,
        resources_element: xml.etree.ElementTree.Element,
        blender_objects: List[bpy.types.Object],
        global_scale: float,
    ) -> None:
        """
        Writes a group of objects into the 3MF archive.

        If use_components is enabled, detects linked duplicates and exports them
        as component instances for smaller file sizes.
        """
        ctx = self.ctx
        transformation = mathutils.Matrix.Scale(global_scale, 4)
        transformation = self._compute_centering_offset(blender_objects, transformation) @ transformation

        # Detect linked duplicates if component optimization is enabled
        component_groups = {}
        if ctx.options.use_components:
            component_groups = detect_linked_duplicates(
                blender_objects,
                export_hidden=ctx.options.export_hidden,
                include_disabled=ctx.options.include_disabled,
            )

            if component_groups and should_use_components(
                component_groups, blender_objects
            ):
                debug(
                    f"Using component optimization: {len(component_groups)} component groups detected"
                )
                ctx.safe_report(
                    {"INFO"},
                    f"Using component optimization: {len(component_groups)} component groups detected",
                )

                for mesh_data, group in component_groups.items():
                    representative_obj = group.objects[0]
                    component_id = self._write_component_definition(
                        resources_element, representative_obj
                    )
                    group.component_id = component_id
                    debug(
                        f"Component definition {component_id}: '{mesh_data.name}' "
                        f"({len(group.objects)} instances)"
                    )
            else:
                component_groups = {}

        build_element = xml.etree.ElementTree.SubElement(
            root, f"{{{MODEL_NAMESPACE}}}build"
        )
        hidden_skipped = 0

        total_objects = sum(
            1
            for obj in blender_objects
            if not (_is_object_excluded(obj, ctx))
            and obj.parent is None
            and obj.type in {"MESH", "EMPTY"}
        )
        processed_objects = 0

        for blender_object in blender_objects:
            if _is_object_excluded(blender_object, ctx):
                hidden_skipped += 1
                continue
            if blender_object.parent is not None:
                continue
            if blender_object.type not in {"MESH", "EMPTY"}:
                continue

            processed_objects += 1
            if total_objects > 0:
                # Scale to 5–44% so all objects stay within the Geometry phase
                # (phase 1, cumulative 5–45%). Using the full range caused later
                # objects to appear in Materials/Segmentation on the browser card.
                progress = 5 + int((processed_objects / total_objects) * 39)
                ctx._progress_update(
                    progress,
                    f"Writing {processed_objects}/{total_objects} objects...",
                    phase=1,  # Geometry
                )

            # When flatten_hierarchy is enabled, EMPTYs are dissolved:
            # their mesh descendants become individual build items with
            # their full world transform, avoiding <components> containers
            # that some printing services reject.
            if ctx.options.flatten_hierarchy and blender_object.type == "EMPTY":
                child_meshes = collect_mesh_objects(
                    [blender_object],
                    export_hidden=ctx.options.export_hidden,
                    include_disabled=ctx.options.include_disabled,
                )
                if child_meshes:
                    debug(
                        f"Flattening EMPTY '{blender_object.name}': "
                        f"{len(child_meshes)} child mesh(es) promoted to build items"
                    )
                for child_obj in child_meshes:
                    child_id, _ = self.write_object_resource(
                        resources_element, child_obj
                    )
                    if child_id is None:
                        continue
                    self._write_build_item(
                        build_element, child_id,
                        transformation @ child_obj.matrix_world,
                        child_obj,
                    )
                continue

            # Check if this object is a component instance
            if (
                component_groups
                and blender_object.type == "MESH"
                and blender_object.data in component_groups
            ):
                objectid = self._write_component_instance(
                    resources_element,
                    blender_object,
                    component_groups[blender_object.data].component_id,
                )
            else:
                objectid, mesh_transformation = self.write_object_resource(
                    resources_element, blender_object
                )

            if objectid is None:
                continue

            self._write_build_item(
                build_element, objectid,
                transformation @ blender_object.matrix_world,
                blender_object,
            )

        if hidden_skipped > 0:
            ctx.safe_report(
                {"INFO"},
                f"Skipped {hidden_skipped} hidden/disabled object(s). "
                "Enable 'Include Hidden' or disable 'Skip Disabled' to export them.",
            )

    def _write_build_item(
        self,
        build_element: xml.etree.ElementTree.Element,
        objectid: int,
        mesh_transformation: mathutils.Matrix,
        blender_object: bpy.types.Object,
    ) -> None:
        """Write a single ``<item>`` inside the ``<build>`` element."""
        ctx = self.ctx
        item_element = xml.etree.ElementTree.SubElement(
            build_element, f"{{{MODEL_NAMESPACE}}}item"
        )
        ctx.num_written += 1
        item_element.attrib[self.attr("objectid")] = str(objectid)

        if mesh_transformation != mathutils.Matrix.Identity(4):
            item_element.attrib[self.attr("transform")] = format_transformation(
                mesh_transformation
            )

        metadata = Metadata()
        metadata.retrieve(blender_object)
        if "3mf:partnumber" in metadata:
            item_element.attrib[self.attr("partnumber")] = metadata[
                "3mf:partnumber"
            ].value
            del metadata["3mf:partnumber"]
        if metadata:
            metadatagroup_element = xml.etree.ElementTree.SubElement(
                item_element, f"{{{MODEL_NAMESPACE}}}metadatagroup"
            )
            write_metadata(metadatagroup_element, metadata, ctx.options.use_orca_format)

    def write_object_resource(
        self,
        resources_element: xml.etree.ElementTree.Element,
        blender_object: bpy.types.Object,
    ) -> Tuple[int, mathutils.Matrix]:
        """
        Write a single Blender object and all of its children to the resources of a 3MF document.
        """
        ctx = self.ctx
        debug(
            f"write_object_resource called for: {blender_object.name}, type: {blender_object.type}"
        )

        new_resource_id = ctx.next_resource_id
        ctx.next_resource_id += 1
        object_element = xml.etree.ElementTree.SubElement(
            resources_element, f"{{{MODEL_NAMESPACE}}}object"
        )
        object_element.attrib[self.attr("id")] = str(new_resource_id)
        object_name = str(blender_object.name)
        object_element.attrib[self.attr("name")] = object_name

        metadata = Metadata()
        metadata.retrieve(blender_object)
        if "3mf:object_type" in metadata:
            object_type = metadata["3mf:object_type"].value
            if object_type != "model":
                object_element.attrib[self.attr("type")] = object_type
            del metadata["3mf:object_type"]

        if blender_object.mode == "EDIT":
            blender_object.update_from_editmode()
        mesh_transformation = blender_object.matrix_world

        child_objects = blender_object.children
        components_element = None
        if child_objects:
            # Filter to MESH and EMPTY children (recurse into nested empties)
            exportable_children = [
                child for child in blender_object.children
                if child.type in {"MESH", "EMPTY"}
            ]
            if exportable_children:
                components_element = xml.etree.ElementTree.SubElement(
                    object_element, f"{{{MODEL_NAMESPACE}}}components"
                )
                for child in exportable_children:
                    child_id, child_transformation = self.write_object_resource(
                        resources_element, child
                    )
                    if child_id is None:
                        continue
                    child_transformation = (
                        mesh_transformation.inverted_safe() @ child_transformation
                    )
                    component_element = xml.etree.ElementTree.SubElement(
                        components_element, f"{{{MODEL_NAMESPACE}}}component"
                    )
                    ctx.num_written += 1
                    component_element.attrib[self.attr("objectid")] = str(child_id)
                    if child_transformation != mathutils.Matrix.Identity(4):
                        component_element.attrib[self.attr("transform")] = (
                            format_transformation(child_transformation)
                        )

                # Remove empty <components> if all children were filtered out
                if len(components_element) == 0:
                    object_element.remove(components_element)
                    components_element = None

        # Get vertex data (may need to apply modifiers)
        original_object = blender_object
        if ctx.options.use_mesh_modifiers:
            dependency_graph = bpy.context.evaluated_depsgraph_get()
            blender_object = blender_object.evaluated_get(dependency_graph)

        _t_mesh = time.perf_counter()
        try:
            mesh = blender_object.to_mesh()
        except RuntimeError:
            # EMPTYs and other non-mesh objects can't produce a mesh, but
            # if they have components they're still valid container objects.
            if components_element is not None and len(components_element) > 0:
                return new_resource_id, mesh_transformation
            resources_element.remove(object_element)
            ctx.next_resource_id = new_resource_id
            return None, mesh_transformation
        if mesh is None:
            if components_element is not None and len(components_element) > 0:
                return new_resource_id, mesh_transformation
            resources_element.remove(object_element)
            ctx.next_resource_id = new_resource_id
            return None, mesh_transformation

        mesh.calc_loop_triangles()
        timing_debug(f"to_mesh + calc_loop_triangles '{object_name}'", (time.perf_counter() - _t_mesh) * 1000)

        # Adaptive pre-subdivision for PAINT mode: split large faces so each
        # triangle can be encoded at full segmentation depth without blocky
        # artefacts.  Only touches the temporary to_mesh() copy.
        if ctx.options.use_orca_format == "PAINT" and mesh.uv_layers.active:
            paint_img = self._find_paint_texture(original_object)
            if paint_img:
                from .segmentation import subdivide_mesh_for_segmentation
                subdivide_mesh_for_segmentation(
                    mesh,
                    ctx.options.subdivision_depth,
                    paint_img.size[0],
                    paint_img.size[1],
                )

        debug(
            f"  Got mesh: {len(mesh.vertices)} vertices, {len(mesh.loop_triangles)} triangles"
        )

        if len(mesh.vertices) >= 3 and len(mesh.loop_triangles) > 0:
            if child_objects:
                mesh_id = ctx.next_resource_id
                ctx.next_resource_id += 1
                mesh_object_element = xml.etree.ElementTree.SubElement(
                    resources_element, f"{{{MODEL_NAMESPACE}}}object"
                )
                mesh_object_element.attrib[self.attr("id")] = str(mesh_id)
                component_element = xml.etree.ElementTree.SubElement(
                    components_element, f"{{{MODEL_NAMESPACE}}}component"
                )
                ctx.num_written += 1
                component_element.attrib[self.attr("objectid")] = str(mesh_id)
            else:
                mesh_object_element = object_element

            mesh_element = xml.etree.ElementTree.SubElement(
                mesh_object_element, f"{{{MODEL_NAMESPACE}}}mesh"
            )

            most_common_material_list_index = 0

            debug(
                f"[standard] write_object_resource: {blender_object.name}, mode={ctx.options.use_orca_format}, "
                f"slicer={ctx.options.mmu_slicer_format}"
            )
            debug(
                f"  mesh has {len(mesh.loop_triangles)} triangles, {len(blender_object.material_slots)} material slots"
            )

            # Check for passthrough multiproperties pid (round-trip export)
            original_mesh_data = original_object.data
            passthrough_pid = original_mesh_data.get("3mf_passthrough_pid")
            use_passthrough = False

            if passthrough_pid and ctx.passthrough_id_remap:
                id_remap = ctx.passthrough_id_remap
                remapped_pid = id_remap.get(passthrough_pid, passthrough_pid)
                object_element.attrib[self.attr("pid")] = str(remapped_pid)
                object_element.attrib[self.attr("pindex")] = "0"
                use_passthrough = True
                debug(f"  Using passthrough multiproperties pid={passthrough_pid} -> {remapped_pid}")

            if not use_passthrough:
                # Check if this object has any textured materials
                has_textured_material = False
                if ctx.texture_groups:
                    for mat_slot in blender_object.material_slots:
                        if (
                            mat_slot.material
                            and mat_slot.material.name in ctx.texture_groups
                        ):
                            has_textured_material = True
                            break

                # In AUTO mode, use face colors mapped to colorgroup IDs
                if (
                    ctx.options.use_orca_format == "AUTO"
                    and ctx.vertex_colors
                    and ctx.options.mmu_slicer_format == "ORCA"
                ):
                    color_counts = {}
                    for triangle in mesh.loop_triangles:
                        triangle_color = get_triangle_color(mesh, triangle, blender_object)
                        debug(
                            f"  triangle {triangle.index}: material_index={triangle.material_index}, "
                            f"color={triangle_color}"
                        )
                        if triangle_color and triangle_color in ctx.vertex_colors:
                            color_counts[triangle_color] = (
                                color_counts.get(triangle_color, 0) + 1
                            )

                    debug(f"  color_counts: {color_counts}")
                    if color_counts:
                        most_common_color = max(color_counts, key=color_counts.get)
                        colorgroup_id = ctx.vertex_colors[most_common_color]
                        object_element.attrib[self.attr("pid")] = str(colorgroup_id)
                        object_element.attrib[self.attr("pindex")] = "0"
                        most_common_material_list_index = colorgroup_id
                elif not has_textured_material:
                    if ctx.material_name_to_index:
                        material_indices = [
                            triangle.material_index for triangle in mesh.loop_triangles
                        ]

                        if material_indices and blender_object.material_slots:
                            counter = collections.Counter(material_indices)
                            most_common_material_object_index = counter.most_common(1)[0][0]
                            most_common_material = blender_object.material_slots[
                                most_common_material_object_index
                            ].material

                            if most_common_material is not None:
                                most_common_material_list_index = (
                                    ctx.material_name_to_index[
                                        most_common_material.name
                                    ]
                                )
                                object_element.attrib[self.attr("pid")] = str(
                                    ctx.material_resource_id
                                )
                                object_element.attrib[self.attr("pindex")] = str(
                                    most_common_material_list_index
                                )

            _t_verts = time.perf_counter()
            write_vertices(
                mesh_element,
                mesh.vertices,
                ctx.options.use_orca_format,
                ctx.options.coordinate_precision,
            )
            timing_debug(f"write_vertices wall '{object_name}'", (time.perf_counter() - _t_verts) * 1000)

            # Generate segmentation strings from UV texture if in PAINT mode
            segmentation_strings = {}
            seam_strings = {}
            support_strings = {}
            debug(
                f"[standard] Checking PAINT export: mode={ctx.options.use_orca_format}",
                f", has_uv={bool(mesh.uv_layers.active)}"
            )
            if ctx.options.use_orca_format == "PAINT" and mesh.uv_layers.active:
                _t_seg = time.perf_counter()
                segmentation_strings = self._extract_segmentation(
                    original_object, blender_object, mesh
                )
                timing_debug(f"_extract_segmentation '{object_name}'", (time.perf_counter() - _t_seg) * 1000)
                _t_seam = time.perf_counter()
                seam_strings = self._extract_auxiliary_segmentation(
                    original_object, blender_object, mesh, "SEAM",
                    subdivided_mesh=mesh,
                )
                timing_debug(f"_extract_auxiliary SEAM '{object_name}'", (time.perf_counter() - _t_seam) * 1000)
                _t_sup = time.perf_counter()
                support_strings = self._extract_auxiliary_segmentation(
                    original_object, blender_object, mesh, "SUPPORT",
                    subdivided_mesh=mesh,
                )
                timing_debug(f"_extract_auxiliary SUPPORT '{object_name}'", (time.perf_counter() - _t_sup) * 1000)

            debug(
                f"[standard] Calling write_triangles with {len(segmentation_strings)} segmentation strings"
            )

            if use_passthrough and mesh.uv_layers.active:
                write_passthrough_triangles(
                    mesh_element, mesh, passthrough_pid, remapped_pid,
                    ctx.options.use_orca_format, ctx.options.coordinate_precision,
                )
            else:
                _t_tris = time.perf_counter()
                write_triangles(
                    mesh_element,
                    mesh.loop_triangles,
                    most_common_material_list_index,
                    blender_object.material_slots,
                    ctx.material_name_to_index,
                    ctx.options.use_orca_format,
                    ctx.options.mmu_slicer_format,
                    ctx.vertex_colors,
                    mesh,
                    blender_object,
                    ctx.texture_groups or None,
                    str(ctx.material_resource_id)
                    if ctx.material_resource_id
                    else None,
                    segmentation_strings,
                    seam_strings=seam_strings,
                    support_strings=support_strings,
                )
                timing_debug(f"write_triangles caller overhead '{object_name}'", (time.perf_counter() - _t_tris) * 1000)

            # Write triangle sets if present (auto-export utility metadata)
            # Skipped when PAINT mode is active since segmentation data replaces it
            has_triangle_sets = (
                "3mf_triangle_set" in mesh.attributes
                or ".sculpt_face_set" in mesh.attributes
            )
            if ctx.options.use_orca_format != "PAINT" and has_triangle_sets:
                # Activate extension on first use
                if not ctx.extension_manager.is_active(TRIANGLE_SETS_EXTENSION.namespace):
                    ctx.extension_manager.activate(TRIANGLE_SETS_EXTENSION.namespace)
                    debug("Activated Triangle Sets extension")
                write_triangle_sets(mesh_element, mesh, original_object.data)

            # Write metadata
            if "3mf:partnumber" in metadata:
                mesh_object_element.attrib[self.attr("partnumber")] = metadata[
                    "3mf:partnumber"
                ].value
                del metadata["3mf:partnumber"]
            if "3mf:object_type" in metadata:
                object_type = metadata["3mf:object_type"].value
                if object_type != "model" and object_type != "other":
                    mesh_object_element.attrib[self.attr("type")] = object_type
                del metadata["3mf:object_type"]
            if metadata:
                metadatagroup_element = xml.etree.ElementTree.SubElement(
                    object_element, f"{{{MODEL_NAMESPACE}}}metadatagroup"
                )
                write_metadata(metadatagroup_element, metadata, ctx.options.use_orca_format)

        # Clean up the temporary mesh created by to_mesh()
        blender_object.to_mesh_clear()

        # If the object has neither mesh nor components, it's invalid per
        # the 3MF spec.  Remove it from the resources and signal the caller
        # to skip any build-item / component reference.
        if len(object_element) == 0:
            resources_element.remove(object_element)
            ctx.next_resource_id = new_resource_id  # revert ID allocation
            warn(f"Skipping '{object_name}': mesh has no triangles")
            return None, mesh_transformation

        return new_resource_id, mesh_transformation

    def _extract_segmentation(
        self,
        original_object: bpy.types.Object,
        eval_object: bpy.types.Object,
        mesh: bpy.types.Mesh,
    ) -> dict:
        """
        Extract segmentation strings from a paint texture on the given object.

        Shared logic used by multiple exporters.

        :param original_object: The original (non-evaluated) Blender object.
        :param eval_object: The evaluated Blender object (with modifiers applied).
        :param mesh: The mesh with loop_triangles already calculated.
        :return: Dict mapping loop_triangle index -> hex segmentation string.
        """
        import ast
        from ..common.colors import hex_to_rgb
        from .segmentation import texture_to_segmentation

        ctx = self.ctx
        original_mesh_data = original_object.data
        debug(
            f"  PAINT mode active — checking custom properties on '{original_mesh_data.name}'"
        )

        if not (
            "3mf_is_paint_texture" in original_mesh_data
            and original_mesh_data["3mf_is_paint_texture"]
        ):
            debug("  WARNING: No paint texture flag found for export")
            return {}

        paint_texture = None
        extruder_colors = {}
        default_extruder = original_mesh_data.get("3mf_paint_default_extruder", 0)
        debug(f"  Found paint texture flag, default_extruder={default_extruder}")

        # Get the stored extruder colors
        if "3mf_paint_extruder_colors" in original_mesh_data:
            try:
                extruder_colors_hex = ast.literal_eval(
                    original_mesh_data["3mf_paint_extruder_colors"]
                )
                for idx, hex_color in extruder_colors_hex.items():
                    extruder_colors[idx] = hex_to_rgb(hex_color)
            except Exception as e:
                debug(f"  WARNING: Failed to parse extruder colors: {e}")

        # Find the MMU paint texture
        for mat_slot in original_object.material_slots:
            if mat_slot.material and mat_slot.material.use_nodes:
                for node in mat_slot.material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        paint_texture = node.image
                        break
                if paint_texture:
                    break

        if not paint_texture or not extruder_colors:
            debug("  WARNING: No paint texture or extruder colors found for export")
            return {}

        debug(f"  Exporting paint texture '{paint_texture.name}' as segmentation")

        # Create progress callback
        def seg_progress(current, total, message):
            if total > 0:
                seg_pct = current / total
                # Segmentation phase = phase 3, cumulative 65–90%
                overall = int(65 + (seg_pct * 24))
                ctx._progress_update(overall, message, phase=3)

        try:
            segmentation_strings = texture_to_segmentation(
                eval_object,
                paint_texture,
                extruder_colors,
                default_extruder,
                progress_callback=seg_progress,
                max_depth=self.ctx.options.subdivision_depth,
                mesh=mesh,
            )
            debug(
                f"  Generated {len(segmentation_strings)} segmentation strings from texture"
            )
            return segmentation_strings
        except Exception as e:
            debug(f"  WARNING: Failed to generate segmentation from texture: {e}")
            import traceback
            traceback.print_exc()
            return {}

    def _write_component_definition(
        self,
        resources_element: xml.etree.ElementTree.Element,
        blender_object: bpy.types.Object,
    ) -> int:
        """
        Write a component definition — a reusable mesh resource.

        :param resources_element: The <resources> element to write to.
        :param blender_object: The Blender object (used as representative for this component).
        :return: The resource ID of the component definition.
        """
        ctx = self.ctx
        component_id = ctx.next_resource_id
        ctx.next_resource_id += 1

        object_element = xml.etree.ElementTree.SubElement(
            resources_element, f"{{{MODEL_NAMESPACE}}}object"
        )
        object_element.attrib[self.attr("id")] = str(component_id)
        mesh_name = str(blender_object.data.name)
        object_element.attrib[self.attr("name")] = mesh_name

        if ctx.options.use_mesh_modifiers:
            dependency_graph = bpy.context.evaluated_depsgraph_get()
            eval_object = blender_object.evaluated_get(dependency_graph)
        else:
            eval_object = blender_object

        try:
            mesh = eval_object.to_mesh()
        except RuntimeError:
            return component_id

        if mesh is None:
            return component_id

        mesh.calc_loop_triangles()

        if len(mesh.vertices) >= 3 and len(mesh.loop_triangles) > 0:
            mesh_element = xml.etree.ElementTree.SubElement(
                object_element, f"{{{MODEL_NAMESPACE}}}mesh"
            )

            most_common_material_list_index = 0

            has_textured_material = False
            if ctx.texture_groups:
                for mat_slot in blender_object.material_slots:
                    if (
                        mat_slot.material
                        and mat_slot.material.name in ctx.texture_groups
                    ):
                        has_textured_material = True
                        break

            if (
                ctx.options.use_orca_format == "AUTO"
                and ctx.vertex_colors
                and ctx.options.mmu_slicer_format == "ORCA"
            ):
                color_counts = {}
                for triangle in mesh.loop_triangles:
                    triangle_color = get_triangle_color(mesh, triangle, blender_object)
                    if triangle_color and triangle_color in ctx.vertex_colors:
                        color_counts[triangle_color] = (
                            color_counts.get(triangle_color, 0) + 1
                        )

                if color_counts:
                    most_common_color = max(color_counts, key=color_counts.get)
                    colorgroup_id = ctx.vertex_colors[most_common_color]
                    object_element.attrib[self.attr("pid")] = str(colorgroup_id)
                    object_element.attrib[self.attr("pindex")] = "0"
                    most_common_material_list_index = colorgroup_id
            elif not has_textured_material and ctx.material_name_to_index:
                material_indices = [
                    triangle.material_index for triangle in mesh.loop_triangles
                ]

                if material_indices and blender_object.material_slots:
                    counter = collections.Counter(material_indices)
                    most_common_material_object_index = counter.most_common(1)[0][0]
                    most_common_material = blender_object.material_slots[
                        most_common_material_object_index
                    ].material

                    if most_common_material is not None:
                        most_common_material_list_index = (
                            ctx.material_name_to_index[most_common_material.name]
                        )
                        object_element.attrib[self.attr("pid")] = str(
                            ctx.material_resource_id
                        )
                        object_element.attrib[self.attr("pindex")] = str(
                            most_common_material_list_index
                        )

            write_vertices(
                mesh_element,
                mesh.vertices,
                ctx.options.use_orca_format,
                ctx.options.coordinate_precision,
            )

            write_triangles(
                mesh_element,
                mesh.loop_triangles,
                most_common_material_list_index,
                blender_object.material_slots,
                ctx.material_name_to_index,
                ctx.options.use_orca_format,
                ctx.options.mmu_slicer_format,
                ctx.vertex_colors,
                mesh,
                blender_object,
                ctx.texture_groups or None,
                str(ctx.material_resource_id)
                if ctx.material_resource_id
                else None,
            )

        eval_object.to_mesh_clear()
        return component_id

    def _write_component_instance(
        self,
        resources_element: xml.etree.ElementTree.Element,
        blender_object: bpy.types.Object,
        component_id: int,
    ) -> int:
        """
        Write a component instance — an object that references a component definition.

        :param resources_element: The <resources> element to write to.
        :param blender_object: The Blender object instance.
        :param component_id: The resource ID of the component definition to reference.
        :return: The resource ID of this instance container.
        """
        ctx = self.ctx
        instance_id = ctx.next_resource_id
        ctx.next_resource_id += 1

        object_element = xml.etree.ElementTree.SubElement(
            resources_element, f"{{{MODEL_NAMESPACE}}}object"
        )
        object_element.attrib[self.attr("id")] = str(instance_id)
        object_name = str(blender_object.name)
        object_element.attrib[self.attr("name")] = object_name

        components_element = xml.etree.ElementTree.SubElement(
            object_element, f"{{{MODEL_NAMESPACE}}}components"
        )
        component_element = xml.etree.ElementTree.SubElement(
            components_element, f"{{{MODEL_NAMESPACE}}}component"
        )
        component_element.attrib[self.attr("objectid")] = str(component_id)

        return instance_id
