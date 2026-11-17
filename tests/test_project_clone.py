import json
import tempfile
import unittest
from pathlib import Path

from admet.core.project import ProjectStore
from admet.core.service import Admet


def _filled_project(root: Path) -> ProjectStore:
    store = ProjectStore.create(root / "march", "march")
    store.update_metadata(
        acquisition_mode="camera_fluidics",
        calibration={"m1": {"profile": "water-x2", "dead_volume_ul": 12.5}},
        profiles=[{"id": "water-x2", "scale": 2.0}],
        liquids=[{"id": "beads", "viscosity_mpa_s": 1.4}],
        qt_checkup={"conditions": {"tubing": "0.5 mm"}},
        optics={"microns_per_pixel": 2.27, "channel_width_um": 125.0, "wall_px": [3.0, 58.0]},
        cache_root="C:\\Users\\ADMIN\\.admet-cache",
        analysis_setup={"matrix": [{"source": "records/camera/run.avi"}]},
    )
    (store.path / "protocols").mkdir()
    (store.path / "protocols" / "dropseq.json").write_text(json.dumps({"name": "dropseq", "steps": []}))
    video = store.records_dir / "camera" / "run.avi"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"RIFF")
    store.upsert_file_path(video, role="control_video")
    store.save()
    return store


class ProjectCloneTests(unittest.TestCase):
    def test_a_clone_keeps_the_settings_and_calibration_and_none_of_the_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _filled_project(Path(tmp))

            clone = ProjectStore.clone_settings(source, Path(tmp) / "april", "april")
            reopened = ProjectStore(clone.path)

            self.assertEqual(reopened.session.project_id, "april")
            for key in ProjectStore.SETTINGS_KEYS:
                self.assertEqual(reopened.session.metadata[key], source.session.metadata[key], key)
            self.assertNotIn("cache_root", reopened.session.metadata)
            self.assertNotIn("analysis_setup", reopened.session.metadata)
            self.assertEqual(json.loads((reopened.path / "protocols" / "dropseq.json").read_text())["name"], "dropseq")
            self.assertEqual(reopened.session.files, ())
            self.assertEqual(reopened.session.items, ())
            self.assertFalse((reopened.path / "records" / "camera").exists())

    def test_a_clone_never_replaces_an_existing_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _filled_project(Path(tmp))
            with self.assertRaises(FileExistsError):
                ProjectStore.clone_settings(source, source.path)

    def test_the_service_opens_the_clone_as_the_current_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _filled_project(Path(tmp))
            service = Admet()
            service.open_project(source.path)

            clone = service.clone_project(source.path, Path(tmp) / "may")

            self.assertEqual(service.project.path, clone.path)
            self.assertEqual(clone.session.metadata["calibration"], source.session.metadata["calibration"])


if __name__ == "__main__":
    unittest.main()
