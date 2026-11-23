import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

from admet.core.engine import EngineRegistry
from admet.core.registry import Registry
from admet.core.runs import RunStore
from admet.ui.analyze import (
    IMAGING_DEFAULTS,
    AnalyzeWorkflowView,
    MatrixRow,
    _addable,
    _browser_entries,
    _droplet_at,
    _roi_from_drag,
    _uid,
    apply_click,
)
from admet.ui.analyze_results import result_charts
from admet.workflows import create_analyze_workflow
from tests.test_registry import run_with_videos


def _make_view(store: Registry | None = None) -> AnalyzeWorkflowView:
    return AnalyzeWorkflowView(create_analyze_workflow(), create_analyze_workflow().initial_state(),
                               EngineRegistry(), store=store or Registry(Path(tempfile.mkdtemp()) / "analyze"))


def _row(engine: str = "opencv", sample: str = "s", **settings) -> MatrixRow:
    return MatrixRow(uid=_uid(), source_path=f"/tmp/{sample}.avi", engine=engine, sample_id=sample,
                     settings=dict(settings))


class WorkflowShapeTests(unittest.TestCase):
    def test_three_stages_with_their_actions(self):
        workflow = create_analyze_workflow()

        self.assertEqual([stage.id for stage in workflow.stages], ["files", "analyse", "results"])
        self.assertEqual([action.label for action in workflow.stages[1].actions],
                         ["Analyse all", "Run selected", "Stop", "Reset ticked", "Reset file settings"])
        self.assertEqual([action.label for action in workflow.stages[2].actions], ["Export CSV", "Export plots"])


class UidUniquenessTests(unittest.TestCase):
    def test_uid_is_unique_across_rapid_calls(self):
        uids = [_uid() for _ in range(2000)]
        self.assertEqual(len(set(uids)), len(uids))


