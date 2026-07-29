# Blender add-on to import and export 3MF files.
# Copyright (C) 2025 Jack (modernization for Blender 4.2+)
# This add-on is free software; you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either version 2 of the License, or (at your option) any later
# version.
# This add-on is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with this program; if not, write to the Free
# Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""
MMU Paint sidebar panel — VIEW3D_PT_mmu_paint.

The main 3D Viewport panel for multi-filament texture painting, visible in
Texture Paint mode (``bl_context = "imagepaint"``).  Contains two UIList
subclasses and the panel's draw logic.

Also registers the depsgraph handler that auto-syncs the palette when the
active object changes.
"""

import bpy
import bpy.types

from .helpers import (
    _get_paint_mesh,
    _sync_filaments_from_mesh,
    _has_vertex_colors,
    draw_add_mix_form,
)


# ===================================================================
#  UILists
# ===================================================================


class MMU_UL_init_filaments(bpy.types.UIList):
    """Two-column initialization filament list: color picker + name."""

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            # Editable color swatch column
            swatch = row.row()
            swatch.ui_units_x = 1.5
            swatch.prop(item, "color", text="")
            # Wider name column
            row.label(text=item.name)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.prop(item, "color", text="")


class MMU_UL_filaments(bpy.types.UIList):
    """Two-column filament list: color swatch + name label."""

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            # Skinny color swatch column (read-only display)
            swatch = row.row()
            swatch.ui_units_x = 1.5
            swatch.enabled = False  # Make read-only
            swatch.prop(item, "color", text="")
            # Wider name column
            row.label(text=item.name)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.prop(item, "color", text="")

    def filter_items(self, context, data, propname):
        """Hide virtual (mix) filaments — they live in the Mix Colors sub-panel."""
        items = getattr(data, propname)
        settings = getattr(context.scene, "mmu_paint", None)
        num_physical = getattr(settings, "num_physical_filaments", 0) if settings else 0
        flt_flags = []
        for idx, item in enumerate(items):
            # Hide if explicitly marked virtual, OR if index is beyond the physical count
            is_virt = getattr(item, "is_virtual", False)
            beyond_physical = (num_physical > 0 and idx >= num_physical)
            if is_virt or beyond_physical:
                flt_flags.append(0)  # hidden
            else:
                flt_flags.append(self.bitflag_filter_item)
        return flt_flags, []


# ===================================================================
#  Panel
# ===================================================================


class VIEW3D_PT_mmu_paint(bpy.types.Panel):
    """MMU Paint Suite — multi-filament texture painting for 3MF export."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "3MF"
    bl_label = "MMU Paint"
    bl_context = "imagepaint"

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None and context.active_object.type == "MESH"
        )

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmu_paint
        mesh = _get_paint_mesh(context)

        if mesh is None:
            # ============================
            #  STATE A: Uninitialized
            # ============================
            box = layout.box()
            box.label(text="Setup MMU Painting", icon="BRUSH_DATA")

            # Initialize list if empty
            if len(settings.init_filaments) == 0:
                box.operator(
                    "mmu.reset_init_filaments",
                    text="Create Default Palette",
                    icon="ADD",
                )
            else:
                # Show filament list
                row = box.row()
                row.template_list(
                    "MMU_UL_init_filaments",
                    "",
                    settings,
                    "init_filaments",
                    settings,
                    "active_init_filament_index",
                    rows=3,
                    maxrows=8,
                )

                # Add/Remove buttons
                col = row.column(align=True)
                col.operator("mmu.add_init_filament", icon="ADD", text="")
                col.operator("mmu.remove_init_filament", icon="REMOVE", text="")

                # Reset and Initialize buttons
                row = box.row(align=True)
                row.operator("mmu.reset_init_filaments", icon="FILE_REFRESH")

                # UV method selection (before Initialize button)
                uv_col = box.column(align=True)
                uv_col.prop(settings, "uv_method")
                if settings.uv_method == "LIGHTMAP":
                    uv_col.prop(settings, "lightmap_divisions")
                elif settings.uv_method == "EXISTING":
                    uv_col.prop(settings, "existing_uv_layer")

                box.operator("mmu.initialize_painting", icon="PLAY", text="Initialize")

                # Bake to MMU — for procedural/complex materials
                obj = context.active_object
                has_mats = obj and obj.data.materials and obj.data.materials[0]
                has_vcol = obj and _has_vertex_colors(obj)
                if has_mats or has_vcol:
                    layout.separator()
                    bake_box = layout.box()
                    bake_box.label(text="From Existing Material", icon="RENDER_STILL")

                    # Skip dissolve checkbox
                    bake_box.prop(settings, "skip_dissolve")

                    if has_mats:
                        bake_row = bake_box.row()
                        bake_row.scale_y = 1.2
                        bake_row.operator("mmu.bake_to_mmu", icon="RENDER_STILL")
                    detect_row = bake_box.row()
                    detect_row.operator(
                        "mmu.detect_material_colors", icon="MATERIAL",
                    )
                    info = bake_box.column(align=True)
                    info.scale_y = 0.7
                    if settings.skip_dissolve:
                        info.label(text="Bake to filament colors,")
                        info.label(text="preserving mesh topology")
                    else:
                        info.label(text="Bake a procedural material to")
                        info.label(text="discrete filament colors for export")

        else:
            # ============================
            #  STATE B: Active palette
            # ============================

            active_layer = settings.active_paint_layer

            # --- Paint layer selector ---
            layer_box = layout.box()
            layer_box.label(text="Paint Layer", icon="OUTLINER_DATA_GP_LAYER")
            layer_row = layer_box.row(align=True)

            # Color layer button
            op = layer_row.operator(
                "mmu.switch_paint_layer", text="Color",
                icon="BRUSHES_ALL",
                depress=(active_layer == "COLOR"),
            )
            op.layer_type = "COLOR"

            # Seam layer button
            has_seam = mesh.get("3mf_has_seam_paint", False)
            if has_seam:
                op = layer_row.operator(
                    "mmu.switch_paint_layer", text="Seam",
                    icon="MOD_EDGESPLIT",
                    depress=(active_layer == "SEAM"),
                )
                op.layer_type = "SEAM"
            else:
                op = layer_row.operator(
                    "mmu.init_auxiliary_paint", text="Init Seam",
                    icon="MOD_EDGESPLIT",
                )
                op.layer_type = "SEAM"

            # Support layer button
            has_support = mesh.get("3mf_has_support_paint", False)
            if has_support:
                op = layer_row.operator(
                    "mmu.switch_paint_layer", text="Support",
                    icon="MOD_LATTICE",
                    depress=(active_layer == "SUPPORT"),
                )
                op.layer_type = "SUPPORT"
            else:
                op = layer_row.operator(
                    "mmu.init_auxiliary_paint", text="Init Support",
                    icon="MOD_LATTICE",
                )
                op.layer_type = "SUPPORT"

            if active_layer == "COLOR":
                # --- Filament list (color layer only) ---
                box = layout.box()
                box.label(text="Filament Palette", icon="COLOR")

                row = box.row()
                row.template_list(
                    "MMU_UL_filaments",
                    "",
                    settings,
                    "filaments",
                    settings,
                    "active_filament_index",
                    rows=3,
                    maxrows=6,
                )

                # Add/Remove buttons
                col = row.column(align=True)
                col.operator("mmu.add_filament", icon="ADD", text="")
                col.operator("mmu.remove_filament", icon="REMOVE", text="")

                # Reassign color button below list
                box.operator("mmu.reassign_filament_color", icon="COLORSET_01_VEC")

            else:
                # --- Seam / Support layer palette ---
                from .helpers import _layer_colors
                bg, enforce, block = _layer_colors(active_layer)
                label = active_layer.title()

                box = layout.box()
                box.label(text=f"{label} Paint", icon="BRUSH_DATA")

                # Enforce / Block color swatches
                row = box.row(align=True)
                enforce_btn = row.operator(
                    "mmu.switch_aux_brush", text="Enforce",
                    depress=True,
                )
                enforce_btn.layer_type = active_layer
                enforce_btn.mode = "ENFORCE"

                block_btn = row.operator(
                    "mmu.switch_aux_brush", text="Block",
                    depress=False,
                )
                block_btn.layer_type = active_layer
                block_btn.mode = "BLOCK"

                info = box.column(align=True)
                info.scale_y = 0.7
                info.label(text="Enforce: painted areas are forced")
                info.label(text="Block: painted areas are prevented")

            # --- Brush falloff warning ---
            brush = context.tool_settings.image_paint.brush
            if brush:
                is_constant = False
                try:
                    if hasattr(brush, "curve_distance_falloff_preset"):
                        is_constant = brush.curve_distance_falloff_preset == "CONSTANT"
                    else:
                        is_constant = brush.curve_preset == "CONSTANT"
                except AttributeError:
                    pass

                if not is_constant:
                    warn_box = layout.box()
                    warn_box.alert = True
                    warn_box.label(text="Soft edges will cause banding", icon="ERROR")
                    warn_box.label(text="issues on export")
                    warn_box.operator("mmu.fix_falloff", icon="CHECKMARK")

            # --- Quantize button ---
            layout.separator()
            quant_box = layout.box()
            quant_box.label(text="Cleanup", icon="BRUSH_DATA")
            quant_box.prop(settings, "quantize_method")
            if settings.quantize_method == "REGION":
                col = quant_box.column(align=True)
                col.prop(settings, "region_similarity")
                col.prop(settings, "min_region_size")
            quant_box.separator()
            quant_box.operator("mmu.quantize_texture", icon="SNAP_ON")
            info = quant_box.column(align=True)
            info.scale_y = 0.7
            info.label(text="Snap all pixels to the nearest")
            info.label(text="filament color to clean up edges")


