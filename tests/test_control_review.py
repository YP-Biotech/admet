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
        item = next(panel.run_list.item(i) for i in range(panel.run_list.count())
                    if panel.run_list.item(i).text().startswith("2026") or "gravimetry" in panel.run_list.item(i).text())
        item.setCheckState(Qt.CheckState.Checked)

        self.assertEqual(len(panel.shown_runs()), 2)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 2)          # 2 runs x the one recorded channel
        panel.channel_boxes[1].setChecked(False)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 0)
        self.assertIn("2 run(s)", panel.status.text())


if __name__ == "__main__":
    unittest.main()
