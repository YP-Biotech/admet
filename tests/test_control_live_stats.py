import unittest

import numpy as np

from admet.engines.acquisition.camera.acquisition import as_8bit, cell_means, grid_edges, sharpness
from admet.ui.window import (
    FocusTracker,
    bytes_per_pixel,
    frame_pixel,
    heat_range,
    heat_rgb,
    live_stats,
    nerd_line,
    optics_record,
    scale_from_walls,
)


class LiveStatsTests(unittest.TestCase):
    def test_a_recording_shows_state_time_fps_frames_and_dropped(self):
        line = live_stats({"camera_connected": True, "camera_live": True, "camera_fps": 4023.4,
                           "camera_recording": True, "camera_record_elapsed": 72.4,
                           "camera_recorded_frames": 291000, "camera_dropped_frames": 22})
        self.assertEqual(line, "REC 01:12 · 4023 fps · 291 000 frames · 22 dropped")

    def test_live_and_idle_and_absent(self):
        self.assertEqual(live_stats({"camera_connected": True, "camera_live": True, "camera_fps": 750.2}),
                         "LIVE · 750 fps")
        self.assertEqual(live_stats({"camera_connected": True}), "connected · not live")
        self.assertEqual(live_stats({}), "")


class NerdModeTests(unittest.TestCase):
    def test_the_nerd_line_reads_size_format_level_exposure_gain_and_data_rate(self):
        metadata = {"camera_connected": True, "camera_frame_shape": [60, 320], "camera_fps": 4000.0,
                    "camera_parameters": {"PixelFormat": {"value": "Mono8"}, "ExposureTime": {"value": 150.0},
                                          "Gain": {"value": 6.0}}}
        self.assertEqual(nerd_line(metadata, 80.25), "320x60  Mono8  avg 80.2  exp 150us  gain 6dB  76.8MB/s")
        self.assertEqual(nerd_line({}), "")

    def test_bytes_per_pixel_by_format(self):
        self.assertEqual([bytes_per_pixel(f) for f in ("Mono8", "Mono10", "Mono10p", "Mono12p", None)],
                         [1.0, 2.0, 1.25, 1.5, 1.0])

    def test_the_nerd_line_carries_the_focus_as_a_share_of_the_best(self):
        metadata = {"camera_connected": True, "camera_frame_shape": [60, 320]}
        self.assertEqual(nerd_line(metadata, None, 0.83), "320x60  focus 83%")
        self.assertEqual(nerd_line(metadata, 80.0, None, (62.4, 97.0)), "320x60  avg 80.0  grid 62-97")

    def test_the_grid_has_about_256_cells_whatever_the_frame_size(self):
        for width, height in ((320, 60), (640, 480), (1920, 1080), (100, 100)):
            rows, cols = grid_edges(width, height)
            count = (len(rows) - 1) * (len(cols) - 1)
            self.assertTrue(230 <= count <= 290, (width, height, count))
            self.assertEqual((rows[0], rows[-1], cols[0], cols[-1]), (0, height, 0, width))
        rows, cols = grid_edges(320, 60)
        self.assertEqual((len(rows) - 1, len(cols) - 1), (7, 37))          # 8.6 px cells
        self.assertTrue(all(7 <= step <= 10 for step in np.diff(rows)))     # even, no leftover strip

    def test_the_heat_map_cells_are_the_grid_means_and_colour_blue_to_red(self):
        frame = np.full((40, 70), 50, dtype=np.uint8)
        frame[:, 32:] = 150
        cells = cell_means(frame, cells=6)              # 2 rows of 20 px, 3 columns of 23, 24 and 23 px
        self.assertEqual(cells.shape, (2, 3))
        self.assertEqual(cells[:, 0].tolist(), [50.0, 50.0])
        self.assertEqual(cells[:, 2].tolist(), [150.0, 150.0])
        self.assertAlmostEqual(cells[0, 1], (9 * 50 + 15 * 150) / 24, places=4)
        self.assertEqual(heat_range(cells), (50.0, 150.0))
        self.assertEqual(heat_range(np.full((2, 2), 80.0)), (78.0, 82.0))         # flat: no noise painting
        self.assertEqual((heat_rgb(0.0), heat_rgb(0.5), heat_rgb(1.0)), ((0, 0, 255), (0, 220, 0), (255, 0, 0)))
        self.assertEqual(cell_means(np.zeros((0, 0), dtype=np.uint8)).size, 0)

    def test_as_8bit_keeps_the_top_byte_of_a_sixteen_bit_frame(self):
        self.assertEqual(as_8bit(np.array([[0, 256], [512, 65535]], dtype=np.uint16)).tolist(), [[0, 1], [2, 255]])
        self.assertEqual(as_8bit(np.zeros((2, 2), dtype=np.uint8)).dtype, np.uint8)


