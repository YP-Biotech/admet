"""The Review runs page: recorded pressure and flow from saved CSVs, offscreen."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.core import rundb
from admet.core.runs import Run
from tests.rundb_helpers import set_timeline, timeline_text
from admet.ui import theme as ui  # noqa: E402
from admet.ui.review import ReviewPanel, project_run, read_fluidics, step_markers  # noqa: E402
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
        Run(self.run).update(condition="oil")                                    # planned into a set

    def test_a_recorded_run_reads_its_channels_and_step_marks(self):
        run = project_run(self.run, "gravimetry")
        t, pressures, flows = read_fluidics(self.run / "run.db")

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
        loaded = self.root / "droplegen_20261022_101500.db"
        shutil.copy(self.run / "run.db", loaded)
        panel.add_files([str(loaded)])
        self.assertEqual(panel.table.rowCount(), 2)                         # the project run, then the file
        self.assertEqual([panel.table.item(r, 3).text() for r in (0, 1)], ["oil", ""])   # the run's set; a file has none
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

        self.assertEqual(len(panel.shown_runs()), 2)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 2)          # 2 runs x the one recorded channel
        self.assertNotEqual(panel.table.item(0, 1).foreground().color(), panel.table.item(1, 1).foreground().color())
        panel.channel_boxes[1].setChecked(False)
        self.assertEqual(len(panel.flow_plot.listDataItems()), 0)
        self.assertIn("2 run(s)", panel.status.text())

    def test_first_run_moves_whole_runs_and_stacked_overlays_every_step(self):
        import math
        import numpy as np
        from admet.ui.review import ReviewRun, placements, segments

        recorded = ReviewRun("run", np.arange(40.0, 500.0), [], [],
                             [(100.0, "▶ 1", ""), (300.0, "■ 1", ""), (320.0, "▶ 2", ""), (420.0, "■ 2", "")])
        loaded = ReviewRun("file", np.arange(30.0, 300.0), [], [])                   # no steps: one segment
        whole = (-math.inf, math.inf)

        self.assertEqual((recorded.anchor, loaded.anchor), (100.0, 30.0))
        self.assertEqual(segments(recorded), [(100.0, 300.0), (320.0, 420.0)])
        # Between runs only: each run whole, its first step on the first run's first step.
        self.assertEqual(placements([recorded, loaded], "first_run"), [[(*whole, 0.0)], [(*whole, 70.0)]])
        # Within runs too: every step of every run starts at 0.
        self.assertEqual(placements([recorded, loaded], "stacked"),
                         [[(100.0, 300.0, -100.0), (320.0, 420.0, -320.0)], [(30.0, 299.0, -30.0)]])
        self.assertEqual(placements([recorded, loaded], "recorded"), [[(*whole, 0.0)], [(*whole, 0.0)]])

    def test_stacked_draws_one_curve_per_step(self):
        panel = ReviewPanel()
        self.addCleanup(panel.close)
        panel.set_project(str(self.root))
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        steps = len([1 for _t, label, _c in panel.shown_runs()[0][1].markers if label.startswith("■")])

        self.assertEqual(len(panel.flow_plot.listDataItems()), 1)                   # first_run: the whole run
        panel.align.setCurrentIndex(panel.align.findData("stacked"))
        self.assertEqual(len(panel.flow_plot.listDataItems()), steps)
        self.assertFalse([i for i in panel.flow_plot.items() if getattr(i, "step_marker", False)])

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


class ReviewActionBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def pump(self, seconds=0.2):
        import time

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def test_load_file_and_reset_view_are_the_stages_actions_not_buttons_on_the_page(self):
        from unittest.mock import patch

        from PySide6.QtWidgets import QPushButton

        from admet.ui.backend import DesktopBackend
        from admet.ui.control import ControlWindow

        with tempfile.TemporaryDirectory() as tmp:
            api = DesktopBackend(simulated=True, home=Path(tmp) / "home")
            self.addCleanup(api.shutdown)
            window = ControlWindow(api)
            self.addCleanup(window.close)
            window.show()
            self.pump()
            ids = [stage.id for stage in window.workflow.stages]
            window._select_stage(ids.index("review"))
            self.pump()
            stage = window.workflow.stages[ids.index("review")]

            specs = {spec[0]: spec for spec in window._action_button_specs(stage)}
            self.assertEqual([spec[0] for spec in window._action_button_specs(stage)][:2], ["Load file…", "Reset view"])
            self.assertTrue(specs["Load file…"][2] and specs["Reset view"][2])
            panel = window._review
            self.assertEqual([b.text() for b in panel.findChildren(QPushButton) if b.text() in ("Load file…", "Reset view")], [])
            with patch.object(panel, "load_files") as load, patch.object(panel, "reset_view") as reset:
                specs["Load file…"][1]()
                specs["Reset view"][1]()
            load.assert_called_once()
            reset.assert_called_once()


if __name__ == "__main__":
    unittest.main()


def qc_run(root: Path, run_id: str = "run_q") -> Path:
    """A run whose timeline file holds readings and QC rows: one 20 s collection of 1170 droplets a second."""

    directory = Path(root) / "runs" / run_id
    directory.mkdir(parents=True)
    qc_columns = ["recording_id", "step", "part", "rate_hz", "rate_half", "count", "diameter", "diameter_unit",
                  "diameter_half", "cv_percent", "cv_half", "n", "bursts", "speed_px_frame", "spacing_px", "state"]
    fluidics_columns = ["pressure_0_mbar", "pressure_1_mbar", "pressure_2_mbar", "flow_0_ul_min", "flow_1_ul_min",
                        "flow_2_ul_min"]
    rows = []
    for tick in range(81):                                                      # 10:00:00 to 10:00:40, every 0.5 s
        seconds = tick * 0.5
        on = 10 <= seconds <= 30
        rows.append((seconds, [f"10:00:{seconds:06.3f}", f"{seconds:.3f}", 1300 if on else 0, 420 if on else 0,
                               410 if on else 0, 300 if on else 0, 40 if on else 0, 40 if on else 0,
                               *[""] * len(qc_columns)]))
    for k, second in enumerate(range(11, 30)):
        at = second + 0.1
        rows.append((at, [f"10:00:{at:06.3f}", f"{at:.3f}", *[""] * len(fluidics_columns), f"{run_id}_step01", 1, 1,
                          1170, 21 if k > 2 else "", 1170 * (k + 1), 124.0, "um", 0.5, 2.7, 0.3, 200, k + 1, 48, 128,
                          "SETTLING" if k < 2 else "STABLE"]))
    set_timeline(directory / "run.db", timeline_text([row for _at, row in sorted(rows, key=lambda item: item[0])],
                                                     ["timestamp", "elapsed_s", *fluidics_columns, *qc_columns]))
    events = [{"step_index": 0, "step_name": "collect", "state": "running", "outcome": "running", "monotonic": 110},
              {"step_index": 0, "step_name": "collect", "state": "running", "outcome": "completed", "monotonic": 130}]
    rundb.write_record(directory / "run.db", {
        "run_id": run_id, "state": "completed", "executed_at": "2026-11-17T10:00:00", "completed_at": "2026-11-17T10:00:40",
        "events": events,
        "artifacts": {"timeline": "run.db", "polling_origin_monotonic": 100, "recording_closed": True}})
    return directory


class DropletQCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = qc_run(self.root)

    def test_the_qc_rows_land_on_the_fluidics_clock_with_the_expected_rate_from_the_flows(self):
        from admet.core.droplets import expected_rate_hz

        run = project_run(self.run, "run_q")
        self.assertEqual(len(run.t), 81)                                                   # QC rows are not readings

        self.assertIsNotNone(run.qc)
        self.assertEqual([round(t, 1) for t in run.qc.t[:3]], [11.1, 12.1, 13.1])            # wall clock → elapsed_s
        self.assertEqual(run.qc.diameter_unit, "um")
        self.assertAlmostEqual(run.qc.expected[5], expected_rate_hz(80.0, 124.0), delta=1)   # cells + beads, no oil
        self.assertTrue(all(s == "STABLE" for s in run.qc.state[2:]))
        self.assertEqual(run.qc.recording[0], "run_q_step01")
        self.assertEqual([(at, label) for at, label, _ in run.markers], [(10, "▶ 1"), (30, "■ 1")])

    def test_the_page_draws_the_rate_its_band_the_expected_line_and_the_cv_and_fills_the_columns(self):
        from admet.ui.review import qc_cells

        panel = ReviewPanel()
        self.addCleanup(panel.close)
        panel.set_project(str(self.root))
        self.assertEqual(panel.table.rowCount(), 1)
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

        run = panel.shown_runs()[0][1]
        self.assertEqual(len(panel.droplet_plot.listDataItems()), 2)            # the rate and the expected line
        self.assertTrue(any(getattr(item, "qc_item", False) for item in panel.droplet_plot.items()))   # the band
        self.assertEqual(len(panel.cv_plot.listDataItems()), 2)                 # SETTLING, then STABLE
        self.assertEqual(qc_cells(run.qc), ("22 230", "1170", "2.7 STABLE"))
        self.assertEqual([panel.table.item(0, c).text() for c in (7, 8, 9)], ["22 230", "1170", "2.7 STABLE"])
        self.assertEqual(panel.table.horizontalHeaderItem(9).text(), "CV, %")

        panel.align.setCurrentIndex(1)                                           # stacked: the step from 0
        self.assertEqual(len(panel.droplet_plot.listDataItems()), 2)
        panel.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(len(panel.droplet_plot.listDataItems()), 0)
        self.assertFalse(any(getattr(item, "qc_item", False) for item in panel.droplet_plot.items()))
