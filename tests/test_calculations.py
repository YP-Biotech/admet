import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from admet.core.protocol_store import write_json
from admet.core.runs import Run
from admet.workflows.calculations import calculate_run, recorded_runs, saved_results
from tests.test_fluid_density import recorded_density


RUN_ID = "run_" + "0" * 32


def archived_density(data_dir):
    directory = Path(data_dir) / "runs" / RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    document, csv_path, events_path, origin = recorded_density(directory)
    csv_path.rename(directory / "run.csv")
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    events_path.unlink()
    write_json(directory / "run.json", {
        "state": "completed", "completed_at": "2026-09-28T12:00:00+00:00", "plan_id": "plan_synthetic",
        "run_id": RUN_ID, "normalized_settings": {"protocol": document}, "events": events,
        "artifacts": {"fluidics_csv": "run.csv", "polling_origin_monotonic": origin, "recording_closed": True},
    })
    return directory


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = archived_density(self.tmp.name)

    def test_plain_protocol_calculates_and_preserves_sources_and_history(self):
        before = (Run(self.directory).inputs_digest(), (self.directory / "run.csv").read_bytes())
        with patch("admet.core.service.Admet.engine_action", side_effect=AssertionError("hardware")):
            first = calculate_run(self.directory, "fluid_density")
            second = calculate_run(self.directory, "fluid_density")
            overview = calculate_run(self.directory, "recording_summary")
        self.assertEqual(first["result"]["status"], "consistent", first["result"]["issues"])
        self.assertAlmostEqual(first["result"]["density_g_ml"], 1.2)
        self.assertIsNone(first["result"]["ci95_g_ml"])
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(saved_results(self.directory)), 3)
        self.assertEqual(len(recorded_runs(self.tmp.name)), 1)
        self.assertEqual([item["path"] for item in first["inputs"]], ["run.json", "run.csv"])
        self.assertEqual(first["calculation_version"], 2)
        self.assertGreater(overview["result"]["statistics"]["flow_1_ul_min"]["samples"], 0)
        self.assertEqual((Run(self.directory).inputs_digest(), (self.directory / "run.csv").read_bytes()), before)

    def test_a_moved_project_still_finds_its_recording(self):
        import shutil

        moved = Path(self.tmp.name) / "moved"
        shutil.copytree(Path(self.tmp.name) / "runs", moved / "runs")
        result = calculate_run(moved / "runs" / RUN_ID, "fluid_density")
        self.assertAlmostEqual(result["result"]["density_g_ml"], 1.2)

    def test_active_or_unclosed_recording_is_refused(self):
        run = Run(self.directory)
        summary = run.read()
        summary["state"] = "executing"
        run.write(summary)
        self.assertEqual(recorded_runs(self.tmp.name), [])
        with self.assertRaisesRegex(ValueError, "finished"):
            calculate_run(self.directory, "fluid_density")
        summary["state"] = "completed"
        summary["artifacts"]["recording_closed"] = False
        run.write(summary)
        with self.assertRaisesRegex(ValueError, "closed"):
            calculate_run(self.directory, "fluid_density")

    def test_missing_origin_produces_inconclusive_not_invented_alignment(self):
        run = Run(self.directory)
        summary = run.read()
        summary["artifacts"].pop("polling_origin_monotonic")
        run.write(summary)
        result = calculate_run(self.directory, "fluid_density")["result"]
        self.assertIsNone(result["density_g_ml"])
        self.assertIn("missing recording clock origin", result["issues"])

    def test_changed_height_label_is_refused(self):
        run = Run(self.directory)
        record = run.read()
        record["normalized_settings"]["protocol"]["steps"][1]["confirm_message"] = "Set outlet 8 cm ABOVE the oil"
        run.write(record)
        with self.assertRaisesRegex(ValueError, "height.*disagree"):
            calculate_run(self.directory, "fluid_density")

    def test_summary_missing_values_remain_null(self):
        (self.directory / "run.csv").write_text("elapsed_s,pressure_1_mbar,flow_1_ul_min\n0,,\n1,nan,\n")
        stats = calculate_run(self.directory, "recording_summary")["result"]["statistics"]
        for entry in stats.values():
            self.assertEqual(entry["missing"], 2)
            self.assertEqual(entry["samples"], 0)
            self.assertIsNone(entry["mean"])
            self.assertIsNone(entry["std"])

    def test_changed_inputs_do_not_save_result(self):
        from admet.workflows.calculations import CALCULATIONS

        original = CALCULATIONS["recording_summary"]["calculate"]

        def modifying(context):
            result = original(context)
            with context["csv"].open("a") as handle:
                handle.write("99999,0,0\n")
            return result

        with patch.dict(CALCULATIONS["recording_summary"], calculate=modifying):
            with self.assertRaisesRegex(ValueError, "changed"):
                calculate_run(self.directory, "recording_summary")
        self.assertEqual(saved_results(self.directory), [])

    def test_measurements_are_hashed_and_changes_mark_results_outdated(self):
        run = Run(self.directory)
        record = run.read()
        fields = {"mass": {"label": "Mass", "unit": "mg", "required": True}}
        record["normalized_settings"]["protocol"]["measurements"] = fields
        record["measurements"] = {"fields": fields, "values": {"mass": None}, "revision": 0}
        run.write(record)
        with self.assertRaisesRegex(ValueError, "Missing measurement"):
            calculate_run(self.directory, "fluid_density")
        run.update(measurements={"fields": fields, "values": {"mass": 10}, "revision": 1})
        result = calculate_run(self.directory, "fluid_density")
        self.assertEqual(result["measurement_revision"], 1)
        self.assertFalse(saved_results(self.directory)[0]["outdated"])
        run.update(measurements={"fields": fields, "values": {"mass": 11}, "revision": 2})
        self.assertTrue(saved_results(self.directory)[0]["outdated"])

    def test_collection_window_integrates_transients_and_rejects_gaps_and_pause(self):
        from admet.workflows.calculation_inputs import step_window, trace, volume_ul
        context = {"directory": self.directory, "csv": self.directory / "run.csv",
                   "summary": {"state": "completed", "artifacts": {"polling_origin_monotonic": 100}}}
        events = [{"step_index": 0, "step_name": "collect", "state": "running", "outcome": "running",
                   "monotonic": 100, "confirmation_message": "wait"},
                  {"step_index": 0, "step_name": "collect", "state": "running", "outcome": "running", "monotonic": 101},
                  {"step_index": 0, "step_name": "collect", "state": "running", "outcome": "completed", "monotonic": 103}]
        context["events"] = events
        context["csv"].write_text("elapsed_s,flow_1_ul_min\n0,0\n1,0\n2,60\n3,60\n4,0\n")
        window = step_window(context, 1)
        self.assertEqual(window, (1, 3))
        self.assertEqual(volume_ul(trace(context, 1, *window)), 1.5)
        events.insert(2, {"step_index": 0, "step_name": "collect", "state": "paused", "monotonic": 102})
        with self.assertRaisesRegex(ValueError, "paused"):
            step_window(context, 1)
        context["csv"].write_text("elapsed_s,flow_1_ul_min\n0,0\n1,\n2,60\n3,60\n4,0\n")
        with self.assertRaisesRegex(ValueError, "Missing"):
            trace(context, 1, 1, 3)
