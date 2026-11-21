"""An old project laid out as run folders, as admet control records now."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from admet.app import main
from admet.core.migrate import migrate_project
from admet.core.registry import Registry
from admet.core.runs import Run, RunStore


def old_project(root: Path) -> Path:
    """A project as earlier versions wrote it: one protocol run with a step video, and a video recorded by hand."""
    project = root / "study.admetp"
    (project / "records" / "protocols" / "run_abc").mkdir(parents=True)
    (project / "records" / "fluidics").mkdir()
    (project / "records" / "camera").mkdir()
    summary = {"plan_id": "plan_1", "operation_id": "run_json_protocol", "state": "completed",
               "created_at": "2026-11-17T16:43:20+02:00", "executed_at": "2026-11-17T16:43:26+02:00",
               "normalized_settings": {"protocol": {"name": "Oil 3", "steps": [
                   {"name": "collect", "trigger_type": "time", "trigger_params": {"duration_s": 1}}]}},
               "rig_fingerprint": {"simulated": True}, "run_id": "run_abc",
               "artifacts": {"fluidics_csv": "records/fluidics/run_abc_20261117_164326.csv",
                             "qc_csv": "records/camera/run_abc.qc.csv", "polling_origin_monotonic": 1000.0,
                             "videos": [{"step": 1, "step_name": "set01_rep01", "part": 1,
                                         "recording_id": "run_abc_step01_20261117_164331",
                                         "video_path": "records/camera/run_abc_step01_20261117_164331.avi",
                                         "started_at": "2026-11-17T16:43:31", "frames_recorded": 10}]}}
    (project / "records" / "protocols" / "run_abc" / "summary.json").write_text(json.dumps(summary))
    events = [{"sequence": 1, "at": "2026-11-17T16:43:26+02:00", "monotonic": 1002.0, "state": "running",
               "outcome": "running", "step_index": 0, "step_name": "collect", "progress": 0.0},
              {"sequence": 2, "at": "2026-11-17T16:43:26+02:00", "monotonic": 1002.5, "state": "running",
               "outcome": "running", "step_index": 0, "step_name": "collect", "progress": 0.5},   # a tick: not kept
              {"sequence": 3, "at": "2026-11-17T16:43:30+02:00", "monotonic": 1006.0, "state": "completed",
               "outcome": "completed", "step_index": 0, "step_name": "collect", "progress": 1.0}]
    (project / "records" / "protocols" / "run_abc" / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n")
    with (project / "records" / "fluidics" / "run_abc_20261117_164326.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "elapsed_s", "pressure_0_mbar", "flow_0_ul_min"])
        for k in range(3):
            writer.writerow([f"16:43:2{6 + k}.000", f"{500 + k:.3f}", "1300", "300"])
    with (project / "records" / "camera" / "run_abc.qc.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "elapsed_s", "recording_id", "step", "part", "rate_hz", "count", "diameter",
                         "diameter_unit", "cv_percent", "state"])
        writer.writerow(["2026-11-17T16:43:31.000", "0.100", "run_abc_step01_20261117_164331", "1", "1", "1400",
                         "1400", "124.0", "um", "3.0", "SETTLING"])
        writer.writerow(["2026-11-17T16:43:32.000", "1.100", "run_abc_step01_20261117_164331", "1", "1", "1410",
                         "2800", "124.0", "um", "3.1", "STABLE"])
    (project / "records" / "camera" / "run_abc_step01_20261117_164331.avi").write_bytes(b"RIFF step video")
    (project / "records" / "camera" / "by_hand_20261116_190500.avi").write_bytes(b"RIFF a recording by hand")
    with (project / "records" / "fluidics" / "by_hand_20261116_190500.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "elapsed_s", "pressure_0_mbar", "flow_0_ul_min"])
        writer.writerow(["19:05:00.000", "10.000", "900", "100"])
    (project / "records" / "protocols" / "run_old").mkdir()                       # its summary names a file that is gone
    (project / "records" / "protocols" / "run_old" / "summary.json").write_text(json.dumps({
        "plan_id": "plan_0", "state": "completed", "executed_at": "2026-11-15T10:00:00+02:00", "run_id": "run_old",
        "normalized_settings": {"protocol": {"name": "Oil 2", "steps": []}},
        "artifacts": {"fluidics_csv": "C:\\old\\run_old.csv", "recording_closed": True}}))
    with (project / "records" / "fluidics" / "run_old_20261115_100000.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "elapsed_s", "pressure_0_mbar", "flow_0_ul_min"])
        writer.writerow(["10:00:00.000", "1.000", "700", "70"])
    (project / "records" / "protocols" / "run_bad").mkdir()                       # failed, with a video of its own
    (project / "records" / "protocols" / "run_bad" / "summary.json").write_text(json.dumps({
        "plan_id": "plan_2", "state": "failed", "executed_at": "2026-11-17T17:00:00+02:00", "run_id": "run_bad",
        "normalized_settings": {"protocol": {"name": "Oil 4", "steps": []}}, "artifacts": {}}))
    (project / "records" / "camera" / "run_bad_20261117_170005.avi").write_bytes(b"RIFF failed video")
    (project / "records" / "protocols" / "run_stuck").mkdir()                     # never finished: a record only
    (project / "records" / "protocols" / "run_stuck" / "summary.json").write_text(json.dumps({
        "plan_id": "plan_3", "run_id": "run_stuck", "state": "executing"}))
    (project / "records" / "camera" / "run_abc_20261117_164340.avi").write_bytes(b"RIFF unlisted")   # the run's, unlisted
    (project / "manifest.json").write_text(json.dumps({"project_id": "study", "metadata": {
        "acquisition_mode": "camera_fluidics", "calibration": {"oil_l": {"profile": "p1"}},
        "profiles": [{"id": "p1", "name": "gen_oil_l", "liquid": "oil", "calibration": "IPA", "unit": "L",
                      "scale": 0.6, "square": 0.0, "cube": 0.0}],
        "optics": {"microns_per_pixel": 2.5, "method": "walls"}, "qt_checkup": {"conditions": {}},
        "analysis_setup": {"matrix": []}, "cache_root": "C:\\cache"}, "files": [
        {"id": "summary", "role": "protocol_run", "path": "records/protocols/run_abc/summary.json"},
        {"id": "video-1", "role": "control_video", "path": "records/camera/run_abc_step01_20261117_164331.avi",
         "metadata": {"step": 1}},
        {"id": "video-3", "role": "control_video", "path": "records/camera/run_bad_20261117_170005.avi"},
        {"id": "video-4", "role": "control_video", "path": "records/camera/run_abc_20261117_164340.avi"},
        {"id": "video-2", "role": "control_video", "path": "records/camera/by_hand_20261116_190500.avi",
         "metadata": {"started_at": "2026-11-16T19:05:00", "stopped_at": "2026-11-16T19:05:30",
                      "fluidics_csv": "records/fluidics/by_hand_20261116_190500.csv", "acquisition_fps": 1000.0}},
    ]}))
    return project


class MigrateTests(unittest.TestCase):
    def test_a_protocol_run_becomes_a_run_folder_with_one_timeline_and_its_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = old_project(Path(tmp))
            registry = Registry(Path(tmp) / "home" / "analyze")

            report = migrate_project(project, Path(tmp) / "data", registry=registry)

            self.assertEqual([folder.name for folder in report.made],
                             ["20261117_164326_oil_3", "20261115_100000_oil_2", "20261116_190500_by_hand"])
            self.assertEqual((report.videos, report.skipped, report.registered), (3, [], 3))
            with (Path(tmp) / "data" / "runs" / "20261115_100000_oil_2" / "run.csv").open(newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle))[0]["flow_0_ul_min"], "70")   # found by the run's name
            self.assertEqual(report.left, {"run_bad": "failed", "run_stuck": "executing"})   # not laid out
            self.assertIn("2 run(s) not completed left in the project: 1 executing, 1 failed", report.lines())
            self.assertTrue((project / "records" / "camera" / "run_bad_20261117_170005.avi").is_file())   # stays
            self.assertFalse((Path(tmp) / "data" / "runs" / "20261117_170000_oil_4").exists())
            run = Run(Path(tmp) / "data" / "runs" / "20261117_164326_oil_3")
            record = run.read()
            self.assertEqual((record["kind"], record["name"], record["state"], record["run_id"]),
                             ("protocol", "oil_3", "completed", "20261117_164326_oil_3"))
            self.assertEqual(record["normalized_settings"]["protocol"]["name"], "Oil 3")
            self.assertEqual(record["artifacts"]["fluidics_csv"], "run.csv")
            self.assertEqual(record["artifacts"]["polling_origin_monotonic"], 1000.0)
            video, unlisted = record["artifacts"]["videos"]
            self.assertEqual((video["video_path"], video["step_name"], video["encoded"]),
                             ("run_abc_step01_20261117_164331.avi", "set01_rep01", True))
            self.assertEqual(unlisted, {"video_path": "run_abc_20261117_164340.avi", "encoded": True})   # named after the run
            self.assertTrue((run.path / "run_abc_step01_20261117_164331.avi").is_file())
            self.assertTrue((run.path / "run_abc_20261117_164340.avi").is_file())
            self.assertFalse((project / "records" / "camera" / "run_abc_step01_20261117_164331.avi").exists())  # moved
            self.assertEqual([e["sequence"] for e in record["events"]], [1, 3])          # the tick between is not kept
            with run.csv_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[0]["pressure_0_mbar"], "1300")
            self.assertEqual((rows[3]["recording_id"], rows[3]["elapsed_s"]),
                             ("run_abc_step01_20261117_164331", "505.000"))             # 16:43:31 on the readings' clock
            self.assertEqual(rows[4]["elapsed_s"], "506.000")
            self.assertEqual(rows[3]["pressure_0_mbar"], "")
            self.assertEqual([run["name"] for run in RunStore(Path(tmp) / "data").list()],
                             ["Oil 3", "by_hand", "Oil 2"])                             # as control names runs, newest first
            self.assertEqual(sorted(entry["sample_id"] for entry in registry.sources()),
                             ["Oil 3 16:43", "Oil 3 16:43 set01_rep01", "by_hand 19:05"])   # the protocol's name, its time

    def test_a_video_recorded_by_hand_is_a_recording_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = old_project(Path(tmp))

            migrate_project(project, Path(tmp) / "data", copy=True)

            run = Run(Path(tmp) / "data" / "runs" / "20261116_190500_by_hand")
            record = run.read()
            self.assertEqual((record["kind"], record["state"], record["started_at"]),
                             ("recording", "completed", "2026-11-16T19:05:00"))
            self.assertEqual(record["recording"]["video_path"], "by_hand_20261116_190500.avi")
            self.assertEqual(record["recording"]["acquisition_fps"], 1000.0)
            self.assertTrue((run.path / "by_hand_20261116_190500.avi").is_file())
            self.assertTrue((project / "records" / "camera" / "by_hand_20261116_190500.avi").is_file())   # copied
            with run.csv_path.open(newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle))[0]["flow_0_ul_min"], "100")

    def test_migrating_again_leaves_the_runs_laid_out_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = old_project(Path(tmp))
            first = migrate_project(project, Path(tmp) / "data", copy=True)
            again = migrate_project(project, Path(tmp) / "data", copy=True)

            self.assertEqual(len(first.made), 3)
            self.assertEqual((again.made, sorted(again.skipped), again.videos),
                             ([], ["20261115_100000_oil_2", "20261116_190500_by_hand", "20261117_164326_oil_3"], 0))
            with self.assertRaises(ValueError):
                migrate_project(Path(tmp) / "nowhere", Path(tmp) / "data")

    def test_the_command_lays_the_runs_out_in_the_homes_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = old_project(Path(tmp))
            home = Path(tmp) / "home"

            code = main(["migrate", str(project), "--home", str(home), "--copy"])

            self.assertEqual(code, 0)
            self.assertEqual(len(list((home / "data" / "runs").iterdir())), 3)
            self.assertEqual(len(Registry(home / "analyze").sources()), 3)
            self.assertFalse((home / "control" / "rig.json").exists())                  # the rig only when asked
            self.assertEqual(main(["migrate", str(Path(tmp) / "nowhere"), "--home", str(home)]), 1)

    def test_the_rig_settings_go_into_the_home_only_when_asked(self):
        from admet.core.home import AdmetHome
        from admet.core.migrate import rig_settings

        with tempfile.TemporaryDirectory() as tmp:
            project = old_project(Path(tmp))
            home = Path(tmp) / "home"
            settings = rig_settings(project)
            self.assertEqual(sorted(settings), ["acquisition_mode", "calibration", "optics", "profiles", "qt_checkup"])
            self.assertNotIn("analysis_setup", settings)                                  # analyze's, not the rig's
            AdmetHome(home).ensure().update_rig(profiles=[{"id": "old"}], liquids=[{"id": "brine"}])

            self.assertEqual(main(["migrate", str(project), "--home", str(home), "--copy", "--rig"]), 0)

            rig = AdmetHome(home).rig()
            self.assertEqual(rig["calibration"], {"oil_l": {"profile": "p1"}})
            self.assertEqual([p["name"] for p in rig["profiles"]], ["gen_oil_l"])          # written over
            self.assertEqual(rig["liquids"], [{"id": "brine"}])                           # not in the project: kept
            self.assertEqual((rig["acquisition_mode"], rig["optics"]["microns_per_pixel"]), ("camera_fluidics", 2.5))
            with self.assertRaises(ValueError):
                rig_settings(Path(tmp) / "nowhere")


if __name__ == "__main__":
    unittest.main()
