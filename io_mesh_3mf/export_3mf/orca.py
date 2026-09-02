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
Orca Slicer / BambuStudio 3MF exporter.

Uses the Production Extension to create multi-file 3MF archives with
individual object model files and paint_color attributes for per-triangle
multi-material data.
"""

from __future__ import annotations

import ast
import datetime
import io
import json
import os
import re
import time
import uuid
import xml.etree.ElementTree
import zipfile
from typing import List, Optional, Set

import bpy
import mathutils
import numpy as np

from ..common.colors import hex_to_rgb
from ..common.constants import (
    MODEL_NAMESPACE,
    MODEL_LOCATION,
    MODEL_REL,
    PRODUCTION_NAMESPACE,
    BAMBU_NAMESPACE,
    RELS_NAMESPACE,
)
from ..common.extensions import PRODUCTION_EXTENSION, ORCA_EXTENSION
from ..common.logging import debug, timing_debug, warn, error
from ..common.xml import format_transformation

from .geometry import _raw_geometry_cache
from .materials import (
    ORCA_FILAMENT_CODES,
    collect_face_colors,
    get_triangle_color,
    material_to_hex_color,
)
from .components import collect_mesh_objects
from .segmentation import texture_to_segmentation
from .standard import BaseExporter, _stream_model_to_file
from .thumbnail import write_thumbnail
from ..import_3mf.archive import get_stashed_config
from ..slicer_profiles import get_profile_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_stashed_settings(
    parent_elem: xml.etree.ElementTree.Element,
    blender_object: bpy.types.Object,
    prop_name: str = "3mf_orca_settings",
) -> None:
    """Write stashed slicer setting overrides as ``<metadata>`` children.

    Reads a JSON dict from *blender_object[prop_name]* and writes each
    key/value pair as a ``<metadata key="..." value="..."/>`` element.

    :param parent_elem: The ``<object>`` or ``<part>`` XML element.
    :param blender_object: Blender object carrying the stashed settings.
    :param prop_name: Custom property name (default ``"3mf_orca_settings"``).
    """
    raw = blender_object.get(prop_name)
    if not raw:
        return
    try:
        settings = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(settings, dict):
        return
    for key, value in settings.items():
        xml.etree.ElementTree.SubElement(
            parent_elem, "metadata",
            key=str(key), value=str(value),
        )
    debug(
        f"Wrote {len(settings)} stashed overrides from "
        f"'{blender_object.name}'.{prop_name}"
    )


class OrcaExporter(BaseExporter):
    """Exports Orca Slicer compatible 3MF files using Production Extension."""

    def execute(
        self,
        context: bpy.types.Context,
        archive: zipfile.ZipFile,
        blender_objects,
        global_scale: float,
    ) -> Set[str]:
        """
        Orca Slicer export using Production Extension structure.

        Creates separate model files for each object with paint_color attributes,
        and a main model file with component references.
        """
        ctx = self.ctx

        # Activate Production Extension for Orca compatibility
        ctx.extension_manager.activate(PRODUCTION_EXTENSION.namespace)
        ctx.extension_manager.activate(ORCA_EXTENSION.namespace)
        debug("Activated Orca Slicer extensions: Production + BambuStudio")

        from .segmentation import clear_export_state_cache
        clear_export_state_cache()

        # Register namespaces
        xml.etree.ElementTree.register_namespace("", MODEL_NAMESPACE)
        xml.etree.ElementTree.register_namespace("p", PRODUCTION_NAMESPACE)
        xml.etree.ElementTree.register_namespace("BambuStudio", BAMBU_NAMESPACE)

        # Collect face colors for Orca export
        ctx.safe_report({"INFO"}, "Collecting face colors for Orca export...")

        # Sync mixed filament definitions from UI collection if user has edited them.
        settings = getattr(context.scene, "mmu_paint", None)
        if settings and settings.has_mixed_filaments and settings.mixed_filaments:
            from ..common.mixed_filaments import MixedFilament, serialize_mixed_filament_definitions
            from ..common.colors import rgb_to_hex
            entries = []
            for item in settings.mixed_filaments:
                # Copy the already-computed display color from the UI item so
                # filament_colour gets accurate swatches without needing to
                # re-derive physical colors at this point in the pipeline.
                disp = rgb_to_hex(*item.display_color[:]) if item.display_color else ""
                entries.append(MixedFilament(
                    component_a=item.component_a,
                    component_b=item.component_b,
                    enabled=item.enabled,
                    deleted=item.deleted,
                    custom=True,
                    mix_b_percent=item.mix_b_percent,
                    distribution_mode=int(item.distribution_mode),
                    manual_pattern=item.manual_pattern,
                    stable_id=item.stable_id,
                    display_color=disp,
                ))
            ctx.mixed_filament_definitions_raw = serialize_mixed_filament_definitions(entries)
            debug(f"Synced {len(entries)} mixed filament entries from UI to raw string")

        # Read mixed filament definitions from scene custom property (set on import).
        if not ctx.mixed_filament_definitions_raw:
            scene_defs = context.scene.get("3mf_mixed_filament_definitions", "")
            if scene_defs:
                ctx.mixed_filament_definitions_raw = str(scene_defs)
                debug(f"Loaded mixed filament definitions from scene property ({len(scene_defs)} chars)")

        # For PAINT mode, collect colors from paint texture metadata instead of face materials
        paint_colors_collected = False
        # FullSpectrum "parts mode": each part carries its own extruder assignment via
        # 3mf_paint_default_extruder.  Only physical filament colors belong in
        # ctx.vertex_colors / filament_colour; virtual slot display colors must NOT be
        # added here (the slicer derives them from mixed_filament_definitions at runtime).
        is_fullspectrum_parts = bool(ctx.mixed_filament_definitions_raw)
        if ctx.options.use_orca_format == "PAINT":
            mesh_objs_for_paint = collect_mesh_objects(
                blender_objects,
                export_hidden=ctx.options.export_hidden,
                include_disabled=ctx.options.include_disabled,
            )
            for blender_object in mesh_objs_for_paint:
                original_object = blender_object
                if hasattr(blender_object, "original"):
                    original_object = blender_object.original

                original_mesh_data = original_object.data
                if (
                    "3mf_is_paint_texture" in original_mesh_data
                    and original_mesh_data["3mf_is_paint_texture"]
                ):
                    if "3mf_paint_extruder_colors" in original_mesh_data:
                        try:
                            extruder_colors_hex = ast.literal_eval(
                                original_mesh_data["3mf_paint_extruder_colors"]
                            )
                            # For FullSpectrum parts mode, only record the physical
                            # filament colors (indices 0..num_physical-1) with 1-based
                            # extruder numbers.  Virtual display colors are excluded so
                            # that filament_colour stays at the physical-filament count.
                            if is_fullspectrum_parts:
                                num_physical = int(
                                    original_mesh_data.get(
                                        "3mf_num_physical_filaments",
                                        len(extruder_colors_hex),
                                    )
                                )
                                for idx, hex_color in extruder_colors_hex.items():
                                    if int(idx) < num_physical and hex_color not in ctx.vertex_colors:
                                        ctx.vertex_colors[hex_color] = int(idx) + 1
                                debug(
                                    f"FullSpectrum parts mode: collected {min(num_physical, len(extruder_colors_hex))} "
                                    f"physical colors (skipping virtual slots)"
                                )
                            else:
                                for idx, hex_color in extruder_colors_hex.items():
                                    if hex_color not in ctx.vertex_colors:
                                        ctx.vertex_colors[hex_color] = idx
                                debug(
                                    f"Collected {len(extruder_colors_hex)} colors from paint texture metadata"
                                )
                            paint_colors_collected = True
                        except Exception as e:
                            warn(f"Failed to parse extruder colors from metadata: {e}")

        # If no paint colors found, fall back to face material colors
        if not paint_colors_collected:
            ctx.vertex_colors = collect_face_colors(
                blender_objects,
                ctx.options.use_mesh_modifiers,
                ctx.safe_report,
                export_hidden=ctx.options.export_hidden,
                include_disabled=ctx.options.include_disabled,
            )

        debug(f"Orca mode enabled with {len(ctx.vertex_colors)} color zones")

        if len(ctx.vertex_colors) == 0:
            warn("No face colors found! Assign materials to faces for color zones.")
            ctx.safe_report(
                {"WARNING"},
                "No face colors detected. Assign different materials to faces in Edit mode.",
            )
        else:
            ctx.safe_report(
                {"INFO"},
                f"Detected {len(ctx.vertex_colors)} color zones for Orca export",
            )

        # Generate build UUID
        build_uuid = str(uuid.uuid4())

        mesh_objects = collect_mesh_objects(
            blender_objects,
            export_hidden=ctx.options.export_hidden,
            include_disabled=ctx.options.include_disabled,
        )

        if not mesh_objects:
            ctx.safe_report({"ERROR"}, "No mesh objects found to export!")
            archive.close()
            return {"CANCELLED"}

        # Build mapping of mesh objects to their parent groups (Empties)
        # Each Empty with children becomes a separate assembly in Orca
        mesh_to_group: dict[str, bpy.types.Object] = {}
        group_empties: list[bpy.types.Object] = []

        for obj in blender_objects:
            if obj.type == "EMPTY" and obj.children:
                group_empties.append(obj)
                # Map all mesh children to this group
                for child in obj.children:
                    if child.type == "MESH":
                        mesh_to_group[child.name] = obj

        debug(f"Detected {len(group_empties)} group(s) from parent Empties")

        # Write individual object model files
        object_data = []

        total_mesh_objects = len(mesh_objects)
        for idx, blender_object in enumerate(mesh_objects):
            # Don't update progress here in PAINT mode - let segmentation callback handle it
            if ctx.options.use_orca_format != "PAINT":
                # Scale to 5–44% so we stay within the Geometry phase (phase 1).
                # Using the full 0–95 range caused later objects to bleed into
                # Materials/Segmentation/Thumbnail phases on the browser card.
                progress = 5 + int(((idx + 1) / total_mesh_objects) * 39)
                ctx._progress_update(
                    progress,
                    f"Exporting {idx + 1}/{total_mesh_objects}: {blender_object.name}",
                    phase=1,  # Geometry
                )
            object_counter = idx + 1
            wrapper_id = object_counter * 2
            mesh_id = object_counter * 2 - 1

            # Generate UUIDs
            wrapper_uuid = f"{object_counter:08x}-61cb-4c03-9d28-80fed5dfa1dc"
            mesh_uuid = f"{object_counter:04x}0000-81cb-4c03-9d28-80fed5dfa1dc"
            component_uuid = f"{object_counter:04x}0000-b206-40ff-9872-83e8017abed1"

            # Create safe filename
            safe_name = re.sub(r"[^\w\-.]", "_", blender_object.name)
            object_path = f"/3D/Objects/{safe_name}_{object_counter}.model"

            # Get transformation
            transformation = blender_object.matrix_world.copy()
            transformation = mathutils.Matrix.Scale(global_scale, 4) @ transformation

            # Write the individual object model file
            self.write_object_model(
                archive, blender_object, object_path, mesh_id, mesh_uuid,
                idx, total_mesh_objects,
            )

            # Track which group this mesh belongs to (if any)
            parent_group = mesh_to_group.get(blender_object.name)
            group_name = str(parent_group.name) if parent_group else None

            object_data.append(
                {
                    "wrapper_id": wrapper_id,
                    "mesh_id": mesh_id,
                    "object_path": object_path,
                    "wrapper_uuid": wrapper_uuid,
                    "mesh_uuid": mesh_uuid,
                    "component_uuid": component_uuid,
                    "transformation": transformation,
                    "name": blender_object.name,
                    "group_name": group_name,  # None if ungrouped
                }
            )

            ctx.num_written += 1

        # Build groups list - each Empty becomes a separate assembly
        groups: list[dict] = []
        next_wrapper_id = len(object_data) * 2 + 1

        for group_empty in group_empties:
            group_name = str(group_empty.name)
            # Get the members of this group
            group_members = [od for od in object_data if od["group_name"] == group_name]

            if not group_members:
                continue

            groups.append({
                "wrapper_id": next_wrapper_id,
                "uuid": str(uuid.uuid4()),
                "name": group_name,
                "members": group_members,
                "empty": group_empty,
            })
            next_wrapper_id += 1

        # Ungrouped objects (meshes not parented to any selected Empty)
        ungrouped = [od for od in object_data if od["group_name"] is None]

        debug(f"Export structure: {len(groups)} groups, {len(ungrouped)} ungrouped objects")

        # Center all objects: XY midpoint → origin, Z floor → 0
        scale_mat = mathutils.Matrix.Scale(global_scale, 4)
        all_corners = [
            scale_mat @ obj.matrix_world @ mathutils.Vector(corner)
            for obj in mesh_objects
            for corner in obj.bound_box
        ]
        if all_corners:
            xs = [v.x for v in all_corners]
            ys = [v.y for v in all_corners]
            zs = [v.z for v in all_corners]
            cx = -(min(xs) + max(xs)) / 2
            cy = -(min(ys) + max(ys)) / 2
            cz = -min(zs)
            for od in object_data:
                od["transformation"][0][3] += cx
                od["transformation"][1][3] += cy
                od["transformation"][2][3] += cz
            debug(f"Applied centering offset ({cx:.2f}, {cy:.2f}, {cz:.2f}) mm")

        # Apply bed center offset to transformations (built-in template only)
        bed_offset_x, bed_offset_y = self._get_bed_center_offset()

        if groups:
            # Multi-group mode: bed offset applied to each group's build item
            for grp in groups:
                grp["bed_offset"] = (bed_offset_x, bed_offset_y)

        # Ungrouped objects get offset applied directly
        if (bed_offset_x != 0.0 or bed_offset_y != 0.0) and ungrouped:
            for od in ungrouped:
                od["transformation"][0][3] += bed_offset_x
                od["transformation"][1][3] += bed_offset_y
            debug(
                f"Applied bed center offset ({bed_offset_x}, {bed_offset_y}) mm "
                f"to {len(ungrouped)} ungrouped objects"
            )

        # Write main 3dmodel.model with wrapper objects and build items
        ctx._progress_update(90, "Writing main model...")
        self.write_main_model(archive, object_data, build_uuid, groups, ungrouped)

        # Write 3D/_rels/3dmodel.model.rels
        ctx._progress_update(93, "Writing relationships...")
        self.write_model_relationships(archive, object_data)

        # Write Orca metadata files
        ctx._progress_update(96, "Writing configuration...")
        self.write_orca_metadata(archive, mesh_objects, object_data, groups, ungrouped)

        # Write thumbnail
        ctx._progress_update(99, "Writing thumbnail...")
        write_thumbnail(archive, ctx, list(blender_objects))

        ctx._progress_update(100, "Finalizing export...")
        return ctx.finalize_export(archive, "Orca-compatible ")

    def write_object_model(
        self,
        archive: zipfile.ZipFile,
        blender_object: bpy.types.Object,
        object_path: str,
        mesh_id: int,
        mesh_uuid: str,
        obj_index: int = 0,
        total_objects: int = 1,
    ) -> None:
        """Write an individual object model file for Orca Slicer."""
        ctx = self.ctx

        root = xml.etree.ElementTree.Element(
            "model",
            attrib={
                "unit": "millimeter",
                "xml:lang": "en-US",
                "xmlns": MODEL_NAMESPACE,
                "xmlns:BambuStudio": BAMBU_NAMESPACE,
                "xmlns:p": PRODUCTION_NAMESPACE,
                "requiredextensions": "p",
            },
        )

        # Add BambuStudio version metadata
        metadata = xml.etree.ElementTree.SubElement(
            root, "metadata", attrib={"name": "BambuStudio:3mfVersion"}
        )
        metadata.text = "1"

        # Resources
        resources = xml.etree.ElementTree.SubElement(root, "resources")

        # Get mesh data
        if ctx.options.use_mesh_modifiers:
            dependency_graph = bpy.context.evaluated_depsgraph_get()
            eval_object = blender_object.evaluated_get(dependency_graph)
        else:
            eval_object = blender_object

        try:
            mesh = eval_object.to_mesh()
        except RuntimeError:
            warn(f"Could not get mesh for object: {blender_object.name}")
            return

        if mesh is None:
            return

        mesh.calc_loop_triangles()

        if len(mesh.loop_triangles) == 0:
            warn(f"Skipping '{blender_object.name}': mesh has no triangles")
            eval_object.to_mesh_clear()
            return

        # Adaptive pre-subdivision for PAINT mode: split large faces so each
        # triangle can be encoded at full segmentation depth.
        # Skip for non-normal parts (modifiers, support enforcers/blockers, etc.)
        # Also skip for FullSpectrum parts mode (no per-triangle encoding needed).
        is_normal_part = blender_object.get("3mf_part_subtype", "normal_part") == "normal_part"
        _fs_mode = bool(
            hasattr(ctx, "mixed_filament_definitions_raw") and ctx.mixed_filament_definitions_raw
        )
        if is_normal_part and ctx.options.use_orca_format == "PAINT" and mesh.uv_layers.active and not _fs_mode:
            original_object = blender_object
            if hasattr(blender_object, "original"):
                original_object = blender_object.original
            paint_img = self._find_paint_texture(original_object)
            if paint_img:
                from .segmentation import subdivide_mesh_for_segmentation
                subdivide_mesh_for_segmentation(
                    mesh,
                    ctx.options.subdivision_depth,
                    paint_img.size[0],
                    paint_img.size[1],
                )

        # Create object element
        obj_elem = xml.etree.ElementTree.SubElement(
            resources,
            "object",
            attrib={
                "id": str(mesh_id),
                "p:UUID": mesh_uuid,
                "type": "model",
            },
        )

        # Mesh element
        mesh_elem = xml.etree.ElementTree.SubElement(obj_elem, "mesh")

        # Vertices — bulk extract via foreach_get + numpy char formatting.
        # No SubElement nodes needed; the streaming writer injects the raw XML string.
        decimals = ctx.options.coordinate_precision
        _t_vert0 = time.perf_counter()
        n_verts = len(mesh.vertices)
        co_flat = np.empty(n_verts * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", co_flat)
        co = co_flat.reshape(n_verts, 3)
        _t_vert1 = time.perf_counter()
        timing_debug(f"orca write_vertices foreach_get ({n_verts} verts)", (_t_vert1 - _t_vert0) * 1000)
        fmt = f"%.{decimals}g"
        xs = np.char.mod(fmt, co[:, 0]).tolist()
        ys = np.char.mod(fmt, co[:, 1]).tolist()
        zs = np.char.mod(fmt, co[:, 2]).tolist()
        _t_vert2 = time.perf_counter()
        timing_debug(f"orca write_vertices str format ({n_verts} verts)", (_t_vert2 - _t_vert1) * 1000)
        vparts = ["<vertices>"]
        vappend = vparts.append
        for i in range(n_verts):
            vappend(f'<vertex x="{xs[i]}" y="{ys[i]}" z="{zs[i]}"/>')
        vparts.append("</vertices>")
        _raw_geometry_cache.setdefault(id(mesh_elem), {})["vertices"] = "".join(vparts)
        _t_vert3 = time.perf_counter()
        timing_debug(f"orca write_vertices build raw XML ({n_verts} verts)", (_t_vert3 - _t_vert2) * 1000)

        # Generate segmentation strings from UV texture if in PAINT mode.
        # Non-normal parts (modifiers, support, negative) get no paint data.
        # FullSpectrum "parts mode" files use extruder=N in model_settings.config
        # instead of per-triangle paint_color; skip the segmentation encoder entirely
        # for those objects so no paint_color attributes are written.
        segmentation_strings = {}
        seam_strings = {}
        support_strings = {}
        is_fullspectrum_parts_mode = bool(
            hasattr(ctx, "mixed_filament_definitions_raw") and ctx.mixed_filament_definitions_raw
        )
        if (
            is_normal_part
            and ctx.options.use_orca_format == "PAINT"
            and mesh.uv_layers.active
            and not is_fullspectrum_parts_mode
        ):
            # Read from original object's data, not the temporary evaluated mesh
            original_object = blender_object
            if hasattr(blender_object, "original"):
                original_object = blender_object.original
            original_mesh_data = original_object.data

            if (
                "3mf_is_paint_texture" in original_mesh_data
                and original_mesh_data["3mf_is_paint_texture"]
            ):
                paint_texture = None
                extruder_colors = {}
                default_extruder = original_mesh_data.get(
                    "3mf_paint_default_extruder", 0
                )

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

                if paint_texture and extruder_colors:
                    debug(
                        f"  Exporting paint texture '{paint_texture.name}' as segmentation"
                    )

                    # Create progress callback for Orca segmentation
                    def orca_seg_progress(current, total_val, message):
                        if total_val > 0:
                            seg_pct = current / total_val
                            # Each object gets its share of the 65–89% range
                            # (Segmentation phase = phase 3, cumulative 65–90%).
                            obj_start = 65 + ((obj_index / total_objects) * 24)
                            obj_end = 65 + (((obj_index + 1) / total_objects) * 24)
                            overall = int(obj_start + (seg_pct * (obj_end - obj_start)))
                            ctx._progress_update(
                                overall, f"{blender_object.name}: {message}", phase=3
                            )

                    try:
                        segmentation_strings = texture_to_segmentation(
                            blender_object,
                            paint_texture,
                            extruder_colors,
                            default_extruder,
                            progress_callback=orca_seg_progress,
                            max_depth=ctx.options.subdivision_depth,
                            mesh=mesh,
                        )
                        debug(
                            f"  Generated {len(segmentation_strings)} segmentation strings"
                        )
                    except Exception as e:
                        debug(
                            f"  WARNING: Failed to generate segmentation from texture: {e}"
                        )
                        import traceback
                        traceback.print_exc()
                        segmentation_strings = {}

            # Seam / support paint (independent of color paint)
            seam_strings = self._extract_auxiliary_segmentation(
                original_object, blender_object, mesh, "SEAM",
                subdivided_mesh=mesh,
            )
            support_strings = self._extract_auxiliary_segmentation(
                original_object, blender_object, mesh, "SUPPORT",
                subdivided_mesh=mesh,
            )

        # Triangles with paint_color — bulk extract + pre-computed slot fragments.
        # foreach_get avoids the per-triangle Blender wrapper overhead.
        _t_tri0 = time.perf_counter()
        n_tris = len(mesh.loop_triangles)
        verts_flat = np.empty(n_tris * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", verts_flat)
        mat_flat = np.empty(n_tris, dtype=np.int32)
        mesh.loop_triangles.foreach_get("material_index", mat_flat)
        verts_list = verts_flat.reshape(n_tris, 3).tolist()
        mat_list = mat_flat.tolist()
        _t_tri1 = time.perf_counter()
        timing_debug(f"orca write_triangles foreach_get ({n_tris} tris)", (_t_tri1 - _t_tri0) * 1000)

        # Pre-compute per-slot paint code fragment — O(n_slots) instead of O(n_triangles).
        n_slots = len(eval_object.material_slots)
        slot_paint_frags = []
        if is_normal_part:
            for slot_idx in range(n_slots):
                frag = ""
                if slot_idx < len(mesh.materials):
                    mat = mesh.materials[slot_idx]
                    if mat is not None:
                        hex_color = material_to_hex_color(mat)
                        if hex_color and hex_color in ctx.vertex_colors:
                            filament_index = ctx.vertex_colors[hex_color]
                            if filament_index < len(ORCA_FILAMENT_CODES):
                                paint_code = ORCA_FILAMENT_CODES[filament_index]
                                if paint_code:
                                    frag = f' paint_color="{paint_code}"'
                slot_paint_frags.append(frag)
        else:
            slot_paint_frags = [""] * n_slots

        has_segmentation = bool(segmentation_strings)
        has_seam = bool(seam_strings)
        has_support = bool(support_strings)
        tparts = ["<triangles>"]
        tappend = tparts.append

        for tri_idx in range(n_tris):
            v = verts_list[tri_idx]
            base_tri = f'<triangle v1="{v[0]}" v2="{v[1]}" v3="{v[2]}"'

            # Check for segmentation string first (PAINT mode with UV texture)
            if has_segmentation:
                seg_str = segmentation_strings.get(tri_idx)
                if seg_str:
                    seam_part = ""
                    sup_part = ""
                    if has_seam:
                        seam = seam_strings.get(tri_idx)
                        if seam:
                            seam_part = f' paint_seam="{seam}"'
                    if has_support:
                        sup = support_strings.get(tri_idx)
                        if sup:
                            sup_part = f' paint_supports="{sup}"'
                    tappend(f'{base_tri} paint_color="{seg_str}"{seam_part}{sup_part}/>')
                    continue

            # Fallback: pre-computed slot paint fragment
            paint_frag = ""
            if is_normal_part and n_slots > 0:
                slot_idx = mat_list[tri_idx]
                if slot_idx < n_slots:
                    paint_frag = slot_paint_frags[slot_idx]

            seam_part = ""
            sup_part = ""
            if has_seam:
                seam = seam_strings.get(tri_idx)
                if seam:
                    seam_part = f' paint_seam="{seam}"'
            if has_support:
                sup = support_strings.get(tri_idx)
                if sup:
                    sup_part = f' paint_supports="{sup}"'

            tappend(f'{base_tri}{paint_frag}{seam_part}{sup_part}/>')

        tparts.append("</triangles>")
        _raw_geometry_cache.setdefault(id(mesh_elem), {})["triangles"] = "".join(tparts)
        _t_tri2 = time.perf_counter()
        timing_debug(f"orca write_triangles loop ({n_tris} tris)", (_t_tri2 - _t_tri1) * 1000)
        timing_debug(f"orca write_object_model geometry TOTAL", (_t_tri2 - _t_vert0) * 1000)

        # Empty build (geometry is in this file, build is in main model)
        xml.etree.ElementTree.SubElement(root, "build")

        # Clean up mesh
        eval_object.to_mesh_clear()

        # Write to archive using the streaming writer — bypasses ElementTree's
        # Python-level DOM walk and injects the pre-built geometry strings directly.
        archive_path = object_path.lstrip("/")
        with archive.open(archive_path, "w") as f:
            _stream_model_to_file(f, root)

        debug(f"Wrote object model: {archive_path}")

    def write_main_model(
        self,
        archive: zipfile.ZipFile,
        object_data: List[dict],
        build_uuid: str,
        groups: List[dict],
        ungrouped: List[dict],
    ) -> None:
        """Write the main 3dmodel.model file with wrapper objects.

        Supports multiple groups (each becomes a separate assembly/plate item)
        plus ungrouped objects as individual items.

        :param groups: List of group dicts with wrapper_id, uuid, name, members, bed_offset
        :param ungrouped: List of object_data dicts for objects not in any group
        """
        root = xml.etree.ElementTree.Element(
            "model",
            attrib={
                "unit": "millimeter",
                "xml:lang": "en-US",
                "xmlns": MODEL_NAMESPACE,
                "xmlns:BambuStudio": BAMBU_NAMESPACE,
                "xmlns:p": PRODUCTION_NAMESPACE,
                "requiredextensions": "p",
            },
        )

        # Metadata
        # Use BambuStudio application name so Bambu Studio recognizes the file
        # as a full project (not just geometry). Orca Slicer does the same.
        # All rights reserved to Bambu Lab for the application name
        meta_app = xml.etree.ElementTree.SubElement(
            root, "metadata", attrib={"name": "Application"}
        )
        meta_app.text = "BambuStudio-2.3.0"

        meta_version = xml.etree.ElementTree.SubElement(
            root, "metadata", attrib={"name": "BambuStudio:3mfVersion"}
        )
        meta_version.text = "1"

        # Standard metadata
        for name in [
            "Copyright",
            "Description",
            "Designer",
            "DesignerCover",
            "DesignerUserId",
            "License",
            "Origin",
        ]:
            meta = xml.etree.ElementTree.SubElement(
                root, "metadata", attrib={"name": name}
            )
            meta.text = ""

        # Creation/modification dates
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        meta_created = xml.etree.ElementTree.SubElement(
            root, "metadata", attrib={"name": "CreationDate"}
        )
        meta_created.text = today
        meta_modified = xml.etree.ElementTree.SubElement(
            root, "metadata", attrib={"name": "ModificationDate"}
        )
        meta_modified.text = today

        # Title from first object or scene
        title = object_data[0]["name"] if object_data else "Blender Export"
        meta_title = xml.etree.ElementTree.SubElement(
            root, "metadata", attrib={"name": "Title"}
        )
        meta_title.text = title

        # Resources - wrapper objects with component references
        resources = xml.etree.ElementTree.SubElement(root, "resources")

        # Write grouped assemblies - each group gets a wrapper object
        for grp in groups:
            wrapper = xml.etree.ElementTree.SubElement(
                resources,
                "object",
                attrib={
                    "id": str(grp["wrapper_id"]),
                    "p:UUID": grp["uuid"],
                    "type": "model",
                },
            )
            components = xml.etree.ElementTree.SubElement(wrapper, "components")
            for member in grp["members"]:
                comp_transform = format_transformation(member["transformation"])
                xml.etree.ElementTree.SubElement(
                    components,
                    "component",
                    attrib={
                        "p:path": member["object_path"],
                        "objectid": str(member["mesh_id"]),
                        "p:UUID": member["component_uuid"],
                        "transform": comp_transform,
                    },
                )

        # Write ungrouped objects - each gets its own wrapper
        for obj in ungrouped:
            wrapper = xml.etree.ElementTree.SubElement(
                resources,
                "object",
                attrib={
                    "id": str(obj["wrapper_id"]),
                    "p:UUID": obj["wrapper_uuid"],
                    "type": "model",
                },
            )

            components = xml.etree.ElementTree.SubElement(wrapper, "components")
            xml.etree.ElementTree.SubElement(
                components,
                "component",
                attrib={
                    "p:path": obj["object_path"],
                    "objectid": str(obj["mesh_id"]),
                    "p:UUID": obj["component_uuid"],
                    "transform": "1 0 0 0 1 0 0 0 1 0 0 0",
                },
            )

        # Build element
        build = xml.etree.ElementTree.SubElement(
            root, "build", attrib={"p:UUID": build_uuid}
        )

        item_idx = 0

        # Build items for groups
        for grp in groups:
            bed_x, bed_y = grp.get("bed_offset", (0.0, 0.0))
            group_transform = (
                f"1.000000000 0.000000000 0.000000000 "
                f"0.000000000 1.000000000 0.000000000 "
                f"0.000000000 0.000000000 1.000000000 "
                f"{bed_x:.9f} {bed_y:.9f} 0.000000000"
            )
            item_uuid = f"0000000{item_idx + 2}-b1ec-4553-aec9-835e5b724bb4"
            xml.etree.ElementTree.SubElement(
                build,
                "item",
                attrib={
                    "objectid": str(grp["wrapper_id"]),
                    "p:UUID": item_uuid,
                    "transform": group_transform,
                    "printable": "1",
                },
            )
            item_idx += 1

        # Build items for ungrouped objects
        for obj in ungrouped:
            item_uuid = f"0000000{item_idx + 2}-b1ec-4553-aec9-835e5b724bb4"
            transform_str = format_transformation(obj["transformation"])

            xml.etree.ElementTree.SubElement(
                build,
                "item",
                attrib={
                    "objectid": str(obj["wrapper_id"]),
                    "p:UUID": item_uuid,
                    "transform": transform_str,
                    "printable": "1",
                },
            )
            item_idx += 1

        # Write to archive
        document = xml.etree.ElementTree.ElementTree(root)
        buffer = io.BytesIO()
        document.write(buffer, xml_declaration=True, encoding="UTF-8")
        xml_content = buffer.getvalue().decode("UTF-8")

        with archive.open(MODEL_LOCATION, "w") as f:
            f.write(xml_content.encode("UTF-8"))

        debug(f"Wrote main model: {MODEL_LOCATION}")

    def write_model_relationships(
        self, archive: zipfile.ZipFile, object_data: List[dict]
    ) -> None:
        """Write the 3D/_rels/3dmodel.model.rels file."""
        root = xml.etree.ElementTree.Element(
            "Relationships", attrib={"xmlns": RELS_NAMESPACE}
        )

        for idx, obj in enumerate(object_data):
            xml.etree.ElementTree.SubElement(
                root,
                "Relationship",
                attrib={
                    "Target": obj["object_path"],
                    "Id": f"rel-{idx + 1}",
                    "Type": MODEL_REL,
                },
            )

        document = xml.etree.ElementTree.ElementTree(root)
        buffer = io.BytesIO()
        document.write(buffer, xml_declaration=True, encoding="UTF-8")
        xml_content = buffer.getvalue().decode("UTF-8")

        with archive.open("3D/_rels/3dmodel.model.rels", "w") as f:
            f.write(xml_content.encode("UTF-8"))

        debug("Wrote 3D/_rels/3dmodel.model.rels")

    def write_orca_metadata(
        self,
        archive: zipfile.ZipFile,
        blender_objects: List[bpy.types.Object],
        object_data: List[dict],
        groups: List[dict],
        ungrouped: List[dict],
    ) -> None:
        """Write Orca Slicer compatible metadata files to the archive.

        :param archive: The ZIP archive to write into.
        :param blender_objects: The Blender objects being exported.
        :param object_data: Per-object export data dicts from ``execute()``.
        :param groups: List of group dicts for grouped assemblies.
        :param ungrouped: List of object_data for ungrouped objects.
        """
        ctx = self.ctx
        debug("Writing Orca metadata files...")

        try:
            # Write project_settings.config from template with updated colors
            project_settings = self.generate_project_settings()
            with archive.open("Metadata/project_settings.config", "w") as f:
                f.write(json.dumps(project_settings, indent=4).encode("utf-8"))
            debug("Wrote project_settings.config")

            # Write model_settings.config with object metadata
            model_settings_xml = self.generate_model_settings(
                blender_objects, object_data, groups, ungrouped
            )
            with archive.open("Metadata/model_settings.config", "w") as f:
                f.write(model_settings_xml.encode("utf-8"))
            debug("Wrote model_settings.config")

            debug(f"Wrote Orca metadata with {len(ctx.vertex_colors)} color zones")
        except Exception as e:
            error(f"Failed to write Orca metadata: {e}")
            ctx.safe_report({"ERROR"}, f"Failed to write Orca metadata: {e}")
            raise

    # Built-in template bed center (Bambu Lab A1: 256x256 mm, origin bottom-left)
    _BED_CENTER_X = 128.0
    _BED_CENTER_Y = 128.0

    def _get_bed_center_offset(self) -> tuple:
        """Return bed center offset for the built-in project template.

        The built-in template (Bambu Lab A1) uses a bottom-left origin.
        Custom templates and stashed configs get no offset — the caller
        handles positioning.

        :return: ``(offset_x, offset_y)`` in millimeters.
        """
        if self.ctx.project_template_path:
            return (0.0, 0.0)
        if get_stashed_config("Metadata/project_settings.config") is not None:
            return (0.0, 0.0)
        if (
            self.ctx.options.slicer_profile != "NONE"
            and get_profile_config(
                self.ctx.options.slicer_profile,
                "Metadata/project_settings.config",
            ) is not None
        ):
            return (0.0, 0.0)
        return (self._BED_CENTER_X, self._BED_CENTER_Y)

    def _get_dominant_color(self, blender_object: bpy.types.Object) -> Optional[str]:
        """Return the hex colour string of the most-common face material on *blender_object*.

        Used to determine the per-object ``extruder`` value in
        ``model_settings.config`` so Orca assigns the right filament even
        when objects have only a single material (no ``paint_color``
        per-triangle overrides needed).

        Returns ``None`` when no material can be determined.
        """
        ctx = self.ctx

        if ctx.options.use_mesh_modifiers:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            eval_obj = blender_object.evaluated_get(depsgraph)
        else:
            eval_obj = blender_object

        try:
            mesh = eval_obj.to_mesh()
        except RuntimeError:
            return None
        if mesh is None:
            return None

        mesh.calc_loop_triangles()
        n_tris = len(mesh.loop_triangles)
        if n_tris == 0:
            eval_obj.to_mesh_clear()
            return None

        n_slots = len(eval_obj.material_slots)
        if n_slots == 0:
            eval_obj.to_mesh_clear()
            return None

        # Bulk-extract material slot indices in one C call, then use bincount to
        # find the most-used slot without a Python loop over all triangles.
        mat_flat = np.empty(n_tris, dtype=np.int32)
        mesh.loop_triangles.foreach_get("material_index", mat_flat)
        counts = np.bincount(mat_flat, minlength=n_slots)
        dominant_slot = int(np.argmax(counts))

        dominant_color = None
        if dominant_slot < len(mesh.materials):
            mat = mesh.materials[dominant_slot]
            if mat is not None:
                dominant_color = material_to_hex_color(mat)

        eval_obj.to_mesh_clear()

        return dominant_color

    def generate_project_settings(self) -> dict:
        """Generate project_settings.config by loading template and updating filament colors.

        Priority order for the base template:
        1. ``ctx.project_template_path`` — explicit custom template from API
        2. Stashed config from a previous import (preserved in Blender text blocks)
        3. Built-in ``orca_project_template.json``

        Regardless of source, ``filament_colour`` is updated and all filament
        arrays are resized to match the current export colors.
        """
        ctx = self.ctx

        # Determine which template to load
        addon_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        builtin_path = os.path.join(addon_dir, "orca_project_template.json")
        template_path = builtin_path

        if ctx.project_template_path:
            if os.path.isfile(ctx.project_template_path):
                template_path = ctx.project_template_path
                debug(f"Using custom project template: {template_path}")
            else:
                warn(
                    f"Custom project template not found: {ctx.project_template_path}. "
                    f"Falling back to built-in template."
                )

        # Priority 2: stashed config from a previous import.
        stashed_config = None
        if template_path == builtin_path:
            stashed_raw = get_stashed_config("Metadata/project_settings.config")
            if stashed_raw is not None:
                try:
                    stashed_config = json.loads(stashed_raw.decode("utf-8"))
                    debug("Using stashed project_settings.config from previous import")
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    warn(f"Stashed project_settings.config is invalid: {e}. Using built-in template.")

        # Priority 3: user-selected slicer profile from addon settings.
        if stashed_config is None and template_path == builtin_path:
            profile_name = ctx.options.slicer_profile
            if profile_name != "NONE":
                profile_raw = get_profile_config(
                    profile_name,
                    "Metadata/project_settings.config",
                )
                if profile_raw is not None:
                    try:
                        stashed_config = json.loads(
                            profile_raw.decode("utf-8"),
                        )
                        debug(
                            f"Using slicer profile '{profile_name}' "
                            f"for project settings"
                        )
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        warn(
                            f"Profile config is invalid: {e}. "
                            f"Using built-in template."
                        )

        try:
            if stashed_config is not None:
                settings = stashed_config
            else:
                with open(template_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            if template_path != builtin_path:
                warn(
                    f"Invalid JSON in custom template: {e}. "
                    f"Falling back to built-in template."
                )
                with open(builtin_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
            else:
                raise

        sorted_colors = sorted(ctx.vertex_colors.items(), key=lambda x: x[1])
        color_list = [color_hex for color_hex, _ in sorted_colors]

        if not color_list:
            color_list = ["#FFFFFF"]

        # For FullSpectrum files, the stashed filament_colour is the canonical list of
        # physical filament colors.  Re-exporting a FullSpectrum file must preserve all
        # physical filaments even when some are not applied to any visible face — the
        # mixed_filament_definitions reference slots by 1-based index, so losing a slot
        # would corrupt the virtual filament recipe.
        if stashed_config and "filament_colour" in stashed_config and ctx.mixed_filament_definitions_raw:
            stashed_physical = [c.upper() for c in stashed_config["filament_colour"]]
            stashed_set = set(stashed_physical)
            # Preserve original order; append any new colours that appeared in the mesh.
            for c in color_list:
                if c.upper() not in stashed_set:
                    stashed_physical.append(c.upper())
                    stashed_set.add(c.upper())
            color_list = stashed_physical

        # Append virtual (mixed) filament display colors after the physical ones.
        # This extends the filament_colour array so the slicer can display blended
        # swatches for each virtual filament slot.
        mixed_defs_to_write = ""
        if hasattr(ctx, "mixed_filament_definitions_raw") and ctx.mixed_filament_definitions_raw:
            mixed_defs_to_write = ctx.mixed_filament_definitions_raw
            from ..common.mixed_filaments import parse_mixed_filament_definitions, populate_display_colors
            entries = parse_mixed_filament_definitions(mixed_defs_to_write)
            # Compute display colors using the physical palette we just built.
            # (UI-synced entries already have display_color set; this fills in
            # any that are missing, e.g. stash-loaded entries.)
            missing = [mf for mf in entries if not mf.display_color]
            if missing:
                populate_display_colors(entries, color_list)
            # For FullSpectrum "parts mode" files the slicer computes virtual filament
            # display colours itself from mixed_filament_definitions at runtime.
            # Do NOT append them to filament_colour here — that would inflate the
            # physical-filament count and cause filament array length mismatches in
            # the slicer (it would try to create 44 extruder slots instead of 4).

        num_colors = len(color_list)
        settings["filament_colour"] = color_list

        # Resize all filament arrays to match the number of colors
        for key, value in list(settings.items()):
            if (
                isinstance(value, list)
                and key.startswith("filament_")
                and key != "filament_colour"
            ):
                if len(value) > 0:
                    if len(value) < num_colors:
                        settings[key] = value + [value[-1]] * (num_colors - len(value))
                    elif len(value) > num_colors:
                        settings[key] = value[:num_colors]

        # Also handle other arrays that need to match filament count
        array_keys_to_resize = [
            "activate_air_filtration",
            "activate_chamber_temp_control",
            "additional_cooling_fan_speed",
            "chamber_temperature",
            "close_fan_the_first_x_layers",
            "complete_print_exhaust_fan_speed",
            "cool_plate_temp",
            "cool_plate_temp_initial_layer",
            "default_filament_colour",
            "eng_plate_temp",
            "eng_plate_temp_initial_layer",
            "hot_plate_temp",
            "hot_plate_temp_initial_layer",
            "nozzle_temperature",
            "nozzle_temperature_initial_layer",
            "textured_plate_temp",
            "textured_plate_temp_initial_layer",
        ]

        for key in array_keys_to_resize:
            if key in settings and isinstance(settings[key], list):
                value = settings[key]
                if len(value) > 0:
                    if len(value) < num_colors:
                        settings[key] = value + [value[-1]] * (num_colors - len(value))
                    elif len(value) > num_colors:
                        settings[key] = value[:num_colors]

        # Write mixed filament definitions (FullSpectrum).  The stashed
        # project_settings already carries these, but if the user has re-exported
        # from scratch (no stash) we still write the definitions if we have them.
        if mixed_defs_to_write:
            settings["mixed_filament_definitions"] = mixed_defs_to_write

        return settings

    def generate_model_settings(
        self,
        blender_objects: List[bpy.types.Object],
        object_data: List[dict],
        groups: List[dict],
        ungrouped: List[dict],
    ) -> str:
        """Generate the model_settings.config XML for Orca Slicer.

        Supports multiple groups (each becomes a separate assembly) plus
        ungrouped objects as individual items.

        :param blender_objects: The Blender objects being exported.
        :param object_data: Per-object export data dicts from ``execute()``.
        :param groups: List of group dicts for grouped assemblies.
        :param ungrouped: List of object_data for ungrouped objects.
        """
        ctx = self.ctx
        root = xml.etree.ElementTree.Element("config")

        # Build a lookup from object name to Blender object
        blender_obj_by_name: dict[str, bpy.types.Object] = {}
        for blender_object in blender_objects:
            if blender_object.type == "MESH":
                blender_obj_by_name[blender_object.name] = blender_object

        # ----- Grouped assemblies: each group is an <object> with multiple <part> -----
        for grp in groups:
            object_elem = xml.etree.ElementTree.SubElement(
                root, "object", id=str(grp["wrapper_id"])
            )
            xml.etree.ElementTree.SubElement(
                object_elem, "metadata", key="name", value=grp["name"]
            )

            # Wrapper-level slicer setting overrides (round-trip passthrough)
            group_empty = grp.get("empty")
            if group_empty is not None:
                _write_stashed_settings(object_elem, group_empty)

            # Determine dominant extruder for entire group by aggregating part colors
            group_dominant_extruder = "1"
            group_color_counts: dict[str, int] = {}

            for member in grp["members"]:
                obj_name = member["name"]
                blender_object = blender_obj_by_name.get(obj_name)
                if blender_object is None:
                    continue

                # Determine per-part extruder.
                # FullSpectrum parts mode: use the stored 1-based extruder index directly
                # so virtual filament slots (5-44) are preserved in the output.
                extruder_value = "1"
                mesh_data = blender_object.data if blender_object else None
                if mesh_data and mesh_data.get("3mf_paint_default_extruder"):
                    extruder_value = str(int(mesh_data["3mf_paint_default_extruder"]))
                elif ctx.vertex_colors:
                    dominant_color = self._get_dominant_color(blender_object)
                    if dominant_color:
                        # Track for group-level dominant
                        group_color_counts[dominant_color] = (
                            group_color_counts.get(dominant_color, 0) + 1
                        )
                        if dominant_color in ctx.vertex_colors:
                            extruder_value = str(ctx.vertex_colors[dominant_color])

                part_subtype = "normal_part"
                if blender_object is not None:
                    part_subtype = blender_object.get("3mf_part_subtype", "normal_part")

                part_elem = xml.etree.ElementTree.SubElement(
                    object_elem, "part", id=str(member["mesh_id"]), subtype=part_subtype
                )
                xml.etree.ElementTree.SubElement(
                    part_elem, "metadata", key="name", value=obj_name
                )
                xml.etree.ElementTree.SubElement(
                    part_elem, "metadata", key="matrix",
                    value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1",
                )
                xml.etree.ElementTree.SubElement(
                    part_elem, "metadata", key="extruder", value=extruder_value
                )

                # Per-part slicer setting overrides (round-trip passthrough)
                if blender_object is not None:
                    _write_stashed_settings(part_elem, blender_object)

            # Set group-level extruder to most common color among its parts
            if group_color_counts and ctx.vertex_colors:
                most_common_color = max(group_color_counts, key=group_color_counts.get)
                if most_common_color in ctx.vertex_colors:
                    group_dominant_extruder = str(ctx.vertex_colors[most_common_color])

            xml.etree.ElementTree.SubElement(
                object_elem, "metadata", key="extruder", value=group_dominant_extruder
            )

        # ----- Ungrouped objects: each is an <object> with a single <part> -----
        for od in ungrouped:
            obj_name = od["name"]
            blender_object = blender_obj_by_name.get(obj_name)

            wrapper_id = od["wrapper_id"]
            mesh_id = od["mesh_id"]

            # Determine the dominant extruder for this object.
            # FullSpectrum parts mode: use the stored 1-based extruder index directly
            # so virtual filament slots (5-44) are preserved in the output.
            extruder_value = "1"
            _mesh_data = blender_object.data if blender_object else None
            if _mesh_data and _mesh_data.get("3mf_paint_default_extruder"):
                extruder_value = str(int(_mesh_data["3mf_paint_default_extruder"]))
            elif blender_object and ctx.vertex_colors:
                dominant_color = self._get_dominant_color(blender_object)
                if dominant_color and dominant_color in ctx.vertex_colors:
                    extruder_value = str(ctx.vertex_colors[dominant_color])

            object_elem = xml.etree.ElementTree.SubElement(
                root, "object", id=str(wrapper_id)
            )
            xml.etree.ElementTree.SubElement(
                object_elem, "metadata", key="name", value=obj_name
            )
            xml.etree.ElementTree.SubElement(
                object_elem, "metadata", key="extruder", value=extruder_value
            )

            # Per-object setting overrides (passthrough, no validation).
            # Priority: explicit API object_settings > stashed from import.
            if obj_name in ctx.object_settings:
                for setting_key, setting_value in ctx.object_settings[obj_name].items():
                    xml.etree.ElementTree.SubElement(
                        object_elem,
                        "metadata",
                        key=str(setting_key),
                        value=str(setting_value),
                    )
                debug(
                    f"Wrote {len(ctx.object_settings[obj_name])} per-object overrides "
                    f"for '{obj_name}'"
                )
            elif blender_object is not None:
                _write_stashed_settings(
                    object_elem, blender_object,
                    prop_name="3mf_orca_wrapper_settings",
                )

            part_subtype = "normal_part"
            if blender_object is not None:
                part_subtype = blender_object.get("3mf_part_subtype", "normal_part")

            part_elem = xml.etree.ElementTree.SubElement(
                object_elem, "part", id=str(mesh_id), subtype=part_subtype
            )
            xml.etree.ElementTree.SubElement(
                part_elem, "metadata", key="name", value=obj_name
            )
            matrix_value = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
            xml.etree.ElementTree.SubElement(
                part_elem, "metadata", key="matrix", value=matrix_value
            )

        # Add plate metadata with model_instance entries
        plate_elem = xml.etree.ElementTree.SubElement(root, "plate")
        xml.etree.ElementTree.SubElement(
            plate_elem, "metadata", key="plater_id", value="1"
        )
        xml.etree.ElementTree.SubElement(
            plate_elem, "metadata", key="plater_name", value=""
        )
        xml.etree.ElementTree.SubElement(
            plate_elem, "metadata", key="locked", value="false"
        )
        xml.etree.ElementTree.SubElement(
            plate_elem, "metadata", key="filament_map_mode", value="Auto For Flush"
        )

        # Model instances for groups
        for grp in groups:
            instance_elem = xml.etree.ElementTree.SubElement(
                plate_elem, "model_instance"
            )
            xml.etree.ElementTree.SubElement(
                instance_elem, "metadata", key="object_id",
                value=str(grp["wrapper_id"]),
            )
            xml.etree.ElementTree.SubElement(
                instance_elem, "metadata", key="instance_id", value="0"
            )
            xml.etree.ElementTree.SubElement(
                instance_elem, "metadata", key="identify_id",
                value=str(grp["wrapper_id"]),
            )

        # Model instances for ungrouped objects
        for od in ungrouped:
            instance_elem = xml.etree.ElementTree.SubElement(
                plate_elem, "model_instance"
            )
            xml.etree.ElementTree.SubElement(
                instance_elem,
                "metadata",
                key="object_id",
                value=str(od["wrapper_id"]),
            )
            xml.etree.ElementTree.SubElement(
                instance_elem, "metadata", key="instance_id", value="0"
            )
            xml.etree.ElementTree.SubElement(
                instance_elem,
                "metadata",
                key="identify_id",
                value=str(od["wrapper_id"]),
            )

        # Add assemble section with real world transforms
        assemble_elem = xml.etree.ElementTree.SubElement(root, "assemble")

        # Assemble items for groups
        for grp in groups:
            bed_x, bed_y = grp.get("bed_offset", (0.0, 0.0))
            group_transform = (
                f"1.000000000 0.000000000 0.000000000 "
                f"0.000000000 1.000000000 0.000000000 "
                f"0.000000000 0.000000000 1.000000000 "
                f"{bed_x:.9f} {bed_y:.9f} 0.000000000"
            )
            xml.etree.ElementTree.SubElement(
                assemble_elem,
                "assemble_item",
                object_id=str(grp["wrapper_id"]),
                instance_id="0",
                transform=group_transform,
                offset="0 0 0",
            )

        # Assemble items for ungrouped objects
        for od in ungrouped:
            transform_str = format_transformation(od["transformation"])
            xml.etree.ElementTree.SubElement(
                assemble_elem,
                "assemble_item",
                object_id=str(od["wrapper_id"]),
                instance_id="0",
                transform=transform_str,
                offset="0 0 0",
            )

        tree = xml.etree.ElementTree.ElementTree(root)

        output = io.BytesIO()
        tree.write(output, encoding="utf-8", xml_declaration=True)
        return output.getvalue().decode("utf-8")
