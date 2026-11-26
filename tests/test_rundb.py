"""run.db: a run's record and timeline in one file, each change a transaction, whole once closed."""

import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from admet.core import rundb


class RecordTests(unittest.TestCase):
    def test_the_record_is_kept_whole_and_two_writers_never_lose_each_others_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.db"
            self.assertEqual(rundb.read_record(path), {})                               # no file: no record
            rundb.write_record(path, {"run_id": "r", "events": []})

            def add(key):
                for n in range(40):
                    rundb.update_record(path, lambda record: {**record, f"{key}{n}": n})

            threads = [threading.Thread(target=add, args=(key,)) for key in "ab"]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            record = rundb.read_record(path)
            self.assertEqual(record["run_id"], "r")
            self.assertEqual(len([key for key in record if key[0] in "ab" and key[1:].isdigit()]), 80)
            self.assertEqual(rundb.update_record(path, lambda record: None), record)       # None leaves it be


class TimelineTests(unittest.TestCase):
    def test_rows_from_two_threads_are_all_committed_and_the_closed_file_is_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.db"
            rundb.write_record(path, {"run_id": "r"})
            writer = rundb.TimelineWriter(path, 3, 3)

            def readings():
                for n in range(300):
                    writer.add_fluidics({"timestamp": "t", "elapsed_s": n * 0.1, "pressure_0_mbar": 1300.0})

            def bursts():
                for n in range(30):
                    writer.add_qc({"timestamp": "t", "elapsed_s": n + 0.05, "recording_id": "a", "state": "STABLE",
                                   "rate_hz": 1170.0})

            threads = [threading.Thread(target=readings), threading.Thread(target=bursts)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rundb.update_record(path, lambda record: {**record, "state": "executing"})   # the record beside it
            writer.close()

            rows = rundb.timeline_rows(path)
            self.assertEqual(len(rows), 330)
            self.assertEqual(sum(1 for row in rows if row["state"] == "STABLE"), 30)
            self.assertEqual([float(row["elapsed_s"]) for row in rows],
                             sorted(float(row["elapsed_s"]) for row in rows))           # one clock, in order
            self.assertEqual(rundb.read_record(path), {"run_id": "r", "state": "executing"})
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["run.db"])   # no WAL left beside it
            connection = rundb.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            finally:
                connection.close()

    def test_a_reader_sees_what_was_committed_while_the_run_still_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.db"
            writer = rundb.TimelineWriter(path, 1, 1)
            try:
                writer.add_fluidics({"timestamp": "t", "elapsed_s": 0.0, "flow_0_ul_min": 300.0})
                deadline = time.monotonic() + 10      # committed within a second; a busy machine gets longer
                while not rundb.timeline_rows(path) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(rundb.timeline_rows(path)[0]["flow_0_ul_min"], "300")
            finally:
                writer.close()

    def test_the_rows_handed_out_are_the_callers_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.db"
            rundb.write_timeline(path, [{"elapsed_s": "1", "flow_0_ul_min": "5"}], 0, 1)
            rows = rundb.timeline_rows(path)
            rows[0]["flow_0_ul_min"] = "changed"
            self.assertEqual(rundb.timeline_rows(path)[0]["flow_0_ul_min"], "5")

    def test_the_channels_follow_the_highest_index_a_column_names(self):
        self.assertEqual(rundb.channel_counts(["elapsed_s", "flow_1_ul_min"]), (0, 2))
        self.assertEqual(rundb.channel_counts(["pressure_2_mbar", "volume_0_ul", "stable_1"]), (3, 2))


class ExportTests(unittest.TestCase):
    def test_the_export_is_the_run_json_and_run_csv_of_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.db"
            rundb.write_record(path, {"run_id": "r", "events": [{"step_name": "collect"}]})
            rundb.write_timeline(path, [
                {"timestamp": "10:00:00.000", "elapsed_s": "0", "pressure_0_mbar": "1300", "flow_0_ul_min": "300"},
                {"timestamp": "10:00:00.050", "elapsed_s": "0.05", "recording_id": "a", "rate_hz": "1170",
                 "state": "STABLE"},
                {"timestamp": "10:00:00.100", "elapsed_s": "0.1", "pressure_0_mbar": "1301", "flow_0_ul_min": "301"},
            ], 1, 1)

            rows = rundb.export_csv(path, Path(tmp) / "run.csv")
            rundb.export_json(path, Path(tmp) / "run.json")

            with (Path(tmp) / "run.csv").open(newline="") as handle:
                reader = csv.DictReader(handle)
                exported = list(reader)
            self.assertEqual(rows, 3)
            self.assertEqual(reader.fieldnames[:4], ["timestamp", "elapsed_s", "pressure_0_mbar", "flow_0_ul_min"])
            self.assertIn("state", reader.fieldnames)
            self.assertEqual([(r["elapsed_s"], r["flow_0_ul_min"], r["state"]) for r in exported],
                             [("0", "300", ""), ("0.05", "", "STABLE"), ("0.1", "301", "")])
            self.assertEqual(json.loads((Path(tmp) / "run.json").read_text())["events"], [{"step_name": "collect"}])