# ===================================================================
#  Mixed filament UIList
# ===================================================================


class MMU_UL_mixed_filaments(bpy.types.UIList):
    """List of virtual mixed filament definitions (OrcaSlicer-FullSpectrum)."""

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)

            # Blended color swatch (read-only)
            swatch = row.row()
            swatch.ui_units_x = 1.5
            swatch.enabled = False
            swatch.prop(item, "display_color", text="")

            # Component labels + ratio — show pattern summary when present
            if item.manual_pattern:
                # Count unique filament IDs in the pattern for a compact label
                flat = item.manual_pattern.replace(",", "")
                ids_seen = []
                for ch in flat:
                    if ch.isdigit() and ch not in ids_seen:
                        ids_seen.append(ch)
                filament_str = "+".join(f"F{c}" for c in ids_seen)
                row.label(text=f"{filament_str}  (Pattern)")
            else:
                pct_a = 100 - item.mix_b_percent
                row.label(text=f"F{item.component_a} + F{item.component_b}  {pct_a}/{item.mix_b_percent}%")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.prop(item, "display_color", text="")

    def filter_items(self, context, data, propname):
        """Show only live (enabled, not deleted) mix entries."""
        items = getattr(data, propname)
        flt_flags = []
        for item in items:
            if not getattr(item, "enabled", True) or getattr(item, "deleted", False):
                flt_flags.append(0)
            else:
                flt_flags.append(self.bitflag_filter_item)
        return flt_flags, []


