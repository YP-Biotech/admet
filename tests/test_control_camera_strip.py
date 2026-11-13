import unittest

from admet.ui.window import camera_strip


def _metadata(**overrides):
    base = {
        "camera_connected": True, "camera_live": True, "camera_frame_shape": [60, 320], "camera_fps": 4023.4,
        "camera_parameters": {"Width": {"value": 320}, "Height": {"value": 60}, "PixelFormat": {"value": "Mono8"},
                              "AcquisitionFrameRate": {"value": 4000.0}, "ExposureTime": {"value": 150.0},
                              "Gain": {"value": 0.0}, "BinningHorizontal": {"value": 1}, "BinningVertical": {"value": 1}},
    }
    base.update(overrides)
    return base


class CameraStripTests(unittest.TestCase):
    def test_a_live_camera_reads_as_one_line(self):
        self.assertEqual(camera_strip(_metadata(), "acA640-750um"),
                         "acA640-750um · 320 × 60 px · Mono8 · 4023 fps (set 4000) · exposure 150 µs · gain 0 · live")

    def test_a_recording_shows_time_frames_and_dropped(self):
        line = camera_strip(_metadata(camera_recording=True, camera_record_elapsed=72.4,
                                      camera_recorded_frames=291000, camera_dropped_frames=22))
        self.assertTrue(line.endswith("REC 01:12 · 291 000 frames · 22 dropped"), line)

    def test_no_camera(self):
        self.assertEqual(camera_strip({}), "Camera: not connected")
        self.assertTrue(camera_strip(_metadata(camera_live=False)).endswith("connected, not live"))


if __name__ == "__main__":
    unittest.main()
