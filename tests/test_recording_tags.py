import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from admet.core.check import check_project, mend_project
from admet.core.project import ProjectStore
from admet.engines.acquisition.camera.tags import ffmpeg_metadata_args, pixel_size_um, read_recording_tag
from admet.engines.acquisition.camera.video import VideoWorker, tag_record

FFMPEG = shutil.which("ffmpeg") is not None


def tagged_avi(path: Path, record: dict, *, frames: int = 2, width: int = 8, height: int = 4) -> None:
    raw = path.with_suffix(".raw")
    raw.write_bytes(bytes(width * height * frames))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pixel_format", "gray",
                    "-video_size", f"{width}x{height}", "-framerate", "24", "-i", str(raw),
                    "-c:v", "rawvideo", "-pix_fmt", "gray", "-f", "avi", *ffmpeg_metadata_args(record), str(path)],
                   check=True)
    raw.unlink()


class TagTests(unittest.TestCase):
    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_the_record_goes_into_the_avi_and_comes_back(self):
        record = {"recording_id": "run_a_step01_20261115_101500", "project_id": "study", "step": 1,
                  "camera": {"model": "acA640-750um", "exposure_us": 150.0, "pixel_size_um": 4.8},
                  "channels": [{"label": "oil", "pressure_mbar": 1300}], "frames_written": 2, "complete": True}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.avi"
            tagged_avi(path, record)

            self.assertEqual(read_recording_tag(path), {"version": 1, **record})

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_a_video_without_a_record_reads_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.avi"
            raw = Path(tmp) / "plain.raw"
            raw.write_bytes(bytes(8 * 4))
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pixel_format", "gray",
                            "-video_size", "8x4", "-framerate", "24", "-i", str(raw), "-c:v", "rawvideo",
                            "-f", "avi", str(plain)], check=True)
            self.assertIsNone(read_recording_tag(plain))
            self.assertIsNone(read_recording_tag(raw))
            self.assertIsNone(read_recording_tag(Path(tmp) / "missing.avi"))

    def test_pixel_size_from_the_model_name(self):
        self.assertEqual(pixel_size_um("acA640-750um"), 4.8)
        self.assertEqual(pixel_size_um("Basler acA640-750um (24091234)"), 4.8)
        self.assertIsNone(pixel_size_um("unknown cam"))
        self.assertIsNone(pixel_size_um(None))


class WriterRecordTests(unittest.TestCase):
    def test_the_record_counts_captured_written_and_dropped(self):
        info = {"video": "x.avi", "width": 2, "height": 2, "fps": 24.0, "frames_captured": 10, "frames_written": 8,
                "started_at": "2026-11-15T10:00:00", "record": {"recording_id": "x", "step": 2, "project_id": "p"}}

        record = tag_record(info)

        self.assertEqual((record["frames_captured"], record["frames_written"], record["frames_dropped"]), (10, 8, 2))
        self.assertFalse(record["complete"])
        self.assertEqual((record["recording_id"], record["step"], record["project_id"]), ("x", 2, "p"))
        self.assertEqual(record["started_at"], "2026-11-15T10:00:00")
        self.assertTrue(tag_record({**info, "frames_written": 10})["complete"])

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_an_encoded_video_carries_its_record_and_needs_no_side_file(self):
        finished = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            worker = VideoWorker(tmp, "clip", 2, 2, 30.0, record={"recording_id": "clip", "project_id": "study"})
            worker.on_finished = lambda path, ok, reason: finished.set()
            self.assertTrue(worker.start())
            for value in (1, 2, 3):
                self.assertTrue(worker.write(np.full((2, 2), value, dtype=np.uint8)))
            video = Path(worker.stop())
            self.assertTrue(finished.wait(10))

            tag = read_recording_tag(video)
            self.assertEqual((tag["project_id"], tag["frames_written"], tag["complete"]), ("study", 3, True))
            self.assertEqual((tag["width"], tag["height"], tag["file_fps"]), (2, 2, 30.0))
            self.assertFalse((Path(tmp) / "clip.video.json").exists())
            self.assertFalse((Path(tmp) / "clip.raw").exists())


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class ForgottenRecordingTests(unittest.TestCase):
    def test_a_video_the_manifest_forgot_is_listed_from_its_own_record(self):
        record = {"recording_id": "run_a_step01_20261115_101500", "project_id": "study", "run_id": "run_a",
                  "step": 1, "step_name": "collect", "part": 1, "started_at": "2026-11-15T10:15:00",
                  "encoded_at": "2026-11-15T10:15:02", "frames_captured": 2, "frames_written": 2,
                  "frames_dropped": 0, "complete": True, "width": 8, "height": 4, "file_fps": 24.0,
                  "acquisition_fps": 1000.0, "camera": {"model": "acA640-750um", "exposure_us": 150.0}}
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore.create(Path(tmp) / "study", "study")
            camera = store.path / "records" / "camera"
            camera.mkdir(parents=True)
            tagged_avi(camera / "run_a_step01_20261115_101500.avi", record)

            report = check_project(store.path)
            self.assertEqual(report.unlisted, ("run_a_step01_20261115_101500.avi",))
            self.assertIn("not listed", "\n".join(report.lines()))

            mend_project(store.path)

            self.assertTrue(check_project(store.path).clean)
            listed = ProjectStore(store.path)
            video = next(file for file in listed.session.files if file.role == "control_video")
            self.assertEqual(video.path, "records/camera/run_a_step01_20261115_101500.avi")
            self.assertEqual((video.metadata["acquisition_fps"], video.metadata["step"], video.metadata["complete"]),
                             (1000.0, 1, True))
            self.assertEqual(video.metadata["camera"]["exposure_us"], 150.0)
            self.assertEqual(listed.register_tagged_recordings(), [])        # nothing left to list


if __name__ == "__main__":
    unittest.main()
