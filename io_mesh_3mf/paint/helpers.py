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
Shared helper functions for the MMU Paint suite.

Provides utility functions used by operators, panels, and the bake module.
Extracted from the monolithic ``panel.py`` for DRY reuse across the paint
package.
"""

import ast

import bpy

from ..common.colors import hex_to_rgb as _rgb_from_hex
from ..common.colors import rgb_to_hex as _hex_from_rgb
from ..common.colors import srgb_to_linear as _srgb_to_linear


# ---------------------------------------------------------------------------
#  Default palette — visually distinct colors for up to 16 filaments
# ---------------------------------------------------------------------------

DEFAULT_PALETTE = [
    (0.800, 0.800, 0.800),  # 1: Light gray (typical default/base)
    (0.900, 0.200, 0.100),  # 2: Red
    (0.100, 0.600, 0.200),  # 3: Green
    (0.200, 0.400, 0.900),  # 4: Blue
    (0.950, 0.750, 0.100),  # 5: Yellow
    (0.900, 0.400, 0.900),  # 6: Magenta
    (0.100, 0.800, 0.800),  # 7: Cyan
    (0.950, 0.550, 0.100),  # 8: Orange
    (0.500, 0.250, 0.600),  # 9: Purple
    (0.400, 0.250, 0.150),  # 10: Brown
    (0.950, 0.450, 0.550),  # 11: Pink
    (0.350, 0.650, 0.450),  # 12: Teal
    (0.600, 0.050, 0.050),  # 13: Dark red
    (0.050, 0.350, 0.550),  # 14: Navy
    (0.450, 0.500, 0.100),  # 15: Olive
    (0.200, 0.200, 0.200),  # 16: Dark gray
]

# ---------------------------------------------------------------------------
#  Seam / Support paint layer constants
# ---------------------------------------------------------------------------

# Background color for seam/support textures (neutral gray — state 0 = auto)
LAYER_BACKGROUND = (0.500, 0.500, 0.500)

# Seam paint: enforce = cyan, block = dark red
SEAM_ENFORCE_COLOR = (0.100, 0.800, 1.000)
SEAM_BLOCK_COLOR = (0.800, 0.150, 0.150)

# Support paint: enforce = green, block = red
SUPPORT_ENFORCE_COLOR = (0.100, 0.900, 0.200)
SUPPORT_BLOCK_COLOR = (1.000, 0.200, 0.100)

# Layer property names on mesh.data
SEAM_UV_LAYER = "Seam_Paint"
SUPPORT_UV_LAYER = "Support_Paint"
COLOR_UV_LAYER = "MMU_Paint"

# Mesh custom property keys
SEAM_FLAG_KEY = "3mf_has_seam_paint"
SUPPORT_FLAG_KEY = "3mf_has_support_paint"
SEAM_COLORS_KEY = "3mf_seam_paint_colors"
SUPPORT_COLORS_KEY = "3mf_support_paint_colors"


def _layer_colors(layer_type):
    """Return ``(background, enforce, block)`` sRGB tuples for a paint layer type."""
    if layer_type == "SEAM":
        return LAYER_BACKGROUND, SEAM_ENFORCE_COLOR, SEAM_BLOCK_COLOR
    elif layer_type == "SUPPORT":
        return LAYER_BACKGROUND, SUPPORT_ENFORCE_COLOR, SUPPORT_BLOCK_COLOR
    return None


def _layer_uv_name(layer_type):
    """Return the UV layer name for the given paint layer type."""
    if layer_type == "SEAM":
        return SEAM_UV_LAYER
    elif layer_type == "SUPPORT":
        return SUPPORT_UV_LAYER
    return COLOR_UV_LAYER


def _layer_flag_key(layer_type):
    """Return the mesh custom property flag key for a layer type."""
    if layer_type == "SEAM":
        return SEAM_FLAG_KEY
    elif layer_type == "SUPPORT":
        return SUPPORT_FLAG_KEY
    return "3mf_is_paint_texture"


def _layer_colors_key(layer_type):
    """Return the mesh custom property colors key for a layer type."""
    if layer_type == "SEAM":
        return SEAM_COLORS_KEY
    elif layer_type == "SUPPORT":
        return SUPPORT_COLORS_KEY
    return "3mf_paint_extruder_colors"


def _get_layer_image(obj, layer_type):
    """Find the paint texture image for a specific layer on the object, or None."""
    if not obj or not obj.data:
        return None
    uv_name = _layer_uv_name(layer_type)
    suffix = f"_{uv_name}"
    # Search images by naming convention: {mesh_name}_{uv_layer_name}
    mesh_name = obj.data.name
    target_name = f"{mesh_name}{suffix}"
    img = bpy.data.images.get(target_name)
    if img:
        return img
    # Fallback: scan materials for TEX_IMAGE nodes whose image name contains the suffix
    if obj.data.materials:
        for mat in obj.data.materials:
            if mat and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        if suffix in node.image.name:
                            return node.image
    return None


# ===================================================================
#  Utility helpers
# ===================================================================


def _get_paint_image(obj):
    """Find the MMU paint texture image on the object's material, or None."""
    if not obj or not obj.data or not obj.data.materials:
        return None
    for mat in obj.data.materials:
        if mat and mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    return node.image
    return None