class CommandLineTests(unittest.TestCase):
    def test_export_run_writes_the_record_and_the_timeline_as_two_files(self):
        from contextlib import redirect_stdout
        from io import StringIO

        from admet.app import main

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "runs" / "20261117_193050_oil"
            run.mkdir(parents=True)
            rundb.write_record(run / "run.db", {"run_id": run.name})
            rundb.write_timeline(run / "run.db", [{"elapsed_s": "0", "flow_0_ul_min": "300"}], 0, 1)
            out = Path(tmp) / "out"
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["export-run", str(run), "--into", str(out)]), 0)
                self.assertEqual(main(["export-run", str(Path(tmp) / "nowhere")]), 1)
            self.assertEqual(json.loads((out / "run.json").read_text())["run_id"], run.name)
            self.assertIn("300", (out / "run.csv").read_text())


if __name__ == "__main__":
    unittest.main()


class ControlDatabaseTests(unittest.TestCase):
    """Every run of a data folder in its control.db; each finished one sealed into its folder as run.db."""

    def test_two_runs_keep_their_records_and_timelines_apart_and_seal_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "control.db"
            first, second = rundb.locator(database, "20261126_140000_a"), rundb.locator(database, "20261126_141500_b")
            rundb.write_record(first, {"run_id": "a"})
            rundb.write_record(second, {"run_id": "b"})
            for target, flow in ((first, 300.0), (second, 250.0)):
                writer = rundb.TimelineWriter(target, 1, 1)
                for k in range(5):
                    writer.add_fluidics({"timestamp": "t", "elapsed_s": k * 0.1, "flow_0_ul_min": flow})
                writer.close()
            wider = rundb.TimelineWriter(second, 3, 3)                   # a rig with more channels later on
            wider.add_fluidics({"timestamp": "t", "elapsed_s": 1.0, "flow_2_ul_min": 40.0})
            wider.close()

            self.assertEqual(rundb.run_ids(database), ["20261126_140000_a", "20261126_141500_b"])
            self.assertEqual({row["flow_0_ul_min"] for row in rundb.timeline_rows(first)}, {"300"})
            self.assertEqual(len(rundb.timeline_rows(second)), 6)
            self.assertEqual(rundb.timeline_rows(second)[-1]["flow_2_ul_min"], "40")
            self.assertFalse(rundb.has_timeline(rundb.locator(database, "20261126_150000_c")))

            (Path(tmp) / "a").mkdir()
            sealed = rundb.seal(first, Path(tmp) / "a" / "run.db")
            self.assertEqual(rundb.read_record(sealed), {"run_id": "a"})
            self.assertEqual(rundb.timeline_rows(sealed), rundb.timeline_rows(first))
            self.assertEqual(sorted(p.name for p in sealed.parent.iterdir()), ["run.db"])     # whole, nothing beside

    def test_a_run_seals_itself_into_its_folder_once_it_has_finished(self):
        from admet.core.runs import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run = store.begin({"plan_id": "p", "tag": "oil 3", "operation_id": "run_json_protocol"})
            self.assertTrue(run.ref.startswith(str(Path(tmp) / "control.db")))
            run.update(state="executing")
            self.assertFalse(run.sealed_path.exists())                   # still going: nothing sealed
            run.update(state="completed")
            self.assertEqual(rundb.read_record(run.sealed_path)["state"], "completed")
            run.add_result({"calculation_id": "recording_summary", "result": {}})
            self.assertEqual(len(rundb.read_record(run.sealed_path)["calculations"]), 1)    # sealed again
            self.assertEqual([entry["run_id"] for entry in store.list()], [run.run_id])
            self.assertTrue(store.exists(run.run_id))
            self.assertFalse(store.exists("20261126_150000_nothing"))
