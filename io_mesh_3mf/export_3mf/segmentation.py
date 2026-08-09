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
Export module for converting UV textures back to hash-based MMU segmentation strings.

This module handles the reverse process of import, converting painted textures back to
the universal hex hash format used by PrusaSlicer, Orca Slicer, and other slicers.

Process:
1. Pre-compute entire texture → state map (numpy vectorized, done ONCE)
2. Sample state map at triangle corners and interior points
3. Recursively build segmentation tree from state differences
4. Encode tree to hex hash string (reversed nibbles)
5. Simplify tree to reduce string length

The output format is slicer-agnostic — works for both paint_color and mmu_segmentation.
"""

import numpy as np
from typing import Tuple, List, Dict
from ..common.segmentation import SegmentationNode, SegmentationEncoder
from ..common.logging import debug, DEBUG_MODE

# Maximum subdivision depth (7 gives 4^7 = 16384 potential leaf nodes per triangle)
MAX_SUBDIVISION_DEPTH = 7

# Keyed by (image.name, w, h, frozenset(color_to_extruder.items()), default_extruder).
# Shared across all objects in one export run; cleared by clear_export_state_cache().
_state_map_cache: Dict[tuple, np.ndarray] = {}


def clear_export_state_cache() -> None:
    """Discard cached state maps from the previous export run."""
    _state_map_cache.clear()

# Maximum iterations for adaptive mesh pre-subdivision
_MAX_SUBDIV_ITERATIONS = 6

# Target pixels per segmentation leaf node (lower = finer detail, more triangles)
_PIXELS_PER_LEAF = 4


def subdivide_mesh_for_segmentation(
    mesh, max_depth: int, tex_width: int, tex_height: int
) -> bool:
    """
    Pre-subdivide mesh faces whose UV footprint exceeds segmentation resolution.

    At *max_depth* ``d``, each triangle encodes up to ``4^d`` leaf colour regions.
    When a triangle covers far more texture pixels than that, the recursive
    encoder cannot capture enough detail and the result looks blocky.

    This function detects such oversized faces (using their UV-space area
    relative to the texture resolution) and iteratively splits them with bmesh
    so each resulting triangle can be encoded at full fidelity.

    Operates **in-place** on the temporary mesh from ``to_mesh()`` — the
    original scene geometry is never touched.

    :param mesh: Blender Mesh datablock (temporary copy from ``to_mesh()``).
    :param max_depth: Maximum recursive segmentation depth per triangle.
    :param tex_width: Texture width in pixels.
    :param tex_height: Texture height in pixels.
    :return: *True* if the mesh was modified, *False* otherwise.
    """
    import bmesh

    if not mesh.uv_layers or not mesh.uv_layers.active:
        return False

    tex_area = tex_width * tex_height
    if tex_area == 0:
        return False

    # Pixel-area budget per triangle: 4^depth leaves × pixels_per_leaf.
    max_pixel_area = (4 ** max_depth) * _PIXELS_PER_LEAF

    bm = bmesh.new()
    bm.from_mesh(mesh)

    uv_layer = bm.loops.layers.uv.active
    if not uv_layer:
        bm.free()
        return False

    modified = False

    for iteration in range(_MAX_SUBDIV_ITERATIONS):
        bm.faces.ensure_lookup_table()
        edges_to_cut = set()
        oversized = 0

        for face in bm.faces:
            loops = face.loops
            if len(loops) < 3:
                continue

            # UV area via cross-product fan (handles tris and quads).
            uvs = [loop[uv_layer].uv for loop in loops]
            uv_area = 0.0
            for i in range(1, len(uvs) - 1):
                dx1 = uvs[i].x - uvs[0].x
                dy1 = uvs[i].y - uvs[0].y
                dx2 = uvs[i + 1].x - uvs[0].x
                dy2 = uvs[i + 1].y - uvs[0].y
                uv_area += abs(dx1 * dy2 - dx2 * dy1) * 0.5

            if uv_area * tex_area > max_pixel_area:
                for edge in face.edges:
                    edges_to_cut.add(edge)
                oversized += 1

        if not edges_to_cut:
            break

        debug(
            f"  Adaptive subdivision pass {iteration + 1}: "
            f"{oversized} oversized faces, {len(edges_to_cut)} edges"
        )

        bmesh.ops.subdivide_edges(bm, edges=list(edges_to_cut), cuts=1)

        # Ensure all faces are triangles after subdivision.
        non_tris = [f for f in bm.faces if len(f.verts) > 3]
        if non_tris:
            bmesh.ops.triangulate(bm, faces=non_tris)

        modified = True

    if modified:
        bm.to_mesh(mesh)
        mesh.update()
        mesh.calc_loop_triangles()
        debug(
            f"  Adaptive subdivision done: "
            f"{len(mesh.vertices)} verts, {len(mesh.loop_triangles)} tris"
        )

    bm.free()
    return modified


def _build_state_map(
    pixels: np.ndarray,
    color_to_extruder: Dict[Tuple[int, int, int], int],
    default_extruder: int,
) -> np.ndarray:
    """
    Convert entire pixel array to a state map using numpy vectorized ops.

    Key insight: per-sample color matching inside recursion was the hot path.
    By converting the full texture once, recursion becomes just
    ``state_map[y, x]`` lookups with no RGB distance work.

    :param pixels: (H, W, 4) float32 array.
    :param color_to_extruder: RGB(0-255) -> 0-based extruder index.
    :param default_extruder: 1-based default extruder number.
    :return: (H, W) uint8 array of state values.
    """
    height, width = pixels.shape[:2]

    # Convert float [0,1] to uint8 [0,255] for RGB channels only.
    rgb_int = (pixels[:, :, :3] * 255).astype(np.uint8)

    known_rgbs = []
    known_states = []
    for rgb, ext_idx in color_to_extruder.items():
        known_rgbs.append(rgb)
        ext_num = ext_idx + 1
        state = 0 if ext_num == default_extruder else ext_num
        known_states.append(state)

    known_rgbs = np.array(known_rgbs, dtype=np.int16)
    known_states = np.array(known_states, dtype=np.uint8)
    n_colors = len(known_rgbs)

    state_map = np.empty((height, width), dtype=np.uint8)

    # Chunking keeps peak memory bounded (important for 4K/8K textures).
    chunk_size = 256
    for y_start in range(0, height, chunk_size):
        y_end = min(y_start + chunk_size, height)
        chunk = rgb_int[y_start:y_end]
        chunk_h = chunk.shape[0]

        chunk_expanded = chunk.reshape(chunk_h, width, 1, 3).astype(np.int16)
        colors_expanded = known_rgbs.reshape(1, 1, n_colors, 3)

        dists = np.sum(np.abs(chunk_expanded - colors_expanded), axis=3)

        nearest_idx = np.argmin(dists, axis=2)

        state_map[y_start:y_end] = known_states[nearest_idx]

    return state_map


def _analyze_recursive(
    state_map: np.ndarray,
    width: int,
    height: int,
    u0: float,
    v0: float,
    u1: float,
    v1: float,
    u2: float,
    v2: float,
    max_depth: int,
) -> SegmentationNode:
    """
    Recursively analyze a triangle's segmentation from the pre-computed state map.

    Insight: passing tuples and callbacks was expensive at this depth.
    Inlining floats and sampling directly keeps recursion overhead low.
    """
    # Inline UV->pixel conversion with rounding to avoid bias.
    wm1 = width - 1
    hm1 = height - 1

    x0 = max(0, min(wm1, int(max(0.0, min(1.0, u0)) * wm1 + 0.5)))
    y0 = max(0, min(hm1, int(max(0.0, min(1.0, v0)) * hm1 + 0.5)))
    s0 = int(state_map[y0, x0])

    x1 = max(0, min(wm1, int(max(0.0, min(1.0, u1)) * wm1 + 0.5)))
    y1 = max(0, min(hm1, int(max(0.0, min(1.0, v1)) * hm1 + 0.5)))
    s1 = int(state_map[y1, x1])

    x2 = max(0, min(wm1, int(max(0.0, min(1.0, u2)) * wm1 + 0.5)))
    y2 = max(0, min(hm1, int(max(0.0, min(1.0, v2)) * hm1 + 0.5)))
    s2 = int(state_map[y2, x2])

    # Even if corners match, interior stripes can cross a triangle.
    # Use a barycentric grid whose density scales with the triangle's pixel
    # footprint: ~0.5 steps per pixel up to N=40, minimum N=6.
    # This guarantees sample spacing of ≤2px regardless of triangle size,
    # so even narrow text strokes are reliably detected before the
    # early-exit fires.
    if s0 == s1 == s2:
        u_span = max(u0, u1, u2) - min(u0, u1, u2)
        v_span = max(v0, v1, v2) - min(v0, v1, v2)
        pixel_span = max(u_span * wm1, v_span * hm1)
        N = max(6, min(int(pixel_span * 0.5 + 0.5), 40))
        uniform = True
        stop = False
        for i in range(N + 1):
            for j in range(N + 1 - i):
                k = N - i - j
                gu = (i * u0 + j * u1 + k * u2) / N
                gv = (i * v0 + j * v1 + k * v2) / N
                gx = max(0, min(wm1, int(max(0.0, min(1.0, gu)) * wm1 + 0.5)))
                gy = max(0, min(hm1, int(max(0.0, min(1.0, gv)) * hm1 + 0.5)))
                if int(state_map[gy, gx]) != s0:
                    uniform = False
                    stop = True
                    break
            if stop:
                break
        if uniform:
            return SegmentationNode(
                state=s0,
                split_sides=0,
                special_side=0,
                children=[],
            )

    # Max depth reached: collapse to the most common corner state.
    if max_depth <= 0:
        if s0 == s1 or s0 == s2:
            return SegmentationNode(
                state=s0, split_sides=0, special_side=0, children=[]
            )
        elif s1 == s2:
            return SegmentationNode(
                state=s1, split_sides=0, special_side=0, children=[]
            )
        else:
            return SegmentationNode(
                state=s0, split_sides=0, special_side=0, children=[]
            )

    # Standard 3-edge split for recursive subdivision.
    m01u = (u0 + u1) * 0.5
    m01v = (v0 + v1) * 0.5
    m12u = (u1 + u2) * 0.5
    m12v = (v1 + v2) * 0.5
    m20u = (u2 + u0) * 0.5
    m20v = (v2 + v0) * 0.5

    nd = max_depth - 1
    c0 = _analyze_recursive(
        state_map, width, height, u0, v0, m01u, m01v, m20u, m20v, nd
    )
    c1 = _analyze_recursive(
        state_map, width, height, m01u, m01v, u1, v1, m12u, m12v, nd
    )
    c2 = _analyze_recursive(
        state_map, width, height, m12u, m12v, u2, v2, m20u, m20v, nd
    )
    c3 = _analyze_recursive(
        state_map, width, height, m01u, m01v, m12u, m12v, m20u, m20v, nd
    )

    if not c0.children and not c1.children and not c2.children and not c3.children:
        if c0.state == c1.state == c2.state == c3.state:
            return SegmentationNode(
                state=c0.state, split_sides=0, special_side=0, children=[]
            )

    return SegmentationNode(
        state=c0.state if c0 else 0,
        split_sides=3,
        special_side=0,
        children=[c3, c2, c1, c0],
    )


def texture_to_segmentation(
    obj, image, extruder_colors: Dict[int, List[float]], default_extruder: int = 1,
    progress_callback=None, max_depth: int = MAX_SUBDIVISION_DEPTH,
    mesh=None,
) -> Dict[int, str]:
    """
    Convert object's UV texture to PrusaSlicer segmentation strings.

    :param obj: Blender object with UV-mapped mesh.
    :param image: Painted texture image.
    :param extruder_colors: Mapping from extruder index to RGBA color.
    :param default_extruder: Default extruder index.
    :param progress_callback: Optional callback(current, total, message) for progress updates.
    :param max_depth: Maximum recursive subdivision depth (4-10, default 7).
    :param mesh: Optional pre-subdivided mesh to use instead of ``obj.data``.
    :return: Dict mapping loop_triangle index -> segmentation_hex_string.
    """
    import time

    t_start = time.perf_counter()

    if mesh is None:
        mesh = obj.data
    width, height = image.size

    color_to_extruder = {}
    debug(f"  Building color->extruder map from {len(extruder_colors)} colors:")
    for extruder, rgba in extruder_colors.items():
        rgb = (int(rgba[0] * 255), int(rgba[1] * 255), int(rgba[2] * 255))
        color_to_extruder[rgb] = extruder
        debug(f"    Extruder {extruder}: RGB {rgb}")
    debug(f"  Default extruder: {default_extruder}")

    # Re-use the state map when multiple objects share the same texture (e.g. puzzle pieces).
    cache_key = (image.name, width, height, frozenset(color_to_extruder.items()), default_extruder)
    if cache_key in _state_map_cache:
        debug(f"  State map cache hit for '{image.name}' — skipping pixel read + build")
        state_map = _state_map_cache[cache_key]
        t_cache = t_start
        t_state = t_start
    else:
        # One-time read from Blender image into numpy for fast access.
        debug(f"  Caching {width}x{height} texture data as numpy array...")
        pixel_count = width * height * 4
        pixels_flat = np.empty(pixel_count, dtype=np.float32)
        image.pixels.foreach_get(pixels_flat)
        pixels = pixels_flat.reshape(height, width, 4)
        t_cache = time.perf_counter()
        debug(f"  Cached pixels in {t_cache - t_start:.2f}s")

        # Pre-compute the entire texture to states (critical performance win).
        debug(f"  Building state map ({width}x{height})...")
        state_map = _build_state_map(pixels, color_to_extruder, default_extruder)
        _state_map_cache[cache_key] = state_map
        t_state = time.perf_counter()

    if DEBUG_MODE:
        unique_states = np.unique(state_map)
        debug(
            f"  State map built in {t_state - t_cache:.2f}s, unique states: {list(unique_states)}"
        )
    else:
        debug(f"  State map built in {t_state - t_cache:.2f}s")

    if not mesh.uv_layers or not mesh.uv_layers.active:
        return {}

    uv_layer = mesh.uv_layers.active.data
    seg_strings = {}

    # Use loop_triangles to handle both quads and triangles.
    # mesh.polygons only has the original faces (quads for a default cube),
    # while loop_triangles gives us the actual triangulated geometry.
    mesh.calc_loop_triangles()
    total_faces = len(mesh.loop_triangles)
    debug(
        f"  Processing {total_faces} triangles (max_depth={max_depth})..."
    )

    encoder = SegmentationEncoder()

    for tri_idx, tri in enumerate(mesh.loop_triangles):
        # Progress callback every 500 triangles
        if progress_callback and tri_idx > 0 and tri_idx % 500 == 0:
            progress_callback(tri_idx, total_faces, f"Segmentation: {tri_idx}/{total_faces}")
        if tri_idx > 0 and tri_idx % 2000 == 0:
            elapsed = time.perf_counter() - t_state
            rate = tri_idx / elapsed if elapsed > 0 else 0
            remaining = (total_faces - tri_idx) / rate if rate > 0 else 0
            debug(
                f"    {tri_idx}/{total_faces} ({rate:.0f}/s, ~{remaining:.0f}s left)"
            )

        li = tri.loops
        uv0 = uv_layer[li[0]].uv
        uv1 = uv_layer[li[1]].uv
        uv2 = uv_layer[li[2]].uv

        tree = _analyze_recursive(
            state_map,
            width,
            height,
            uv0[0],
            uv0[1],
            uv1[0],
            uv1[1],
            uv2[0],
            uv2[1],
            max_depth,
        )

        if tree.children or tree.state != 0:
            encoder._nibbles = []
            hex_string = encoder.encode(tree)
            if hex_string and hex_string != "0":
                seg_strings[tri_idx] = hex_string

    t_end = time.perf_counter()
    debug(
        f"  Processed {total_faces} triangles in {t_end - t_state:.1f}s, found {len(seg_strings)} with segmentation"
    )
    debug(f"  Total export time: {t_end - t_start:.1f}s")

    return seg_strings