def _get_paint_mesh(context):
    """Return the active mesh if it has MMU paint data, else None."""
    obj = context.active_object
    if obj and obj.type == "MESH" and obj.data.get("3mf_is_paint_texture"):
        return obj.data
    return None


def _sync_filaments_from_mesh(context):
    """
    Load the filament palette from the active mesh's custom properties
    into the scene-level MMUPaintSettings collection.
    """
    settings = context.scene.mmu_paint
    mesh = _get_paint_mesh(context)

    if mesh is None:
        settings.filaments.clear()
        settings.loaded_mesh_name = ""
        return

    # Already in sync?  Also re-sync when the mixed filament count changes
    # (e.g. after a FullSpectrum import that runs after initial palette load).
    num_virtual = sum(
        1 for m in getattr(settings, "mixed_filaments", [])
        if m.enabled and not m.deleted
    ) if getattr(settings, "has_mixed_filaments", False) else 0
    physical_count_expected = len(settings.filaments) - num_virtual
    if (
        settings.loaded_mesh_name == mesh.name
        and len(settings.filaments) > 0
        and physical_count_expected >= 0
    ):
        return

    colors_str = mesh.get("3mf_paint_extruder_colors", "")
    if not colors_str:
        settings.filaments.clear()
        settings.loaded_mesh_name = ""
        return

    try:
        colors_dict = ast.literal_eval(colors_str)
    except (ValueError, SyntaxError):
        settings.filaments.clear()
        settings.loaded_mesh_name = ""
        return

    settings.filaments.clear()
    # If the mesh carries a stored physical-filament count, only load those
    # entries from the colour dict.  Virtual (mix) entries are re-appended
    # dynamically below from settings.mixed_filaments, which avoids duplicate
    # slots and ensures num_physical_filaments is set correctly.
    stored_num_physical = int(mesh.get("3mf_num_physical_filaments", 0))
    for idx in sorted(colors_dict.keys()):
        if stored_num_physical > 0 and int(idx) >= stored_num_physical:
            break  # Stop at the physical/virtual boundary
        item = settings.filaments.add()
        item.index = idx
        item.name = f"Filament {idx + 1}"
        hex_col = colors_dict[idx]
        rgb = _rgb_from_hex(hex_col)
        item.color = rgb

    # Append virtual (mixed) filament slots after the physical ones.
    # They are marked is_virtual=True so the main palette UIList can hide them;
    # the Mix Colors sub-panel exposes them for selection.
    num_physical = len(settings.filaments)
    if hasattr(settings, "num_physical_filaments"):
        settings.num_physical_filaments = num_physical
    if getattr(settings, "has_mixed_filaments", False):
        virt_idx = num_physical  # 0-based index continuing from physicals
        for mf_item in settings.mixed_filaments:
            mf_item.palette_index = -1  # reset; only set for active entries
            if not mf_item.enabled or mf_item.deleted:
                continue
            item = settings.filaments.add()
            item.index = virt_idx
            item.name = f"Mix {mf_item.component_a}+{mf_item.component_b}"
            item.color = tuple(mf_item.display_color)
            item.is_virtual = True
            mf_item.palette_index = virt_idx
            virt_idx += 1

    settings.loaded_mesh_name = mesh.name
    # Clamp active index
    if settings.active_filament_index >= len(settings.filaments):
        settings.active_filament_index = 0


