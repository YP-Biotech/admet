"""A video's scale on analyze: the channel's walls measured, listed with the clicked one in the file's profile."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_scale_walls import channel


class AnalyzeScaleTests(unittest.TestCase):
    def test_a_video_without_a_measured_scale_has_its_walls_measured_and_it_comes_before_the_clicked_one(self):
        import cv2

        from admet.ui import analyze

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 24, (320, 60), False)
            for x in range(0, 320, 10):
                writer.write(channel(droplet_at=x))
            writer.release()
            old = {"optics": {"microns_per_pixel": 2.5, "method": "walls", "channel_width_um": 125.0}}
            with patch.object(analyze, "_video_record", return_value=old):
                scales = analyze._scales(SimpleNamespace(source_path=str(path)))
            self.assertEqual([origin for origin, _scale, _how in scales], ["Walls in the video", "Set on control"])
            self.assertAlmostEqual(scales[0][1], 125 / 48.5, delta=0.02)
            self.assertEqual(scales[1][1:], (2.5, "walls clicked on control"))

            new = {**old, "optics_measured": {"microns_per_pixel": 2.58, "wall_px": [4.3, 52.8],
                                              "channel_width_um": 125.0}}
            with patch.object(analyze, "_video_record", return_value=new):
                scales = analyze._scales(SimpleNamespace(source_path=str(path)))
            self.assertEqual(scales[0][:2], ("Walls at the recording's start", 2.58))
            self.assertIn("48.50 px apart", scales[0][2])


if __name__ == "__main__":
    unittest.main()
