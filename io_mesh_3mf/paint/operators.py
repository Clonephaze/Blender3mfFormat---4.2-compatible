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
Operators for the MMU Paint suite.

All ``MMU_OT_*`` operators that drive the painting workflow — initialization,
filament management, brush control, color reassignment, and the post-import
popup.
"""

import bmesh
import numpy as np
import bpy
import bpy.props
import bpy.types

from ..common.colors import rgb_to_hex as _hex_from_rgb
from ..common.logging import debug, warn

from .helpers import (
    DEFAULT_PALETTE,
    _get_paint_image,
    _get_paint_mesh,
    _sync_filaments_from_mesh,
    _write_colors_to_mesh,
    _configure_paint_brush,
    _set_brush_color,
    _has_vertex_colors,
    _refresh_virtual_slots_in_palette,
)
from .color_detection import (
    _collect_material_colors,
    _get_any_image_texture,
    _has_color_attribute_node,
    _extract_texture_colors,
    _extract_vertex_colors,
)


# ===================================================================
#  Initialization operators
# ===================================================================


class MMU_OT_initialize(bpy.types.Operator):
    """Initialize MMU painting on the active mesh object"""

    bl_idname = "mmu.initialize_painting"
    bl_label = "Initialize MMU Painting"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == "MESH"
            and not obj.data.get("3mf_is_paint_texture")
        )

    def execute(self, context):
        # Push a single undo step so Ctrl+Z restores the entire
        # pre-initialization state in one go (mode_set and UV ops
        # inside this method would otherwise fragment the undo stack).
        bpy.ops.ed.undo_push(message="Before MMU Initialize")

        obj = context.active_object
        mesh = obj.data
        settings = context.scene.mmu_paint

        # Use init_filaments for colors
        if len(settings.init_filaments) < 2:
            self.report({"ERROR"}, "At least 2 filaments required")
            return {"CANCELLED"}

        # --- Create dedicated MMU_Paint UV layer ---
        mmu_layer = mesh.uv_layers.get("MMU_Paint")
        if mmu_layer is None:
            mmu_layer = mesh.uv_layers.new(name="MMU_Paint")
        mesh.uv_layers.active = mmu_layer
        mmu_layer.active_render = True

        context.view_layer.objects.active = obj
        uv_method = settings.uv_method

        if uv_method == "EXISTING":
            # Copy the user's chosen UV layer into MMU_Paint, skipping
            # dissolve and UV projection entirely.
            layer_name = settings.existing_uv_layer.strip() or "MMU_Paint"
            src_layer = mesh.uv_layers.get(layer_name)
            if src_layer is None:
                warn(
                    f"Initialize: UV layer '{layer_name}' not found -- "
                    "falling back to Smart UV Project"
                )
                uv_method = "SMART"  # fall through below
            elif src_layer.name != "MMU_Paint":
                num_loops = len(mesh.loops)
                src_flat = np.zeros(num_loops * 2, dtype=np.float64)
                src_layer.data.foreach_get("uv", src_flat)
                mmu_layer.data.foreach_set("uv", src_flat)
                debug(f"Initialize: copied UV data from '{src_layer.name}' -> 'MMU_Paint'")

        if uv_method != "EXISTING":
            # Limited Dissolve merges coplanar triangles, giving each face
            # more UV space and reducing blurriness.  ~2 deg is conservative
            # enough to keep all intentional geometry detail.
            if not settings.skip_dissolve:
                bm = bmesh.new()
                bm.from_mesh(mesh)
                bmesh.ops.dissolve_limit(
                    bm, angle_limit=0.0349,
                    verts=bm.verts, edges=bm.edges,
                )
                bm.to_mesh(mesh)
                bm.free()
                mesh.update()

            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")

            if uv_method == "LIGHTMAP":
                bpy.ops.uv.lightmap_pack(
                    PREF_CONTEXT="ALL_FACES",
                    PREF_PACK_IN_ONE=True,
                    PREF_NEW_UVLAYER=False,
                    PREF_BOX_DIV=settings.lightmap_divisions,
                    PREF_MARGIN_DIV=0.05,
                )
            else:
                bpy.ops.uv.smart_project(
                    angle_limit=1.15192,
                    margin_method="SCALED",
                    rotate_method="AXIS_ALIGNED",
                    island_margin=0.002,
                    area_weight=0.6,
                    correct_aspect=True,
                    scale_to_bounds=False,
                )

            bpy.ops.object.mode_set(mode="OBJECT")

        # --- Texture size by triangle count ---
        tri_count = len(mesh.polygons)
        if tri_count < 5000:
            texture_size = 2048
        elif tri_count < 20000:
            texture_size = 4096
        else:
            texture_size = 8192

        # Get base color from first init filament
        base_color = tuple(settings.init_filaments[0].color[:])

        # --- Create image filled with base color ---
        image_name = f"{mesh.name}_MMU_Paint"
        image = bpy.data.images.new(
            image_name, width=texture_size, height=texture_size, alpha=True
        )
        # Fill entire image with base color
        fill = np.empty((texture_size, texture_size, 4), dtype=np.float32)
        fill[:, :, 0] = base_color[0]
        fill[:, :, 1] = base_color[1]
        fill[:, :, 2] = base_color[2]
        fill[:, :, 3] = 1.0
        image.pixels.foreach_set(fill.ravel())
        image.pack()

        # --- Material setup ---
        mat = bpy.data.materials.new(name=image_name)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()

        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.image = image
        tex_node.location = (-300, 0)

        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (100, 0)

        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (400, 0)

        links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

        # Clear existing materials, assign ours
        mesh.materials.clear()
        mesh.materials.append(mat)
        num_faces = len(mesh.polygons)
        if num_faces > 0:
            material_indices = [0] * num_faces
            mesh.polygons.foreach_set("material_index", material_indices)

        # --- Build palette from init_filaments ---
        colors_dict = {}
        for i, item in enumerate(settings.init_filaments):
            colors_dict[i] = _hex_from_rgb(*item.color[:])

        # --- Store custom properties ---
        mesh["3mf_is_paint_texture"] = True
        mesh["3mf_paint_default_extruder"] = 1  # 1-based
        mesh["3mf_paint_extruder_colors"] = str(colors_dict)

        # --- Populate panel filaments ---
        settings.loaded_mesh_name = ""  # Force reload
        _sync_filaments_from_mesh(context)

        # Set active node so texture paint knows which image to paint on
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE":
                    mat.node_tree.nodes.active = node
                    break

        # Switch to Texture Paint mode FIRST -- ts.image_paint / brush
        # are not reliably available until we're in paint mode.
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")

        # --- Setup brush and canvas (must be in TEXTURE_PAINT mode) ---
        _configure_paint_brush(context)

        ts = context.tool_settings
        if hasattr(ts.image_paint, "canvas"):
            ts.image_paint.canvas = image

        if len(settings.filaments) > 0:
            settings.active_filament_index = 0
            _set_brush_color(context, settings.filaments[0].color[:])

        count = len(settings.init_filaments)
        self.report(
            {"INFO"},
            f"Initialized MMU painting with {count} filaments at {texture_size}x{texture_size}",
        )
        return {"FINISHED"}


class MMU_OT_add_init_filament(bpy.types.Operator):
    """Add a filament to the initialization list"""

    bl_idname = "mmu.add_init_filament"
    bl_label = "Add Filament"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        settings = context.scene.mmu_paint

        if len(settings.init_filaments) >= 16:
            self.report({"ERROR"}, "Maximum 16 filaments supported")
            return {"CANCELLED"}

        idx = len(settings.init_filaments)
        item = settings.init_filaments.add()
        item.name = f"Filament {idx + 1}"

        # Pick color from palette
        if idx < len(DEFAULT_PALETTE):
            item.color = DEFAULT_PALETTE[idx]
        else:
            item.color = DEFAULT_PALETTE[idx % len(DEFAULT_PALETTE)]

        return {"FINISHED"}


class MMU_OT_remove_init_filament(bpy.types.Operator):
    """Remove the selected filament from the initialization list"""

    bl_idname = "mmu.remove_init_filament"
    bl_label = "Remove Filament"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        settings = context.scene.mmu_paint

        if len(settings.init_filaments) <= 2:
            self.report({"ERROR"}, "Minimum 2 filaments required")
            return {"CANCELLED"}

        idx = settings.active_init_filament_index
        if idx < 0 or idx >= len(settings.init_filaments):
            return {"CANCELLED"}

        settings.init_filaments.remove(idx)

        # Rename remaining filaments
        for i, item in enumerate(settings.init_filaments):
            item.name = f"Filament {i + 1}"

        # Clamp selection
        if settings.active_init_filament_index >= len(settings.init_filaments):
            settings.active_init_filament_index = len(settings.init_filaments) - 1

        return {"FINISHED"}


class MMU_OT_reset_init_filaments(bpy.types.Operator):
    """Reset initialization filaments to default 4-color palette"""

    bl_idname = "mmu.reset_init_filaments"
    bl_label = "Reset to Defaults"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        settings = context.scene.mmu_paint
        settings.init_filaments.clear()

        # Create default 4 filaments
        for i in range(4):
            item = settings.init_filaments.add()
            item.name = f"Filament {i + 1}"
            item.color = DEFAULT_PALETTE[i]

        settings.active_init_filament_index = 0
        return {"FINISHED"}


class MMU_OT_detect_material_colors(bpy.types.Operator):
    """Detect colors from the active object's material setup and populate the filament list"""

    bl_idname = "mmu.detect_material_colors"
    bl_label = "Detect from Materials"
    bl_description = (
        "Scan the active object's shader node trees for colors.\n"
        "Reads Color Ramp stops, Principled BSDF Base Color, RGB nodes,\n"
        "and viewport display colors, then populates the filament list.\n"
        "If an image texture or vertex colors are detected, prompts for\n"
        "the number of dominant colors to extract"
    )
    bl_options = {"INTERNAL"}

    num_colors: bpy.props.IntProperty(
        name="Number of Colors",
        description="How many dominant colors to extract from the texture",
        default=4,
        min=2,
        max=16,
    )

    # Internal: which source type was detected
    _source: str = "NODES"  # "NODES", "IMAGE", or "VERTEX"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            return False
        if obj.data.get("3mf_is_paint_texture"):
            return False
        # Allow if object has materials OR vertex colors
        has_materials = bool(obj.data.materials)
        has_vertex = (
            hasattr(obj.data, "color_attributes")
            and len(obj.data.color_attributes) > 0
        )
        return has_materials or has_vertex

    def invoke(self, context, event):
        obj = context.active_object

        # Check for image texture on active material
        image = _get_any_image_texture(obj)
        debug(f"[Detect] _get_any_image_texture -> {image}")
        if image is not None:
            self._source = "IMAGE"
            debug(f"[Detect] Source = IMAGE, image = '{image.name}' ({image.size[0]}x{image.size[1]})")
            return context.window_manager.invoke_props_dialog(
                self, title="Detect Colors from Image Texture",
            )

        # Check for vertex colors -- either via color attributes on the
        # mesh or a Color Attribute node feeding a Principled BSDF
        has_vc = _has_vertex_colors(obj)
        has_ca_node = _has_color_attribute_node(obj)
        debug(f"[Detect] _has_vertex_colors -> {has_vc}, _has_color_attribute_node -> {has_ca_node}")
        if has_vc or has_ca_node:
            self._source = "VERTEX"
            debug("[Detect] Source = VERTEX")
            return context.window_manager.invoke_props_dialog(
                self, title="Detect Colors from Vertex Colors",
            )

        # No texture sources -- run node detection immediately
        self._source = "NODES"
        debug("[Detect] Source = NODES (fallback to shader node detection)")
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        if self._source == "IMAGE":
            layout.label(text="Image texture detected on this object.")
        else:
            layout.label(text="Vertex color data detected on this object.")
        layout.label(text="How many dominant colors to extract?")
        layout.separator()
        layout.prop(self, "num_colors", slider=True)

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.mmu_paint
        debug(f"[Detect] execute() _source={self._source}, num_colors={self.num_colors}")

        # --- Texture-based detection ---
        if self._source == "IMAGE":
            image = _get_any_image_texture(obj)
            if image is None:
                self.report({"WARNING"}, "No image texture found")
                return {"CANCELLED"}
            colors = _extract_texture_colors(image, self.num_colors)
            source_label = f"image texture '{image.name}'"

        elif self._source == "VERTEX":
            colors = _extract_vertex_colors(obj, self.num_colors)
            source_label = "vertex colors"

        else:
            # Node-tree detection (original behavior)
            colors = _collect_material_colors(obj)
            source_label = "materials"

        debug(f"[Detect] Got {len(colors)} colors from {self._source}:")
        for i, c in enumerate(colors):
            debug(f"  [{i}] sRGB ({c[0]:.3f}, {c[1]:.3f}, {c[2]:.3f})  ~  {_hex_from_rgb(c[0], c[1], c[2])}")

        if not colors:
            self.report({"WARNING"}, f"No colors detected from {source_label}")
            return {"CANCELLED"}

        # Clamp to 16 filaments max
        if len(colors) > 16:
            colors = colors[:16]

        # Clear existing init filaments and populate with detected colors
        settings.init_filaments.clear()
        for i, rgb in enumerate(colors):
            item = settings.init_filaments.add()
            item.name = f"Filament {i + 1}"
            item.color = rgb

        settings.active_init_filament_index = 0
        self.report({"INFO"}, f"Detected {len(colors)} colors from {source_label}")

        # Force panel redraw so the color swatches update immediately
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
            elif area.type == "PROPERTIES":
                area.tag_redraw()

        return {"FINISHED"}


