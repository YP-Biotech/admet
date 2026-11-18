"""A run is a folder under data/runs: its record, its timeline and its videos, nothing pointing outside."""

import json
import tempfile
import unittest
from pathlib import Path

from admet.core.runs import RECORD, RUN_ID, RunStore, run_name


def plan(tag="", name="dropseq_3n", fields=None):
    protocol = {"name": name, "steps": [{"name": "collect", "trigger_type": "time", "trigger_params": {"duration_s": 1}}]}
    if fields:
        protocol["measurements"] = fields
    return {"plan_id": "plan_1", "operation_id": "run_json_protocol", "tag": tag, "state": "executing",
            "executed_at": "2026-11-18T18:30:00", "normalized_settings": {"protocol": protocol}}


class NameTests(unittest.TestCase):
    def test_a_name_is_folder_safe(self):
        self.assertEqual(run_name("Oil 3 / dSurf 2%"), "oil_3_dsurf_2")
        self.assertEqual(run_name(""), "run")
        self.assertEqual(run_name("x" * 80), "x" * 60)


class StoreTests(unittest.TestCase):
    def test_a_protocol_run_gets_a_dated_folder_named_after_its_tag_and_its_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run = store.begin(plan(tag="Oil 3", fields={"mass": {"label": "Mass", "unit": "mg"}}))

            self.assertTrue(RUN_ID.fullmatch(run.run_id), run.run_id)
            self.assertTrue(run.run_id.endswith("_oil_3"))
            self.assertEqual(run.path, Path(tmp) / "runs" / run.run_id)
            record = json.loads((run.path / RECORD).read_text())
            self.assertEqual((record["run_id"], record["name"], record["kind"], record["tag"]),
                             (run.run_id, "oil_3", "protocol", "Oil 3"))
            self.assertEqual(record["measurements"], {"fields": {"mass": {"label": "Mass", "unit": "mg"}},
                                                      "values": {"mass": None}, "revision": 0})
            self.assertEqual((record["events"], record["calculations"], record["artifacts"]), ([], [], {}))
            self.assertEqual(run.csv_path, run.path / "run.csv")

    def test_an_unnamed_run_takes_the_protocols_name_and_two_in_one_second_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            first = store.begin(plan())
            second = store.begin(plan())
            self.assertTrue(first.run_id.endswith("_dropseq_3n"))
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertEqual(len(store.list()), 2)

    def test_a_recording_by_hand_is_a_run_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunStore(tmp).begin_recording("oil cap")
            record = run.read()
            self.assertEqual((record["kind"], record["state"], record["name"]), ("recording", "recording", "oil_cap"))
            self.assertIsNone(record["measurements"])
            self.assertTrue(record["started_at"])

    def test_the_list_names_runs_by_tag_then_protocol_then_label_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            tagged = store.begin(plan(tag="Oil 3"))
            tagged.update(executed_at="2026-11-18T10:00:00", state="completed")
            plain = store.begin(plan())
            plain.update(executed_at="2026-11-18T11:00:00", state="failed")
            by_hand = store.begin_recording("cap")
            by_hand.update(started_at="2026-11-18T12:00:00")

            listed = store.list()

            self.assertEqual([entry["name"] for entry in listed], ["cap", "dropseq_3n", "Oil 3"])
            self.assertEqual([entry["state"] for entry in listed], ["recording", "failed", "completed"])
            self.assertEqual(listed[2]["directory"], str(tagged.path))
            self.assertEqual([entry["measurements"] for entry in listed], [False, False, False])

    def test_only_a_run_id_opens_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run = store.begin(plan())
            self.assertEqual(store.open(run.run_id).path, run.path)
            for bad in ("../../elsewhere", "run_" + "a" * 32, "", None, "20261118_183000_Oil"):
                with self.subTest(run_id=bad), self.assertRaisesRegex(ValueError, "invalid run ID"):
                    store.open(bad)
            with self.assertRaises(LookupError):
                store.open("20261118_183000_gone")
            self.assertFalse(store.exists("20261118_183000_gone"))


class RecordTests(unittest.TestCase):
    def test_only_events_that_change_something_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunStore(tmp).begin(plan())
            tick = {"state": "running", "outcome": "running", "step_index": 0, "step_name": "collect",
                    "confirmation_message": "", "error": "", "progress": 0.1, "monotonic": 1.0}
            self.assertTrue(run.event(tick))
            self.assertFalse(run.event({**tick, "progress": 0.5, "monotonic": 2.0}))      # a tick, not a change
            self.assertTrue(run.event({**tick, "confirmation_message": "ready?", "monotonic": 3.0}))
            self.assertTrue(run.event({**tick, "outcome": "completed", "monotonic": 4.0}))
            self.assertEqual([e["monotonic"] for e in run.read()["events"]], [1.0, 3.0, 4.0])

    def test_measurements_are_validated_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunStore(tmp).begin(plan(fields={"mass": {"label": "Mass", "unit": "mg"}}))
            self.assertEqual(run.measurements()["revision"], 0)
            self.assertEqual(run.measurements({"mass": 12.5})["values"], {"mass": 12.5})
            self.assertEqual(run.measurements()["revision"], 1)
            for bad in ({"unknown": 1}, {"mass": "12"}, {"mass": True}):
                with self.subTest(changes=bad), self.assertRaises(ValueError):
                    run.measurements(bad)
            with self.assertRaisesRegex(ValueError, "no measurements"):
                RunStore(tmp).begin(plan()).measurements()

    def test_results_are_appended_and_leave_the_inputs_digest_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunStore(tmp).begin(plan())
            before = run.inputs_digest()
            kept = run.add_result({"calculation_id": "recording_summary", "result": {"ok": True}})
            self.assertEqual(run.inputs_digest(), before)                       # a result is not an input
            self.assertEqual(run.result(kept["id"])["result"], {"ok": True})
            self.assertEqual(len(run.results()), 1)
            run.update(state="completed")
            self.assertNotEqual(run.inputs_digest(), before)                    # the record itself is

    def test_a_finished_video_is_noted_in_its_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunStore(tmp).begin(plan())
            run.update(artifacts={"videos": [{"step": 1, "video_path": "oil_step01_x.avi"}]})
            self.assertTrue(run.video_finished("oil_step01_x.avi", True, ""))
            self.assertFalse(run.video_finished("other.avi", True, ""))
            self.assertTrue(run.read()["artifacts"]["videos"][0]["encoded"])
            run.update(recording={"video_path": "cap_1.avi"})
            self.assertTrue(run.video_finished("cap_1.avi", False, "no ffmpeg"))
            self.assertEqual(run.read()["recording"]["encode_error"], "no ffmpeg")


if __name__ == "__main__":
    unittest.main()
