"""The Review runs page: recorded pressure and flow from saved CSVs, offscreen."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.review import ReviewPanel, project_run, read_fluidics_csv, step_markers  # noqa: E402
from tests.test_metrology import archive  # noqa: E402


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = archive(tmp.name, "gravimetry")

    def test_a_recorded_run_reads_its_channels_and_step_marks(self):
        run = project_run(self.run, "gravimetry")
        t, pressures, flows = read_fluidics_csv(self.run / "fluidics.csv")

        # The archive recorded channel 1 only: it stays channel 1, the others are empty.
        self.assertEqual(len(run.flows), 3)
        self.assertEqual(len(run.t), len(t))
        self.assertGreater(max(flows[1]), 10)
        self.assertTrue(all(map(lambda v: v != v, flows[0])))
        starts = [label for _t, label, _color in run.markers if label.startswith("▶")]
        ends = [label for _t, label, _color in run.markers if label.startswith("■")]
        self.assertEqual(len(starts), len(ends))
        self.assertIn("▶ 1", starts)
        self.assertTrue(all(0 <= at <= t[-1] + 5 for at, _label, _color in run.markers))

    def test_markers_skip_confirm_waits_and_run_level_events(self):
        events = [{"step_name": "a", "step_index": 0, "monotonic": 101, "outcome": "running", "confirmation_message": "ok?"},
                  {"step_name": "a", "step_index": 0, "monotonic": 102, "outcome": "running", "confirmation_message": ""},
                  {"step_name": "a", "step_index": 0, "monotonic": 105, "outcome": "cancelled"},
                  {"step_name": "", "monotonic": 106, "outcome": "cancelled"}]

        self.assertEqual([(at, label) for at, label, _ in step_markers(events, 100)], [(2, "▶ 1"), (5, "■ 1")])

    def test_runs_overlay_and_channels_toggle(self):
        panel = ReviewPanel()
        self.addCleanup(panel.close)
        panel.set_project(str(self.root))
        loaded = self.root / "droplegen_20261022_101500.csv"
        shutil.copy(self.run / "fluidics.csv", loaded)
        panel.add_files([str(loaded)])
        self.assertEqual(panel.table.rowCount(), 2)                         # the project run, then the file
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

        self.assertEqual(len(panel.shown_runs()), 2)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 2)          # 2 runs x the one recorded channel
        self.assertNotEqual(panel.table.item(0, 1).foreground().color(), panel.table.item(1, 1).foreground().color())
        panel.channel_boxes[1].setChecked(False)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 0)
        self.assertIn("2 run(s)", panel.status.text())

    def test_runs_align_on_their_first_step_or_their_first_row(self):
        import numpy as np
        from admet.ui.review import ReviewRun, offsets

        recorded = ReviewRun("run", np.arange(40.0, 400.0), [], [], [(100.0, "▶ 1", ""), (300.0, "■ 1", "")])
        loaded = ReviewRun("file", np.arange(30.0, 300.0), [], [])                   # no steps: its first row

        self.assertEqual((recorded.anchor, loaded.anchor), (100.0, 30.0))
        self.assertEqual(offsets([recorded, loaded], "first_run"), [0.0, 70.0])      # file's start lands on step 1
        self.assertEqual(offsets([loaded, recorded], "first_run"), [0.0, -70.0])     # the top ticked run leads
        self.assertEqual(offsets([recorded, loaded], "stacked"), [-100.0, -30.0])
        self.assertEqual(offsets([recorded, loaded], "recorded"), [0.0, 0.0])

    def test_crosshair_reads_and_pins_a_reference(self):
        panel = ReviewPanel()
        self.addCleanup(panel.close)
        crosshair = panel.crosshairs[panel.flow_plot]
        crosshair.pin(10.0, 40.0)
        crosshair.hover(12.5, 41.5)

        self.assertIn("t=12.50 s  41.50 µL/min", crosshair.label.toPlainText())
        self.assertIn("Δt=2.50 s  Δ=1.50 µL/min", crosshair.label.toPlainText())
        crosshair.clear()
        crosshair.hover(12.5, 41.5)
        self.assertNotIn("Δt", crosshair.label.toPlainText())

    def test_ctrl_wheel_zooms_a_plot_and_the_plain_wheel_scrolls_the_page(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QScrollArea
        from admet.ui.control import _WheelGuard

        scroll = QScrollArea()
        self.addCleanup(scroll.close)
        panel = ReviewPanel()
        scroll.setWidget(panel)
        guard = _WheelGuard(scroll)

        def wheel(modifiers):
            return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120),
                               Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False)

        viewport = panel.flow_plot.viewport()
        self.assertFalse(guard.eventFilter(viewport, wheel(Qt.KeyboardModifier.ControlModifier)))
        self.assertFalse(guard.eventFilter(viewport, wheel(Qt.KeyboardModifier.MetaModifier)))
        self.assertTrue(guard.eventFilter(viewport, wheel(Qt.KeyboardModifier.NoModifier)))


if __name__ == "__main__":
    unittest.main()
