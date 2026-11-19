"""Export: a finished run copied whole to the server, every file checked, then its videos let go here."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.core.export import ExportError, export_run, export_runs, file_digest
from admet.core.runs import Run, RunStore
from admet.core.service import Admet


def finished_run(data_dir, name="oil", *, state="completed", videos=2, kind="protocol") -> Run:
    store = RunStore(data_dir)
    if kind == "recording":
        run = store.begin_recording(name)
    else:
        run = store.begin({"plan_id": "p", "tag": name, "operation_id": "run_json_protocol",
                           "normalized_settings": {"protocol": {"name": "dropseq", "steps": [
                               {"name": "collect", "trigger_type": "time", "trigger_params": {"duration_s": 1}}]}}})
    names = [f"{run.name}_step{i + 1:02d}_20261118_1800{i:02d}.avi" for i in range(videos)]
    for index, video in enumerate(names):
        (run.path / video).write_bytes(os.urandom(4096) + bytes([index]) * 100)
    run.csv_path.write_text("timestamp,elapsed_s\n10:00:00.000,0.000\n")
    run.update(state=state, executed_at="2026-11-18T18:00:00", completed_at="2026-11-18T18:01:00",
               artifacts={"fluidics_csv": "run.csv", "videos": [{"step": i + 1, "video_path": v} for i, v in enumerate(names)]})
    return run


class ExportRunTests(unittest.TestCase):
    def test_a_finished_run_is_copied_whole_checked_and_its_videos_let_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = finished_run(Path(tmp) / "data")
            originals = {path.name: file_digest(path) for path in run.path.iterdir() if path.suffix == ".avi"}
            server = Path(tmp) / "server"
            server.mkdir()
            seen = []

            report = export_run(run, server, progress=lambda *args: seen.append(args))

            copy = server / run.run_id
            self.assertEqual(sorted(p.name for p in copy.iterdir()), sorted([*originals, "run.csv", "run.json"]))
            self.assertEqual({name: file_digest(copy / name) for name in originals}, originals)
            self.assertEqual([p.name for p in run.path.iterdir() if p.suffix == ".avi"], [])     # gone from here
            self.assertTrue(run.csv_path.is_file() and run.record_path.is_file())              # these stay
            note = run.read()["export"]
            self.assertEqual((note["to"], sorted(note["videos_removed"])), (str(server), sorted(originals)))
            self.assertEqual({name: entry["sha256"] for name, entry in note["files"].items() if name in originals}, originals)
            self.assertEqual(json.loads((copy / "run.json").read_text())["export"], note)       # both copies agree
            self.assertEqual(sorted(report["removed"]), sorted(originals))
            self.assertTrue(seen and all(args[0] == run.run_id for args in seen))

    def test_exporting_again_sends_a_changed_record_and_leaves_the_videos_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = finished_run(Path(tmp) / "data")
            server = Path(tmp) / "server"
            server.mkdir()
            export_run(run, server)
            copy = server / run.run_id
            video = next(p for p in copy.iterdir() if p.suffix == ".avi")
            stamp = video.stat().st_mtime_ns
            run.add_result({"calculation_id": "recording_summary", "result": {}, "created_at": "2026-11-18T19:00:00"})

            report = export_run(run, server)

            self.assertEqual(json.loads((copy / "run.json").read_text())["calculations"][0]["calculation_id"],
                             "recording_summary")
            self.assertEqual(video.stat().st_mtime_ns, stamp)                                     # not copied again
            self.assertEqual(report["removed"], [])
            self.assertEqual(len(run.read()["export"]["videos_removed"]), 2)                      # remembered

    def test_a_run_still_going_or_still_encoding_is_left_for_next_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            server = Path(tmp) / "server"
            server.mkdir()
            going = finished_run(data, "going", state="executing")
            encoding = finished_run(data, "encoding")
            (encoding.path / "x.video.json").write_text("{}")
            done = finished_run(data, "done", kind="recording")

            reports = export_runs(RunStore(data), server)

            by_run = {report["run_id"]: report for report in reports}
            self.assertIn("still going", by_run[going.run_id]["error"])
            self.assertIn("encoded", by_run[encoding.run_id]["error"])
            self.assertEqual(len(by_run[done.run_id]["removed"]), 2)
            self.assertEqual(len([p for p in going.path.iterdir() if p.suffix == ".avi"]), 2)   # untouched

    def test_a_missing_server_is_refused_before_anything_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = finished_run(Path(tmp) / "data")
            with self.assertRaisesRegex(ExportError, "mounted"):
                export_run(run, Path(tmp) / "not_mounted" / "runs")
            with self.assertRaisesRegex(ExportError, "mounted"):
                export_runs(RunStore(Path(tmp) / "data"), Path(tmp) / "not_mounted")
            self.assertEqual(len([p for p in run.path.iterdir() if p.suffix == ".avi"]), 2)
            self.assertNotIn("export", run.read())

    def test_a_copy_that_does_not_read_back_whole_keeps_the_video_here(self):
        import admet.core.export as export

        with tempfile.TemporaryDirectory() as tmp:
            run = finished_run(Path(tmp) / "data", videos=1)
            server = Path(tmp) / "server"
            server.mkdir()
            real = export.file_digest

            def corrupted(path):
                return "0" * 64 if Path(path).parent == server / run.run_id and Path(path).suffix == ".avi" else real(path)

            with patch.object(export, "file_digest", corrupted):
                with self.assertRaisesRegex(ExportError, "did not arrive whole"):
                    export_run(run, server)
            self.assertEqual(len([p for p in run.path.iterdir() if p.suffix == ".avi"]), 1)
            self.assertEqual([p for p in (server / run.run_id).iterdir() if p.suffix == ".avi"], [])
            self.assertNotIn("export", run.read())


class ServiceTests(unittest.TestCase):
    def test_the_rig_remembers_the_export_folder_and_exports_to_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet(home=tmp)
            with self.assertRaisesRegex(ValueError, "export folder"):
                admet.export_runs()
            server = Path(tmp) / "server"
            server.mkdir()
            admet.set_export_path(f"  {server}  ")
            self.assertEqual(Admet(home=tmp).export_path(), str(server))
            run = finished_run(admet.home.data_dir)

            reports = admet.export_runs()

            self.assertEqual([r["run_id"] for r in reports], [run.run_id])
            self.assertTrue((server / run.run_id / "run.csv").is_file())
            admet.set_export_path("")
            self.assertEqual(admet.export_path(), "")
            self.assertNotIn("export", admet.rig())


if __name__ == "__main__":
    unittest.main()
