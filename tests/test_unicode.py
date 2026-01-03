"""
Unicode character tests for Blender 3MF addon.

Tests to ensure non-ASCII characters (Chinese, Japanese, Korean, etc.)
are properly handled throughout import/export operations.
"""

import bpy
import unittest
import zipfile
import xml.etree.ElementTree as ET
from test_base import Blender3mfTestCase


class UnicodeObjectNamesTests(Blender3mfTestCase):
    """Test Unicode characters in object names."""

    def test_export_chinese_object_name(self):
        """Export object with Chinese characters in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "丁七亇三角形"

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

        # Verify XML contains the Chinese name correctly encoded
        with zipfile.ZipFile(self.temp_file, 'r') as archive:
            model_data = archive.read('3D/3dmodel.model')
            root = ET.fromstring(model_data)

            ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
            objects = root.findall('.//m:object', ns)
            self.assertGreater(len(objects), 0)

            # Find object with our name - check both with and without namespace prefix
            found = False
            for obj in objects:
                name = obj.get('{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}name') or obj.get('name')
                if name == "丁七亇三角形":
                    found = True
                    break
            self.assertTrue(found, "Chinese object name not found in exported XML")

    def test_export_japanese_object_name(self):
        """Export object with Japanese characters in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "立方体オブジェクト"

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_korean_object_name(self):
        """Export object with Korean characters in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "정육면체객체"

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_emoji_object_name(self):
        """Export object with emoji in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "Cube🎨✨🚀"

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_mixed_unicode_object_name(self):
        """Export object with mixed Unicode characters in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "Test测试テストДомΣύστημα"

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())


class UnicodeMaterialNamesTests(Blender3mfTestCase):
    """Test Unicode characters in material names."""

    def test_export_chinese_material_name(self):
        """Export material with Chinese characters in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object

        mat = bpy.data.materials.new("红色材料")
        mat.use_nodes = True
        cube.data.materials.append(mat)

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

        # Verify XML contains the Chinese material name
        with zipfile.ZipFile(self.temp_file, 'r') as archive:
            model_data = archive.read('3D/3dmodel.model')
            root = ET.fromstring(model_data)

            ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
            materials = root.findall('.//m:base', ns)

            # Find material with our name - check both with and without namespace prefix
            found = False
            for mat in materials:
                name = mat.get('{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}name')
                name = name or mat.get('name')
                if name == "红色材料":
                    found = True
                    break
            self.assertTrue(found, "Chinese material name not found in exported XML")

    def test_export_japanese_material_name(self):
        """Export material with Japanese characters in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object

        mat = bpy.data.materials.new("青いマテリアル")
        mat.use_nodes = True
        cube.data.materials.append(mat)

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_multiple_unicode_materials(self):
        """Export multiple materials with different Unicode names."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object

        mat1 = bpy.data.materials.new("材料一")
        mat1.use_nodes = True
        mat2 = bpy.data.materials.new("素材二")
        mat2.use_nodes = True

        cube.data.materials.append(mat1)
        cube.data.materials.append(mat2)

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())


class UnicodeMetadataTests(Blender3mfTestCase):
    """Test Unicode characters in metadata."""

    def test_export_unicode_custom_property(self):
        """Export object with Unicode custom property."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object

        # Add custom property with Unicode value
        cube["作者"] = {
            "datatype": "xs:string",
            "preserve": True,
            "value": "张三"
        }

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_unicode_partnumber(self):
        """Export object with Unicode partnumber."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object

        # Add partnumber with Unicode
        cube["3mf:partnumber"] = "零件-001-型号甲"

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

        # Verify XML contains the Unicode partnumber
        with zipfile.ZipFile(self.temp_file, 'r') as archive:
            model_data = archive.read('3D/3dmodel.model')
            root = ET.fromstring(model_data)

            ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
            items = root.findall('.//m:item', ns)

            # Check if any item has the Unicode partnumber - check both with and without namespace prefix
            found = False
            for item in items:
                partnumber = item.get('{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}partnumber')
                partnumber = partnumber or item.get('partnumber')
                if partnumber == "零件-001-型号甲":
                    found = True
                    break
            self.assertTrue(found, "Unicode partnumber not found in exported XML")


