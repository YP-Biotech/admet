"""One home for the rig: ~/.admet, or ADMET_HOME, or the one asked for, with rig.json in its control room."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.core.home import RIG_KEYS, AdmetHome
from admet.core.service import Admet
from tests.sdk import needs_fluigent_sdk


class ResolveTests(unittest.TestCase):
    def test_the_home_asked_for_wins_then_the_environment_then_the_user_home(self):
        with patch.dict(os.environ, {"ADMET_HOME": "/env/admet"}):
            self.assertEqual(AdmetHome.resolve("/asked").root, Path("/asked"))
            self.assertEqual(AdmetHome.resolve().root, Path("/env/admet"))
        with patch.dict(os.environ):
            os.environ.pop("ADMET_HOME", None)
            self.assertEqual(AdmetHome.resolve().root, Path.home() / ".admet")

    def test_ensure_makes_the_three_rooms(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = AdmetHome(Path(tmp) / "h").ensure()

            self.assertEqual({p.name for p in home.root.iterdir()}, {"control", "data", "analyze"})
            self.assertEqual(home.rig_path, home.root / "control" / "rig.json")


class RigTests(unittest.TestCase):
    def test_settings_round_trip_and_none_removes_a_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = AdmetHome(tmp)
            self.assertEqual(home.rig(), {})

            home.update_rig(acquisition_mode="camera_fluidics", optics={"microns_per_pixel": 1.14})
            self.assertEqual(AdmetHome(tmp).rig(),
                             {"acquisition_mode": "camera_fluidics", "optics": {"microns_per_pixel": 1.14}})
            home.update_rig(optics=None)

            self.assertEqual(AdmetHome(tmp).rig(), {"acquisition_mode": "camera_fluidics"})
            self.assertEqual(list(home.control_dir.glob("*.tmp")), [])             # written whole, then moved

    def test_only_rig_settings_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyError) as caught:
                AdmetHome(tmp).update_rig(colour="blue")

            self.assertIn("colour", str(caught.exception))
            self.assertEqual(set(RIG_KEYS),
                             {"acquisition_mode", "calibration", "profiles", "liquids", "optics", "qt_checkup", "export"})

    def test_a_damaged_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = AdmetHome(tmp).ensure()
            home.rig_path.write_text("{not json", encoding="utf-8")

            self.assertEqual(home.rig(), {})


class ServiceTests(unittest.TestCase):
    def test_rig_settings_outlive_the_service_and_the_engine_learns_the_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet(home=tmp)
            admet.update_rig(optics={"microns_per_pixel": 1.14, "channel_width_um": 100.0}, acquisition_mode="camera_fluidics")

            engine = admet.engine("acquisition")
            self.assertEqual(engine.recording_context["optics"]["microns_per_pixel"], 1.14)
            later = Admet(home=tmp)
            self.assertEqual(later.rig()["acquisition_mode"], "camera_fluidics")
            self.assertEqual(later.engine("acquisition").recording_context["optics"]["channel_width_um"], 100.0)

    def test_an_unknown_scale_is_not_pushed_to_the_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet(home=tmp)
            admet.update_rig(optics={"microns_per_pixel": 0.0})

            self.assertEqual(admet.engine("acquisition").recording_context, {})

    @needs_fluigent_sdk
    def test_a_recording_lands_under_the_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet(home=tmp)
            admet.do("connect_fluidics", {"simulated": True})
            try:
                started = admet.do("start_recording", {"recording_label": "home"})
                time.sleep(0.2)
                admet.do("stop_recording")
            finally:
                admet.do("disconnect_fluidics")

            self.assertTrue(Path(started["csv_path"]).is_relative_to(Path(tmp) / "data"))
            self.assertEqual(admet.describe_home()["data"], str(Path(tmp) / "data"))


class WindowTests(unittest.TestCase):
    def test_the_window_shows_the_home_and_offers_no_project(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from admet.ui.backend import DesktopBackend
        from admet.ui.control import ControlWindow

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            api = DesktopBackend(simulated=True, home=tmp)
            self.addCleanup(api.shutdown)
            window = ControlWindow(api)
            self.addCleanup(window.close)
            window.show()
            app.processEvents()

            self.assertEqual(window._project_text(), f"Home: {Path(tmp)}")
            self.assertTrue(window._project_ready())
            for gone in ("_new_project", "_clone_project", "_show_project_menu", "_save_project"):
                self.assertFalse(hasattr(window, gone), gone)
            self.assertEqual(window.project_path, Path(tmp) / "data")


if __name__ == "__main__":
    unittest.main()
