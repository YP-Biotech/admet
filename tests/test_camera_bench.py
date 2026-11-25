import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from admet.engines.acquisition.camera.bench import STAGES, report_lines, run_stage


class NumberedCamera:
    """Hands over frames as fast as asked, numbered and stamped with the time it hands them over, in
    nanoseconds as a Basler camera stamps; every 100th number never arrives, as when pylon's buffers
    overflow."""

    def __init__(self):
        self.number = 0
        self.frame = np.full((60, 320), 128, dtype=np.uint8)

    def start_grabbing(self, *, latest_only=True):
        pass

    def stop_grabbing(self):
        pass

    def grab(self, timeout_ms=5):
        self.number += 2 if self.number % 100 == 99 else 1
        return self.frame.copy(), time.perf_counter_ns(), self.number


class UnnumberedCamera(NumberedCamera):
    """A camera whose frames come without usable numbers or stamps: the same ones every time."""

    def grab(self, timeout_ms=5):
        return self.frame.copy(), 0, 0


class CameraBenchTests(unittest.TestCase):
    def test_each_stage_counts_every_hand_over_from_the_camera_to_the_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            for stage in STAGES:
                result = run_stage(NumberedCamera(), stage, 0.3, Path(folder), fps_hint=0.0, width=320, height=60)
                self.assertEqual(result.made, result.reached_python + result.camera_skipped)
                self.assertGreater(result.camera_skipped, 0)
                self.assertTrue(result.numbered)
                self.assertAlmostEqual(result.camera_fps, result.made / result.seconds, delta=0.1 * result.camera_fps)
                self.assertEqual(result.taken, result.written + result.writer_dropped)
                self.assertEqual(result.lost, result.made - result.written)
                self.assertGreater(result.grab_us_p99, 0.0)
            self.assertEqual(list(Path(folder).iterdir()), [])            # the test recordings are gone

    def test_the_report_says_where_the_frames_went(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(NumberedCamera(), "grab", 0.2, Path(folder), fps_hint=0.0)
        lines = report_lines([result])
        self.assertTrue(lines[1].startswith("grab"))
        self.assertTrue(any("never reached Python" in line for line in lines))
        self.assertFalse(any("disk fell behind" in line for line in lines))


class UnnumberedBenchTests(unittest.TestCase):
    def test_without_the_cameras_numbers_the_rate_is_the_pcs_and_nothing_is_counted_as_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(UnnumberedCamera(), "grab", 0.2, Path(folder), fps_hint=4000.0)
        self.assertFalse(result.numbered)
        self.assertEqual((result.made, result.camera_skipped), (result.reached_python, 0))
        self.assertAlmostEqual(result.camera_fps, result.reached_python / result.seconds, delta=0.05 * result.camera_fps)
        self.assertTrue(any("no usable frame numbers" in line for line in report_lines([result])))
        self.assertTrue(report_lines([result])[-1].endswith("that the PC can see"))

    def test_the_folder_for_the_test_recordings_is_made_when_it_is_missing(self):
        from types import SimpleNamespace

        from admet.engines.acquisition.camera.bench import bench

        camera = NumberedCamera()
        camera.get_resulting_framerate = lambda: 4000.0
        controller = SimpleNamespace(camera=camera, connect=lambda settings: {"camera_connect_ok": True},
                                     recording_frame_size=lambda settings: (320, 60), disconnect=lambda: None)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "not" / "there"
            results = bench({}, seconds=0.1, out_dir=out, stages=("disk",), controller=controller)
            self.assertTrue(out.is_dir())
            self.assertEqual(list(out.iterdir()), [])                                   # its test files gone again
        self.assertEqual(results[0].stage, "disk")


if __name__ == "__main__":
    unittest.main()
