"""Experiment steps: Plan asks for the run's name, and an added step can be taken out again."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.backend import DesktopBackend  # noqa: E402
from admet.ui.control import ControlWindow  # noqa: E402

DOCUMENT = {
    "name": "steps_test",
    "steps": [{"sensor_setpoints": {"0": 10, "1": 5, "2": 5}, "trigger_type": "time",
               "trigger_params": {"duration_s": 0.2}, "on_complete": "zero"}],
}


class ExperimentStepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.api = DesktopBackend(simulated=True, home=tmp.name)
        self.addCleanup(self.api.shutdown)
        self.window = ControlWindow(self.api)
        self.addCleanup(self.window.close)
        self.window.show()
        self.pump()

    def pump(self, seconds=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)   # as the running loop does
            time.sleep(0.01)

    def stage_index(self, stage_id):
        return next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == stage_id)

    def wait_plan(self, editor, previous_id=None):
        # Compared by id: the editor swaps its plan for a fresh copy from the backend on every refresh.
        for _ in range(100):
            self.pump(0.05)
            if editor.plan is not None and editor.plan["plan_id"] != previous_id:
                return editor.plan
        self.fail("no plan arrived")

    def test_the_run_and_set_rows_of_the_parameter_table_name_the_plan_and_the_step(self):
        window = self.window
        index = self.stage_index("experiment_1")
        window._select_stage(index)
        editor = window._protocol_editor(window.workflow.stages[index])
        editor.set_document(DOCUMENT)
        self.pump(0.05)

        self.assertEqual([editor.parameter_table.item(0, c).text() for c in (0, 2)], ["Run", "Condition"])
        self.assertEqual(editor.run_editors["name"].placeholderText(), "steps_test")        # unnamed: the protocol's
        editor.run_editors["name"].setText("Oil 3")
        editor.run_editors["name"].editingFinished.emit()
        editor.run_editors["condition"].setText(" oil ")
        editor.run_editors["condition"].editingFinished.emit()
        self.assertEqual(json.loads(editor.editor.toPlainText())["run"], {"name": "Oil 3", "condition": "oil"})
        editor.build_plan()
        plan = self.wait_plan(editor)

        self.assertEqual((plan["tag"], plan["condition"]), ("Oil 3", "oil"))
        self.assertEqual(window.workflow.stages[index].label, "Oil 3")       # the step takes the run's name
        self.assertEqual(window.toc_rows[index]["label"].text(), f"{index + 1}. Oil 3")
        self.assertIs(editor.stage, window.workflow.stages[index])
        editor.run_editors["condition"].setText("")
        editor.run_editors["condition"].editingFinished.emit()
        self.assertEqual(json.loads(editor.editor.toPlainText())["run"], {"name": "Oil 3"})   # an empty set is none
        editor.run_editors["name"].setText("")
        editor.run_editors["name"].editingFinished.emit()
        self.assertNotIn("run", json.loads(editor.editor.toPlainText()))                    # nothing left: no block
        editor.build_plan()
        again = self.wait_plan(editor, previous_id=plan["plan_id"])
        self.assertEqual((again["tag"], again["condition"]), ("", ""))
        self.assertEqual(window.workflow.stages[index].label, "Oil 3")       # the step keeps its name
        editor.run_editors["name"].setText("Oil 4")                           # typed, not left: Plan takes it too
        editor.build_plan()
        third = self.wait_plan(editor, previous_id=again["plan_id"])
        self.assertEqual((third["tag"], window.workflow.stages[index].label), ("Oil 4", "Oil 4"))

    def test_an_added_step_can_be_removed_and_its_plan_goes_with_it(self):
        window = self.window
        before = len(window.workflow.stages)
        stage = window._add_protocol_stage("Oil 3")
        self.pump()
        index = self.stage_index(stage.id)
        self.assertEqual(window.workflow_state.index, index)                  # the new step is the one shown
        self.assertIsNotNone(window.toc_rows[index]["remove"])
        self.assertIsNone(window.toc_rows[0]["remove"])                       # stock stages have no cross
        editor = window._protocol_editor(stage)
        editor.set_document(DOCUMENT)
        editor.build_plan()
        self.wait_plan(editor)
        self.assertIn(stage.id, self.api._previews)

        with patch("admet.ui.control.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            self.assertFalse(window._remove_protocol_stage(stage.id))
        self.assertEqual(len(window.workflow.stages), before + 1)
        with patch("admet.ui.control.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(window._remove_protocol_stage(stage.id))
        self.pump()

        self.assertEqual([len(window.workflow.stages), len(window.toc_rows)], [before, before])
        self.assertNotIn(stage.id, [s.id for s in window.workflow.stages])
        self.assertNotIn(stage.id, window.protocol_editors)
        self.assertNotIn(stage.id, self.api._previews)
        self.assertEqual(window.workflow.stages[window.workflow_state.index].id, "experiment_1")   # the step before it
        self.assertFalse(window._remove_protocol_stage(window.workflow.stages[0].id))

    def test_removing_the_step_shown_keeps_the_live_panels_for_the_stage_shown_next(self):
        import shiboken6

        window = self.window
        for index in range(len(window.workflow.stages)):                      # every page exists, as after a walk
            window._select_stage(index)
            self.pump(0.05)
        first = window._add_protocol_stage("Oil 1")
        second = window._add_protocol_stage("Oil 2")
        self.pump()
        self.assertTrue(window.current_stage_page.isAncestorOf(window.monitor_table))   # the live set is on Oil 2

        self.assertTrue(window._remove_protocol_stage(second.id))
        self.pump()

        self.assertEqual(window.workflow.stages[window.workflow_state.index].id, first.id)
        for widget in (window.plot_panel, window.preview, window.monitor_table):
            self.assertTrue(shiboken6.isValid(widget))                       # not deleted with the page
        self.assertTrue(window.current_stage_page.isAncestorOf(window.monitor_table))   # shown on Oil 1 now
        self.assertTrue(window.current_stage_page.isAncestorOf(window.protocol_editors[first.id]))
        window._poll()                                                        # what the timer runs: no dead widgets
        window._select_stage(self.stage_index("experiment_1"))
        self.pump()
        self.assertTrue(window._remove_protocol_stage("experiment_1"))      # the stock step, while shown
        self.pump()
        again = window._add_protocol_stage("Experiment 1")
        self.pump()
        self.assertEqual(window.workflow.stages[window.workflow_state.index].id, again.id)
        self.assertTrue(window.current_stage_page.isAncestorOf(window.protocol_editors[again.id]))
        self.assertTrue(window.current_stage_page.isAncestorOf(window.monitor_table))
        self.assertEqual([b.text() for b in window._transport_button_refs][:2], ["Plan", "Execute"])
        window._poll()

    def test_a_step_whose_protocol_runs_stays(self):
        window = self.window
        stage = window._add_protocol_stage("Oil 3")
        window._pipeline_stage_id = stage.id
        with patch.object(window, "_pipeline_active", return_value=True):
            self.assertFalse(window._remove_protocol_stage(stage.id))
        self.assertIn(stage.id, [s.id for s in window.workflow.stages])


if __name__ == "__main__":
    unittest.main()