def _write_colors_to_mesh(context):
    """Write the physical filament palette back to the mesh custom properties.

    Only physical (non-virtual) entries are written so round-tripped files
    do not accidentally re-import mixed-filament display colours as real slots.
    ``3mf_num_physical_filaments`` is also kept in sync so
    ``_sync_filaments_from_mesh`` can restore the physical/virtual boundary.
    """
    mesh = _get_paint_mesh(context)
    if mesh is None:
        return
    settings = context.scene.mmu_paint
    num_physical = (
        settings.num_physical_filaments
        if settings.num_physical_filaments > 0
        else sum(1 for f in settings.filaments if not f.is_virtual)
    )
    colors_dict = {}
    for i, item in enumerate(settings.filaments):
        if i >= num_physical:
            break
        colors_dict[item.index] = _hex_from_rgb(*item.color)
    mesh["3mf_paint_extruder_colors"] = str(colors_dict)
    mesh["3mf_num_physical_filaments"] = num_physical


def _refresh_virtual_slots_in_palette(settings) -> None:
    """Re-append virtual (mixed) filament slots into the live palette.

    Removes any existing virtual entries (those beyond the physical count as
    stored in the mesh property) and re-adds them from ``settings.mixed_filaments``.
    Called after ``_populate_mixed_filaments_on_scene`` so the palette is
    immediately correct without waiting for the depsgraph sync.

    Safe to call even when ``has_mixed_filaments`` is False — it becomes a no-op.
    """
    if not getattr(settings, "has_mixed_filaments", False):
        return

    # Use num_physical_filaments if available (set during sync).  Fall back to
    # counting by is_virtual flag, then the old heuristic for compatibility.
    if getattr(settings, "num_physical_filaments", 0) > 0:
        num_physical = settings.num_physical_filaments
    else:
        # Count non-virtual entries directly
        non_virtual = [f for f in settings.filaments if not getattr(f, "is_virtual", False)]
        num_physical = len(non_virtual)
        if num_physical == 0:
            return  # Palette not yet populated — sync will handle it

    # Trim back to physical count (remove any virtual entries already there)
    while len(settings.filaments) > num_physical:
        settings.filaments.remove(len(settings.filaments) - 1)

    num_physical = len(settings.filaments)
    if hasattr(settings, "num_physical_filaments"):
        settings.num_physical_filaments = num_physical
    virt_idx = num_physical
    for mf_item in settings.mixed_filaments:
        mf_item.palette_index = -1
        if not mf_item.enabled or mf_item.deleted:
            continue
        item = settings.filaments.add()
        item.index = virt_idx
        item.name = f"Mix {mf_item.component_a}+{mf_item.component_b}"
        item.color = tuple(mf_item.display_color)
        item.is_virtual = True
        mf_item.palette_index = virt_idx
        virt_idx += 1


def _configure_paint_brush(context):
    """
    Configure or create a texture paint brush for MMU painting.

    Blender 4.x: Create/get a custom '3MF Paint' brush and assign it.
    Blender 5.0+: Configure the currently active brush (read-only assignment).

    Returns the brush object or None.
    """
    ts = context.tool_settings

    if bpy.app.version >= (5, 0, 0):
        # Blender 5.0+: Configure active brush (read-only assignment)
        brush = ts.image_paint.brush if ts.image_paint else None
        if brush is None:
            return None
    else:
        # Blender 4.x: Create/get custom brush and assign it
        brush_name = "3MF Paint"
        brush = bpy.data.brushes.get(brush_name)
        if brush is None:
            brush = bpy.data.brushes.new(name=brush_name, mode="TEXTURE_PAINT")
        # Try to assign (writable in 4.x)
        try:
            ts.image_paint.brush = brush
        except AttributeError:
            pass  # Fall back to active brush if assignment fails

    # Configure brush settings (common to both versions)
    if brush:
        brush.blend = "MIX"
        brush.strength = 1.0
        try:
            if hasattr(brush, "curve_distance_falloff_preset"):
                brush.curve_distance_falloff_preset = "CONSTANT"
            else:
                brush.curve_preset = "CONSTANT"
        except (AttributeError, TypeError, ValueError, RuntimeError) as e:
            print(f"Failed to set brush falloff: {e}")

    return brush