def _blurred(image: np.ndarray, passes: int) -> np.ndarray:
    out = image.astype(np.float32)
    for _ in range(passes):
        out = (np.roll(out, 1, 0) + np.roll(out, -1, 0) + np.roll(out, 1, 1) + np.roll(out, -1, 1) + out) / 5
    return out.astype(np.uint8)


class SharpnessTests(unittest.TestCase):
    def setUp(self):
        y, x = np.mgrid[0:60, 0:120]
        self.sharp = np.full((60, 120), 90, dtype=np.uint8)
        self.sharp[(y - 30) ** 2 + (x - 60) ** 2 < 400] = 40      # a droplet
        self.sharp[8:10, :] = 20                                  # a channel wall
        self.sharp[50:52, :] = 20

    def test_a_sharp_frame_scores_above_its_blurred_self_and_a_flat_frame_scores_nothing(self):
        scores = [sharpness(self.sharp), sharpness(_blurred(self.sharp, 2)), sharpness(_blurred(self.sharp, 6))]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(scores[0], 2 * scores[2])
        self.assertEqual(sharpness(np.full((60, 120), 90, dtype=np.uint8)), 0.0)
        self.assertEqual(sharpness(np.zeros((3, 3), dtype=np.uint8)), 0.0)

    def test_the_score_barely_moves_with_the_gain(self):
        brighter = (self.sharp.astype(np.uint16) * 2).astype(np.uint8)
        self.assertAlmostEqual(sharpness(brighter) / sharpness(self.sharp), 1.0, delta=0.05)


class FocusTrackerTests(unittest.TestCase):
    def test_the_fraction_peaks_at_the_sharpest_position_and_falls_on_either_side(self):
        tracker = FocusTracker(window_s=0.5)
        self.assertIsNone(tracker.fraction)
        fractions = []
        for i, score in enumerate([2.0, 4.0, 8.0, 4.0, 2.0]):
            tracker.add(score, now=float(i))          # one sample a second: each one is its own window
            fractions.append(round(tracker.fraction, 2))
        self.assertEqual(fractions, [1.0, 1.0, 1.0, 0.5, 0.25])
        self.assertEqual(tracker.best, 8.0)

    def test_the_window_averages_half_a_second_and_a_reset_starts_over(self):
        tracker = FocusTracker(window_s=0.5)
        tracker.add(2.0, now=10.0)
        tracker.add(4.0, now=10.2)
        self.assertEqual(tracker.current, 3.0)
        tracker.add(4.0, now=10.8)                    # the first sample has left the window
        self.assertEqual(tracker.current, 4.0)
        tracker.reset()
        self.assertIsNone(tracker.fraction)
        self.assertEqual(tracker.best, 0.0)


class ScaleMeasurementTests(unittest.TestCase):
    def test_the_scale_is_the_channel_width_over_the_wall_distance(self):
        self.assertAlmostEqual(scale_from_walls(3, 58, 125.0), 125 / 55)
        self.assertAlmostEqual(scale_from_walls(58, 3, 125.0), 125 / 55)          # either order
        self.assertEqual(scale_from_walls(10, 10, 125.0), 0.0)
        record = optics_record(3, 58, 125.0, [60, 320], "2026-11-17T10:00:00")
        self.assertEqual((record["microns_per_pixel"], record["wall_px"], record["frame"], record["method"]),
                         (2.2727, [3.0, 58.0], [320, 60], "walls"))

    def test_a_click_on_the_widget_maps_to_the_frame_pixel(self):
        rect, shape = (10, 20, 640, 120), (60, 320)                           # the frame shown twice its size
        self.assertEqual(frame_pixel((10, 20), rect, shape), (0.0, 0.0))
        self.assertEqual(frame_pixel((330, 80), rect, shape), (160.0, 30.0))
        self.assertIsNone(frame_pixel((5, 80), rect, shape))                 # left of the frame
        self.assertIsNone(frame_pixel((330, 200), rect, shape))              # below it
        self.assertIsNone(frame_pixel((330, 80), (0, 0, 0, 0), shape))       # nothing shown yet


if __name__ == "__main__":
    unittest.main()