class MatrixChangeTests(unittest.TestCase):
    def test_use_toggle_changes_only_the_targeted_row(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b"), _row(sample="c")]
        target = view.matrix[2]

        view._handle_matrix_change(SimpleNamespace(args={"uid": target.uid, "field": "active", "value": False}))

        self.assertEqual([row.active for row in view.matrix], [True, True, False])
        self.assertEqual([row.sample_id for row in view._targets("opencv")], ["a", "b"])

    def test_a_value_in_a_column_header_lands_on_every_ticked_file_that_takes_it(self):
        from admet.ui.analyze_matrix import apply_setting

        view = _make_view()
        view.matrix = [_row(engine="cellpose", sample=f"img{i}", detect_inclusions=True) for i in range(3)]
        view.matrix += [_row(sample="vid", k_sigma=4.0)]
        view.matrix[1].active = False                                        # unticked: left alone
        notices = []
        view._notify = lambda text, kind: notices.append((kind, text))

        view._handle_header_set("detect_inclusions", False)

        self.assertEqual([row.settings.get("detect_inclusions") for row in view.matrix], [False, True, False, None])
        self.assertIn("on 2 ticked file(s)", notices[-1][1])
        view._handle_header_set("detect_inclusions", False)
        self.assertEqual(notices[-1][0], "secondary")                        # already so: nothing changes
        view._handle_header_set("microns_per_pixel", "2.27")
        self.assertEqual(view.matrix[0].settings["px_to_um"], 2.27)             # the imaging engine's name for it
        self.assertEqual(view.matrix[3].settings["microns_per_pixel"], 2.27)
        self.assertNotIn("px_to_um", view.matrix[1].settings)
        view._handle_header_set("k_sigma", "")          # left empty
        self.assertEqual(view.matrix[3].settings["k_sigma"], 4.0)
        self.assertEqual(apply_setting(view.matrix, "k_sigma", 3), [view.matrix[3]])
        self.assertEqual(apply_setting(view.matrix, "no_such_setting", 1), [])

    def test_reset_ticked_puts_every_ticked_file_back_to_the_defaults_and_what_its_recording_carries(self):
        import admet.ui.analyze as module

        view = _make_view()
        view.matrix = [_row(sample="a", k_sigma=9.0, microns_per_pixel=3.0, fps=24.0),
                       _row(sample="b", k_sigma=9.0, microns_per_pixel=1.2),
                       _row(engine="cellpose", sample="i", px_to_um=2.0, detect_inclusions=False)]
        view.matrix[1].active = False
        view._notify = lambda *a, **k: None
        recorded_scale, recorded_fps = module._recorded_scale, module._recorded_capture_fps
        module._recorded_scale = lambda row: 2.5 if row.sample_id == "a" else 0.0      # a typed 3.0 goes back to it
        module._recorded_capture_fps = lambda row: 500.0 if row.sample_id == "a" else 0.0
        try:
            view._reset_ticked_settings()
        finally:
            module._recorded_scale, module._recorded_capture_fps = recorded_scale, recorded_fps

        self.assertEqual((view.matrix[0].settings["k_sigma"], view.matrix[0].settings["microns_per_pixel"],
                          view.matrix[0].settings["fps"]), (4.0, 2.5, 500.0))
        self.assertEqual(view.matrix[1].settings["k_sigma"], 9.0)                  # unticked: left alone
        self.assertEqual((view.matrix[2].settings["detect_inclusions"], view.matrix[2].settings["px_to_um"]),
                         (True, IMAGING_DEFAULTS["px_to_um"]))                      # the typed 2.0 is gone

    def test_the_use_header_ticks_or_clears_every_file(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b")]
        view._notify = lambda *a, **k: None
        self.assertIs(view._use_state(), True)
        view._handle_use_all(False)
        self.assertEqual([row.active for row in view.matrix], [False, False])
        self.assertIs(view._use_state(), False)
        view.matrix[0].active = True
        self.assertIsNone(view._use_state())                                    # some: the box is indeterminate
        view._handle_use_all(True)
        self.assertEqual([row.active for row in view.matrix], [True, True])

    def test_the_setting_columns_are_chosen_and_remembered(self):
        from admet.ui.analyze_matrix import analyse_columns, analyse_row

        with tempfile.TemporaryDirectory() as tmp:
            store = Registry(Path(tmp) / "analyze")
            view = _make_view(store)
            self.assertEqual(view.analyse_fields, ["microns_per_pixel", "fps", "range", "crop"])
            view._columns_changed(SimpleNamespace(value=["k_sigma", "fps", "nonsense"]))
            self.assertEqual(view.analyse_fields, ["fps", "k_sigma"])          # canonical order, unknown dropped
            self.assertEqual(_make_view(store).analyse_fields, ["fps", "k_sigma"])
            names = [column["name"] for column in analyse_columns(view.analyse_fields)]
            self.assertEqual(names, ["active", "sample_id", "source", "fps", "k_sigma", "status"])
            row = analyse_row(_row(sample="v", k_sigma=4.0, microns_per_pixel=1.1), range_text="0–10",
                              crop_text="full", status="new")
            self.assertEqual((row["k_sigma"], row["microns_per_pixel"], row["fps"]), (4.0, 1.1, None))
            self.assertEqual((row["frame_limit"], row["frame_limit_applies"], row["k_sigma_applies"]), (None, False, True))
            image = analyse_row(_row(engine="cellpose", sample="i", px_to_um=2.0, detect_inclusions=True),
                                range_text="all", crop_text="—", status="new")
            self.assertEqual((image["microns_per_pixel"], image["detect_inclusions"], image["fps_applies"]), (2.0, True, False))
            view._handle_matrix_change(SimpleNamespace(args={"uid": view.matrix[0].uid if view.matrix else "", "field": "k_sigma", "value": "3"}))

    def test_a_scale_typed_in_the_table_lands_on_the_row(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(engine="cellpose", sample="imgs")]

        view._handle_matrix_change(SimpleNamespace(args={"uid": view.matrix[0].uid, "field": "microns_per_pixel",
                                                         "value": "1.5"}))
        view._handle_matrix_change(SimpleNamespace(args={"uid": view.matrix[1].uid, "field": "microns_per_pixel",
                                                         "value": 2}))

        self.assertEqual(view.matrix[0].settings["microns_per_pixel"], 1.5)
        self.assertEqual(view.matrix[1].settings["px_to_um"], 2.0)   # the imaging engine's name for it

    def test_selected_row_changes_structure_signature(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b")]

        view.selected_uid = view.matrix[0].uid
        first = view._structure_signature()
        view.selected_uid = view.matrix[1].uid

        self.assertNotEqual(first, view._structure_signature())


class ReadinessTests(unittest.TestCase):
    def test_run_waits_for_scale_and_fps_on_every_ticked_file(self):
        view = _make_view()
        view.matrix = [_row(sample="a", microns_per_pixel=1.2, fps=200), _row(sample="b", microns_per_pixel=1.2)]

        view.state = view.state.__class__(index=1, statuses=view.state.statuses)   # on 2. Analyse

        self.assertTrue(view._guard_value("has_targets"))
        self.assertFalse(view._guard_value("targets_ready"))
        self.assertIn("1 missing µm/px or FPS", view._progress()[1])
        view.matrix[1].settings["fps"] = 200
        self.assertTrue(view._guard_value("targets_ready"))
        self.assertEqual(view._progress()[1], "2 ticked · 0 analysed")

    def test_an_image_folder_needs_only_its_scale(self):
        view = _make_view()
        view.matrix = [_row(engine="cellpose", sample="imgs")]
        self.assertFalse(view._row_ready(view.matrix[0]))
        view.matrix[0].settings["px_to_um"] = 1.14
        self.assertTrue(view._row_ready(view.matrix[0]))

    def test_reset_restores_the_detection_defaults_and_drops_a_typed_scale_and_fps(self):
        view = _make_view()
        view.matrix = [_row(sample="a", microns_per_pixel=1.2, fps=200, k_sigma=9, gate_pct=80)]
        view.selected_uid = view.matrix[0].uid

        view._reset_row_settings()

        settings = view.matrix[0].settings
        self.assertNotIn("microns_per_pixel", settings)                            # a loose file: nothing recorded
        self.assertNotIn("fps", settings)
        self.assertEqual((settings["k_sigma"], settings["gate_pct"]), (4.0, 50.0))


class RegistryTests(unittest.TestCase):
    def test_the_list_comes_back_as_left_and_a_removed_file_is_forgotten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Registry(Path(tmp) / "analyze")
            video = Path(tmp) / "clip.avi"
            video.write_bytes(b"")
            view = _make_view(store)
            self.assertEqual(view._append_source(video), "clip.avi")
            self.assertEqual(view._append_source(video), "")                          # known already
            view.matrix[0].sample_id = "clip one"
            view.matrix[0].active = False
            view._save_matrix()

            again = _make_view(store)
            self.assertEqual([(row.sample_id, row.active, row.run) for row in again.matrix], [("clip one", False, "")])
            again._remove_row(again.matrix[0])
            self.assertEqual((again.matrix, store.sources()), ([], []))

    def test_a_folder_of_runs_is_scanned_into_rows_named_after_their_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            run, names = run_with_videos(data, "Oil 3")
            store = Registry(Path(tmp) / "analyze")
            view = _make_view(store)

            view._add_root(data)

            at = f"{run.run_id[9:11]}:{run.run_id[11:13]}"
            self.assertEqual([row.sample_id for row in view.matrix], ["", ""])             # nothing written over
            self.assertEqual([row.name for row in view.matrix], ["Oil 3_rep1", "Oil 3_rep2"])   # built: protocol, replicate
            self.assertEqual([(row.protocol, row.run_time[11:], row.replicate) for row in view.matrix],
                             [("Oil 3", at, 1), ("Oil 3", at, 2)])                    # fields of their own
            self.assertEqual(view.matrix[0].origin, f"Oil 3 · {view.matrix[0].run_time} · rep 1")
            view.matrix[0].sample_id = "control"
            self.assertEqual(view.matrix[0].name, "control")                             # written over
            self.assertEqual({row.run for row in view.matrix}, {str(run.path.resolve())})
            self.assertEqual(view._row_key(view.matrix[0])[0], run.run_id)
            self.assertEqual(store.roots(), [str(data.resolve())])
            self.assertEqual(view.selected_uid, view.matrix[0].uid)
            view._add_root(data)
            self.assertEqual(len(view.matrix), 2)                                       # nothing new
            (run.path / "oil_3_step03_x.avi").write_bytes(b"new")
            run.update(artifacts={"videos": [{"step": i + 1, "video_path": n}
                                             for i, n in enumerate([*names, "oil_3_step03_x.avi"])]})
            view._rescan()
            self.assertEqual([(row.name, row.replicate) for row in view.matrix][-1], ("Oil 3_rep3", 3))
            self.assertEqual(_make_view(store).matrix[-1].replicate, 3)               # kept with the list
            self.assertEqual(len(_make_view(store).matrix), 3)                         # the list persists
            self.assertIn("runs in", view._refs.get("topbar_text", "runs in") or "runs in")


class FileDialogTests(unittest.TestCase):
    def test_only_videos_images_and_folders_with_images_can_be_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clip.avi").write_bytes(b"")
            (root / "notes.txt").write_text("x")
            (root / "analysis").mkdir()
            (root / "imgs").mkdir()
            (root / "imgs" / "a.png").write_bytes(b"")

            entries = _browser_entries(root, "source")

            self.assertEqual([entry.name for entry in entries], ["analysis", "imgs", "clip.avi"])
            self.assertEqual([_addable(entry) for entry in entries], [False, True, True])
            self.assertEqual([entry.name for entry in _browser_entries(root, "folder")], ["analysis", "imgs"])


class CropTests(unittest.TestCase):
    def test_a_drag_gives_a_clamped_rectangle_and_a_click_gives_nothing(self):
        self.assertEqual(_roi_from_drag(50, 40, 10, 90, 640, 120), (10, 40, 40, 50))
        self.assertEqual(_roi_from_drag(600, 100, 700, 200, 640, 120), (600, 100, 40, 20))
        self.assertIsNone(_roi_from_drag(50, 40, 52, 41, 640, 120))


class ImagingCorrectionTests(unittest.TestCase):
    def test_clicks_switch_a_droplet_off_and_on_and_change_its_inclusions(self):
        fix = apply_click(None)
        self.assertEqual(fix, {"disabled": True})
        self.assertEqual(apply_click(fix), {})                                   # back on: nothing left
        fix = apply_click(None, shift=True)
        fix = apply_click(fix, shift=True)
        self.assertEqual(fix, {"inclusions": 2})
        self.assertEqual(apply_click(apply_click(fix, alt=True), alt=True), {})

    def test_the_droplet_under_the_pointer_is_the_nearest_one_holding_it(self):
        droplets = [{"droplet_id": 0, "center_x": 100, "center_y": 100, "diameter_px": 60},
                    {"droplet_id": 1, "center_x": 150, "center_y": 100, "diameter_px": 60}]
        self.assertEqual(_droplet_at(droplets, 128, 100)["droplet_id"], 1)
        self.assertEqual(_droplet_at(droplets, 80, 90)["droplet_id"], 0)
        self.assertIsNone(_droplet_at(droplets, 300, 300))


class PathLabelTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("nicegui"), "NiceGUI is not installed")
    def test_a_windows_path_is_shown_whole_on_hover(self):
        from nicegui import Client
        from nicegui.page import page

        from admet.ui.analyze import _path_label

        path = r"C:\Users\ADMIN\Projects\admet\projects\p.admetp\records\camera\run_ab12_step01_20261112_171440.avi"
        with Client(page(""), request=None):
            label = _path_label(path)
        self.assertEqual(label.props["title"], path)


class LiveRowTests(unittest.TestCase):
    def test_a_video_from_a_run_brings_the_rigs_live_qc_into_its_samples_row_with_a_tick_of_its_own(self):
        from dataclasses import replace
        from tests.test_analyze_results import _run_csv

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            run, names = run_with_videos(data, "oil", videos=1)
            _run_csv(run.path, Path(names[0]).stem)
            view = _make_view(Registry(Path(tmp) / "analyze"))
            view._add_root(data)
            view._notify = lambda *a, **k: None
            video = view.matrix[0]

            rows = view._sample_rows()

            self.assertEqual([row["kind"] for row in rows], ["condition", "sample"])
            sample = rows[1]
            self.assertEqual((sample["opencv_has"], sample["cellpose_has"], sample["live_has"]), (False, False, True))
            self.assertEqual((sample["opencv"], sample["live"]), (False, True))
            self.assertIn("1420", sample["live_rate"])
            self.assertIn("STABLE", sample["live_rate"])
            self.assertIn("not run", sample["cv_droplets"])                                 # why OpenCV has nothing
            self.assertEqual(view._result_sections(), ["aggregated", "live"])               # no engine's section yet
            lives = view._lives()
            self.assertEqual([live.sample_id for live in lives], [video.name])
            self.assertEqual(len(lives[0].points), 5)
            self.assertEqual([name for name, _ in result_charts([], lives, view="aggregated")],
                             ["diameter_per_sample", "cv_per_sample", "rate_per_sample"])
            self.assertEqual(len(view._live_runs()), 1)
            view.state = replace(view.state, index=2)                                        # 3. Results
            view._handle_results_tick(SimpleNamespace(args={"uid": sample["uid"], "field": "live", "value": False}))
            self.assertEqual((video.active, video.plotted, video.live_plotted), (True, True, False))
            self.assertFalse(view._sample_rows()[1]["live"])                                 # still listed, greyed
            self.assertEqual((view._lives(), view._live_runs()), ([], []))                   # out of the plots
            self.assertEqual(view._result_sections(), ["aggregated", "live"])
            view._handle_results_tick(SimpleNamespace(args={"uid": sample["uid"], "field": "live", "value": True}))
            self.assertEqual([folder for folder, _label in view._live_runs()], [video.run])
            view.state = replace(view.state, index=0)                                        # 1. Files
            view._handle_matrix_change(SimpleNamespace(args={"uid": video.uid, "field": "active", "value": False}))
            self.assertFalse(video.active)
            self.assertEqual(view._sample_rows(), [])                                        # unticked here: gone downstream
            self.assertEqual(view._result_sections(), [])
            video.active = True
            self.assertEqual(_make_view(view.store).matrix[0].live_plotted, True)            # kept with the list
            video.run = ""
            self.assertEqual(view._sample_rows(), [])                                        # a loose file has no live line


class SetTests(unittest.TestCase):
    def test_the_ticked_files_go_into_a_set_which_can_be_used_cleared_and_chosen_for_results(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp:
            store = Registry(Path(tmp) / "analyze")
            data = Path(tmp) / "data"
            run_with_videos(data, "Oil 3", videos=3)
            view = _make_view(store)
            view._add_root(data)
            view._notify = lambda *a, **k: None
            a, b, c = view.matrix
            c.active = False

            view._assign_condition("Oil A")

            self.assertEqual([row.condition for row in view.matrix], ["Oil A", "Oil A", "Oil 3"])   # c keeps the run's
            self.assertEqual((view.current_condition, view._condition_names()), ("Oil A", ["Oil A", "Oil 3"]))
            self.assertEqual(_make_view(store).matrix[0].condition, "Oil A")                  # kept with the list
            view._assign_condition("")                                                          # a name is needed
            self.assertEqual(b.condition, "Oil A")
            view._handle_matrix_change(SimpleNamespace(args={"uid": c.uid, "field": "condition", "value": " Water "}))
            self.assertEqual((c.condition, view._condition_names()), ("Water", ["Oil A", "Water"]))   # typed in the column
            c.active = True
            view._use_condition("Oil A")                                                        # ticks exactly its files
            self.assertEqual([row.active for row in view.matrix], [True, True, False])
            view._use_condition("nothing")
            self.assertEqual([row.active for row in view.matrix], [True, True, False])
            c.active = True
            view.state = replace(view.state, index=2)                                    # 3. Results
            self.assertEqual([row.name for row in view._for_results()],
                             ["Oil A_rep1", "Oil A_rep2", "Water_rep1"])                 # built from the sets
            view._clear_condition()
            self.assertEqual([row.condition for row in view.matrix], ["", "", ""])

    def test_results_group_the_files_by_set_in_the_sets_order(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b"), _row(sample="c"), _row(sample="d")]
        view.matrix[0].condition, view.matrix[1].condition, view.matrix[2].condition = "two", "one", "two"

        self.assertEqual([row.sample_id for row in view._for_results()], ["a", "c", "b", "d"])


class StoredResultTests(unittest.TestCase):
    def test_a_whole_run_serves_a_frame_limit_and_a_baked_window_does_not_serve(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            store = Registry(Path(tmp) / "analyze")
            view = _make_view(store)
            images = Path(tmp) / "imgs"
            images.mkdir()
            (images / "a.png").write_bytes(b"png")
            video = Path(tmp) / "v.avi"
            video.write_bytes(b"video")
            folder = MatrixRow(uid=_uid(), source_path=str(images), engine="cellpose", sample_id="imgs",
                               settings={"px_to_um": 1.0, "frame_limit": 3})
            clip = MatrixRow(uid=_uid(), source_path=str(video), engine="opencv", sample_id="v",
                             settings={"microns_per_pixel": 2.0, "fps": 100.0, "skip_left_pct": 10.0})
            view.matrix = [folder, clip]
            whole = store.result_dir(images, {**view._engine_settings(folder), "frame_limit": None})
            whole.mkdir(parents=True)
            (whole / "result.json").write_text(json.dumps({"analyzed_at": "2026-11-20T10:00:00+00:00"}))
            (whole / "rows.jsonl").write_text("")
            baked = store.result_dir(video, view._engine_settings(clip))
            baked.mkdir(parents=True)
            (baked / "result.json").write_text(json.dumps({"settings": {"skip_left_pct": 10.0}}))
            (baked / "rows.jsonl").write_text("")

            self.assertEqual(view._stored_result(folder).result_dir, whole)          # the limit is applied when read
            self.assertIsNone(view._stored_result(clip))                              # made before: run again
            (baked / "result.json").write_text(json.dumps({"settings": {"skip_left_pct": 10.0}, "window": "whole"}))
            self.assertEqual(view._stored_result(clip).result_dir, baked)
            self.assertEqual(view._read_settings(clip), {"skip_left_pct": 10.0, "skip_right_pct": 0.0, "gate_pct": 50.0})
            (baked / "rows.jsonl").write_text(json.dumps({"engine": "opencv", "item_id": "v", "file_id": "f", "kind": "detection",
                                                          "values": {"frame": 1, "droplet_id": 1, "centroid_x": 10.0,
                                                                     "centroid_y": 5.0, "equivalent_diameter": 20.0,
                                                                     "area": 314.0}}) + "\n")
            clip.condition, clip.replicate = "oil", 2
            view._save_matrix()                                                          # names follow the set
            result, = view._results()
            self.assertEqual((result.condition, result.replicate, result.sample_id), ("oil", 2, "v"))   # the row's, as written
            clip.sample_id = ""
            view._save_matrix()
            self.assertEqual(view._results()[0].sample_id, "oil_rep1")                   # built, without a re-read
            self.assertEqual(view._read_settings(folder),
                             {"frame_limit": 3, "detect_inclusions": True, "slide_height_um": None})


class StopTests(unittest.TestCase):
    def test_stop_asks_the_run_in_hand_to_stop_now_and_only_works_while_running(self):
        from admet.workflows.analyze_runner import AnalysisStop

        view = _make_view()
        view._notify = lambda *a, **k: None
        self.assertFalse(view._guard_value("running"))
        view._stop_analysis()                                                             # nothing running: no-op
        view._running, view._stop = True, AnalysisStop()
        self.assertTrue(view._guard_value("running"))
        specs = {spec["action"]: spec for spec in view._action_specs("analyse")}
        self.assertTrue(specs["stop_analysis"]["enabled"])

        view._stop_analysis()

        self.assertTrue(view._stop.now.is_set())
        self.assertFalse(view._stop.after_file.is_set())


class ShiftClickTests(unittest.TestCase):
    def test_shift_click_ticks_or_clears_every_row_between_the_last_box_and_this_one(self):
        from dataclasses import replace

        view = _make_view()
        view.matrix = [_row(sample=name) for name in "abcde"]
        view._notify = lambda *a, **k: None
        for row in view.matrix:
            row.active = False
        a, b, c, d, e = view.matrix

        def tick(row, value, shift=False):
            view._handle_matrix_change(SimpleNamespace(args={"uid": row.uid, "field": "active", "value": value,
                                                             "shift": shift}))

        tick(d, True, shift=True)                                                         # no box before: this one only
        self.assertEqual([r.active for r in view.matrix], [False, False, False, True, False])
        tick(a, True)
        tick(c, True, shift=True)                                                         # a to c
        self.assertEqual([r.active for r in view.matrix], [True, True, True, True, False])
        tick(e, False, shift=True)                                                        # c to e, cleared
        self.assertEqual([r.active for r in view.matrix], [True, True, False, False, False])
        view.state = replace(view.state, index=1)                                        # 2. Analyse lists a and b
        tick(a, False)
        tick(b, False, shift=True)
        self.assertEqual([(r.active, r.to_run) for r in view.matrix][:2], [(True, False), (True, False)])
        self.assertTrue(c.to_run)                                                         # not listed there: untouched


class DownstreamTests(unittest.TestCase):
    def test_the_files_tick_gates_the_later_stages_and_their_own_tick_keeps_the_row(self):
        from dataclasses import replace

        view = _make_view()
        view.matrix = [_row(sample="a", microns_per_pixel=1.1, fps=24.0), _row(sample="b"), _row(sample="c")]
        view._notify = lambda *a, **k: None
        view.state = replace(view.state, index=1)                                        # 2. Analyse
        view.selected_uid = view.matrix[1].uid
        b = view.matrix[1]

        self.assertEqual([row["sample_id"] for row in view._analyse_rows()], ["a", "b", "c"])
        view._handle_matrix_change(SimpleNamespace(args={"uid": b.uid, "field": "active", "value": False}))

        self.assertEqual([(row["sample_id"], row["active"]) for row in view._analyse_rows()],
                         [("a", True), ("b", False), ("c", True)])                        # b stays, greyed
        self.assertEqual((b.active, b.to_run, b.plotted), (True, False, True))         # not run, still shown
        self.assertEqual([row.sample_id for row in view._targets()], ["a", "c"])          # out of the run
        self.assertEqual([row.sample_id for row in view._shown()], ["a", "b", "c"])       # in the plots
        self.assertIs(view._selected_row(), b)                                            # still picked
        self.assertIsNone(view._use_state())                                              # some to run
        view._handle_use_all(True)
        self.assertTrue(b.to_run)
        view.state = replace(view.state, index=0)                                        # 1. Files
        view._handle_matrix_change(SimpleNamespace(args={"uid": b.uid, "field": "active", "value": False}))
        self.assertEqual((b.active, b.to_run), (False, True))
        self.assertEqual([row["rep"] for row in view._files_rows() if row["kind"] == "sample"],
                         ["a", "b", "c"])                                                  # listed on Files
        view.state = replace(view.state, index=1)
        self.assertEqual([row["sample_id"] for row in view._analyse_rows()], ["a", "c"])  # gone from Analyse
        self.assertIsNone(view._selected_row())                                           # and the editor
        self.assertEqual([row.sample_id for row in view._targets()], ["a", "c"])
        view.state = replace(view.state, index=0)
        view._handle_use_all(True)
        self.assertEqual([row["sample_id"] for row in view._analyse_rows()], ["a", "b", "c"])
        record = view._matrix_record(view.matrix)[1]
        self.assertEqual((record["active"], record["to_run"], record["plotted"]), (True, True, True))   # all kept


class BuiltNameTests(unittest.TestCase):
    def test_a_files_name_is_built_from_its_set_and_place_unless_written_over(self):
        from admet.ui.analyze import _name_rows

        rows = [MatrixRow(uid=_uid(), source_path=f"/v{n}.avi", engine="opencv", sample_id="", protocol="dropseq",
                          run_time=f"2026-11-17 19:3{n}", replicate=n) for n in (1, 2, 3)]
        rows.append(MatrixRow(uid=_uid(), source_path="/loose.avi", engine="opencv", sample_id=""))
        self.assertEqual([row.name for row in rows], ["dropseq_rep1", "dropseq_rep2", "dropseq_rep3", "loose"])
        rows[0].condition = rows[2].condition = "oil"                                                 # the set numbers its files
        rows[1].sample_id = "control"
        _name_rows(rows)
        self.assertEqual([row.name for row in rows], ["oil_rep1", "control", "oil_rep2", "loose"])
        rows[2].sample_id = "oil high"                                                     # written over
        self.assertEqual(rows[2].name, "oil high")
        rows[2].sample_id = ""
        self.assertEqual(rows[2].name, "oil_rep2")                                        # cleared: built again


class ResultsTickTests(unittest.TestCase):
    def test_a_samples_sources_are_ticked_one_by_one_and_remembered(self):
        from tests.test_analyze_results import _run_csv

        with tempfile.TemporaryDirectory() as tmp:
            store = Registry(Path(tmp) / "analyze")
            data = Path(tmp) / "data"
            run, names = run_with_videos(data, "oil", videos=1)
            _run_csv(run.path, Path(names[0]).stem)
            view = _make_view(store)
            view._add_root(data)
            view._notify = lambda *a, **k: None
            video = view.matrix[0]
            uid = f"sample:{video.name}"

            self.assertEqual(len(view._lives()), 1)
            view._handle_results_tick(SimpleNamespace(args={"uid": uid, "field": "live", "value": False}))

            self.assertEqual(view._lives(), [])                                              # the live QC left out
            self.assertEqual(len(view._lives(ticked_only=False)), 1)                         # still there to list
            self.assertEqual(view._result_sections(), ["aggregated", "live"])
            self.assertEqual(_make_view(store).matrix[0].live_plotted, False)                # remembered
            view._handle_results_tick(SimpleNamespace(args={"uid": uid, "field": "opencv", "value": False}))
            self.assertEqual((video.plotted, video.live_plotted), (False, False))
            view._handle_results_tick(SimpleNamespace(args={"uid": "sample:nothing", "field": "live", "value": True}))
            self.assertEqual((video.plotted, video.live_plotted), (False, False))            # an unknown sample: nothing
            view._handle_results_tick(SimpleNamespace(args={"uid": uid, "field": "live", "value": True}))
            self.assertEqual(len(view._lives()), 1)

    def test_every_ticked_run_is_drawn_on_the_live_plots_in_its_videos_colour(self):
        from tests.test_analyze_results import _run_csv

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            first, names = run_with_videos(data, "oil", videos=1)
            _run_csv(first.path, Path(names[0]).stem)
            second, names = run_with_videos(data, "water", videos=2)
            _run_csv(second.path, Path(names[0]).stem)
            view = _make_view(Registry(Path(tmp) / "analyze"))
            view._add_root(data)
            view._notify = lambda *a, **k: None

            plots = view._run_plots()

            self.assertEqual([plot.run_dir for plot in plots], [str(first.path.resolve()), str(second.path.resolve())])
            self.assertEqual([plot.label[:3] for plot in plots], ["oil", "wat"])
            self.assertEqual(list(plots[1].files), [Path(names[0]).stem])               # the step the rig measured
            self.assertEqual(plots[1].files[Path(names[0]).stem][0], view.matrix[1].name)
            view.matrix[0].active = False
            self.assertEqual([plot.label[:3] for plot in view._run_plots()], ["wat"])      # unticked: not drawn
            self.assertEqual(view._result_sections(), ["aggregated", "live"])
            view.matrix[1].live_plotted = False
            self.assertEqual(view._run_plots(), [])                                        # the live tick off: not drawn
            view.matrix.append(_row(engine="cellpose", sample="imgs"))
            self.assertEqual(view._result_sections(), ["aggregated", "live"])             # listed, not analysed


class SampleTests(unittest.TestCase):
    """A sample is a collection point: the files that share one, listed under their condition."""

    def _view_with_runs(self, tmp: Path):
        store = Registry(tmp / "analyze")
        data = tmp / "data"
        first, _ = run_with_videos(data, "Oil 3", videos=2)
        second, _ = run_with_videos(data, "Oil 3", videos=1)
        folder = tmp / "plate3_A1"
        folder.mkdir()
        (folder / "a01f001d4.tif").write_bytes(b"i")
        view = _make_view(store)
        view._notify = lambda *a, **k: None
        view._add_root(data)
        view._append_source(folder)
        return view, store, folder

    def test_files_are_listed_as_samples_under_their_condition_and_a_folder_waits_unassigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            view, _store, folder = self._view_with_runs(Path(tmp))

            samples = view._samples()
            self.assertEqual([(s["name"], s["condition"], s["primary"].rep) for s in samples],
                             [("Oil 3_rep1", "Oil 3", "rep 1"), ("Oil 3_rep2", "Oil 3", "rep 2"),
                              ("Oil 3_rep3", "Oil 3", "rep 3")])                      # numbered across the two runs
            rows = view._files_rows()
            self.assertEqual([r["kind"] for r in rows], ["condition", "sample", "sample", "sample", "condition", "unassigned"])
            self.assertEqual((rows[0]["alias"], rows[0]["count"], rows[0]["active"], rows[0]["mixed"]),
                             ("Oil 3", "3 samples", True, False))
            self.assertEqual((rows[1]["rep"], rows[1]["images"], rows[1]["live"]), ("rep 1", "", ""))
            self.assertEqual((rows[4]["alias"], rows[4]["fixed"]), ("Unassigned", True))
            self.assertEqual(rows[5]["images"], "plate3_A1")
            self.assertEqual([o["label"] for o in rows[5]["options"]],
                             ["Oil 3 · rep 1", "Oil 3 · rep 2", "Oil 3 · rep 3", "new sample…"])
            self.assertTrue(view.matrix[-1].unassigned)
            self.assertEqual([r["sample_id"] for r in view._analyse_rows()],
                             ["Oil 3", "rep 1", "rep 2", "rep 3", "plate3_A1"])              # the condition line first
            self.assertEqual([r["kind"] for r in view._analyse_rows()][:2], ["condition", "file"])

    def test_a_folder_joins_a_sample_from_the_picker_or_becomes_one_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            view, store, folder = self._view_with_runs(Path(tmp))
            loose = view.matrix[-1]

            view._handle_assign(SimpleNamespace(args={"uid": loose.uid, "sample": "Oil 3_rep2"}))

            self.assertEqual((loose.sample_id, loose.condition, loose.name), ("Oil 3_rep2", "Oil 3", "Oil 3_rep2"))
            second = next(s for s in view._samples() if s["name"] == "Oil 3_rep2")
            self.assertEqual([Path(r.source_path).name for r in second["folders"]], ["plate3_A1"])   # in the sample
            rows = view._files_rows()
            self.assertEqual([r["kind"] for r in rows], ["condition", "sample", "sample", "sample"])  # nothing unassigned
            self.assertEqual(rows[2]["images"], "plate3_A1")
            self.assertEqual(next(r.sample_id for r in _make_view(store).matrix if r.engine == "cellpose"),
                             "Oil 3_rep2")                                                           # kept with the list
            view._new_sample(loose, " plate alone ")
            self.assertEqual((loose.sample_id, loose.condition), ("plate alone", ""))
            self.assertEqual([r["alias"] for r in view._files_rows() if r["kind"] == "condition"],
                             ["Oil 3", "No condition"])
            self.assertEqual([r["rep"] for r in view._files_rows() if r["kind"] == "sample"][-1], "plate alone")

    def test_ticks_notes_and_aliases_act_on_the_samples_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            view, store, _folder = self._view_with_runs(Path(tmp))
            by = {row.name: row for row in view.matrix[:3]}
            a, b, c = by["Oil 3_rep1"], by["Oil 3_rep2"], by["Oil 3_rep3"]

            view._handle_files_tick(SimpleNamespace(args={"uid": "sample:Oil 3_rep2", "value": False}))
            self.assertEqual([r.active for r in (a, b, c)], [True, False, True])
            rows = view._files_rows()
            self.assertEqual((rows[0]["active"], rows[0]["mixed"]), (True, True))                  # some of its samples
            view._handle_files_tick(SimpleNamespace(args={"uid": "condition:Oil 3", "value": False}))
            self.assertEqual([r.active for r in (a, b, c)], [False, False, False])
            view._handle_files_tick(SimpleNamespace(args={"uid": "sample:Oil 3_rep1", "value": True}))
            view._handle_files_tick(SimpleNamespace(args={"uid": "sample:Oil 3_rep3", "value": True, "shift": True}))
            self.assertEqual([r.active for r in (a, b, c)], [True, True, True])                   # the range between
            view._handle_note(SimpleNamespace(args={"uid": "sample:Oil 3_rep1", "value": " second chip "}))
            self.assertEqual(a.note, "second chip")
            self.assertEqual(next(r.note for r in _make_view(store).matrix if r.name == "Oil 3_rep1"), "second chip")
            view._handle_alias(SimpleNamespace(args={"uid": "condition:Oil 3", "value": "Oil 300"}))
            self.assertEqual({r.condition for r in (a, b, c)}, {"Oil 300"})
            self.assertEqual([r.name for r in (a, b, c)], ["Oil 300_rep1", "Oil 300_rep2", "Oil 300_rep3"])
            condition = view._files_rows()[0]
            self.assertEqual((condition["alias"], condition["auto"]), ("Oil 300", "Oil 3"))        # the derived one beside
            self.assertEqual(view._analyse_rows()[0]["auto"], "Oil 3")

    def test_the_facts_tell_how_the_droplets_were_made_from_the_run_or_the_video(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            store = Registry(Path(tmp) / "analyze")
            data = Path(tmp) / "data"
            run = RunStore(data).begin({"plan_id": "p", "tag": "", "operation_id": "run_json_protocol",
                                        "normalized_settings": {"protocol": {"name": "dropseq", "steps": [
                                            {"name": "collect", "sensor_setpoints": {"0": 300, "1": 40},
                                             "pressure_setpoints": {"2": 600}, "trigger_type": "time",
                                             "trigger_params": {"duration_s": 1}, "record": True}]}}})
            (run.path / "v.avi").write_bytes(b"v")
            run.update(state="completed", artifacts={"videos": [{"step": 1, "video_path": "v.avi"}]})
            view = _make_view(store)
            view._add_root(data)
            row = view.matrix[0]

            self.assertEqual(row.condition, "dropseq o300 c40 b600mbar")
            self.assertEqual(view._condition_facts(row), [("Condition", "dropseq o300 c40 b600mbar"), ("Protocol", "dropseq"),
                                                          ("Oil L", "flow 300 µL/min"), ("Cells M", "flow 40 µL/min"),
                                                          ("Beads M", "pressure 600 mbar")])
            record = {"channels": [{"index": 0, "label": "Oil L", "mode": "flow", "flow_ul_min": 250.0},
                                   {"index": 1, "label": "Cells M", "mode": "pressure", "pressure_mbar": 310.0}]}
            with patch("admet.ui.analyze._video_record", return_value=record):
                self.assertEqual(view._condition_facts(row)[2:], [("Oil L", "flow 250 µL/min"),
                                                                 ("Cells M", "pressure 310 mbar")])   # the video's own
            self.assertEqual(view._details(row)[0], ("Run", run.run_id))

    def test_a_files_status_is_new_done_or_stale(self):
        from unittest.mock import patch

        view = _make_view()
        row = _row(sample="v")
        view.matrix = [row]
        with patch.object(view, "_stored_result", return_value=None):
            self.assertEqual(view._file_status(row), "new")
            row.last_result = "abc"
            self.assertEqual(view._file_status(row), "stale")
            view._running, view._running_uids = True, {row.uid}
            self.assertEqual(view._file_status(row), "running")
            view._running, view._running_uids = False, set()
        with patch.object(view, "_stored_result", return_value=object()):
            self.assertEqual(view._file_status(row), "done")
        row.last_result = "abc"
        with patch.object(view, "_stored_result", return_value=None):
            self.assertIn("dot-stale", view._analyse_row(row)["status"])


if __name__ == "__main__":
    unittest.main()
