"""run.db: a run's record and timeline in one file, each change a transaction, whole once closed."""

import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path

from admet.core import rundb
from admet.core.migrate import migrate_runs


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
                for _ in range(40):                                                      # committed within a second
                    if rundb.timeline_rows(path):
                        break
                    threading.Event().wait(0.05)
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
    def test_export_run_writes_the_two_files_and_migrate_takes_a_folder_of_runs(self):
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

            old = Path(tmp) / "old" / "runs" / "20261116_100000_oil"
            old.mkdir(parents=True)
            (old / "run.json").write_text(json.dumps({"run_id": old.name}))
            with redirect_stdout(StringIO()) as printed:
                self.assertEqual(main(["migrate", str(Path(tmp) / "old")]), 0)
            self.assertIn("1 run(s) given a run.db", printed.getvalue())
            self.assertEqual(rundb.read_record(old / "run.db")["run_id"], old.name)


class MigrateRunsTests(unittest.TestCase):
    def test_a_run_folder_of_before_gets_its_run_db_and_keeps_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "data" / "runs" / "20261117_193050_dropseq_3n"
            folder.mkdir(parents=True)
            record = {"run_id": folder.name, "state": "completed",
                      "artifacts": {"fluidics_csv": "run.csv", "videos": [{"video_path": "a.avi", "fluidics_csv": "run.csv"}]},
                      "recording": {"video_path": "", "fluidics_csv": "run.csv"}}
            (folder / "run.json").write_text(json.dumps(record))
            (folder / "run.csv").write_text("timestamp,elapsed_s,pressure_0_mbar,flow_0_ul_min,recording_id,state\n"
                                            "10:00:00.000,0.000,1300.00,300.000,,\n"
                                            "10:00:00.050,0.050,,,a,STABLE\n")
            (Path(tmp) / "data" / "runs" / "broken").mkdir()
            (Path(tmp) / "data" / "runs" / "broken" / "run.json").write_text("not json")

            report = migrate_runs(Path(tmp) / "data")
            again = migrate_runs(Path(tmp) / "data")

            self.assertEqual([path.name for path in report.made], [folder.name])
            self.assertIn("broken", report.failed)
            self.assertEqual(again.skipped, [folder.name])
            kept = rundb.read_record(folder / "run.db")
            self.assertEqual(kept["artifacts"]["timeline"], "run.db")
            self.assertNotIn("fluidics_csv", json.dumps(kept))
            rows = rundb.timeline_rows(folder / "run.db")
            self.assertEqual([(r["pressure_0_mbar"], r["state"]) for r in rows], [("1300", ""), ("", "STABLE")])
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["run.csv", "run.db", "run.json"])   # kept
            self.assertFalse((Path(tmp) / "data" / "runs" / "broken" / "run.db").exists())
            self.assertIn("1 run(s) given a run.db", report.lines()[0])


if __name__ == "__main__":
    unittest.main()
