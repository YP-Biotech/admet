"""A video's blur share on analyze: measured on its own droplets, cached, ready to be read with."""

import tempfile
import unittest
from pathlib import Path

from tests.test_blur import smeared_frame


class VideoBlurShareTests(unittest.TestCase):
    def test_the_share_is_measured_on_the_videos_droplets(self):
        import cv2

        from admet.engines.opencv.detection import detect
        from admet.ui.analyze import _video_blur_share

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 24, (320, 60), False)
            frames = [smeared_frame(10.0, x0=x0) for x0 in range(90, 230, 2)] * 3
            frames += [smeared_frame(0.0, x0=-100)] * 60                                 # the empty channel
            for frame in frames:
                writer.write(frame)
            writer.release()
            measured = _video_blur_share(str(path), 10.0, (("k_sigma", 4.0), ("min_area", 800.0), ("satellite_px", 13.0)))

        background = smeared_frame(0.0, x0=-100)
        still = detect(smeared_frame(0.0), background, {})["properties"][0]["bbox_width"]
        seen = detect(smeared_frame(10.0), background, {})["properties"][0]["bbox_width"]
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured["share"], (seen - still) / 10.0, delta=0.15)
        self.assertEqual(measured["blur_px"], 10.0)


if __name__ == "__main__":
    unittest.main()
