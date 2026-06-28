import unittest

from admet.engines.analyze import create_analyze_registry


class AnalyzeRegistryTests(unittest.TestCase):
    def test_registry_lists_migrated_analysis_engines(self):
        registry = create_analyze_registry()

        self.assertEqual(registry.ids(), ("cellpose", "dummy", "opencv"))

    def test_registry_creates_dummy_and_cellpose_without_heavy_runtime_imports(self):
        registry = create_analyze_registry()

        self.assertEqual(registry.create("dummy").id, "dummy")
        cellpose = registry.create("cellpose")
        self.assertEqual(cellpose.id, "cellpose")
        self.assertIn("input_dir", cellpose.settings.defaults())


if __name__ == "__main__":
    unittest.main()