# ===================================================================
#  Runtime painting operators
# ===================================================================


class MMU_OT_select_filament(bpy.types.Operator):
    """Select a filament and set it as the active brush color"""

    bl_idname = "mmu.select_filament"
    bl_label = "Select Filament"
    bl_options = {"INTERNAL"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        settings = context.scene.mmu_paint
        if 0 <= self.index < len(settings.filaments):
            settings.active_filament_index = self.index
            _set_brush_color(context, settings.filaments[self.index].color[:])
        return {"FINISHED"}


class MMU_OT_add_filament(bpy.types.Operator):
    """Add a new filament to the palette"""

    bl_idname = "mmu.add_filament"
    bl_label = "Add Filament"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        mesh = _get_paint_mesh(context)
        if mesh is None:
            return False
        settings = context.scene.mmu_paint
        num_physical = (
            settings.num_physical_filaments
            if settings.num_physical_filaments > 0
            else sum(1 for f in settings.filaments if not f.is_virtual)
        )
        return num_physical < 16

    def execute(self, context):
        settings = context.scene.mmu_paint
        num_physical = (
            settings.num_physical_filaments
            if settings.num_physical_filaments > 0
            else sum(1 for f in settings.filaments if not f.is_virtual)
        )

        if num_physical >= 16:
            self.report({"ERROR"}, "Maximum 16 filaments supported")
            return {"CANCELLED"}

        # Trim any virtual slots off the end so the new physical item can be
        # appended cleanly at position num_physical.  _refresh_virtual_slots_in_palette
        # will re-append them correctly afterward.
        while len(settings.filaments) > num_physical:
            settings.filaments.remove(len(settings.filaments) - 1)

        new_index = num_physical
        new_color = DEFAULT_PALETTE[new_index % len(DEFAULT_PALETTE)]

        item = settings.filaments.add()
        item.index = new_index
        item.name = f"Filament {new_index + 1}"
        item.color = new_color
        item.is_virtual = False

        settings.num_physical_filaments = num_physical + 1
        _write_colors_to_mesh(context)
        _refresh_virtual_slots_in_palette(settings)

        self.report(
            {"WARNING"},
            f"Added filament {new_index + 1}. "
            f"Ensure your printer profile supports {num_physical + 1} filaments.",
        )
        return {"FINISHED"}


class MMU_OT_remove_filament(bpy.types.Operator):
    """Remove the selected filament from the palette"""

    bl_idname = "mmu.remove_filament"
    bl_label = "Remove Filament"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        mesh = _get_paint_mesh(context)
        if mesh is None:
            return False
        settings = context.scene.mmu_paint
        num_physical = (
            settings.num_physical_filaments
            if settings.num_physical_filaments > 0
            else sum(1 for f in settings.filaments if not f.is_virtual)
        )
        return num_physical > 2

    def execute(self, context):
        settings = context.scene.mmu_paint
        idx = settings.active_filament_index
        if idx < 0 or idx >= len(settings.filaments):
            return {"CANCELLED"}

        num_physical = (
            settings.num_physical_filaments
            if settings.num_physical_filaments > 0
            else sum(1 for f in settings.filaments if not f.is_virtual)
        )

        if num_physical <= 2:
            self.report({"ERROR"}, "Minimum 2 filaments required")
            return {"CANCELLED"}

        # Guard: only allow removing physical filaments from this operator.
        if idx >= num_physical:
            self.report({"WARNING"}, "Select a physical filament to remove")
            return {"CANCELLED"}

        removed = settings.filaments[idx]
        removed_color = tuple(removed.color[:])

        # Determine the new base color (what will be filament 0 after removal).
        # If removing filament 0, the new base is current filament 1.
        # Otherwise, the base stays filament 0.
        if idx == 0:
            new_base_color = tuple(settings.filaments[1].color[:])
        else:
            new_base_color = tuple(settings.filaments[0].color[:])

        # Replace all pixels of the removed color with the new base color
        obj = context.active_object
        image = _get_paint_image(obj)
        replaced_count = 0

        if image is not None:
            w, h = image.size
            pixels_flat = np.empty(w * h * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels_flat)
            pixels = pixels_flat.reshape(h, w, 4)

            old_arr = np.array(removed_color, dtype=np.float32)
            new_arr = np.array(new_base_color, dtype=np.float32)

            tolerance = 3.0 / 255.0
            mask = np.all(np.abs(pixels[:, :, :3] - old_arr) < tolerance, axis=2)
            replaced_count = int(np.count_nonzero(mask))

            if replaced_count > 0:
                pixels[mask, 0] = new_arr[0]
                pixels[mask, 1] = new_arr[1]
                pixels[mask, 2] = new_arr[2]
                image.pixels.foreach_set(pixels.ravel())
                image.update()

        settings.filaments.remove(idx)
        num_physical_new = num_physical - 1

        # Re-index physical items only; virtual slots will be rebuilt below.
        for i in range(min(num_physical_new, len(settings.filaments))):
            settings.filaments[i].index = i
            settings.filaments[i].name = f"Filament {i + 1}"

        # Clamp selection to physical range
        if settings.active_filament_index >= num_physical_new:
            settings.active_filament_index = max(0, num_physical_new - 1)

        settings.num_physical_filaments = num_physical_new
        _write_colors_to_mesh(context)
        _refresh_virtual_slots_in_palette(settings)

        msg = f"Removed filament. {num_physical_new} remaining."
        if replaced_count > 0:
            msg += f" Replaced {replaced_count} painted pixels with base color."
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class MMU_OT_fix_falloff(bpy.types.Operator):
    """Set brush falloff to Constant to prevent banding on export"""

    bl_idname = "mmu.fix_falloff"
    bl_label = "Fix Brush Falloff"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        brush = context.tool_settings.image_paint.brush
        if brush:
            try:
                if hasattr(brush, "curve_distance_falloff_preset"):
                    brush.curve_distance_falloff_preset = "CONSTANT"
                else:
                    brush.curve_preset = "CONSTANT"
                self.report({"INFO"}, "Brush falloff set to Constant")
            except (AttributeError, TypeError, ValueError, RuntimeError) as e:
                self.report({"WARNING"}, f"Failed to set brush falloff: {e}")
            return {"FINISHED"}


class MMU_OT_switch_to_paint(bpy.types.Operator):
    """Switch to Texture Paint mode and open the MMU Paint panel"""

    bl_idname = "mmu.switch_to_paint"
    bl_label = "Open MMU Paint Mode"
    bl_description = "Switch to Texture Paint mode to paint multi-material regions"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({"WARNING"}, "Select a mesh object first")
            return {"CANCELLED"}

        # Switch to texture paint
        bpy.ops.object.mode_set(mode="TEXTURE_PAINT")

        # Setup brush
        _configure_paint_brush(context)
        ts = context.tool_settings

        # Select the paint image
        image = _get_paint_image(obj)
        if image and hasattr(ts.image_paint, "canvas"):
            ts.image_paint.canvas = image

        # Set active node
        if obj.data.materials:
            mat = obj.data.materials[0]
            if mat and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == "TEX_IMAGE":
                        mat.node_tree.nodes.active = node
                        break

        # Sync filament palette
        _sync_filaments_from_mesh(context)

        # Set brush to first filament color
        settings = context.scene.mmu_paint
        if len(settings.filaments) > 0:
            _set_brush_color(context, settings.filaments[0].color[:])

        # Try to open the sidebar panel
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                for space in area.spaces:
                    if space.type == "VIEW_3D":
                        space.show_region_ui = True
                break

        return {"FINISHED"}


class MMU_OT_reassign_filament_color(bpy.types.Operator):
    """Reassign a filament color -- replaces all pixels of old color with new color"""

    bl_idname = "mmu.reassign_filament_color"
    bl_label = "Reassign Filament Color"
    bl_options = {"REGISTER", "UNDO"}

    new_color: bpy.props.FloatVectorProperty(
        name="New Color",
        subtype="COLOR_GAMMA",
        size=3,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0),
        description="New color to replace the current filament color",
    )

    @classmethod
    def poll(cls, context):
        mesh = _get_paint_mesh(context)
        if mesh is None:
            return False
        settings = context.scene.mmu_paint
        return len(settings.filaments) > 0 and settings.active_filament_index < len(
            settings.filaments
        )

    def invoke(self, context, event):
        settings = context.scene.mmu_paint
        idx = settings.active_filament_index
        if idx < len(settings.filaments):
            # Initialize color picker with current color
            self.new_color = settings.filaments[idx].color[:]
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmu_paint
        idx = settings.active_filament_index

        if idx < len(settings.filaments):
            item = settings.filaments[idx]
            layout.label(text=f"Reassigning {item.name}")
            layout.label(
                text="This will replace all pixels of the current color", icon="INFO"
            )
            layout.label(text="with the new color you choose.")
            layout.separator()
            layout.prop(self, "new_color", text="New Color")

    def execute(self, context):
        settings = context.scene.mmu_paint
        idx = settings.active_filament_index

        if idx >= len(settings.filaments):
            return {"CANCELLED"}

        item = settings.filaments[idx]
        obj = context.active_object
        image = _get_paint_image(obj)
        if image is None:
            self.report({"WARNING"}, "No paint texture found")
            return {"CANCELLED"}

        old_rgb = tuple(item.color[:])
        new_rgb = tuple(self.new_color[:])

        # Skip if colors are identical
        if all(abs(o - n) < 0.002 for o, n in zip(old_rgb, new_rgb)):
            return {"CANCELLED"}

        # Bulk pixel replacement
        w, h = image.size
        pixel_count = w * h * 4
        pixels_flat = np.empty(pixel_count, dtype=np.float32)
        image.pixels.foreach_get(pixels_flat)
        pixels = pixels_flat.reshape(h, w, 4)

        old_arr = np.array(old_rgb, dtype=np.float32)
        new_arr = np.array(new_rgb, dtype=np.float32)

        tolerance = 3.0 / 255.0
        mask = np.all(np.abs(pixels[:, :, :3] - old_arr) < tolerance, axis=2)

        num_changed = np.count_nonzero(mask)
        if num_changed == 0:
            self.report({"INFO"}, "No pixels found with the current color")
            return {"CANCELLED"}

        pixels[mask, 0] = new_arr[0]
        pixels[mask, 1] = new_arr[1]
        pixels[mask, 2] = new_arr[2]

        image.pixels.foreach_set(pixels.ravel())
        image.update()

        # Update stored color
        item.color = new_rgb
        _write_colors_to_mesh(context)

        # Update brush if this is the active filament
        _set_brush_color(context, new_rgb)

        self.report({"INFO"}, f"Reassigned {num_changed} pixels to new color")
        return {"FINISHED"}


