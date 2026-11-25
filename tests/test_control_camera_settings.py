"""The camera as set is kept with the rig and set again when control opens, and every recording says how it was set."""

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.backend import DesktopBackend  # noqa: E402
from admet.ui.control import KEPT_CAMERA_SETTINGS, ControlWindow  # noqa: E402


class KeptCameraSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.api = DesktopBackend(simulated=True, home=tmp.name)
        self.addCleanup(self.api.shutdown)

    def window(self):
        window = ControlWindow(self.api)
        self.addCleanup(window.close)
        return window

    def test_exposure_gain_and_frame_set_once_are_there_the_next_time_control_opens(self):
        first = self.window()
        for name, value in (("camera_exposure_us", 59.0), ("camera_gain", 8.0), ("camera_width", 320),
                            ("camera_height", 60), ("camera_readout", "Normal")):
            first._set_value(name, value)

        kept = self.api.camera_settings
        self.assertEqual((kept["camera_exposure_us"], kept["camera_gain"], kept["camera_width"], kept["camera_height"],
                          kept["camera_readout"]), (59.0, 8.0, 320, 60, "Normal"))
        self.assertEqual(set(kept), set(KEPT_CAMERA_SETTINGS))                  # the whole camera, not only what changed
        self.assertNotIn("camera_microns_per_pixel", kept)                     # the scale is kept as optics

        second = self.window()
        self.assertEqual((second.values["camera_exposure_us"], second.values["camera_gain"],
                          second.values["camera_width"], second.values["camera_readout"]), (59.0, 8.0, 320, "Normal"))

    def test_a_kept_value_no_longer_valid_leaves_the_default_and_the_rest_still_loads(self):
        self.api.save_camera_settings({"camera_exposure_us": 59.0, "camera_readout": "Turbo", "camera_unknown": 1})
        window = self.window()
        self.assertEqual(window.values["camera_exposure_us"], 59.0)
        self.assertEqual(window.values["camera_readout"], "Fast")
        self.assertNotIn("camera_unknown", window.values)


class RecordedCameraRateTests(unittest.TestCase):
    def test_the_camera_observation_carries_the_rate_the_camera_makes_frames_at(self):
        from admet.engines.acquisition.camera.controller import CameraController

        class Camera:
            connected = True

            def get_settings(self, names):
                return {name: {"value": 59.0 if name == "ExposureTime" else 1.0} for name in names}

            def get_resulting_framerate(self):
                return 4026.1

        controller = CameraController(Camera())
        controller.read_parameters()
        observation = controller.observation()
        self.assertEqual((observation["resulting_frame_rate_hz"], observation["exposure_us"]), (4026.1, 59.0))


if __name__ == "__main__":
    unittest.main()
