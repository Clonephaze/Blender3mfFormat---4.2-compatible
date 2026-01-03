# Blender 3MF Format - Integration Test Suite

This directory contains **integration tests** for the Blender 3MF addon. All tests run directly in Blender's Python environment using the real `bpy` API - no mocks!

These tests validate **user-facing functionality** - what actually happens when users export/import 3MF files in Blender.

## 🚀 Quick Start

### Run All Tests
```powershell
# Windows (PowerShell)
.\tests\run_tests.ps1

# macOS/Linux (Bash)
./tests/run_tests.sh

# Direct invocation
blender --background --python tests/run_tests.py
```

### Run Specific Test Modules
```powershell
# Smoke tests only (fast)
blender --background --python tests/run_tests.py -- test_smoke

# Export tests only
blender --background --python tests/run_tests.py -- test_export

# Import tests only
blender --background --python tests/run_tests.py -- test_import
```

## 📋 Test Modules

Tests are organized by functionality:

- **`test_smoke.py`** - Fast smoke tests (8 tests, <2s) - Basic sanity checks
- **`test_export.py`** - Export functionality (17 tests) - Materials, transformations, archive structure
- **`test_import.py`** - Import functionality (11 tests) - Import, roundtrips, API compatibility
- **`test_unicode.py`** - Unicode character handling (20+ tests) - Chinese, Japanese, Korean, emoji support

**Total: 50+ integration tests** covering end-to-end workflows including Unicode support

## 📁 Test Structure

```
tests/
├── conftest.py           # Pytest fixtures and configuration
├── pytest.ini            # Pytest settings
├── run_pytest.py         # Python test runner
├── run_pytest.ps1        # PowerShell test runner
├── run_pytest.sh         # Bash test runner
├── test_smoke.py         # Fast smoke tests
├── test_export.py        # Export functionality tests
├── test_import.py        # Import functionality tests
├── test_unicode.py       # Unicode character handling tests
└── resources/            # Test data files
    ├── only_3dmodel_file.3mf
    ├── corrupt_archive.3mf
    └── empty_archive.zip
```

## 🔧 Setup

### Prerequisites

1. **Blender 4.2+** installed
2. **No external dependencies required** - uses only Python/Blender built-ins (unittest)

## 📝 Writing Tests

### Example Test

```python
import bpy
from test_base import Blender3mfTestCase

class MyTests(Blender3mfTestCase):
    """Test suite for my feature."""
    
    def test_export_cube(self):
        """Export a simple cube."""
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
        
        result = bpy.ops.export_mesh.threemf(filepath=str(self.temp_file))
        
        self.assertIn('FINISHED', result)
        self.assertTrue(self.temp_file.exists())
```

### Available Base Class Features

Inherit from `Blender3mfTestCase` to get:
- **`self.temp_file`** - Unique temporary .3mf file path
- **`self.test_resources_dir`** - Path to test data files  
- **`self.clean_scene()`** - Reset scene to empty
- **`self.create_red_material()`** - Create red Principled BSDF material
- **`self.create_blue_material()`** - Create blue Principled BSDF material

Scene is automatically cleaned before/after each test.

## 🎯 Running Specific Tests

```powershell
# Run specific test file
blender --background --python tests/run_tests.py -- test_export

# Run single test class
python -m unittest tests.test_export.ExportMaterialTests

# Run single test method
python -m unittest tests.test_export.ExportMaterialTests.test_export_with_none_material

# Note: unittest discovery requires Blender in background mode
```

## 🧪 Test Coverage

Current integration test coverage (36 tests):

### Smoke Tests (8 tests, <2s)
- ✅ Blender version check
- ✅ Addon import and registration
- ✅ Operators available
- ✅ Basic export/import
- ✅ Scene cleanup
- ✅ Material helpers

### Export Tests (17 tests)
- ✅ Basic export (cube, multiple objects, empty scene)
- ✅ Materials (single, multiple, None slots, mixed)
- ✅ Archive structure (valid ZIP, XML, vertices, triangles)
- ✅ Transformations (location, rotation, scale, parent-child)
- ✅ Edge cases (non-mesh objects, no faces)
- ✅ Options (selection only, modifiers)

### Import & Roundtrip Tests (11 tests)
- ✅ Basic import (valid files, errors, corrupt files)
- ✅ Roundtrips (geometry, materials, dimensions preserved)
- ✅ API compatibility (PrincipledBSDFWrapper, depsgraph, loop_triangles)

## 📊 Test Philosophy

### Integration vs Unit

**These tests (tests/)**: Validate **user-facing behavior**
- Test through public Blender operators (`bpy.ops.export_mesh.threemf()`)
- Verify end-to-end workflows work correctly
- Catch regressions in real-world usage
- Run slower (~1.5s) but always accurate

**Legacy tests (test/)**: Validate **implementation details**
- Test internal methods (`unit_scale()`, `read_content_types()`, etc.)
- Use mocks because operators can't be directly instantiated
- Verify edge cases in parsers/formatters
- Run fast (<0.1s) but artificial

### Why Both?

Import3MF and Export3MF are Blender operators (bpy_struct) - they can't be instantiated like `Export3MF()` outside of Blender's operator system. Internal methods testing requires the legacy mock-based approach in `test/`.

## 🐛 Debugging Failed Tests

```powershell
# Run with verbose output and full tracebacks
.\tests\run_pytest.ps1 -Verbose

# Run Blender in foreground to see graphics (if needed)
blender --python tests/run_pytest.py -- -v -s tests/test_export.py::test_failing_test
```

## 🔄 Relationship with Legacy Tests

The `test/` directory (legacy unit tests) and `tests/` directory (integration tests) serve **complementary purposes**:

| Aspect | Legacy (`test/`) | Integration (`tests/`) |
|--------|------------------|------------------------|
| **What** | Internal implementation | User-facing functionality |
| **How** | Mocked bpy | Real bpy in Blender |
| **Speed** | Very fast (~0.5s total) | Slower (~1.5s total) |
| **Coverage** | 158 tests, edge cases | 36 tests, workflows |
| **When** | Algorithm development | Pre-commit validation |

**Use both**: Run legacy tests for quick iteration, integration tests before committing.

## 📚 Resources

- [pytest documentation](https://docs.pytest.org/)
- [Blender Python API](https://docs.blender.org/api/current/)
- [pytest markers](https://docs.pytest.org/en/stable/example/markers.html)
- [pytest fixtures](https://docs.pytest.org/en/stable/fixture.html)

## 🤝 Contributing

When adding new tests:

1. Use descriptive test names: `test_export_with_empty_material_slots`
2. Add appropriate markers: `@pytest.mark.material`
3. Use fixtures for setup/teardown
4. Write docstrings explaining what's being tested
5. Test edge cases and error conditions

For questions or issues, check the main project README or open an issue.