class MMU_OT_import_paint_popup(bpy.types.Operator):
    """Post-import popup asking to switch to Texture Paint mode"""

    bl_idname = "mmu.import_paint_popup"
    bl_label = "MMU Paint Data Detected"
    bl_options = {"INTERNAL", "UNDO"}

    object_name: bpy.props.StringProperty()

    def execute(self, context):
        """User clicked 'Switch to Texture Paint'."""
        # Select the imported object
        obj = bpy.data.objects.get(self.object_name)
        if obj:
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            context.view_layer.objects.active = obj

        bpy.ops.mmu.switch_to_paint()
        return {"FINISHED"}

    def cancel(self, context):
        """User dismissed the popup -- stay in Object mode."""
        pass

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=350)

    def draw(self, context):
        layout = self.layout
        layout.label(text="This 3MF file contains multi-material paint data.")
        layout.label(text="Would you like to switch to Texture Paint mode")
        layout.label(text="to view and edit the paint regions?")
        layout.separator()
        box = layout.box()
        box.label(text="After switching, open the sidebar (N key) and", icon="INFO")
        box.label(text="click the '3MF' tab to access the paint tools.")


# ===================================================================
#  Seam / Support paint layer operators
# ===================================================================


class MMU_OT_init_auxiliary_paint(bpy.types.Operator):
    """Initialize seam or support painting on the active mesh"""

    bl_idname = "mmu.init_auxiliary_paint"
    bl_label = "Initialize Paint Layer"
    bl_options = {"REGISTER", "UNDO"}

    layer_type: bpy.props.EnumProperty(
        name="Layer",
        items=[
            ("SEAM", "Seam", "Seam enforcement paint"),
            ("SUPPORT", "Support", "Support enforcement paint"),
        ],
        default="SEAM",
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        from .helpers import (
            _layer_colors, _layer_uv_name, _layer_flag_key, _layer_colors_key,
        )

        bpy.ops.ed.undo_push(message=f"Before {self.layer_type.title()} Paint Init")

        obj = context.active_object
        mesh = obj.data
        settings = context.scene.mmu_paint
        layer_type = self.layer_type

        bg, enforce, block = _layer_colors(layer_type)
        uv_name = _layer_uv_name(layer_type)
        flag_key = _layer_flag_key(layer_type)
        colors_key = _layer_colors_key(layer_type)

        # Create UV layer
        uv_layer = mesh.uv_layers.get(uv_name)
        if uv_layer is None:
            uv_layer = mesh.uv_layers.new(name=uv_name)

        # Copy UVs from MMU_Paint if available, otherwise unwrap
        mmu_uv = mesh.uv_layers.get("MMU_Paint")
        if mmu_uv:
            # Copy UV coordinates from the color paint layer
            num_loops = len(mesh.loops)
            uv_flat = [0.0] * (num_loops * 2)
            mmu_uv.data.foreach_get("uv", uv_flat)
            uv_layer.data.foreach_set("uv", uv_flat)
        else:
            # No existing UVs — do a fresh unwrap
            mesh.uv_layers.active = uv_layer
            context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(
                angle_limit=1.15192,
                margin_method="SCALED",
                rotate_method="AXIS_ALIGNED",
                island_margin=0.002,
                area_weight=0.6,
                correct_aspect=True,
                scale_to_bounds=False,
            )
            bpy.ops.object.mode_set(mode="OBJECT")

        # Texture size matching color paint
        tri_count = len(mesh.polygons)
        if tri_count < 5000:
            texture_size = 2048
        elif tri_count < 20000:
            texture_size = 4096
        else:
            texture_size = 8192

        # Create image filled with background color
        image_name = f"{mesh.name}_{uv_name}"
        image = bpy.data.images.new(
            image_name, width=texture_size, height=texture_size, alpha=True
        )
        fill = np.empty((texture_size, texture_size, 4), dtype=np.float32)
        fill[:, :, 0] = bg[0]
        fill[:, :, 1] = bg[1]
        fill[:, :, 2] = bg[2]
        fill[:, :, 3] = 1.0
        image.pixels.foreach_set(fill.ravel())
        image.pack()

        # Add TEX_IMAGE node to the paint material
        if mesh.materials and mesh.materials[0] and mesh.materials[0].use_nodes:
            mat = mesh.materials[0]
            tex_node = mat.node_tree.nodes.new("ShaderNodeTexImage")
            tex_node.image = image
            tex_node.label = uv_name
            tex_node.name = uv_name
            tex_node.location = (-300, -300 if layer_type == "SEAM" else -500)

        # Store custom properties
        mesh[flag_key] = True
        color_dict = {
            1: _hex_from_rgb(*enforce),
            2: _hex_from_rgb(*block),
        }
        mesh[colors_key] = str(color_dict)

        # Switch to the new layer
        settings.active_paint_layer = layer_type
        _switch_to_layer(context, layer_type)

        label = layer_type.title()
        self.report(
            {"INFO"},
            f"Initialized {label} paint layer at {texture_size}x{texture_size}",
        )
        return {"FINISHED"}


class MMU_OT_switch_paint_layer(bpy.types.Operator):
    """Switch the active paint layer (Color / Seam / Support)"""

    bl_idname = "mmu.switch_paint_layer"
    bl_label = "Switch Paint Layer"
    bl_options = {"INTERNAL"}

    layer_type: bpy.props.EnumProperty(
        name="Layer",
        items=[
            ("COLOR", "Color", "MMU color paint"),
            ("SEAM", "Seam", "Seam enforcement paint"),
            ("SUPPORT", "Support", "Support enforcement paint"),
        ],
        default="COLOR",
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        settings = context.scene.mmu_paint
        settings.active_paint_layer = self.layer_type
        _switch_to_layer(context, self.layer_type)
        return {"FINISHED"}


def _switch_to_layer(context, layer_type):
    """Internal helper to activate a paint layer's UV, image, and brush color."""
    from .helpers import (
        _layer_uv_name, _layer_colors, _get_layer_image,
    )

    obj = context.active_object
    if not obj or not obj.data:
        return

    mesh = obj.data
    uv_name = _layer_uv_name(layer_type)

    # Activate the UV layer
    uv_layer = mesh.uv_layers.get(uv_name)
    if uv_layer:
        mesh.uv_layers.active = uv_layer

    # Find and activate the image for painting
    image = _get_layer_image(obj, layer_type)
    if not image and layer_type == "COLOR":
        # Fallback: scan for the segmentation image
        image = _get_paint_image(context)

    if image:
        # Set active TEX_IMAGE node in the material
        if mesh.materials and mesh.materials[0] and mesh.materials[0].use_nodes:
            mat = mesh.materials[0]
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image == image:
                    mat.node_tree.nodes.active = node
                    break

        # Set canvas image
        ts = context.tool_settings
        if hasattr(ts.image_paint, "canvas"):
            ts.image_paint.canvas = image

    # Set brush color
    if layer_type == "COLOR":
        # Restore filament color
        settings = context.scene.mmu_paint
        idx = settings.active_filament_index
        if 0 <= idx < len(settings.filaments):
            _set_brush_color(context, tuple(settings.filaments[idx].color[:]))
    else:
        # For seam/support, use enforce color as default brush
        bg, enforce, block = _layer_colors(layer_type)
        _set_brush_color(context, enforce)


class MMU_OT_switch_aux_brush(bpy.types.Operator):
    """Switch between enforce and block brush colors for seam/support layers"""

    bl_idname = "mmu.switch_aux_brush"
    bl_label = "Switch Brush Mode"
    bl_options = {"INTERNAL"}

    layer_type: bpy.props.EnumProperty(
        name="Layer",
        items=[
            ("SEAM", "Seam", ""),
            ("SUPPORT", "Support", ""),
        ],
        default="SEAM",
    )

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=[
            ("ENFORCE", "Enforce", "Paint enforce regions"),
            ("BLOCK", "Block", "Paint block regions"),
        ],
        default="BLOCK",
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        from .helpers import _layer_colors
        bg, enforce, block = _layer_colors(self.layer_type)
        if self.mode == "ENFORCE":
            _set_brush_color(context, enforce)
        else:
            _set_brush_color(context, block)
        return {"FINISHED"}


# ===================================================================
#  Mixed filament operators (OrcaSlicer-FullSpectrum)
# ===================================================================


def _physical_hex_colors(settings) -> list:
    """Return a list of ``"#RRGGBB"`` strings for the physical filament palette.

    Reads from ``settings.filaments`` (live paint palette) when populated,
    otherwise falls back to ``settings.init_filaments`` (bake setup palette).
    This allows the mix operators to work correctly both before baking
    (where only init_filaments is populated) and during active painting
    (where filaments holds the live palette).
    """
    from ..common.colors import rgb_to_hex

    # Prefer the live palette when it has physical (non-virtual) entries.
    num_virt = sum(1 for m in settings.mixed_filaments if m.enabled and not m.deleted)
    num_physical_live = len(settings.filaments) - num_virt
    if num_physical_live > 0:
        return [rgb_to_hex(*fi.color[:3]) for fi in settings.filaments[:num_physical_live]]

    # Fall back to the init palette used in the bake panel.
    if settings.init_filaments:
        return [rgb_to_hex(*fi.color[:3]) for fi in settings.init_filaments]

    return []


def _next_stable_id(settings) -> int:
    """Return the next unused stable_id (max existing + 1, minimum 1)."""
    if not settings.mixed_filaments:
        return 1
    return max((m.stable_id for m in settings.mixed_filaments), default=0) + 1


def _next_unused_pair(settings) -> tuple:
    """Return the first C(N,2) pair not already defined (1-based).

    Falls back to (1, 2) if all pairs are taken.
    """
    existing = {
        (m.component_a, m.component_b)
        for m in settings.mixed_filaments
        if not m.deleted
    }
    num_physical = max(len(_physical_hex_colors(settings)), 1)

    for a in range(1, num_physical + 1):
        for b in range(a + 1, num_physical + 1):
            if (a, b) not in existing:
                return (a, b)
    return (1, 2)


def _recompute_display_color_for_item(item, settings) -> None:
    """Recompute ``item.display_color`` from physical palette + mix ratio."""
    from ..common.mixed_filaments import compute_display_color, MixedFilament

    physical = _physical_hex_colors(settings)
    if not physical:
        return

    mf = MixedFilament(
        component_a=item.component_a,
        component_b=item.component_b,
        mix_b_percent=item.mix_b_percent,
        distribution_mode=int(item.distribution_mode),
        manual_pattern=item.manual_pattern,
    )
    hex_color = compute_display_color(mf, physical)
    try:
        from ..common.colors import hex_to_rgb
        r, g, b = hex_to_rgb(hex_color)
        item.display_color = (r, g, b)
    except Exception:
        pass


class MMU_OT_add_mixed_filament(bpy.types.Operator):
    """Add a new virtual mixed filament entry to the palette."""

    bl_idname = "mmu.add_mixed_filament"
    bl_label = "Add Mix"
    bl_description = "Add a new virtual mixed filament (OrcaSlicer-FullSpectrum)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.mmu_paint is not None

    def execute(self, context):
        settings = context.scene.mmu_paint
        a, b = _next_unused_pair(settings)

        item = settings.mixed_filaments.add()
        item.component_a = a
        item.component_b = b
        item.mix_b_percent = 50
        item.distribution_mode = "2"
        item.ui_type = "gradient"
        item.enabled = True
        item.deleted = False
        item.stable_id = _next_stable_id(settings)

        _recompute_display_color_for_item(item, settings)
        settings.has_mixed_filaments = True
        settings.active_mixed_filament_index = len(settings.mixed_filaments) - 1

        _refresh_virtual_slots_in_palette(settings)
        return {"FINISHED"}


class MMU_OT_remove_mixed_filament(bpy.types.Operator):
    """Remove the active virtual mixed filament entry."""

    bl_idname = "mmu.remove_mixed_filament"
    bl_label = "Remove Mix"
    bl_description = "Remove the selected virtual mixed filament"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = context.scene.mmu_paint
        return (
            settings is not None
            and settings.has_mixed_filaments
            and len(settings.mixed_filaments) > 0
        )

    def execute(self, context):
        settings = context.scene.mmu_paint
        idx = settings.active_mixed_filament_index
        if 0 <= idx < len(settings.mixed_filaments):
            # Soft-delete: mark deleted so stable_id gap is preserved for round-trip
            settings.mixed_filaments[idx].deleted = True
            settings.mixed_filaments[idx].enabled = False
            # Clamp active index
            settings.active_mixed_filament_index = max(0, idx - 1)

        _refresh_virtual_slots_in_palette(settings)
        return {"FINISHED"}


class MMU_OT_recompute_mix_color(bpy.types.Operator):
    """Recompute the display color for the active mixed filament entry."""

    bl_idname = "mmu.recompute_mix_color"
    bl_label = "Update Color"
    bl_description = "Recompute the blended swatch color from current component settings"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = context.scene.mmu_paint
        return (
            settings is not None
            and settings.has_mixed_filaments
            and len(settings.mixed_filaments) > 0
        )

    def execute(self, context):
        import numpy as np
        settings = context.scene.mmu_paint
        idx = settings.active_mixed_filament_index
        if not (0 <= idx < len(settings.mixed_filaments)):
            return {"FINISHED"}

        item = settings.mixed_filaments[idx]

        # Capture old display color before recompute (for pixel replacement)
        palette_idx = getattr(item, "palette_index", -1)
        old_rgb = None
        if 0 <= palette_idx < len(settings.filaments):
            old_rgb = tuple(settings.filaments[palette_idx].color[:3])

        # Recompute display color
        _recompute_display_color_for_item(item, settings)
        _refresh_virtual_slots_in_palette(settings)

        new_rgb = tuple(item.display_color[:])

        # Update the palette entry's stored color
        if 0 <= palette_idx < len(settings.filaments):
            settings.filaments[palette_idx].color = new_rgb[:3]
            _write_colors_to_mesh(context)

        # Update brush to the new color
        _set_brush_color(context, new_rgb)

        # Reassign pixels in the paint texture (old color → new color)
        if old_rgb is not None and any(abs(o - n) > 0.002 for o, n in zip(old_rgb, new_rgb)):
            obj = context.active_object
            image = _get_paint_image(obj)
            if image is not None:
                w, h = image.size
                pixels_flat = np.empty(w * h * 4, dtype=np.float32)
                image.pixels.foreach_get(pixels_flat)
                pixels = pixels_flat.reshape(h, w, 4)

                old_arr = np.array(old_rgb, dtype=np.float32)
                new_arr = np.array(new_rgb, dtype=np.float32)
                tolerance = 3.0 / 255.0
                mask = np.all(np.abs(pixels[:, :, :3] - old_arr) < tolerance, axis=2)
                num_changed = int(np.count_nonzero(mask))
                if num_changed > 0:
                    pixels[mask, 0] = new_arr[0]
                    pixels[mask, 1] = new_arr[1]
                    pixels[mask, 2] = new_arr[2]
                    image.pixels.foreach_set(pixels.ravel())
                    image.update()
                    self.report({"INFO"}, f"Updated mix color; reassigned {num_changed} pixels")
                    return {"FINISHED"}

        return {"FINISHED"}


# ===================================================================
#  Add-mix menu and three add-type operators
# ===================================================================


class MMU_MT_add_mix_menu(bpy.types.Menu):
    """Popup menu offering three ways to add a virtual mixed filament."""

    bl_idname = "MMU_MT_add_mix_menu"
    bl_label = "Add Mix"

    def draw(self, context):
        layout = self.layout
        layout.operator("mmu.add_mix_by_color", icon="EYEDROPPER", text="Add: Color")
        layout.operator("mmu.add_mix_gradient", icon="IPO_LINEAR", text="Add: Gradient")
        layout.operator("mmu.add_mix_pattern", icon="TEXTURE", text="Add: Pattern")


class MMU_OT_add_mix_gradient(bpy.types.Operator):
    """Add a new gradient-type virtual mixed filament."""

    bl_idname = "mmu.add_mix_gradient"
    bl_label = "Add: Gradient"
    bl_description = "Add a new gradient virtual mixed filament (blend ratio between two filaments)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.mmu_paint is not None

    def execute(self, context):
        settings = context.scene.mmu_paint
        a, b = _next_unused_pair(settings)

        item = settings.mixed_filaments.add()
        item.component_a = a
        item.component_b = b
        item.mix_b_percent = 50
        item.distribution_mode = "2"
        item.ui_type = "gradient"
        item.enabled = True
        item.deleted = False
        item.stable_id = _next_stable_id(settings)

        _recompute_display_color_for_item(item, settings)
        settings.has_mixed_filaments = True
        settings.active_mixed_filament_index = len(settings.mixed_filaments) - 1
        _refresh_virtual_slots_in_palette(settings)
        return {"FINISHED"}


class MMU_OT_add_mix_pattern(bpy.types.Operator):
    """Add a new pattern-type virtual mixed filament."""

    bl_idname = "mmu.add_mix_pattern"
    bl_label = "Add: Pattern"
    bl_description = "Add a new pattern virtual mixed filament (repeating layer sequence)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.mmu_paint is not None

    def execute(self, context):
        settings = context.scene.mmu_paint
        a, b = _next_unused_pair(settings)

        item = settings.mixed_filaments.add()
        item.component_a = a
        item.component_b = b
        item.mix_b_percent = 50
        item.distribution_mode = "2"
        item.ui_type = "pattern"
        item.manual_pattern = "12"
        item.enabled = True
        item.deleted = False
        item.stable_id = _next_stable_id(settings)

        _recompute_display_color_for_item(item, settings)
        settings.has_mixed_filaments = True
        settings.active_mixed_filament_index = len(settings.mixed_filaments) - 1
        _refresh_virtual_slots_in_palette(settings)
        return {"FINISHED"}


class MMU_OT_add_mix_by_color(bpy.types.Operator):
    """Find the best filament blend matching the target color set in the panel."""

    bl_idname = "mmu.add_mix_by_color"
    bl_label = "Add: Color"
    bl_description = (
        "Search all filament pairs and three-way patterns to find the best pigment "
        "blend matching the Target Color shown in the panel"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.mmu_paint is not None

    def execute(self, context):
        from ..common.mixed_filaments import MixedFilament, compute_display_color, DIST_SIMPLE
        from ..common.colors import hex_to_rgb

        settings = context.scene.mmu_paint
        physical_hexes = _physical_hex_colors(settings)

        if not physical_hexes:
            self.report({'WARNING'}, "No filaments in palette — add filaments first")
            return {'CANCELLED'}

        # Target colour in 0-255 space for distance comparisons.
        tr = settings.mix_target_color[0] * 255.0
        tg = settings.mix_target_color[1] * 255.0
        tb = settings.mix_target_color[2] * 255.0

        best_dist = float("inf")
        best_a = 1
        best_b = min(2, len(physical_hexes))
        best_mix = 50
        best_pattern = ""
        best_ui_type = "gradient"

        def _dist(hex_color):
            r, g, b = hex_to_rgb(hex_color)
            return (r * 255.0 - tr) ** 2 + (g * 255.0 - tg) ** 2 + (b * 255.0 - tb) ** 2

        n = len(physical_hexes)

        # --- 2-way gradient search: all ordered pairs × mix ratios ---
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                for mix_b in range(0, 101, 5):
                    mf = MixedFilament(
                        component_a=i + 1,
                        component_b=j + 1,
                        mix_b_percent=mix_b,
                        distribution_mode=DIST_SIMPLE,
                    )
                    d = _dist(compute_display_color(mf, physical_hexes))
                    if d < best_dist:
                        best_dist = d
                        best_a, best_b, best_mix = i + 1, j + 1, mix_b
                        best_pattern = ""
                        best_ui_type = "gradient"

        # --- 3-way pattern search: all triples (A < B < C), four proportion variants ---
        # Since k >= 2 (0-based), the direct digit str(k+1) >= '3' so it never
        # conflicts with the reserved '1' (component_a) and '2' (component_b) tokens.
        for i in range(n):
            for j in range(i + 1, n):
                for k in range(j + 1, n):
                    c_digit = str(k + 1)
                    # Try equal and skewed proportions to cover the triangle interior.
                    for pat in (
                        "12" + c_digit,          # 1:1:1
                        "112" + c_digit,          # 2:1:1 (A-heavy)
                        "122" + c_digit,          # 1:2:1 (B-heavy)
                        "12" + c_digit * 2,       # 1:1:2 (C-heavy)
                    ):
                        mf = MixedFilament(
                            component_a=i + 1,
                            component_b=j + 1,
                            mix_b_percent=50,
                            manual_pattern=pat,
                            distribution_mode=DIST_SIMPLE,
                        )
                        d = _dist(compute_display_color(mf, physical_hexes))
                        if d < best_dist:
                            best_dist = d
                            best_a, best_b, best_mix = i + 1, j + 1, 50
                            best_pattern = pat
                            best_ui_type = "pattern"

        item = settings.mixed_filaments.add()
        item.component_a = best_a
        item.component_b = best_b
        item.mix_b_percent = best_mix
        item.distribution_mode = "2"
        item.ui_type = best_ui_type
        item.manual_pattern = best_pattern
        item.enabled = True
        item.deleted = False
        item.stable_id = _next_stable_id(settings)

        _recompute_display_color_for_item(item, settings)
        settings.has_mixed_filaments = True
        settings.active_mixed_filament_index = len(settings.mixed_filaments) - 1
        _refresh_virtual_slots_in_palette(settings)
        return {"FINISHED"}


class MMU_OT_add_mix_confirm(bpy.types.Operator):
    """Confirm the inline add-mix form and create the new mixed filament."""

    bl_idname = "mmu.add_mix_confirm"
    bl_label = "Add"
    bl_description = "Create a new mixed filament using the settings shown above"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.mmu_paint is not None

    def execute(self, context):
        settings = context.scene.mmu_paint
        mode = settings.add_mix_mode

        if mode == 'COLOR':
            # Delegate to the existing color-solver operator.
            result = bpy.ops.mmu.add_mix_by_color('EXEC_DEFAULT')
            if result != {'FINISHED'}:
                return result
        elif mode == 'GRADIENT':
            physical_hexes = _physical_hex_colors(settings)
            if not physical_hexes:
                self.report({'WARNING'}, "No filaments in palette — add filaments first")
                return {'CANCELLED'}
            item = settings.mixed_filaments.add()
            item.component_a = settings.add_mix_component_a
            item.component_b = settings.add_mix_component_b
            item.mix_b_percent = settings.add_mix_mix_b_percent
            item.distribution_mode = "2"
            item.ui_type = "gradient"
            item.manual_pattern = ""
            item.enabled = True
            item.deleted = False
            item.stable_id = _next_stable_id(settings)
            _recompute_display_color_for_item(item, settings)
            settings.has_mixed_filaments = True
            settings.active_mixed_filament_index = len(settings.mixed_filaments) - 1
            _refresh_virtual_slots_in_palette(settings)
        elif mode == 'PATTERN':
            physical_hexes = _physical_hex_colors(settings)
            if not physical_hexes:
                self.report({'WARNING'}, "No filaments in palette — add filaments first")
                return {'CANCELLED'}
            pat = settings.add_mix_manual_pattern.strip()
            if not pat or not all(c.isdigit() and c != '0' for c in pat):
                self.report({'WARNING'}, "Invalid pattern — use digits 1–9, e.g. '12' or '112'")
                return {'CANCELLED'}
            item = settings.mixed_filaments.add()
            item.component_a = settings.add_mix_component_a
            item.component_b = settings.add_mix_component_b
            item.mix_b_percent = 50
            item.distribution_mode = "2"
            item.ui_type = "pattern"
            item.manual_pattern = pat
            item.enabled = True
            item.deleted = False
            item.stable_id = _next_stable_id(settings)
            _recompute_display_color_for_item(item, settings)
            settings.has_mixed_filaments = True
            settings.active_mixed_filament_index = len(settings.mixed_filaments) - 1
            _refresh_virtual_slots_in_palette(settings)

        settings.show_add_mix_section = False
        return {"FINISHED"}


class MMU_OT_cancel_add_mix(bpy.types.Operator):
    """Hide the inline add-mix form without creating anything."""

    bl_idname = "mmu.cancel_add_mix"
    bl_label = "Cancel"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.scene.mmu_paint is not None

    def execute(self, context):
        context.scene.mmu_paint.show_add_mix_section = False
        return {"FINISHED"}
