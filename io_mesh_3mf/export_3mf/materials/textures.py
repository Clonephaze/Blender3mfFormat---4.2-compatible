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
Texture export functionality for 3MF Materials Extension.

Handles:
- Detecting textured materials (base color textures)
- Detecting PBR textured materials (roughness, metallic, normal maps)
- Writing texture images to archive
- Writing texture2d and texture2dgroup elements
- Writing texture relationships
- Writing pbmetallictexturedisplayproperties
"""

import os
import tempfile
import xml.etree.ElementTree
import zipfile
from typing import Optional, Dict, List, Tuple

import bpy

from ...common.constants import (
    MATERIAL_NAMESPACE,
    TEXTURE_MIMETYPE_PNG,
    TEXTURE_MIMETYPE_JPEG,
    TEXTURE_REL,
    RELS_NAMESPACE,
)
from ...common import debug, warn, error


def detect_textured_materials(
    blender_objects: List[bpy.types.Object],
) -> Dict[str, Dict]:
    """
    Detect materials with Image Texture nodes connected to Base Color.

    Returns a dictionary of material name -> texture info:
    {
        "material_name": {
            "image": bpy.types.Image,
            "tilestyleu": str,
            "tilestylev": str,
            "filter": str,
            "original_path": str  # from custom property if imported
        }
    }
    """
    textured_materials = {}

    for blender_object in blender_objects:
        for material_slot in blender_object.material_slots:
            material = material_slot.material
            if material is None or not material.use_nodes:
                continue

            material_name = str(material.name)
            if material_name in textured_materials:
                continue

            # Find Image Texture node connected to Principled BSDF Base Color
            image_info = _find_base_color_texture(material)
            if image_info:
                textured_materials[material_name] = image_info
                debug(f"Detected textured material: {material_name}")

    return textured_materials


def _find_base_color_texture(material: bpy.types.Material) -> Optional[Dict]:
    """
    Find Image Texture node connected to Principled BSDF Base Color input.

    :param material: Blender material to analyze
    :return: Dict with image info, or None if not found
    """
    if not material.node_tree:
        return None

    nodes = material.node_tree.nodes
    links = material.node_tree.links

    # Find Principled BSDF node
    principled = None
    for node in nodes:
        if node.type == "BSDF_PRINCIPLED":
            principled = node
            break

    if not principled:
        return None

    # Check Base Color input for image texture
    base_color_input = principled.inputs.get("Base Color")
    if not base_color_input or not base_color_input.is_linked:
        return None

    # Trace back to find Image Texture node
    for link in links:
        if link.to_socket == base_color_input:
            from_node = link.from_node
            if from_node.type == "TEX_IMAGE" and from_node.image:
                image = from_node.image

                # Determine tile style from extension mode
                extension = getattr(from_node, "extension", "REPEAT")
                if extension == "CLIP":
                    tilestyleu = "clamp"
                    tilestylev = "clamp"
                elif extension == "EXTEND":
                    tilestyleu = "mirror"  # Closest approximation
                    tilestylev = "mirror"
                else:
                    tilestyleu = "wrap"
                    tilestylev = "wrap"

                # Check for stored metadata from import
                tilestyleu = material.get("3mf_texture_tilestyleu", tilestyleu)
                tilestylev = material.get("3mf_texture_tilestylev", tilestylev)
                filter_mode = material.get("3mf_texture_filter", "auto")
                original_path = material.get("3mf_texture_path", "")

                # Determine filter from interpolation
                interpolation = getattr(from_node, "interpolation", "Linear")
                if interpolation == "Closest":
                    filter_mode = "nearest"
                elif filter_mode not in ("linear", "nearest"):
                    filter_mode = "auto"

                return {
                    "image": image,
                    "tilestyleu": tilestyleu,
                    "tilestylev": tilestylev,
                    "filter": filter_mode,
                    "original_path": original_path,
                }

    return None


def _find_texture_from_input(material: bpy.types.Material, input_name: str, non_color: bool = False) -> Optional[Dict]:
    """
    Find Image Texture node connected to a specific Principled BSDF input.

    :param material: Blender material to analyze
    :param input_name: Name of the Principled BSDF input (e.g., 'Roughness', 'Metallic')
    :param non_color: Whether this texture should be non-color data
    :return: Dict with image info, or None if not found
    """
    if not material.node_tree:
        return None

    nodes = material.node_tree.nodes
    links = material.node_tree.links

    # Find Principled BSDF node
    principled = None
    for node in nodes:
        if node.type == "BSDF_PRINCIPLED":
            principled = node
            break

    if not principled:
        return None

    # Check the specified input for image texture
    target_input = principled.inputs.get(input_name)
    if not target_input or not target_input.is_linked:
        return None

    # Trace back to find Image Texture node (may go through other nodes)
    def trace_to_image_texture(socket, depth=0):
        """Recursively trace back through nodes to find Image Texture."""
        if depth > 10:  # Prevent infinite loops
            return None
        for link in links:
            if link.to_socket == socket:
                from_node = link.from_node
                if from_node.type == "TEX_IMAGE" and from_node.image:
                    return from_node
                # Check if we can trace through this node (e.g., Normal Map, Invert)
                if from_node.inputs:
                    # Try common input names
                    for input_sock in from_node.inputs:
                        if input_sock.is_linked:
                            result = trace_to_image_texture(input_sock, depth + 1)
                            if result:
                                return result
        return None

    tex_node = trace_to_image_texture(target_input)
    if not tex_node:
        return None

    image = tex_node.image

    # Determine tile style from extension mode
    extension = getattr(tex_node, "extension", "REPEAT")
    if extension == "CLIP":
        tilestyleu = "clamp"
        tilestylev = "clamp"
    elif extension == "EXTEND":
        tilestyleu = "mirror"  # Closest approximation
        tilestylev = "mirror"
    else:
        tilestyleu = "wrap"
        tilestylev = "wrap"

    # Determine filter from interpolation
    interpolation = getattr(tex_node, "interpolation", "Linear")
    if interpolation == "Closest":
        filter_mode = "nearest"
    else:
        filter_mode = "auto"

    return {
        "image": image,
        "tilestyleu": tilestyleu,
        "tilestylev": tilestylev,
        "filter": filter_mode,
        "non_color": non_color,
    }


def detect_pbr_textured_materials(
    blender_objects: List[bpy.types.Object],
) -> Dict[str, Dict]:
    """
    Detect materials with PBR texture nodes connected to Principled BSDF.

    Detects textures connected to:
    - Base Color
    - Roughness
    - Metallic
    - Normal (through Normal Map node)

    Returns a dictionary of material name -> PBR texture info:
    {
        "material_name": {
            "base_color": {image info dict},     # or None
            "roughness": {image info dict},       # or None
            "metallic": {image info dict},        # or None
            "normal": {image info dict},          # or None
        }
    }
    """
    pbr_materials = {}

    for blender_object in blender_objects:
        for material_slot in blender_object.material_slots:
            material = material_slot.material
            if material is None or not material.use_nodes:
                continue

            material_name = str(material.name)
            if material_name in pbr_materials:
                continue

            # Check for PBR textures
            base_color = _find_base_color_texture(material)
            roughness = _find_texture_from_input(material, "Roughness", non_color=True)
            metallic = _find_texture_from_input(material, "Metallic", non_color=True)
            normal = _find_texture_from_input(material, "Normal", non_color=True)

            # Only include if at least one texture is found
            if base_color or roughness or metallic or normal:
                pbr_materials[material_name] = {
                    "base_color": base_color,
                    "roughness": roughness,
                    "metallic": metallic,
                    "normal": normal,
                }
                texture_types = [
                    t for t in ["base_color", "roughness", "metallic", "normal"] if pbr_materials[material_name][t]
                ]
                debug(f"Detected PBR material '{material_name}' with textures: {texture_types}")

    return pbr_materials


def write_textures_to_archive(archive: zipfile.ZipFile, textured_materials: Dict[str, Dict]) -> Dict[str, str]:
    """
    Write texture images to the 3MF archive.

    :param archive: The 3MF zip archive
    :param textured_materials: Dict from detect_textured_materials()
    :return: Dict mapping image name -> archive path (e.g. "/3D/Textures/image.png")
    """
    image_to_path = {}
    texture_folder = "3D/Textures"

    for mat_name, tex_info in textured_materials.items():
        image = tex_info["image"]
        image_name = str(image.name)

        # Skip if already written in this pass
        if image_name in image_to_path:
            continue

        # Determine output format and path
        # Prefer original format if available, otherwise use PNG
        original_path = tex_info.get("original_path", "")
        if original_path and original_path.lower().endswith(".jpg"):
            ext = ".jpg"
        elif original_path and original_path.lower().endswith(".jpeg"):
            ext = ".jpeg"
        else:
            ext = ".png"

        # Use original archive path when available (round-trip preservation).
        # This keeps the texture2d path, relationships, and content types
        # consistent with what the original 3MF file used.
        if original_path:
            archive_path = original_path.lstrip("/")
            full_archive_path = f"/{archive_path}"
        else:
            # Sanitize filename
            safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in image_name)
            if not safe_name.lower().endswith(ext):
                safe_name += ext

            archive_path = f"{texture_folder}/{safe_name}"
            full_archive_path = f"/{archive_path}"

        # Check if already in archive (e.g., written by PBR texture pass)
        try:
            archive.getinfo(archive_path)
            image_to_path[image_name] = full_archive_path
            debug(f"Texture '{image_name}' already in archive at {archive_path}")
            continue
        except KeyError:
            pass

        try:
            # Save image to temporary file, then add to archive
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp_path = tmp.name

            # Determine format for save
            if ext in (".jpg", ".jpeg"):
                file_format = "JPEG"
            else:
                file_format = "PNG"

            # Save image
            original_filepath = image.filepath_raw
            image.filepath_raw = tmp_path
            image.file_format = file_format
            image.save()
            image.filepath_raw = original_filepath

            # Add to archive
            archive.write(tmp_path, archive_path)
            os.unlink(tmp_path)

            image_to_path[image_name] = full_archive_path
            debug(f"Wrote texture '{image_name}' to {archive_path}")

        except Exception as e:
            warn(f"Failed to write texture '{image_name}': {e}")
            # Try alternative: if image is packed, write from packed data
            if image.packed_file:
                try:
                    archive.writestr(archive_path, image.packed_file.data)
                    image_to_path[image_name] = full_archive_path
                    debug(f"Wrote packed texture '{image_name}' to {archive_path}")
                except Exception as e2:
                    error(f"Failed to write packed texture '{image_name}': {e2}")

    return image_to_path


def write_texture_relationships(archive: zipfile.ZipFile, image_to_path: Dict[str, str]) -> None:
    """
    Write the model's relationship file to declare texture resources.

    According to the 3MF spec and OPC conventions, textures should be declared
    as relationships in 3D/_rels/3dmodel.model.rels.

    :param archive: The 3MF zip archive
    :param image_to_path: Dict mapping image name -> archive path
    """
    if not image_to_path:
        return

    # Create relationships XML
    relationships_element = xml.etree.ElementTree.Element(f"{{{RELS_NAMESPACE}}}Relationships")

    # Add a relationship for each texture
    rel_id = 1
    for image_name, archive_path in image_to_path.items():
        rel_element = xml.etree.ElementTree.SubElement(relationships_element, f"{{{RELS_NAMESPACE}}}Relationship")
        rel_element.attrib[f"{{{RELS_NAMESPACE}}}Type"] = TEXTURE_REL
        rel_element.attrib[f"{{{RELS_NAMESPACE}}}Target"] = archive_path
        rel_element.attrib[f"{{{RELS_NAMESPACE}}}Id"] = f"rel{rel_id}"
        rel_id += 1

    # Write to archive at 3D/_rels/3dmodel.model.rels
    # Use namespace-qualified attributes + default_namespace to produce clean
    # XML like annotations.write_rels() does, avoiding ns0: prefixes.
    rels_path = "3D/_rels/3dmodel.model.rels"
    tree = xml.etree.ElementTree.ElementTree(relationships_element)
    with archive.open(rels_path, "w") as f:
        tree.write(
            f,
            xml_declaration=True,
            encoding="UTF-8",
            default_namespace=RELS_NAMESPACE,
        )

    debug(f"Wrote {len(image_to_path)} texture relationships to {rels_path}")


def write_texture_resources(
    resources_element: xml.etree.ElementTree.Element,
    textured_materials: Dict[str, Dict],
    image_to_path: Dict[str, str],
    next_resource_id: int,
    precision: int = 6,
) -> Tuple[Dict[str, int], int]:
    """
    Write texture2d and texture2dgroup elements for textured materials.

    :param resources_element: The <resources> element
    :param textured_materials: Dict from detect_textured_materials()
    :param image_to_path: Dict from write_textures_to_archive()
    :param next_resource_id: Next available resource ID
    :param precision: Decimal places for UV coordinates
    :return: Tuple of (material_name -> texture_group_id mapping, updated next_resource_id)
    """
    material_to_texture_group = {}

    # Track written textures (image path -> texture2d ID)
    texture_ids = {}

    for mat_name, tex_info in textured_materials.items():
        image = tex_info["image"]
        image_name = str(image.name)

        if image_name not in image_to_path:
            continue

        archive_path = image_to_path[image_name]

        # Write texture2d element if not already written for this image
        if archive_path not in texture_ids:
            texture_id = str(next_resource_id)
            next_resource_id += 1

            # Determine content type
            if archive_path.lower().endswith((".jpg", ".jpeg")):
                contenttype = TEXTURE_MIMETYPE_JPEG
            else:
                contenttype = TEXTURE_MIMETYPE_PNG

            texture_attrib = {
                "id": texture_id,
                "path": archive_path,
                "contenttype": contenttype,
            }

            # Add optional tile style attributes if not default
            if tex_info["tilestyleu"] != "wrap":
                texture_attrib["tilestyleu"] = tex_info["tilestyleu"]
            if tex_info["tilestylev"] != "wrap":
                texture_attrib["tilestylev"] = tex_info["tilestylev"]
            if tex_info["filter"] != "auto":
                texture_attrib["filter"] = tex_info["filter"]

            xml.etree.ElementTree.SubElement(
                resources_element,
                f"{{{MATERIAL_NAMESPACE}}}texture2d",
                attrib=texture_attrib,
            )
            texture_ids[archive_path] = texture_id
            debug(f"Created texture2d ID {texture_id} for {archive_path}")

        # Create texture2dgroup for this material
        # Note: tex2coord elements will be added when writing triangles
        texture2d_id = texture_ids[archive_path]
        group_id = str(next_resource_id)
        next_resource_id += 1

        group_element = xml.etree.ElementTree.SubElement(
            resources_element,
            f"{{{MATERIAL_NAMESPACE}}}texture2dgroup",
            attrib={
                "id": group_id,
                "texid": texture2d_id,
            },
        )

        # Store for later tex2coord population
        material_to_texture_group[mat_name] = {
            "group_id": group_id,
            "group_element": group_element,
            "tex2coords": {},  # UV tuple -> index mapping
            "next_index": 0,
            "precision": precision,
        }
        debug(f"Created texture2dgroup ID {group_id} for material {mat_name}")

    return material_to_texture_group, next_resource_id


def get_or_create_tex2coord(texture_group_data: Dict, u: float, v: float) -> int:
    """
    Get or create a tex2coord index for a UV coordinate.

    :param texture_group_data: Dict with tex2coords mapping and group_element
    :param u: U texture coordinate
    :param v: V texture coordinate
    :return: Index of the tex2coord in the texture2dgroup
    """
    precision = texture_group_data.get("precision", 6)
    # Round UV to precision for deduplication
    u_rounded = round(u, precision)
    v_rounded = round(v, precision)
    uv_key = (u_rounded, v_rounded)

    if uv_key in texture_group_data["tex2coords"]:
        return texture_group_data["tex2coords"][uv_key]

    # Create new tex2coord element
    index = texture_group_data["next_index"]
    texture_group_data["next_index"] = index + 1

    xml.etree.ElementTree.SubElement(
        texture_group_data["group_element"],
        f"{{{MATERIAL_NAMESPACE}}}tex2coord",
        attrib={
            "u": f"{u_rounded:.{precision}g}",
            "v": f"{v_rounded:.{precision}g}",
        },
    )

    texture_group_data["tex2coords"][uv_key] = index
    return index


def write_pbr_textures_to_archive(archive: zipfile.ZipFile, pbr_materials: Dict[str, Dict]) -> Dict[str, str]:
    """
    Write ALL PBR texture images (base_color, roughness, metallic, normal) to the 3MF archive.

    For materials with PBR textures (roughness/metallic), all textures including base color
    should go through pbmetallictexturedisplayproperties, not texture2dgroup.

    :param archive: The 3MF zip archive
    :param pbr_materials: Dict from detect_pbr_textured_materials()
    :return: Dict mapping image name -> archive path
    """
    image_to_path = {}
    texture_folder = "3D/Textures"

    # Collect all unique images from all PBR channels (including base_color)
    for mat_name, pbr_info in pbr_materials.items():
        # Only process materials that have roughness or metallic textures
        # These are the ones that will use pbmetallictexturedisplayproperties
        if not pbr_info.get("roughness") and not pbr_info.get("metallic"):
            continue

        # Include base_color along with PBR channels
        for channel in ["base_color", "roughness", "metallic", "normal"]:
            tex_info = pbr_info.get(channel)
            if not tex_info:
                continue

            image = tex_info["image"]
            image_name = str(image.name)

            if image_name in image_to_path:
                continue

            # Determine output format - PBR textures typically stay as-is
            ext = ".png"
            if image.filepath_raw:
                if image.filepath_raw.lower().endswith((".jpg", ".jpeg")):
                    ext = ".jpg"

            # Sanitize filename
            safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in image_name)
            if not safe_name.lower().endswith(ext):
                safe_name += ext

            archive_path = f"{texture_folder}/{safe_name}"
            full_archive_path = f"/{archive_path}"

            try:
                # Save image to temporary file, then add to archive
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp_path = tmp.name

                file_format = "JPEG" if ext in (".jpg", ".jpeg") else "PNG"

                original_filepath = image.filepath_raw
                image.filepath_raw = tmp_path
                image.file_format = file_format
                image.save()
                image.filepath_raw = original_filepath

                archive.write(tmp_path, archive_path)
                os.unlink(tmp_path)

                image_to_path[image_name] = full_archive_path
                debug(f"Wrote PBR texture '{image_name}' to {archive_path}")

            except Exception as e:
                warn(f"Failed to write PBR texture '{image_name}': {e}")
                # Try packed data if available
                if image.packed_file:
                    try:
                        archive.writestr(archive_path, image.packed_file.data)
                        image_to_path[image_name] = full_archive_path
                        debug(f"Wrote packed PBR texture '{image_name}' to {archive_path}")
                    except Exception as e2:
                        error(f"Failed to write packed PBR texture '{image_name}': {e2}")

    return image_to_path


def write_pbr_texture_display_properties(
    resources_element: xml.etree.ElementTree.Element,
    pbr_materials: Dict[str, Dict],
    image_to_path: Dict[str, str],
    next_resource_id: int,
    basematerials_element: Optional[xml.etree.ElementTree.Element] = None,
) -> Tuple[Dict[str, str], int]:
    """
    Write pbmetallictexturedisplayproperties elements for PBR materials.

    Creates texture2d resources for ALL PBR textures (base color, roughness, metallic)
    and links them via pbmetallictexturedisplayproperties.

    Per 3MF Materials Extension spec, pbmetallictexturedisplayproperties supports:
    - basecolortextureid: texture for base color (replaces texture2dgroup approach)
    - metallictextureid: texture for metallic
    - roughnesstextureid: texture for roughness

    :param resources_element: The <resources> element
    :param pbr_materials: Dict from detect_pbr_textured_materials()
    :param image_to_path: Dict mapping image name -> archive path
    :param next_resource_id: Next available resource ID
    :param basematerials_element: Optional basematerials element to link via displaypropertiesid
    :return: Tuple of (material_name -> display_props_id mapping, updated next_resource_id)
    """
    material_to_display_props = {}
    texture_ids = {}  # image path -> texture2d ID
    first_display_props_id = None  # Track first ID for basematerials linkage

    def get_or_create_texture2d(tex_info: dict | None, tex_type: str) -> str:
        """Helper to create texture2d resource and return its ID."""
        nonlocal next_resource_id

        if not tex_info or not tex_info.get("image"):
            return ""

        image_name = str(tex_info["image"].name)
        archive_path = image_to_path.get(image_name)
        if not archive_path:
            return ""

        if archive_path not in texture_ids:
            tex_id = str(next_resource_id)
            next_resource_id += 1

            is_jpeg = archive_path.lower().endswith((".jpg", ".jpeg"))
            contenttype = TEXTURE_MIMETYPE_JPEG if is_jpeg else TEXTURE_MIMETYPE_PNG

            xml.etree.ElementTree.SubElement(
                resources_element,
                f"{{{MATERIAL_NAMESPACE}}}texture2d",
                attrib={
                    "id": tex_id,
                    "path": archive_path,
                    "contenttype": contenttype,
                },
            )
            texture_ids[archive_path] = tex_id
            debug(f"Created texture2d ID {tex_id} for {tex_type}: {archive_path}")

        return texture_ids[archive_path]

    for mat_name, pbr_info in pbr_materials.items():
        base_color_tex = pbr_info.get("base_color")
        roughness_tex = pbr_info.get("roughness")
        metallic_tex = pbr_info.get("metallic")

        # Only create pbmetallictexturedisplayproperties if we have PBR textures
        # (roughness or metallic). Base color alone uses texture2dgroup.
        if not roughness_tex and not metallic_tex:
            continue

        # Create texture2d resources for each texture type
        basecolor_texid = get_or_create_texture2d(base_color_tex, "base_color")
        roughness_texid = get_or_create_texture2d(roughness_tex, "roughness")
        metallic_texid = get_or_create_texture2d(metallic_tex, "metallic")

        # Create pbmetallictexturedisplayproperties with all available textures
        display_props_id = str(next_resource_id)
        next_resource_id += 1

        if first_display_props_id is None:
            first_display_props_id = display_props_id

        attrib = {
            "id": display_props_id,
            "name": mat_name,
        }
        if basecolor_texid:
            attrib["basecolortextureid"] = basecolor_texid
        if metallic_texid:
            attrib["metallictextureid"] = metallic_texid
        if roughness_texid:
            attrib["roughnesstextureid"] = roughness_texid

        xml.etree.ElementTree.SubElement(
            resources_element,
            f"{{{MATERIAL_NAMESPACE}}}pbmetallictexturedisplayproperties",
            attrib=attrib,
        )

        material_to_display_props[mat_name] = display_props_id
        debug(
            f"Created pbmetallictexturedisplayproperties ID {display_props_id} for '{mat_name}' "
            f"(basecolor={basecolor_texid or 'none'}, roughness={roughness_texid or 'none'}, "
            f"metallic={metallic_texid or 'none'})"
        )

    # Link basematerials to display properties
    # Note: 3MF spec allows only ONE displaypropertiesid per basematerials
    # Textured PBR takes priority over scalar PBR
    if first_display_props_id and basematerials_element is not None:
        basematerials_element.set("displaypropertiesid", first_display_props_id)
        debug(f"Linked basematerials to pbmetallictexturedisplayproperties ID {first_display_props_id}")

    return material_to_display_props, next_resource_id