class UnicodeRoundtripTests(Blender3mfTestCase):
    """Test Unicode characters survive export and import cycles."""

    def test_roundtrip_chinese_object_name(self):
        """Verify Chinese object name survives roundtrip."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        original_name = "测试立方体"
        cube.name = original_name

        # Export
        bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        # Clear scene
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()

        # Import back
        result = bpy.ops.import_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertGreater(len(bpy.data.objects), 0)

        # Verify name preserved
        imported = bpy.data.objects[0]
        self.assertEqual(imported.name, original_name)

    def test_roundtrip_japanese_material_name(self):
        """Verify Japanese material name survives roundtrip."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object

        original_mat_name = "赤い素材"
        mat = bpy.data.materials.new(original_mat_name)
        mat.use_nodes = True
        cube.data.materials.append(mat)

        # Export
        bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        # Clear scene
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()

        # Import back
        result = bpy.ops.import_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)

        # Verify material name preserved (Blender may add .001 suffix for duplicates)
        imported = bpy.data.objects[0]
        if imported.data.materials:
            imported_mat_name = imported.data.materials[0].name
            # Check if it's either exact match or with Blender's auto-suffix
            self.assertTrue(
                imported_mat_name == original_mat_name or imported_mat_name.startswith(original_mat_name + '.'),
                f"Material name '{imported_mat_name}' doesn't match original '{original_mat_name}'"
            )

    def test_roundtrip_unicode_metadata(self):
        """Verify Unicode metadata survives roundtrip."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object

        # Add metadata with Unicode
        metadata_key = "제목"
        metadata_value = "테스트 객체"
        cube[metadata_key] = {
            "datatype": "xs:string",
            "preserve": True,
            "value": metadata_value
        }

        # Export
        bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        # Clear scene
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()

        # Import back
        result = bpy.ops.import_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)

        # Verify metadata preserved
        imported = bpy.data.objects[0]
        self.assertIn(metadata_key, imported.keys())
        self.assertEqual(imported[metadata_key]["value"], metadata_value)

    def test_roundtrip_multiple_unicode_objects(self):
        """Verify multiple objects with Unicode names survive roundtrip."""
        objects_data = [
            ("中文对象", (0, 0, 0)),
            ("日本語オブジェクト", (3, 0, 0)),
            ("한글 객체", (-3, 0, 0)),
            ("Объект", (0, 3, 0)),
        ]

        for name, location in objects_data:
            bpy.ops.mesh.primitive_cube_add(location=location)
            cube = bpy.context.object
            cube.name = name

        # Export
        bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        # Clear scene
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()

        # Import back
        result = bpy.ops.import_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertEqual(len(bpy.data.objects), len(objects_data))

        # Verify all names are preserved (may be in different order)
        imported_names = {obj.name for obj in bpy.data.objects}
        original_names = {name for name, _ in objects_data}
        self.assertEqual(imported_names, original_names)


class UnicodeEdgeCasesTests(Blender3mfTestCase):
    """Test edge cases with Unicode characters."""

    def test_export_rtl_text_object_name(self):
        """Export object with right-to-left text in name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "مكعب اختبار"  # Arabic

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_combining_characters(self):
        """Export object with combining Unicode characters."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "Café"  # Contains combining character é

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_surrogate_pair_emoji(self):
        """Export object with emoji requiring surrogate pairs."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        cube.name = "Test👨‍👩‍👧‍👦"  # Family emoji with zero-width joiners

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())

    def test_export_very_long_unicode_name(self):
        """Export object with very long Unicode name."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        cube = bpy.context.object
        # Create a long name with repeating Unicode characters
        cube.name = "测试" * 50  # 100 Chinese characters

        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))

        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())


if __name__ == '__main__':
    unittest.main()
