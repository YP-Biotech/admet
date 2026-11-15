import unittest

from admet.ui.window import live_stats


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


if __name__ == "__main__":
    unittest.main()
