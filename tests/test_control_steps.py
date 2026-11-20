"""Experiment steps: Plan asks for the run's name, and an added step can be taken out again."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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

    def test_plan_asks_for_the_runs_name_and_set_and_the_plan_carries_them(self):
        window = self.window
        index = self.stage_index("experiment_1")
        window._select_stage(index)
        editor = window._protocol_editor(window.workflow.stages[index])
        editor.set_document(DOCUMENT)
        asked = []

        def ask(parent, name, group):
            asked.append((name, group))
            return "Oil 3", " oil "

        with patch("admet.ui.protocols.PlanDialog.ask", side_effect=ask):
            editor.build_plan()
            plan = self.wait_plan(editor)

        self.assertEqual((plan["tag"], plan["set"]), ("Oil 3", "oil"))
        self.assertEqual(asked[0], ("Experiment 1", ""))                     # the step's own name is proposed
        self.assertEqual(window.workflow.stages[index].label, "Oil 3")       # and the step takes the name typed
        self.assertEqual(window.toc_rows[index]["label"].text(), f"{index + 1}. Oil 3")
        self.assertIs(editor.stage, window.workflow.stages[index])
        with patch("admet.ui.protocols.PlanDialog.ask", return_value=None):
            editor.build_plan()
            self.pump(0.3)
        self.assertEqual(editor.plan["plan_id"], plan["plan_id"])              # backing out plans nothing
        with patch("admet.ui.protocols.PlanDialog.ask", side_effect=ask):
            editor.build_plan()
            again = self.wait_plan(editor, previous_id=plan["plan_id"])
        self.assertEqual(asked[1], ("Oil 3", "oil"))                          # the last name and set are proposed
        with patch("admet.ui.protocols.PlanDialog.ask", return_value=("", "")):
            editor.build_plan()
            again = self.wait_plan(editor, previous_id=again["plan_id"])
        self.assertEqual((again["tag"], again["set"]), ("Oil 3", ""))         # an empty name keeps the last one
        self.assertEqual(window.workflow.stages[index].label, "Oil 3")
        self.assertEqual(window.run_set, "")                                   # an empty set is none

    def test_the_plan_dialog_proposes_the_name_and_the_set_and_returns_them_as_typed(self):
        from admet.ui.protocols import PlanDialog

        dialog = PlanDialog(None, "Oil 3", "oil")
        self.addCleanup(dialog.close)
        self.assertEqual((dialog.name.text(), dialog.group.text()), ("Oil 3", "oil"))
        dialog.group.setText("oil 2")
        dialog.accept()
        self.assertEqual((dialog.name.text(), dialog.group.text()), ("Oil 3", "oil 2"))

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
        with patch("admet.ui.protocols.PlanDialog.ask", return_value=("Oil 3", "")):
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

    def test_a_step_whose_protocol_runs_stays(self):
        window = self.window
        stage = window._add_protocol_stage("Oil 3")
        window._pipeline_stage_id = stage.id
        with patch.object(window, "_pipeline_active", return_value=True):
            self.assertFalse(window._remove_protocol_stage(stage.id))
        self.assertIn(stage.id, [s.id for s in window.workflow.stages])


if __name__ == "__main__":
    unittest.main()