def _set_brush_color(context, color_rgb):
    """Set the active texture paint brush color to the given (r, g, b) sRGB tuple.

    The palette stores colors as raw sRGB values (matching the hex colors in the
    3MF file).  Blender's brush.color expects **linear** values — it will convert
    linear → sRGB internally when writing to an sRGB-tagged image.  We therefore
    convert sRGB → linear here so that the painted pixels end up with the same
    raw sRGB values that the import renderer wrote via foreach_set.

    CRITICAL: Blender has a "Unified Color" system where the paint color can be
    stored either in the brush OR in the unified paint settings (shared across all
    brushes).  We set BOTH to ensure the color updates correctly.
    """
    # Ensure we have a proper 3-element tuple
    color_rgb = tuple(color_rgb[:3])

    # Convert sRGB → linear so Blender's paint system round-trips correctly.
    linear_rgb = (
        _srgb_to_linear(color_rgb[0]),
        _srgb_to_linear(color_rgb[1]),
        _srgb_to_linear(color_rgb[2]),
    )

    ts = context.tool_settings
    if not ts.image_paint:
        return

    brush = ts.image_paint.brush
    if not brush:
        return

    try:
        # 1. Set brush color (used when unified color is OFF)
        brush.color = linear_rgb

        # 2. Set unified paint settings color (used when unified color is ON)
        # This is the key - most users have "Unified Color" enabled by default
        # ts.image_paint is the Paint settings object with unified_paint_settings
        ups = ts.image_paint.unified_paint_settings
        if ups:
            # ALWAYS set the unified color - this is what actually controls the paint color
            # when "use_unified_color" is enabled (which is the default)
            ups.color = linear_rgb

        # 3. Force UI refresh to show the new color
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()

    except Exception:
        pass


def _has_vertex_colors(obj):
    """Return True if *obj* has a non-empty color attribute."""
    if not obj or not obj.data:
        return False
    if not hasattr(obj.data, "color_attributes"):
        return False
    ca = obj.data.color_attributes
    # active_color can be None even when color data exists, so
    # just check if there are any color attributes at all.
    return len(ca) > 0


# ===================================================================
#  Property update callbacks
# ===================================================================


def _on_active_filament_changed(self, context):
    """When user selects a different filament in the list, update brush color."""
    try:
        settings = context.scene.mmu_paint
        idx = settings.active_filament_index
        if 0 <= idx < len(settings.filaments):
            color = tuple(settings.filaments[idx].color[:])
            _set_brush_color(context, color)
    except Exception:
        pass  # Silently ignore context errors during undo/redo


def _on_active_mix_filament_changed(self, context):
    """When user clicks a mix filament row, auto-select it as the active brush color."""
    try:
        settings = context.scene.mmu_paint
        idx = settings.active_mixed_filament_index
        if 0 <= idx < len(settings.mixed_filaments):
            mf = settings.mixed_filaments[idx]
            if mf.enabled and not mf.deleted:
                color = tuple(mf.display_color[:])
                _set_brush_color(context, color)
                # Also set active_filament_index to the virtual palette slot so
                # export / bake tools see the right index.
                if mf.palette_index >= 0:
                    settings.active_filament_index = mf.palette_index
    except Exception:
        pass  # Silently ignore context errors during undo/redo


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------

def draw_add_mix_form(layout, settings):
    """Draw the inline add-mix form into *layout*.

    Used by both VIEW3D_PT_mmu_mix_colors (mmu_panel.py) and the bake panel
    (bake.py).  The box/panel wrapping is handled by the caller.
    """
    col = layout.column(align=True)
    col.prop(settings, "add_mix_mode", text="")
    col.separator()

    mode = settings.add_mix_mode
    if mode == 'COLOR':
        row = col.row(align=True)
        row.label(text="Target:", icon="EYEDROPPER")
        row.prop(settings, "mix_target_color", text="")
    elif mode == 'GRADIENT':
        col.prop(settings, "add_mix_component_a", text="Component A")
        col.prop(settings, "add_mix_component_b", text="Component B")
        col.prop(settings, "add_mix_mix_b_percent", text="Mix B %", slider=True)
    elif mode == 'PATTERN':
        col.prop(settings, "add_mix_component_a", text="Component A")
        col.prop(settings, "add_mix_component_b", text="Component B")
        col.prop(settings, "add_mix_manual_pattern", text="Pattern")

    col.separator()
    row = col.row(align=True)
    row.operator("mmu.add_mix_confirm", icon="ADD")
    row.operator("mmu.cancel_add_mix", icon="X", text="")
