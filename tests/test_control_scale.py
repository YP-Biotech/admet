"""µm per pixel measured on the preview: two clicks, a width, kept with the project and stamped into recordings."""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.backend import DesktopBackend  # noqa: E402
from admet.ui.control import ControlWindow, PreviewDisplay  # noqa: E402


class ScaleOnThePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.api = DesktopBackend(simulated=True)
        self.addCleanup(self.api.shutdown)
        self.api.create_project(Path(tmp.name) / "p.admetp")
        self.window = ControlWindow(self.api)
        self.addCleanup(self.window.close)
        self.window.show()
        self.pump()

    def pump(self, seconds=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def test_two_clicks_and_a_width_give_the_scale_to_the_project_and_the_recordings(self):
        window = self.window
        preview = window.preview
        self.assertIsNotNone(preview)
        preview.resize(640, 120)
        preview.set_frame(np.full((60, 320), 120, dtype=np.uint8))
        preview.repaint()
        window._ask_channel_width = lambda: 125.0

        window._begin_scale_measurement()
        window._preview_clicked(100, 3)
        self.assertEqual(preview._marks, [3.0])
        window._preview_clicked(100, 58)

        optics = self.api.optics
        self.assertAlmostEqual(optics["microns_per_pixel"], 125 / 55, places=3)
        self.assertEqual((optics["channel_width_um"], optics["wall_px"], optics["frame"]), (125.0, [3.0, 58.0], [320, 60]))
        self.assertEqual(preview._marks, [])
        self.assertAlmostEqual(self.api.engine.recording_context["optics"]["microns_per_pixel"], 125 / 55, places=3)
        self.assertAlmostEqual(self.api.engine.recording_facts()["optics"]["microns_per_pixel"], 125 / 55, places=3)

    def test_a_click_on_the_widget_reaches_the_window_in_frame_pixels(self):
        preview = PreviewDisplay()
        preview.resize(640, 120)
        preview.set_frame(np.zeros((60, 320), dtype=np.uint8))
        preview.show()
        self.pump()
        seen = []
        preview.frame_clicked.connect(lambda x, y: seen.append((x, y)))
        rect = preview.frame_rect
        point = QPointF(rect.x() + rect.width() * 0.5, rect.y() + rect.height() * 0.05)
        preview.mousePressEvent(QMouseEvent(QMouseEvent.Type.MouseButtonPress, point, preview.mapToGlobal(point.toPoint()),
                                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                            Qt.KeyboardModifier.NoModifier))
        self.assertEqual(seen, [(160, 3)])
        preview.close()

    def test_without_a_frame_the_measurement_asks_for_live_first(self):
        notes = []
        self.window._notify = lambda text, kind="primary", *, timeout_ms=3500: notes.append(text)
        self.window._begin_scale_measurement()
        self.assertTrue(notes and "Live" in notes[0])
        self.assertEqual(self.api.optics, {})


if __name__ == "__main__":
    unittest.main()