# ===================================================================
#  Mix Colors sub-panel
# ===================================================================


class VIEW3D_PT_mmu_mix_colors(bpy.types.Panel):
    """Mix Colors sub-panel — virtual mixed filament palette (FullSpectrum).

    Collapsed by default and only visible when:
    - A FullSpectrum file has been imported (``scene.mmu_paint.has_mixed_filaments``), OR
    - The user has enabled "Show Mixed Filaments" in addon Preferences.

    Lives inside ``VIEW3D_PT_mmu_paint`` and only appears in Texture Paint mode
    with an active paint mesh.
    """

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "3MF"
    bl_label = "Mix Colors"
    bl_parent_id = "VIEW3D_PT_mmu_paint"
    bl_options = {'DEFAULT_CLOSED'}
    bl_context = "imagepaint"

    @classmethod
    def poll(cls, context):
        # Must be in active paint state (mesh present)
        from .helpers import _get_paint_mesh
        if _get_paint_mesh(context) is None:
            return False
        # Visible when mixed filaments are present OR user has opted in via prefs
        addon_pkg = __package__.rsplit(".", 1)[0]  # "io_mesh_3mf"
        prefs_entry = context.preferences.addons.get(addon_pkg)
        if prefs_entry and getattr(prefs_entry.preferences, "show_mixed_filaments", False):
            return True
        settings = getattr(context.scene, "mmu_paint", None)
        return settings is not None and settings.has_mixed_filaments

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmu_paint

        # --- Mixed filament list (shown when any exist) ---
        if settings.has_mixed_filaments and settings.mixed_filaments:
            row = layout.row()
            row.template_list(
                "MMU_UL_mixed_filaments",
                "",
                settings,
                "mixed_filaments",
                settings,
                "active_mixed_filament_index",
                rows=3,
                maxrows=8,
            )

            col = row.column(align=True)
            col.prop(
                settings,
                "show_add_mix_section",
                icon="ADD",
                text="",
                toggle=True,
                emboss=True,
            )
            col.operator("mmu.remove_mixed_filament", icon="REMOVE", text="")
        else:
            row = layout.row()
            row.label(text="No mixed filaments defined.", icon="INFO")
            row.prop(
                settings,
                "show_add_mix_section",
                icon="ADD",
                text="",
                toggle=True,
                emboss=True,
            )

        # --- Inline add-mix form ---
        if settings.show_add_mix_section:
            box = layout.box()
            box.label(text="Add Color", icon="ADD")
            draw_add_mix_form(box, settings)

        # --- Active entry detail ---
        if settings.has_mixed_filaments and settings.mixed_filaments:
            idx = settings.active_mixed_filament_index
            if 0 <= idx < len(settings.mixed_filaments):
                mf = settings.mixed_filaments[idx]
                header, panel = layout.panel(f"mmu_mix_edit_{idx}", default_closed=False)
                header.label(text="Edit Color", icon="COLORSET_13_VEC")
                if panel:
                    col = panel.column(align=True)
                    col.prop(mf, "ui_type", text="Type")
                    col.separator()
                    ut = mf.ui_type
                    if ut == "gradient":
                        col.prop(mf, "component_a", text="Component A")
                        col.prop(mf, "component_b", text="Component B")
                        col.prop(mf, "mix_b_percent", slider=True)
                    elif ut == "pattern":
                        col.prop(mf, "component_a", text="Component A")
                        col.prop(mf, "component_b", text="Component B")
                        col.prop(mf, "manual_pattern")
                    elif ut == "layer_cycle":
                        col.prop(mf, "component_a", text="Component A")
                        col.prop(mf, "component_b", text="Component B")
                        col.label(text="Round-trip only — not editable in OrcaSlicer", icon="INFO")
                    elif ut == "pointillism":
                        col.prop(mf, "component_a", text="Component A")
                        col.prop(mf, "component_b", text="Component B")
                        col.prop(mf, "mix_b_percent", slider=True)
                        col.label(text="Round-trip only — not editable in OrcaSlicer", icon="INFO")
                    panel.operator("mmu.recompute_mix_color", icon="FILE_REFRESH", text="Update Color")


# ===================================================================
#  Object-switch handler
# ===================================================================

_last_active_object_name = ""


def _on_depsgraph_update(scene, depsgraph=None):
    """Re-sync the panel palette when the active object changes."""
    global _last_active_object_name

    try:
        ctx = bpy.context
        obj = ctx.active_object
        current_name = obj.name if obj else ""

        if current_name != _last_active_object_name:
            _last_active_object_name = current_name
            if obj and obj.type == "MESH":
                settings = scene.mmu_paint
                settings.loaded_mesh_name = ""  # Force resync
                _sync_filaments_from_mesh(ctx)
    except Exception:
        pass  # Silently ignore context errors during undo/redo/render
