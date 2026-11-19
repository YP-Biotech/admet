import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

from admet.core.engine import EngineRegistry
from admet.core.registry import Registry
from admet.ui.analyze import (
    AnalyzeWorkflowView,
    MatrixRow,
    _addable,
    _browser_entries,
    _droplet_at,
    _roi_from_drag,
    _uid,
    apply_click,
)
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
                         ["Run all ticked", "Run selected", "Reset file settings"])
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

        view._handle_header_set(SimpleNamespace(args={"field": "detect_inclusions", "value": False}))

        self.assertEqual([row.settings.get("detect_inclusions") for row in view.matrix], [False, True, False, None])
        self.assertIn("on 2 ticked file(s)", notices[-1][1])
        view._handle_header_set(SimpleNamespace(args={"field": "detect_inclusions", "value": False}))
        self.assertEqual(notices[-1][0], "secondary")                        # already so: nothing changes
        view._handle_header_set(SimpleNamespace(args={"field": "microns_per_pixel", "value": "2.27"}))
        self.assertEqual(view.matrix[0].settings["px_to_um"], 2.27)             # the imaging engine's name for it
        self.assertEqual(view.matrix[3].settings["microns_per_pixel"], 2.27)
        self.assertNotIn("px_to_um", view.matrix[1].settings)
        view._handle_header_set(SimpleNamespace(args={"field": "k_sigma", "value": ""}))          # left empty
        self.assertEqual(view.matrix[3].settings["k_sigma"], 4.0)
        self.assertEqual(apply_setting(view.matrix, "k_sigma", 3), [view.matrix[3]])
        self.assertEqual(apply_setting(view.matrix, "no_such_setting", 1), [])

    def test_the_use_header_ticks_or_clears_every_file(self):
        view = _make_view()
        view.matrix = [_row(sample="a"), _row(sample="b")]
        view._notify = lambda *a, **k: None
        self.assertIs(view._use_state(), True)
        view._handle_use_all(SimpleNamespace(args={"value": False}))
        self.assertEqual([row.active for row in view.matrix], [False, False])
        self.assertIs(view._use_state(), False)
        view.matrix[0].active = True
        self.assertIsNone(view._use_state())                                    # some: the box is indeterminate
        view._handle_use_all(SimpleNamespace(args={"value": True}))
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
            names = [column["name"] for column in analyse_columns(view.analyse_fields, None)]
            self.assertEqual(names, ["active", "sample_id", "source", "engine", "fps", "k_sigma", "analyzed"])
            self.assertIsNone(analyse_columns(view.analyse_fields, None)[0]["all"])
            row = analyse_row(_row(sample="v", k_sigma=4.0, microns_per_pixel=1.1), range_text="0–10",
                              crop_text="full", analyzed="")
            self.assertEqual((row["k_sigma"], row["microns_per_pixel"], row["fps"]), (4.0, 1.1, None))
            self.assertEqual((row["frame_limit"], row["frame_limit_applies"], row["k_sigma_applies"]), (None, False, True))
            image = analyse_row(_row(engine="cellpose", sample="i", px_to_um=2.0, detect_inclusions=True),
                                range_text="all", crop_text="—", analyzed="")
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

    def test_reset_keeps_scale_and_fps_and_restores_detection_defaults(self):
        view = _make_view()
        view.matrix = [_row(sample="a", microns_per_pixel=1.2, fps=200, k_sigma=9, gate_pct=80)]
        view.selected_uid = view.matrix[0].uid

        view._reset_row_settings()

        settings = view.matrix[0].settings
        self.assertEqual((settings["microns_per_pixel"], settings["fps"]), (1.2, 200))
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

            at = f"{run.run_id[9:11]}:{run.run_id[11:13]}"                      # the run's time tells runs apart
            self.assertEqual([row.sample_id for row in view.matrix], [f"Oil 3 {at} step 1", f"Oil 3 {at} step 2"])
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
            self.assertEqual([row.sample_id for row in view.matrix][-1], f"Oil 3 {at} step 3")
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
    def test_a_video_from_a_run_shows_the_rigs_live_qc_as_a_line_that_ticks_with_the_file(self):
        from tests.test_analyze_results import _run_csv

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            run, names = run_with_videos(data, "oil", videos=1)
            _run_csv(run.path, Path(names[0]).stem)
            view = _make_view(Registry(Path(tmp) / "analyze"))
            view._add_root(data)

            rows = view._results_rows()

            self.assertEqual([row["uid"].endswith(":live") for row in rows], [True])   # no analysis yet: the rig's line alone
            self.assertIn("STABLE", rows[0]["analysed"])
            self.assertIn("oil", rows[0]["sample"])
            view._handle_matrix_change(SimpleNamespace(args={"uid": rows[0]["uid"], "field": "active", "value": False}))
            self.assertFalse(view.matrix[0].active)
            self.assertFalse(view._results_rows()[0]["active"])
            view.matrix[0].run = ""
            self.assertEqual(view._results_rows(), [])                                   # a loose file has no live line


if __name__ == "__main__":
    unittest.main()
