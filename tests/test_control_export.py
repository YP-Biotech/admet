"""The Export data page: the folder the rig remembers, the runs and where their videos are, the Export button."""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.core.home import AdmetHome  # noqa: E402
from admet.ui import theme as ui  # noqa: E402
from admet.ui.backend import DesktopBackend  # noqa: E402
from admet.ui.control import ControlWindow  # noqa: E402
from admet.ui.export import ExportPanel  # noqa: E402
from tests.test_export import finished_run  # noqa: E402


class ExportPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.api = DesktopBackend(simulated=True, home=self.root / "home")
        self.addCleanup(self.api.shutdown)

    def pump(self, seconds=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def cells(self, panel, row):
        return [panel.table.item(row, column).text() for column in range(panel.table.columnCount())]

    def test_the_page_lists_the_runs_exports_them_and_remembers_the_folder(self):
        run = finished_run(self.api.home.data_dir, "oil 3")
        server = self.root / "server"
        server.mkdir()
        panel = ExportPanel(self.api)
        self.addCleanup(panel.close)

        self.assertEqual(self.cells(panel, 0)[0], "oil 3")
        self.assertEqual(self.cells(panel, 0)[3:], ["2 here", ""])
        self.assertFalse(panel.export_button.isEnabled())                       # nowhere to send them yet
        panel.path.setText(str(server))
        panel.path.editingFinished.emit()
        self.assertEqual(AdmetHome(self.root / "home").rig()["export"]["path"], str(server))
        self.assertTrue(panel.export_button.isEnabled())

        panel.export()
        for _ in range(100):
            self.pump(0.05)
            if not panel.tasks.busy:
                break
        self.assertFalse(panel.tasks.busy)

        self.assertEqual(self.cells(panel, 0)[3], "2 on the server")
        self.assertTrue(self.cells(panel, 0)[4])
        self.assertIn("1 run(s) exported, 2 video(s)", panel.status.text())
        self.assertTrue((server / run.run_id / "run.csv").is_file())
        self.assertEqual([p for p in run.path.iterdir() if p.suffix == ".avi"], [])

    def test_the_window_has_the_export_stage_and_mounts_the_page(self):
        window = ControlWindow(self.api)
        self.addCleanup(window.close)
        window.show()
        self.pump()
        ids = [stage.id for stage in window.workflow.stages]
        self.assertLess(ids.index("review"), ids.index("export"))
        self.assertLess(ids.index("export"), ids.index("wash"))

        window._select_stage(ids.index("export"))
        self.pump()

        self.assertIsNotNone(window._export)
        self.assertTrue(window._export.isVisible())
        rows = [window._video_rows()]
        self.assertEqual(rows, [[]])


if __name__ == "__main__":
    unittest.main()
