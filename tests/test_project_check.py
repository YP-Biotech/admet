import json
import tempfile
import unittest
from pathlib import Path

from admet.core.check import check_project, mend_project
from admet.core.project import ProjectStore
from admet.core.session import load_session, validate_session


class ProjectCheckTests(unittest.TestCase):
    def test_a_dangling_file_name_is_found_and_mended(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore.create(Path(tmp) / "study", "study")
            manifest = store.path / "manifest.json"
            data = json.loads(manifest.read_text())
            data["files"] = [{"id": "kept", "path": "records/kept.avi", "role": "control_video"}]
            data["items"] = [{"id": "analysis", "project_type": "analysis_run", "engine": "analyze",
                              "files": ["kept", "analysis-opencv-gone"]}]
            manifest.write_text(json.dumps(data))                # written behind the validator's back

            report = check_project(store.path)
            self.assertEqual(report.dangling, {"analysis": ("analysis-opencv-gone",)})
            self.assertIn("analysis-opencv-gone", "\n".join(report.lines()))

            mend_project(store.path)

            self.assertTrue(check_project(store.path).clean)
            validate_session(load_session(store.path))          # what control's save needs
            self.assertEqual(json.loads(manifest.read_text())["items"][0]["files"], ["kept"])
            self.assertTrue((store.path / "manifest.json.bak").is_file())

    def test_a_clean_project_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore.create(Path(tmp) / "study", "study")
            self.assertTrue(check_project(store.path).clean)
            self.assertIn("every item names only files", check_project(store.path).lines()[0])


if __name__ == "__main__":
    unittest.main()
