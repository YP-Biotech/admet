import tempfile
import unittest
from pathlib import Path

import numpy as np

from admet.engines.acquisition.camera.bench import STAGES, report_lines, run_stage


class NumberedCamera:
    """Hands over frames as fast as asked, numbered and stamped as a 4000 fps camera would;
    every 100th number never arrives, as when pylon's buffers overflow."""

    def __init__(self):
        self.number = 0
        self.frame = np.full((60, 320), 128, dtype=np.uint8)

    def start_grabbing(self, *, latest_only=True):
        pass

    def stop_grabbing(self):
        pass

    def grab(self, timeout_ms=5):
        self.number += 2 if self.number % 100 == 99 else 1
        return self.frame.copy(), self.number * 250_000, self.number


class CameraBenchTests(unittest.TestCase):
    def test_each_stage_counts_every_hand_over_from_the_camera_to_the_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            for stage in STAGES:
                result = run_stage(NumberedCamera(), stage, 0.3, Path(folder), fps_hint=4000.0, width=320, height=60)
                self.assertEqual(result.made, result.reached_python + result.camera_skipped)
                self.assertGreater(result.camera_skipped, 0)
                self.assertAlmostEqual(result.camera_fps, 4000.0, delta=5.0)
                self.assertEqual(result.taken, result.written + result.writer_dropped)
                self.assertEqual(result.lost, result.made - result.written)
                self.assertGreater(result.grab_us_p99, 0.0)
            self.assertEqual(list(Path(folder).iterdir()), [])            # the test recordings are gone

    def test_the_report_says_where_the_frames_went(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(NumberedCamera(), "grab", 0.2, Path(folder), fps_hint=4000.0)
        lines = report_lines([result])
        self.assertTrue(lines[1].startswith("grab"))
        self.assertTrue(any("never reached Python" in line for line in lines))
        self.assertFalse(any("disk fell behind" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
