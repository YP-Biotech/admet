"""The scale from the channel's own walls: found on the empty channel, not clicked."""

import unittest
from types import SimpleNamespace

import numpy as np

from admet.engines.opencv.scale import background, scale_from_background, wall_rows


def channel(top=4.3, bottom=52.8, height=60, width=320, depth=50.0, droplet_at=None):
    """An empty channel 210 bright, with a dark wall line, a few px wide, at each of two rows."""
    rows = np.arange(height, dtype=float)[:, None]
    image = 210 - depth * (np.exp(-((rows - top) / 1.5) ** 2) + np.exp(-((rows - bottom) / 1.5) ** 2))
    image = np.repeat(image, width, axis=1)
    if droplet_at is not None:                                       # a dark ring passing through
        yy, xx = np.mgrid[:height, :width]
        ring = np.abs(np.hypot(xx - droplet_at, yy - 28) - 24) < 3
        image[ring] = 120
    return np.clip(image, 0, 255).astype(np.uint8)


class WallScaleTests(unittest.TestCase):
    def test_the_walls_are_placed_to_a_fraction_of_a_pixel_and_give_the_scale(self):
        top, bottom = wall_rows(channel())
        self.assertAlmostEqual(top, 4.3, delta=0.15)
        self.assertAlmostEqual(bottom, 52.8, delta=0.15)
        scale = scale_from_background(channel(), 125.0)
        self.assertAlmostEqual(scale["microns_per_pixel"], 125 / 48.5, delta=0.01)
        self.assertEqual((scale["channel_width_um"], scale["frame"], scale["method"]), (125.0, [320, 60], "walls_auto"))

    def test_droplets_passing_are_gone_from_the_background_the_walls_are_found_on(self):
        frames = [channel(droplet_at=x) for x in range(30, 300, 30)] + [channel()] * 4
        self.assertAlmostEqual(scale_from_background(background(frames), 125.0)["microns_per_pixel"], 125 / 48.5,
                               delta=0.01)

    def test_no_scale_rather_than_a_wrong_one(self):
        self.assertIsNone(scale_from_background(np.full((60, 320), 200, np.uint8), 125.0))        # no walls
        self.assertIsNone(scale_from_background(channel(depth=4.0), 125.0))                       # too faint
        self.assertIsNone(scale_from_background(channel(top=0.0), 125.0))                         # cut off at the edge
        self.assertIsNone(scale_from_background(channel(top=25, bottom=33), 125.0))               # too close
        self.assertIsNone(scale_from_background(channel(), 0.0))                                  # no width known


class ControllerScaleTests(unittest.TestCase):
    def test_the_preview_frames_kept_give_the_scale_and_a_new_frame_size_starts_over(self):
        from admet.engines.acquisition.camera.controller import CameraController

        controller = CameraController(SimpleNamespace(connected=True))
        self.assertIsNone(controller.measured_scale(125.0))                                       # nothing seen yet
        for x in range(30, 300, 25):
            controller._scale_frame_at = 0.0
            controller._on_frame(channel(droplet_at=x))
        self.assertAlmostEqual(controller.measured_scale(125.0)["microns_per_pixel"], 125 / 48.5, delta=0.01)
        controller._scale_frame_at = 0.0
        controller._on_frame(np.zeros((120, 320), np.uint8))
        self.assertIsNone(controller.measured_scale(125.0))


if __name__ == "__main__":
    unittest.main()
