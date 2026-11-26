"""The Experiment page: a table of runs designed in control, and Run next running them one after another."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.backend import DesktopBackend  # noqa: E402
from admet.ui.control import ControlWindow  # noqa: E402
from admet.ui.experiment import ExperimentPanel  # noqa: E402

PROTOCOL = {
    "name": "short_run", "parameters": {"oil_flow": ["Oil flow rate, µL/min", 10]},
    "steps": [{"sensor_setpoints": {"0": "oil_flow", "1": 5, "2": 5}, "trigger_type": "time",
               "trigger_params": {"duration_s": 0.2}, "on_complete": "zero"}],
}


def pump(app, seconds=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.01)


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.api = DesktopBackend(simulated=True, home=tmp.name)
        self.addCleanup(self.api.shutdown)
        self.api.service.protocol_store().save(PROTOCOL)


class PanelTests(Case):
    def setUp(self):
        super().setUp()
        self.notes = []
        self.panel = ExperimentPanel(self.api, notify=lambda text, kind="primary": self.notes.append((text, kind)))

    def column(self, name):
        return self.panel._columns.index(name)

    def test_rows_are_added_duplicated_and_removed_and_kept_in_control_db(self):
        panel = self.panel
        experiment = panel.new_experiment("sweep")
        panel.add_run("short_run", {"oil_flow": 20})
        panel.table.selectRow(0)
        panel.duplicate_run()
        panel.add_run("short_run")
        self.assertEqual([run.run_id(experiment.code) for run in experiment.runs],
                         [f"{experiment.code}-0{n}" for n in (1, 2, 3)])
        self.assertEqual([run.parameters for run in experiment.runs], [{"oil_flow": 20}] * 3)
        self.assertEqual(panel.table.rowCount(), 3)
        self.assertIn("oil_flow", panel._columns)
        panel.table.selectRow(2)
        self.assertTrue(panel.remove_run())

        stored = self.api.service.experiments.load(experiment.code, self.api.service.protocol_document)
        self.assertEqual((stored.name, len(stored.runs)), ("sweep", 2))

    def test_an_edited_cell_is_checked_against_its_protocol_and_a_wrong_value_is_put_back(self):
        panel = self.panel
        panel.new_experiment()
        run = panel.add_run("short_run")
        cell = panel.table.item(0, self.column("oil_flow"))
        cell.setText("35")
        self.assertEqual(run.parameters, {"oil_flow": 35})
        panel.table.item(0, self.column("oil_flow")).setText("lots")
        self.assertEqual(run.parameters, {"oil_flow": 35})
        self.assertEqual(panel.table.item(0, self.column("oil_flow")).text(), "35")
        self.assertEqual(self.notes[-1][1], "danger")

    def test_a_design_comes_in_as_json_and_goes_out_again(self):
        design = {"experiment": "imported", "code": "K7Q", "protocol": "short_run",
                  "runs": [{"parameters": {"oil_flow": 12}}, {"parameters": {"oil_flow": 14}, "condition": "high"}]}
        source = self.tmp / "design.json"
        source.write_text(json.dumps(design))
        first = self.panel.import_json(source)
        self.assertEqual(first.code, "K7Q")
        self.assertEqual([run.condition for run in first.runs], ["12", "high"])
        again = self.panel.import_json(source)                 # the code is taken: a fresh one, new ids
        self.assertNotEqual(again.code, "K7Q")
        self.assertEqual(again.runs[0].run_id(again.code), f"{again.code}-01")

        target = self.tmp / "out.json"
        self.panel.export_json(target)
        written = json.loads(target.read_text())
        self.assertEqual([run["id"] for run in written["runs"]], [f"{again.code}-01", f"{again.code}-02"])

    def test_run_next_hands_over_the_next_planned_row_and_mark_records_where_it_got_to(self):
        panel = self.panel
        experiment = panel.new_experiment()
        panel.add_run("short_run")
        panel.add_run("short_run", {"oil_flow": 30})
        asked = []
        panel.run_requested.connect(lambda exp, run: asked.append(run.run_id(exp.code)))
        panel.run_next()
        code = experiment.code
        panel.mark(f"{code}-01", "done", "run_folder_1")
        panel.run_next()
        panel.mark(f"{code}-02", "failed")
        self.assertIsNone(panel.run_next())

        self.assertEqual(asked, [f"{code}-01", f"{code}-02"])
        self.assertEqual(self.panel.table.item(0, self.column("Run folder")).text(), "run_folder_1")
        self.assertEqual([run.state for run in experiment.runs], ["done", "failed"])
        self.assertEqual(self.notes[-1][1], "success")


class WindowTests(Case):
    def setUp(self):
        super().setUp()
        self.window = ControlWindow(self.api)
        self.addCleanup(self.window.close)
        self.window.show()
        pump(self.app)

    def wait(self, condition, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pump(self.app, 0.05)
            if condition():
                return
        self.fail("timed out")

    def test_run_next_runs_a_row_marks_it_done_and_asks_before_the_next(self):
        window = self.window
        self.api.call("connect_fluidics")
        self.api.call("apply_corrections")
        index = next(i for i, stage in enumerate(window.workflow.stages) if stage.id == "experiment")
        window._select_stage(index)
        pump(self.app)
        panel = window._experiment
        self.assertIsNotNone(panel)
        experiment = panel.new_experiment("two runs")
        panel.add_run("short_run", {"oil_flow": 15})
        panel.add_run("short_run", {"oil_flow": 20})
        code = experiment.code
        asked = []

        with patch.object(window, "_confirm", side_effect=lambda text, then, **_: asked.append((text, then))):
            panel.run_next()
            self.wait(lambda: experiment.runs[0].state == "done")
            self.wait(lambda: asked)

        stage = next(stage for stage in window.workflow.stages if stage.id == "experiment_run")
        editor = window.protocol_editors[stage.id]
        self.assertEqual(editor.plan["tag"], f"{code}-01")
        self.assertTrue(experiment.runs[0].run_folder)
        record = self.api.service.runs.open(experiment.runs[0].run_folder).read()
        self.assertEqual((record.get("run_code"), record.get("experiment")), (f"{code}-01", "two runs"))
        self.assertEqual(asked[0][0], f"Run {code}-01 is finished. Proceed to run {code}-02?")

        asked[0][1]()                                          # Proceed
        self.wait(lambda: experiment.runs[1].state == "done")
        self.assertEqual([s.id for s in window.workflow.stages].count("experiment_run"), 1)
        stored = self.api.service.experiments.load(code, self.api.service.protocol_document)
        self.assertEqual([run.state for run in stored.runs], ["done", "done"])


if __name__ == "__main__":
    unittest.main()
