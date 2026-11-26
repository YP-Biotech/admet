"""A protocol step that ends on the first of several stops: written in JSON, planned and run."""

import tempfile
import time
import unittest

from admet.core.runs import Run
from admet.ui.backend import DesktopBackend
from admet.workflows.json_protocol import normalize, resolve


def protocol(seconds="hold_s"):
    return {"name": "several_stops", "parameters": {"hold_s": ["Longest, s", 0.3]},
            "steps": [{"name": "collect", "sensor_setpoints": {"0": 30, "1": 10},
                       "trigger_type": "any",
                       "trigger_params": {"stops": [
                           {"trigger_type": "volume", "trigger_params": {"sensor_index": 0, "target_volume_ul": 100}},
                           {"trigger_type": "total_volume", "trigger_params": {"sensors": [0, 1],
                                                                                "target_volume_ul": 100}},
                           {"trigger_type": "time", "trigger_params": {"duration_s": seconds}}]},
                       "timeout_s": 30, "on_complete": "zero"}]}


class ProtocolTests(unittest.TestCase):
    def test_a_stops_parameters_are_worked_out_and_a_wrong_stop_is_refused(self):
        steps = resolve(protocol("hold_s * 2"))["steps"]
        self.assertEqual(steps[0]["trigger_params"]["stops"][2]["trigger_params"], {"duration_s": 0.6})
        wrong = protocol()
        wrong["steps"][0]["trigger_params"]["stops"][1]["trigger_params"].pop("sensors")
        with self.assertRaisesRegex(ValueError, "step 1: stop 2: .*needs sensors"):
            normalize(resolve(wrong))
        untimed = protocol()
        del untimed["steps"][0]["timeout_s"]
        with self.assertRaisesRegex(ValueError, "timeout_s"):
            normalize(resolve(untimed))


class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backend = DesktopBackend(simulated=True, home=tmp.name)
        self.addCleanup(self.backend.shutdown)
        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")

    def test_the_stop_expected_first_times_the_plan_and_the_one_met_first_ends_the_step(self):
        plan = self.backend.preview("experiment_1", protocol())
        self.assertEqual(plan["steps"][0]["expected_duration_s"], 0.3)          # time beats 200 s of oil
        self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1})
        deadline = time.monotonic() + 5
        while (current := self.backend.call("planned_protocols", {"plan_id": plan["plan_id"]})["plans"][0])["state"] \
                != "completed" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(current["state"], "completed")
        record = Run(self.backend.service.runs.open(current["run_id"]).path).read()
        ended = [event["ended_by"] for event in record["events"] if event.get("ended_by")]
        self.assertEqual(ended, ["Time: 0s"])


if __name__ == "__main__":
    unittest.main()
