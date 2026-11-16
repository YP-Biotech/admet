import unittest

import numpy as np

from admet.engines.acquisition.camera.acquisition import as_8bit
from admet.ui.window import bytes_per_pixel, live_stats, nerd_line


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
        self.assertEqual(nerd_line(metadata, 80.25),
                         "320 × 60 px · Mono8 · mean 80.2 · exposure 150 µs · gain 6 dB · 76.8 MB/s")
        self.assertEqual(nerd_line({}), "")

    def test_bytes_per_pixel_by_format(self):
        self.assertEqual([bytes_per_pixel(f) for f in ("Mono8", "Mono10", "Mono10p", "Mono12p", None)],
                         [1.0, 2.0, 1.25, 1.5, 1.0])

    def test_as_8bit_keeps_the_top_byte_of_a_sixteen_bit_frame(self):
        self.assertEqual(as_8bit(np.array([[0, 256], [512, 65535]], dtype=np.uint16)).tolist(), [[0, 1], [2, 255]])
        self.assertEqual(as_8bit(np.zeros((2, 2), dtype=np.uint8)).dtype, np.uint8)


if __name__ == "__main__":
    unittest.main()
