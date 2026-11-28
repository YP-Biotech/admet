"""The Experiment page: a table of runs designed in control, and Run next running them one after another."""

import json
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.backend import DesktopBackend  # noqa: E402
from admet.ui.control import ControlWindow  # noqa: E402
from admet.ui.experiment import HEADERS, ExperimentPanel  # noqa: E402

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


class PanelTests(Case):
    def setUp(self):
        super().setUp()
        self.notes = []
        self.panel = ExperimentPanel(self.api, notify=lambda text, kind="primary": self.notes.append((text, kind)))

    def cell(self, row, header):
        return self.panel.table.item(row, HEADERS.index(header))

    def test_rows_are_added_duplicated_and_removed_and_kept_in_control_db(self):
        panel = self.panel
        experiment = panel.new_experiment("sweep")
        panel.add_run(oil=250)
        panel.table.selectRow(0)
        panel.duplicate_run()
        panel.add_run()
        self.assertEqual([run.run_id(experiment.code) for run in experiment.runs],
                         [f"{experiment.code}-0{n}" for n in (1, 2, 3)])
        self.assertEqual([run.oil for run in experiment.runs], [250, 250, 250])
        self.assertEqual(panel.table.rowCount(), 3)
        self.assertEqual(self.cell(0, "Condition").text(), "250/40/40")               # its setpoints, greyed
        panel.table.selectRow(2)
        self.assertTrue(panel.remove_run())

        stored = self.api.service.experiments.load(experiment.code)
        self.assertEqual((stored.name, len(stored.runs)), ("sweep", 2))

    def test_an_edited_cell_is_checked_and_a_value_that_cannot_run_is_put_back(self):
        panel = self.panel
        panel.new_experiment()
        run = panel.add_run()
        self.cell(0, "Oil").setText("350")
        self.cell(0, "Mode").setText("p")
        self.cell(0, "Stop s").setText("45")
        self.cell(0, "Video").setText("yes")
        self.assertEqual((run.oil, run.mode, run.stop_s, run.video), (350, "pressure", 45, True))
        self.assertEqual(self.cell(0, "Condition").text(), "350/40/40 mbar")
        self.cell(0, "Replicates").setText("many")
        self.assertEqual(run.replicates, 3)
        self.assertEqual(self.cell(0, "Replicates").text(), "3")
        self.assertEqual(self.notes[-1][1], "danger")
        self.cell(0, "Stop oil µL").setText("")
        self.cell(0, "Stop s").setText("")                                              # the last stop: refused
        self.assertEqual((run.stop_oil_ul, run.stop_s), (None, 45))

    def test_a_design_comes_in_as_json_and_goes_out_again(self):
        design = {"experiment": "imported", "code": "K7Q", "replicates": 2,
                  "runs": [{"oil": 300}, {"oil": 350, "condition": "high"}]}
        source = self.tmp / "design.json"
        source.write_text(json.dumps(design))
        first = self.panel.import_json(source)
        self.assertEqual(first.code, "K7Q")
        self.assertEqual([run.condition_text() for run in first.runs], ["300/40/40", "high"])
        again = self.panel.import_json(source)                 # the code is taken: a fresh one, new ids
        self.assertNotEqual(again.code, "K7Q")
        self.assertEqual(again.runs[0].run_id(again.code), f"{again.code}-01")

        target = self.tmp / "out.json"
        self.panel.export_json(target)
        written = json.loads(target.read_text())
        self.assertEqual([(run["id"], run["oil"], run["replicates"]) for run in written["runs"]],
                         [(f"{again.code}-01", 300, 2), (f"{again.code}-02", 350, 2)])

    def test_run_next_hands_over_the_next_planned_row_and_mark_records_where_it_got_to(self):
        panel = self.panel
        experiment = panel.new_experiment()
        panel.add_run()
        panel.add_run(oil=250)
        asked = []
        panel.run_requested.connect(lambda exp, run: asked.append(run.run_id(exp.code)))
        panel.run_next()
        code = experiment.code
        panel.mark(f"{code}-01", "done", "run_folder_1")
        panel.run_next()
        panel.mark(f"{code}-02", "failed")
        self.assertIsNone(panel.run_next())

        self.assertEqual(asked, [f"{code}-01", f"{code}-02"])
        self.assertEqual(self.cell(0, "Run folder").text(), "run_folder_1")
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
        """Until CONDITION, confirming each replicate's gate as the operator would."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pump(self.app, 0.05)
            if self.api.call("observe")["protocol"]["confirmation_message"]:
                self.gates += 1
                self.api.call("control_protocol", {"action": "confirm", "timeout_s": 0.1})
            if condition():
                return
        self.fail("timed out")

    def test_run_next_runs_a_row_marks_it_done_and_asks_before_the_next(self):
        window = self.window
        self.gates = 0
        self.api.call("connect_fluidics")
        self.api.call("apply_corrections")
        index = next(i for i, stage in enumerate(window.workflow.stages) if stage.id == "experiment")
        window._select_stage(index)
        pump(self.app)
        panel = window._experiment
        self.assertIsNotNone(panel)
        experiment = panel.new_experiment("two runs")
        panel.add_run(oil=30, cells=10, beads=10, stop_oil_ul=None, stop_s=0.2, replicates=2)
        panel.add_run(oil=40, replicates=1)
        code = experiment.code
        asked = []

        with patch.object(window, "_confirm", side_effect=lambda text, then, **_: asked.append((text, then))):
            panel.run_next()
            self.wait(lambda: experiment.runs[0].state == "done")
            self.wait(lambda: asked)

        stage = next(stage for stage in window.workflow.stages if stage.id == "experiment_run")
        editor = window.protocol_editors[stage.id]
        self.assertEqual(editor.plan["tag"], f"{code}-01")
        self.assertTrue(editor.library.isHidden() and editor.editor.isHidden())   # the row is the definition
        self.assertIsNone(editor.parameter_table)
        self.assertFalse(editor.table.isHidden())                                 # only the plan shows
        self.assertTrue(experiment.runs[0].run_folder)
        record = self.api.service.runs.open(experiment.runs[0].run_folder).read()
        self.assertEqual((record.get("run_code"), record.get("experiment")), (f"{code}-01", "two runs"))
        self.assertEqual(asked[0][0], f"Run {code}-01 is finished. Proceed to run {code}-02?")
        self.assertEqual(self.gates, 2)                       # one before each replicate

        asked[0][1]()                                          # Proceed
        self.wait(lambda: experiment.runs[1].state == "done")
        self.assertEqual([s.id for s in window.workflow.stages].count("experiment_run"), 1)
        stored = self.api.service.experiments.load(code)
        self.assertEqual([run.state for run in stored.runs], ["done", "done"])

    def test_a_custom_protocol_runs_with_the_next_id_of_the_days_custom_runs(self):
        window = self.window
        self.gates = 0
        self.api.call("connect_fluidics")
        self.api.call("apply_corrections")
        index = next(i for i, stage in enumerate(window.workflow.stages) if stage.id == "custom")
        window._select_stage(index)
        pump(self.app)
        editor = window._protocol_editor(window.workflow.stages[index])
        editor.set_document(CUSTOM)
        pump(self.app, 0.05)
        self.assertFalse(editor.library.isHidden() or editor.editor.isHidden())   # chosen and edited here
        editor.run_editors["condition"].setText("oil only")
        editor.build_plan()
        self.wait(lambda: editor.plan is not None)
        day = self.api.service.experiments.custom(date.today().isoformat())
        self.assertEqual((editor.plan["run_code"], editor.plan["condition"], editor.plan["experiment"]),
                         (f"{day.code}-01", "oil only", day.name))
        editor.execute()
        self.wait(lambda: editor.plan["state"] == "completed")
        self.wait(lambda: self.api.service.experiments.load(day.code).runs
                  and self.api.service.experiments.load(day.code).runs[0].state == "done")
        row = self.api.service.experiments.load(day.code).runs[0]
        self.assertEqual((row.protocol, row.condition, row.run_folder), ("custom_test", "oil only", editor.plan["run_id"]))

        previous = editor.plan["plan_id"]
        editor.build_plan()
        self.wait(lambda: editor.plan["plan_id"] != previous)
        self.assertEqual(editor.plan["run_code"], f"{day.code}-02")              # the next one
        self.assertNotIn(day.code, [window._experiment.chooser.itemData(i) for i in range(window._experiment.chooser.count())]
                         if window._experiment is not None else [])

        errors = []
        editor.set_document({**CUSTOM, "run": {"name": "oil 3"}})                 # names are not the protocol's
        with patch.object(editor, "error", side_effect=lambda exc: errors.append(str(exc))):
            editor.build_plan()
            self.wait(lambda: errors)
        self.assertIn("unknown", errors[0])


CUSTOM = {"name": "custom_test", "steps": [{"sensor_setpoints": {"0": 10}, "trigger_type": "time",
                                            "trigger_params": {"duration_s": 0.2}, "on_complete": "zero"}]}


if __name__ == "__main__":
    unittest.main()
