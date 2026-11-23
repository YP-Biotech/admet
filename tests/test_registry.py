"""Analyze's registry: the folders it watches, the sources it knows, and results keyed by content."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from admet.core.registry import Registry, content_key, infer_engine, protocol_condition, replicate_of
from admet.core.runs import RunStore


def run_with_videos(data_dir, tag, videos=2, kind="protocol", condition=""):
    store = RunStore(data_dir)
    if kind == "recording":
        run = store.begin_recording(tag)
    else:
        run = store.begin({"plan_id": "p", "tag": tag, "condition": condition, "operation_id": "run_json_protocol",
                           "normalized_settings": {"protocol": {"name": "dropseq", "steps": [
                               {"name": "collect", "trigger_type": "time", "trigger_params": {"duration_s": 1}}]}}})
    names = []
    for index in range(videos):
        name = f"{run.name}_step{index + 1:02d}_20261119_1000{index:02d}.avi"
        (run.path / name).write_bytes(bytes([index]) * 2048)
        names.append(name)
    if kind == "recording":
        run.update(state="completed", recording={"video_path": names[0], "fluidics_csv": "run.csv"})
    else:
        run.update(state="completed", artifacts={"videos": [{"step": i + 1, "video_path": n} for i, n in enumerate(names)]})
    return run, names


class RegistryTests(unittest.TestCase):
    def test_roots_and_sources_round_trip_and_keep_their_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(Path(tmp) / "analyze")
            self.assertEqual((registry.roots(), registry.sources()), ([], []))
            self.assertTrue(registry.add_root(Path(tmp) / "data"))
            self.assertFalse(registry.add_root(Path(tmp) / "data"))               # watched already
            video = Path(tmp) / "clip.avi"
            video.write_bytes(b"x")
            entry = registry.add_source(video, sample_id="clip one")
            self.assertEqual((entry["engine"], entry["sample_id"], entry["active"], entry["condition"]),
                             ("opencv", "clip one", True, ""))
            self.assertIsNone(registry.add_source(video))                            # known already
            registry.update_source(video, active=False, settings={"fps": 24.0})
            again = Registry(Path(tmp) / "analyze")
            self.assertEqual(again.roots(), [str((Path(tmp) / "data").resolve())])
            self.assertEqual(again.source(video)["settings"], {"fps": 24.0})
            self.assertFalse(again.source(video)["active"])
            self.assertTrue(again.remove_source(video))
            self.assertFalse(again.remove_source(video))
            registry.remove_root(Path(tmp) / "data")
            self.assertEqual(Registry(Path(tmp) / "analyze").roots(), [])

    def test_the_list_is_saved_whole_in_the_screens_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(Path(tmp) / "analyze")
            a, b = Path(tmp) / "a.avi", Path(tmp) / "b.avi"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            registry.add_source(a)
            registry.add_source(b)
            registry.set_sources([{"path": str(b), "engine": "opencv", "sample_id": "second", "active": True,
                                   "cache_policy": "use", "settings": {}, "run": ""},
                                  {"path": str(a), "engine": "opencv", "sample_id": "first", "active": False,
                                   "cache_policy": "use", "settings": {"fps": 30.0}, "run": ""}])
            sources = Registry(Path(tmp) / "analyze").sources()
            self.assertEqual([s["sample_id"] for s in sources], ["second", "first"])
            self.assertEqual(sources[1]["settings"], {"fps": 30.0})
            self.assertTrue(all(s.get("added_at") for s in sources))                  # kept from the first add

    def test_scanning_a_data_folder_registers_every_video_with_its_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            run, names = run_with_videos(data, "Oil 3", condition="oil")
            by_hand, _ = run_with_videos(data, "cap", videos=1, kind="recording")
            going = RunStore(data).begin({"plan_id": "p", "tag": "going", "operation_id": "x",
                                           "normalized_settings": {"protocol": {"name": "d", "steps": [
                                               {"name": "s", "trigger_type": "time", "trigger_params": {"duration_s": 1}}]}}})
            (going.path / "going_step01_x.avi").write_bytes(b"v")                    # not in its record: not listed
            registry = Registry(Path(tmp) / "analyze")
            registry.add_root(data)

            added = registry.scan(data)

            def at(run_id):
                return f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]} {run_id[9:11]}:{run_id[11:13]}"
            self.assertEqual([entry["sample_id"] for entry in added], ["", "", ""])      # nothing written over
            self.assertEqual([(entry["protocol"], entry["run_time"], entry["replicate"]) for entry in added],
                             [("cap", at(by_hand.run_id), None), ("Oil 3", at(run.run_id), 1),
                              ("Oil 3", at(run.run_id), 2)])                            # fields of their own
            self.assertEqual([entry["condition"] for entry in added], ["", "oil", "oil"])       # the run's set, from control
            self.assertEqual({entry["run"] for entry in added}, {str(run.path.resolve()), str(by_hand.path.resolve())})
            self.assertTrue(all(entry["engine"] == "opencv" for entry in added))
            self.assertEqual(registry.scan(), [])                                     # nothing new the second time
            (run.path / "oil_3_step03_x.avi").write_bytes(b"new")
            run.update(artifacts={"videos": [{"step": i + 1, "video_path": n} for i, n in enumerate(
                [*names, "oil_3_step03_x.avi"])]})
            self.assertEqual([e["replicate"] for e in registry.scan()], [3])
            run.update(artifacts={"videos": [{"step": 1, "step_name": "set01_rep01", "video_path": "oil_3_r1.avi"},
                                             {"step": 2, "step_name": "set01_rep02", "video_path": "oil_3_r2.avi"}]})
            for video in ("oil_3_r1.avi", "oil_3_r2.avi"):
                (run.path / video).write_bytes(b"v")
            self.assertEqual([e["replicate"] for e in registry.scan()], [1, 2])          # set01_rep02: the second
            self.assertEqual(replicate_of({"step": 1, "step_name": "Record"}, 1), None)   # a lone video
            self.assertEqual(replicate_of({"step": 2, "step_name": "collect"}, 3), 2)

    def test_a_run_without_a_set_is_put_in_one_by_its_protocol_and_setpoints(self):
        parameterised = {"name": "dropseq_3n", "parameters": {"oil_value": ["Oil", 300], "cells_value": ["Cells", 40]},
                         "parameter_values": {"oil_value": 246.0, "cells_value": 69},
                         "steps": [{"name": "prime", "sensor_setpoints": {"0": 100}, "trigger_type": "time",
                                    "trigger_params": {"duration_s": 1}},
                                   {"name": "set01_rep01", "sensor_setpoints": {"0": "oil_value", "1": "cells_value",
                                                                                "2": "cells_value"},
                                    "trigger_type": "time", "trigger_params": {"duration_s": 1}, "record": True}]}
        self.assertEqual(protocol_condition({"normalized_settings": {"protocol": parameterised}}), "dropseq_3n o246 c69 b69")
        literal = {"name": "dropseq", "steps": [
            {"name": "run", "pressure_setpoints": {"0": 1300, "1": 540}, "sensor_setpoints": {"2": 0},
             "trigger_type": "time", "trigger_params": {"duration_s": 1}},
            {"name": "stop", "trigger_type": "time", "trigger_params": {"duration_s": 1}}]}
        self.assertEqual(protocol_condition({"normalized_settings": {"protocol": literal}}), "dropseq o1300mbar c540mbar b0")
        self.assertEqual(protocol_condition({"tag": "Oil 3", "normalized_settings": {"protocol": literal}}), "Oil 3 o1300mbar c540mbar b0")
        self.assertEqual(protocol_condition({"normalized_settings": {"protocol": {"name": "wash", "steps": []}}}), "wash")
        self.assertEqual(protocol_condition({"kind": "recording"}), "")                       # by hand: no protocol
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            store = RunStore(data)
            run = store.begin({"plan_id": "p", "tag": "", "operation_id": "run_json_protocol",
                               "normalized_settings": {"protocol": literal}})
            (run.path / "v.avi").write_bytes(b"v")
            run.update(state="completed", artifacts={"videos": [{"step": 1, "video_path": "v.avi"}]})
            chosen = store.begin({"plan_id": "q", "tag": "", "condition": "controls", "operation_id": "run_json_protocol",
                                  "normalized_settings": {"protocol": literal}})
            (chosen.path / "w.avi").write_bytes(b"w")
            chosen.update(state="completed", artifacts={"videos": [{"step": 1, "video_path": "w.avi"}]})
            registry = Registry(Path(tmp) / "analyze")
            self.assertEqual(sorted(e["condition"] for e in registry.scan(data)),
                             ["controls", "dropseq o1300mbar c540mbar b0"])             # the planned set wins

    def test_the_homes_data_folder_is_watched_from_the_start(self):
        from admet.app import analyze_registry

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            run, _ = run_with_videos(home / "data", "oil")                            # what the rig recorded
            elsewhere, _ = run_with_videos(Path(tmp) / "export", "cap", videos=1)

            store = analyze_registry(str(home))
            self.assertEqual(store.roots(), [str((home / "data").resolve())])
            self.assertEqual(len(store.sources()), 2)                                  # listed with no folder added
            again = analyze_registry(str(home), str(Path(tmp) / "export"))
            self.assertEqual(again.roots(), [str((home / "data").resolve()), str((Path(tmp) / "export").resolve())])
            self.assertEqual(len(again.sources()), 3)
            self.assertEqual(len(analyze_registry(str(home)).sources()), 3)            # remembered, nothing doubled

    def test_an_export_folder_and_a_single_run_scan_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, _ = run_with_videos(Path(tmp) / "data", "oil")
            server = Path(tmp) / "server"
            shutil.copytree(run.path, server / run.run_id)                           # as export lays them out
            registry = Registry(Path(tmp) / "analyze")
            self.assertEqual(len(registry.scan(server)), 2)
            self.assertEqual(len(Registry(Path(tmp) / "analyze2").scan(server / run.run_id)), 2)

    def test_a_result_is_keyed_by_content_and_detection_settings_not_by_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "a.avi"
            video.write_bytes(bytes(range(256)) * 20000)                              # 5 MB: head and tail hashed
            copy = Path(tmp) / "elsewhere" / "b.avi"
            copy.parent.mkdir()
            shutil.copy(video, copy)
            settings = {"min_area": 800, "fps": 24.0, "microns_per_pixel": 1.1, "skip_left_pct": 0.0,
                        "skip_right_pct": 0.0, "gate_pct": 50.0}
            key = content_key(video, settings)
            self.assertEqual(content_key(copy, settings), key)                        # a copy finds its result
            self.assertEqual(content_key(copy, {**settings, "fps": 30.0, "microns_per_pixel": 2.0}), key)   # read-time values
            self.assertEqual(content_key(copy, {**settings, "skip_left_pct": 10.0, "skip_right_pct": 5.0,
                                                "gate_pct": 60.0}), key)             # the window and the gate too
            self.assertNotEqual(content_key(copy, {**settings, "min_area": 600}), key)                      # detection
            video.write_bytes(bytes(range(256)) * 20000 + b"x")
            self.assertNotEqual(content_key(video, settings), key)
            registry = Registry(Path(tmp) / "analyze")
            self.assertEqual(registry.result_dir(copy, settings), Path(tmp) / "analyze" / "results" / key)
            images = Path(tmp) / "imgs"
            images.mkdir()
            (images / "a.png").write_bytes(b"png")
            self.assertEqual(infer_engine(images), "cellpose")
            self.assertEqual(len(content_key(images)), 24)

    def test_a_damaged_registry_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(Path(tmp) / "analyze")
            registry.root.mkdir()
            registry.path.write_text("{oops")
            self.assertEqual(registry.sources(), [])
            registry.add_root(tmp)
            self.assertEqual(json.loads(registry.path.read_text())["roots"], [str(Path(tmp).resolve())])


if __name__ == "__main__":
    unittest.main()
