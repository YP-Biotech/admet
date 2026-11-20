"""The analyze screen: the files analyze knows, their set-up and run, and what was found.

Three stages. On *Files* the user adds folders of runs or single files and ticks them; on
*Analyse* each file is set up on its own preview and run; *Results* is view only, with export.
One screen state per browser page; the file list and settings are kept in analyze's registry,
so it comes back as left.
"""

from __future__ import annotations

import base64
import html
import itertools
import json
import math
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from admet.core.engine import EngineRegistry, ParamKind
from admet.core.home import AdmetHome
from admet.core.registry import Registry, infer_engine
from admet.engines.cellpose.detection import CellposeDetection, read_image_8bit
from admet.engines.opencv.settings import OPENCV_SETTINGS
from admet.ui import theme
from admet.ui.analyze_matrix import (
    ANALYSE_COLUMN_LABELS,
    DEFAULT_ANALYSE_FIELDS,
    analyse_columns,
    analyse_fields,
    analyse_row,
    apply_setting,
    cast_matrix_value,
    files_columns,
    files_row,
    matrix_field_kind,
    results_columns,
    setting_key,
    wire_analyse_table,
    wire_files_table,
    wire_results_table,
)
from admet.ui.analyze_results import (
    AGGREGATED_FIELDS,
    FileResult,
    LiveQC,
    RESULT_LABELS,
    RESULT_SECTIONS,
    RunPlot,
    StoredResult,
    aggregated_cells,
    corrected_inclusions,
    correction_key,
    corrections_summary,
    dispersed_flow_ul_min,
    export_csv_zip,
    file_results,
    iter_rows,
    live_cells,
    live_qc,
    load_stored_result,
    local_time,
    plot_styles,
    result_charts,
    results_cells,
    run_label,
    stale_window,
    runs_charts,
    zip_bytes,
)
from admet.ui.window import log_state, panel_specs, settings_panel_state, structure_signature
from admet.ui.workflow_view import action_button_state, current_stage, instruction_text, stage_by_id, toc_row_states
from admet.workflows import StageStatus, Workflow, WorkflowState
from admet.workflows.analyze_runner import AnalysisStop, AnalyzeBatchReport, AnalyzeBatchRunner, AnalyzeTarget

VIDEO_SUFFIXES = {".avi", ".mp4", ".mov", ".mkv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
# Per-file detection settings a video row starts with and "Reset file settings" goes back to.
RESULT_DATA = {"opencv": "OpenCV", "cellpose": "Cellpose", "live": "Live QC"}
NO_SET = "(no set)"
DETECTION_DEFAULTS = {param.name: param.default for param in OPENCV_SETTINGS.params
                      if param.name in {"k_sigma", "satellite_px", "min_area", "skip_left_pct",
                                        "skip_right_pct", "gate_pct"}}
IMAGING_DEFAULTS = {"px_to_um": 1.14, "frame_limit": 0, "detect_inclusions": True}


@dataclass
class MatrixRow:
    uid: str
    source_path: str
    engine: str
    sample_id: str
    cache_policy: str = "use"
    active: bool = True                # ticked on 1. Files: goes on to the stages after it
    to_run: bool = True                # ticked on 2. Analyse: in the run
    plotted: bool = True               # ticked on 3. Results: in the plots and the export
    settings: dict[str, Any] = field(default_factory=dict)
    run: str = ""                      # the run folder the file came from, when it did
    protocol: str = ""                 # the run's tag or protocol
    run_time: str = ""                 # when the run began, "2026-11-17 19:30"
    replicate: int | None = None       # which replicate of its run the file is
    set: str = ""                      # the set the file is in: the group it belongs to, the user's to give
    built: str = ""                    # the sample name built for the file, from its set and its place in it

    @property
    def name(self) -> str:
        """The sample name in force: what was written over, else the built one, else the file's own."""
        return self.sample_id or self.built or self.own_name

    @property
    def own_name(self) -> str:
        """The file's name on its own: its protocol with its replicate, or its file name."""
        base = self.protocol or Path(self.source_path).stem
        return f"{base}_rep{self.replicate}" if self.replicate else base

    @property
    def origin(self) -> str:
        """Where the file is from, in words: protocol, time and replicate."""
        rep = f"rep {self.replicate}" if self.replicate else ""
        return " · ".join(part for part in (self.protocol, self.run_time, rep) if part)

    @property
    def run_name(self) -> str:
        return Path(self.run).name if self.run else ""


def render_workflow(
    workflow: Workflow,
    state: WorkflowState,
    _settings: Any | None = None,
    *,
    registry: EngineRegistry | None = None,
    api: Any | None = None,
    store: Registry | None = None,
) -> None:
    from nicegui import ui

    if registry is None:
        engine = getattr(api, "engine", None)
        registry = EngineRegistry()
        if engine is not None:
            registry.register(engine.id, lambda: engine)

    ui.add_head_html(_style())
    AnalyzeWorkflowView(workflow, state, registry, store=store).render()


class AnalyzeWorkflowView:
    def __init__(self, workflow: Workflow, state: WorkflowState, registry: EngineRegistry,
                 *, store: Registry | None = None) -> None:
        self.workflow = workflow
        self.state = state
        self.registry = registry
        self.store = store if store is not None else Registry(AdmetHome.resolve().ensure().analyze_dir)
        self.selected_uid = ""
        self._saved_matrix: list[dict[str, Any]] | None = None
        self.matrix: list[MatrixRow] = self._load_matrix()
        # The setting columns the Analyse table shows, as chosen last time.
        self.analyse_fields: list[str] = analyse_fields(
            self.store.view_setting("analyse_columns", list(DEFAULT_ANALYSE_FIELDS))) or list(DEFAULT_ANALYSE_FIELDS)
        self.current_set = str(self.store.view_setting("current_set", "") or "")   # the set in use
        chosen_sets = self.store.view_setting("results_sets", None)                 # the sets 3. Results takes
        self.results_sets: list[str] | None = [str(n) for n in chosen_sets] if isinstance(chosen_sets, list) else None
        self._last_tick = ""                            # the Use box clicked last, for shift-click ranges
        # The kinds of data 3. Results uses, as chosen last time: the engines' analyses and the rig's live QC.
        chosen = self.store.view_setting("results_data", list(RESULT_DATA))
        self.results_data: set[str] = {kind for kind in RESULT_DATA if kind in (chosen or [])}
        self._run_progress = 0.0
        self._run_step_progress = 0.0
        self._running = False
        self._stop: AnalysisStop | None = None          # how the run in progress is told to stop
        self.show_overlay = True
        self.show_stages = False
        self.notice = "Add a folder of runs, or files, to start."
        self.notice_kind = "primary"
        self.action_log: list[str] = ["Analyze UI ready."]
        self._mounted_signature: tuple[Any, ...] | None = None
        self._refs: dict[str, Any] = {}
        self._table_refs: dict[str, list[Any]] = {}
        self._button_refs: list[Any] = []
        self._chart_refs: list[tuple[str, Any]] = []

    # -- shell ---------------------------------------------------------------------------

    def render(self) -> None:
        from nicegui import ui

        ui.query("body").classes("m-0")
        with ui.column().classes("shell w-full"):
            self._refs["topbar"] = ui.row().classes("topbar w-full items-center justify-between px-4 py-3")
            with ui.row().classes("w-full gap-4 flex-nowrap items-start"):
                self._refs["sidebar"] = ui.column().classes("left-rail shrink-0")
                self._refs["content"] = ui.column().classes("grow min-w-0 gap-3")
        self._mount_topbar()
        self._mount_sidebar()
        self._mount_content_shell()
        self._render_current_stage(force_mount=True)

    def _mount_topbar(self) -> None:
        from nicegui import ui

        topbar = self._refs.get("topbar")
        if topbar is None:
            return
        topbar.clear()
        with topbar:
            with ui.row().classes("items-baseline gap-3"):
                ui.label("admet analyze").classes("text-xl font-semibold")
                roots = self.store.roots()
                text = "runs in " + ", ".join(_compact_path(root) for root in roots) if roots else "no folder of runs yet"
                hint = ui.label(text).classes("muted text-xs root-hint")
                hint.props["title"] = "\n".join(roots) if roots else "Add folder… on 1. Files"
            with ui.row().classes("items-center gap-2"):
                ui.button("Rescan", on_click=self._rescan).props("dense no-caps outline")

    def _mount_sidebar(self) -> None:
        sidebar = self._refs.get("sidebar")
        if sidebar is None:
            return
        sidebar.clear()
        with sidebar:
            self._render_toc()
            self._render_instruction_card()
            self._render_notice_card()

    def _mount_content_shell(self) -> None:
        from nicegui import ui

        content = self._refs.get("content")
        if content is None:
            return
        content.clear()
        with content:
            for spec in panel_specs(include_results=False):
                with ui.column().classes("admet-panel w-full gap-0") as box:
                    ui.label(spec.title).classes("admet-box-title")
                    body = ui.column().classes("admet-panel-body w-full gap-2")
                self._refs[f"{spec.key}_box"] = box
                self._refs[f"{spec.key}_body"] = body

    def _render_current_stage(self, *, force_mount: bool = False) -> None:
        if "main_body" not in self._refs:
            return
        signature = self._structure_signature()
        if force_mount or signature != self._mounted_signature:
            self._mount_stage()
            self._mounted_signature = signature
        self._sync_stage()

    def _structure_signature(self) -> tuple[Any, ...]:
        return structure_signature(self._stage_id(), self.selected_uid, tuple(self.store.roots()),
                                   tuple(self.analyse_fields),
                                   tuple(row.uid for row in self.matrix), self._running,
                                   tuple(sorted(self.results_data)), self.current_set,
                                   tuple(self.results_sets or ()))

    def _mount_stage(self) -> None:
        for key in ("action_box_body", "action_panel_body", "main_body", "log_body"):
            body = self._refs.get(key)
            if body is not None:
                body.clear()
        # clear() deletes the elements inside these bodies; drop their refs so nothing
        # writes to a deleted element. Stages that need them create them again below.
        for key in ("action_progress", "action_progress_text", "log", "frame_image", "frame_caption",
                    "stages", "crop_label", "editor_warnings", "image_preview", "image_image", "image_caption",
                    "corrections_table", "files_hint", "use_all_boxes"):
            self._refs.pop(key, None)
        self._table_refs = {}
        self._button_refs = []
        self._chart_refs = []
        self._mount_action_box()
        self._mount_action_panel()
        self._mount_main_window()
        self._mount_log()

    def _sync_stage(self) -> None:
        self._sync_topbar()
        self._mount_sidebar()
        self._sync_action_box()
        self._sync_tables()
        self._sync_previews()
        self._sync_log()

    def _sync_topbar(self) -> None:
        self._mount_topbar()                    # the folders watched may have changed

    def _mount_action_box(self) -> None:
        from nicegui import ui

        body = self._refs["action_box_body"]
        with body, ui.column().classes("admet-process w-full gap-0 overflow-hidden"):
            self._refs["action_progress"] = ui.linear_progress(value=0.0, show_value=False).classes("w-full")
            self._refs["action_progress_text"] = ui.label("").classes("admet-progress-text")
            with ui.row().classes("admet-transport w-full items-stretch gap-0"):
                for index, spec in enumerate(self._action_specs(self._stage_id())):
                    if index:
                        ui.element("span").classes("admet-transport-separator")
                    button = ui.button(spec["label"], on_click=spec["handler"]).props(
                        "unelevated dense no-caps flat")
                    classes = "admet-transport-btn grow"
                    if spec.get("active"):
                        classes += " admet-transport-btn-active"
                    if spec.get("warning"):
                        classes += " admet-transport-btn-warning"
                    button.classes(classes)
                    self._button_refs.append(button)

    def _sync_action_box(self) -> None:
        fraction, text = self._progress()
        progress = self._refs.get("action_progress")
        if progress is not None:
            progress.set_value(fraction)
        progress_text = self._refs.get("action_progress_text")
        if progress_text is not None:
            progress_text.set_text(text)
        specs = self._action_specs(self._stage_id())
        if len(specs) != len(self._button_refs):
            return
        for button, spec in zip(self._button_refs, specs, strict=True):
            button.set_text(spec["label"])
            # While a run is on, only Stop works
            button.set_enabled(bool(spec.get("enabled", True)) and (not self._running or spec.get("action") == "stop_analysis"))

    def _progress(self) -> tuple[float, str]:
        """How far the current stage is, as a fraction and in words."""
        if self._running:
            return self._run_progress / 100.0, (f"file {self._run_step_progress:.0f} % · "
                                                f"all {self._run_progress:.0f} %")
        stage_id = self._stage_id()
        ticked = self._targets()
        if stage_id == "files":
            if not self.matrix:
                return 0.0, "no files"
            return 1.0, f"{_count(len(self.matrix), 'file')} · {len(ticked)} ticked"
        if stage_id == "analyse":
            if not ticked:
                return 0.0, "no files ticked"
            analysed = sum(1 for row in ticked if self._analyzed_at(row))
            missing = sum(1 for row in ticked if not self._row_ready(row))
            text = f"{len(ticked)} ticked · {analysed} analysed"
            if missing:
                text += f" · {missing} missing µm/px or FPS"
            return analysed / len(ticked), text
        results, lives = self._results(), self._lives()
        if not results and not lives:
            return 0.0, "nothing analysed yet"
        return 1.0, f"{_count(len(results), 'result')}" + (f" · {len(lives)} live" if lives else "")

    def _mount_action_panel(self) -> None:
        body = self._refs["action_panel_body"]
        with body:
            panel = settings_panel_state(self._stage())
            if panel.kind == "results":
                self._render_results_table()
            elif self._stage_id() == "analyse":
                self._render_analyse_table()
            else:
                self._render_files_table()

    def _mount_main_window(self) -> None:
        with self._refs["main_body"]:
            self._mount_main_window_content()

    def _mount_main_window_content(self) -> None:
        kind = self._stage().editor.kind if self._stage().editor is not None else ""
        if kind == "file_facts":
            self._render_file_facts()
        elif kind == "file_editor":
            self._render_file_editor()
        else:
            self._render_results_plots()

    def _remount_main(self) -> None:
        body = self._refs.get("main_body")
        if body is None:
            return
        body.clear()
        self._chart_refs = []
        with body:
            self._mount_main_window_content()

    def _mount_log(self) -> None:
        from nicegui import ui

        with self._refs["log_body"]:
            self._refs["log"] = ui.html("").classes("log-text w-full")

    def _sync_tables(self) -> None:
        row_sources = {"files": self._files_rows, "analyse": self._analyse_rows, "results": self._aggregated_rows}
        for key, tables in self._table_refs.items():
            tables[:] = [table for table in tables if not table.is_deleted]
            source = row_sources.get(key)
            if source is None or not tables:
                continue
            rows = source()
            for table in tables:
                table.rows = rows
                if key == "analyse":
                    table.columns = analyse_columns(self.analyse_fields)
                table.update()
        hint = self._refs.get("files_hint")
        if hint is not None:
            hint.set_visibility(not self.matrix)
        self._sync_use_all()

    def _sync_use_all(self) -> None:
        boxes = [box for box in self._refs.get("use_all_boxes", []) if not box.is_deleted]
        self._refs["use_all_boxes"] = boxes
        for box in boxes:
            box.value = self._use_state()

    def _render_use_all_header(self, table: Any) -> None:
        """A box in the table's Use header that ticks or clears every file; it reads indeterminate for some.
        A NiceGUI element in the header slot, as a slot template cannot reach Python; a plain th, as
        Quasar's q-th looks its column up by the vnode key, which NiceGUI uses for the element id."""
        from nicegui import ui

        with table.add_slot("header-cell-active"), ui.element("th").classes("text-center"):
            # update:model-value is the browser's click; setting the value from Python stays quiet
            box = ui.checkbox(value=self._use_state()).props("dense")
            box.on("update:model-value", lambda event: self._handle_use_all(bool(event.args)), [None])
            self._refs.setdefault("use_all_boxes", []).append(box)

    def _sync_previews(self) -> None:
        target = self._selected_row()
        image = self._refs.get("frame_image")
        if target is not None and image is not None and target.engine == "opencv":
            source = Path(target.source_path)
            frame_index = _preview_frame_index(target)
            frame = _read_video_frame(source, frame_index)
            if not frame["error"]:
                image.set_source(frame["src"])
                image.set_content(self._overlay_svg(target, frame_index, frame["width"], frame["height"]))
            caption = self._refs.get("frame_caption")
            if caption is not None:
                frames, width, height = _video_size(source)
                caption.set_text(f"frame {frame_index} of {frames} · {width}×{height} px")
            crop = self._refs.get("crop_label")
            if crop is not None:
                crop.set_text(_crop_text(target, (frame["width"], frame["height"]) if not frame["error"] else None))
            stages = self._refs.get("stages")
            if stages is not None:
                stages.content = self._stages_html(target)
            warnings = self._refs.get("editor_warnings")
            if warnings is not None:
                frames, width, height = _video_size(source)
                warnings.content = "".join(f'<div class="editor-note editor-warning">{html.escape(w)}</div>'
                                           for w in _video_setting_warnings(target, frames, width, height))
        preview = self._refs.get("image_preview")
        if target is not None and preview is not None and target.engine == "cellpose":
            preview.content = self._image_preview_html(target)
        image = self._refs.get("image_image")
        if target is not None and image is not None and target.engine == "cellpose":
            source = Path(target.source_path)
            index = _row_int(target, "image_frame", 1)
            shown = _read_image_source(source, index)
            if not shown["error"]:
                frame_id = _frame_id_at(source, index)
                image.set_source(shown["src"])
                image.set_content(self._image_overlay_svg(target, frame_id, shown["width"], shown["height"]))
                caption = self._refs.get("image_caption")
                if caption is not None:
                    caption.set_text(f"image {index} of {_image_count(source)} · frame {frame_id} · "
                                     f"{shown['width']}×{shown['height']} px")
            table = self._refs.get("corrections_table")
            if table is not None:
                table.rows = _corrections_rows(target)
                table.update()

    def _sync_log(self) -> None:
        log = self._refs.get("log")
        if log is not None:
            state = log_state(self.action_log)
            log.content = "<br>".join(html.escape(line) for line in state.lines) or state.empty_text

    def _register_table(self, key: str, table: Any) -> None:
        self._table_refs.setdefault(key, []).append(table)

    # -- sidebar -------------------------------------------------------------------------

    def _render_toc(self) -> None:
        from nicegui import ui

        with ui.column().classes("workflow-toc w-full"):
            ui.label("Workflow").classes("toc-title")
            for row in toc_row_states(self.workflow, self.state, status_for=lambda _i, stage: self._status(stage.id)):
                dot = _dot_class(row.status, row.selected)
                with ui.row().classes("toc-row w-full items-center gap-2").on(
                        "click", lambda _event, idx=row.index: self._activate(idx)):
                    ui.element("span").classes(dot)
                    ui.label(row.label).classes("text-sm" + (" font-semibold" if row.selected else ""))

    def _render_instruction_card(self) -> None:
        from nicegui import ui

        with ui.column().classes("notification-card w-full"):
            ui.label("Instructions").classes("notification-title")
            ui.label(self._instruction()).classes("notification-text")

    def _render_notice_card(self) -> None:
        from nicegui import ui

        classes = "notification-card w-full"
        if self.notice_kind != "primary":
            classes += f" notification-card-{self.notice_kind}"
        with ui.column().classes(classes):
            ui.label("Notification").classes("notification-title")
            ui.label(self.notice).classes("notification-text")

    def _status(self, stage_id: str) -> StageStatus:
        """A stage's dot, from what is actually there: done when its work is done."""
        ticked = self._targets()
        if stage_id == "files":
            done = bool(self.matrix)
        elif stage_id == "analyse":
            done = bool(ticked) and all(self._analyzed_at(row) for row in ticked)
        else:
            done = bool(self._results())
        if done:
            return StageStatus.COMPLETE
        return StageStatus.ACTIVE if stage_id == self._stage_id() else StageStatus.PENDING

    # -- 1. Files -------------------------------------------------------------------------

    def _render_files_table(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-3"):
            self._render_sets_bar()
            table = ui.table(columns=files_columns(), rows=self._files_rows(), row_key="uid").classes(
                "slim-table matrix-table w-full").props("dense flat hide-bottom")
            self._register_table("files", table)
            wire_files_table(table, self._handle_matrix_change, self._handle_matrix_remove)
            self._render_use_all_header(table)
            table.on("rowClick", self._select_row_event)
            self._refs["files_hint"] = ui.label("No files yet. Add files… to list videos or image folders."
                                                ).classes("muted text-xs")

    # -- sets: the group a file is in ---------------------------------------------------------

    def _set_names(self) -> list[str]:
        """Every set a listed file is in, in the list's order."""
        return list(dict.fromkeys(row.set for row in self.matrix if row.set))

    def _render_sets_bar(self) -> None:
        """Set: choosing one ticks exactly its files. Assign… puts the ticked files into a set, named
        anew or chosen; Clear takes the ticked files out of theirs."""
        from nicegui import ui

        names = self._set_names()
        with ui.row().classes("items-center gap-2 no-wrap w-full"):
            ui.label("Set").classes("text-xs muted")
            ui.select(names, value=self.current_set if self.current_set in names else None,
                      on_change=self._choose_set).props("dense outlined options-dense clearable").classes("sets-select")
            ui.button("Assign…", on_click=self._ask_set_name).props("dense flat no-caps").set_enabled(
                bool(self._listed()))
            ui.button("Clear", on_click=self._clear_set).props("dense flat no-caps").set_enabled(
                any(row.set for row in self._listed()))

    def _ask_set_name(self) -> None:
        from nicegui import ui

        with ui.dialog() as dialog, ui.card().classes("gap-2"):
            ui.label(f"Put the {len(self._listed())} ticked file(s) into the set").classes("text-sm")
            name = ui.input("Set", value=self.current_set).props("dense outlined autofocus").classes("w-64")
            name.on("keydown.enter", lambda _e: (dialog.close(), self._assign_set(name.value)))
            with ui.row().classes("gap-2"):
                ui.button("Assign", on_click=lambda: (dialog.close(), self._assign_set(name.value))).props("dense no-caps")
                ui.button("Cancel", on_click=dialog.close).props("dense flat no-caps")
        dialog.open()

    def _assign_set(self, name: Any) -> None:
        """The ticked files go into the set NAME, made on the spot when new."""
        name = str(name or "").strip()
        if not name:
            self._notify("A set needs a name.", "warning")
            return
        rows = self._listed()
        if not rows:
            self._notify("Tick the files to put into the set first.", "warning")
            return
        for row in rows:
            row.set = name
        self._set_current_set(name)
        self._save_matrix()
        self._log(f"set: {len(rows)} file(s) put into '{name}'")
        self._notify(f"{_count(len(rows), 'file')} in the set '{name}'.", "success")
        self._refresh(force_mount=True)

    def _clear_set(self) -> None:
        rows = [row for row in self._listed() if row.set]
        for row in rows:
            row.set = ""
        self._save_matrix()
        self._log(f"set: {len(rows)} file(s) taken out of their sets")
        self._notify(f"{_count(len(rows), 'file')} out of their sets.", "secondary")
        self._refresh(force_mount=True)

    def _choose_set(self, event: Any) -> None:
        name = getattr(event, "value", None)
        if not name:
            self._set_current_set("")
            self._refresh(force_mount=True)
            return
        self._use_set(str(name))

    def _use_set(self, name: str) -> None:
        """Tick exactly the files of the set."""
        members = [row for row in self.matrix if row.set == name]
        if not members:
            self._notify(f"No file is in the set '{name}'.", "warning")
            return
        for row in self.matrix:
            row.active = row.set == name
        self._set_current_set(name)
        self._save_matrix()
        self._log(f"set: '{name}' in use, {len(members)} file(s) ticked")
        self._notify(f"Set '{name}': {_count(len(members), 'file')} ticked.", "success")
        self._refresh(force_mount=True)

    def _set_current_set(self, name: str) -> None:
        self.current_set = name
        self.store.set_view_setting("current_set", name)

    def _in_results_sets(self, row: MatrixRow) -> bool:
        """Whether 3. Results takes the file's set: every set unless some are chosen there."""
        return self.results_sets is None or (row.set or NO_SET) in self.results_sets

    def _for_results(self) -> list[MatrixRow]:
        """The files 3. Results lists: listed past 1. Files and of the sets chosen, grouped by set."""
        rows = [row for row in self._listed() if self._in_results_sets(row)]
        order = list(dict.fromkeys(row.set for row in rows))
        return sorted(rows, key=lambda row: order.index(row.set))

    def _set_results_sets(self, event: Any) -> None:
        chosen = getattr(event, "value", None)
        names = [str(name) for name in chosen] if isinstance(chosen, list) else []
        wanted = None if not names or set(names) >= set(self._results_set_choices()) else names
        if wanted == self.results_sets:
            return
        self.results_sets = wanted
        self.store.set_view_setting("results_sets", wanted)
        self._refresh(force_mount=True)

    def _results_set_choices(self) -> list[str]:
        """The sets to choose from on 3. Results, with "(no set)" for the files in none; nothing without sets."""
        names = self._set_names()
        if names and any(not row.set for row in self._listed()):
            names.append(NO_SET)
        return names

    def _files_rows(self) -> list[dict[str, Any]]:
        return [files_row(row, count=self._count_text(row), analyzed=self._analyzed_at(row),
                          removable=True) for row in self.matrix]

    def _count_text(self, row: MatrixRow) -> str:
        source = Path(row.source_path)
        if row.engine == "opencv":
            size = _known_video_size(source)
            return str(size[0]) if size else "unreadable"
        return str(_image_count(source))

    def _render_file_facts(self) -> None:
        from nicegui import ui

        target = self._selected_row()
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Selected file").classes("section-title")
            if target is None:
                ui.label("Select a file in the list to see it." if self.matrix else
                         "Nothing to show until files are added.").classes("muted text-xs")
                return
            source = Path(target.source_path)
            with ui.element("div").classes("facts-grid w-full"):
                with ui.column().classes("gap-1 min-w-0"):
                    if target.engine == "opencv":
                        frame = _read_video_frame(source, 0)
                        if frame["error"]:
                            ui.label(_friendly_video_error(frame["error"])).classes("editor-note editor-warning")
                        else:
                            ui.image(frame["src"]).classes("facts-image").style(
                                f"max-width:{frame['width']}px")
                    else:
                        ui.html(self._image_preview_html(target)).classes("w-full")
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(target.name).classes("text-sm font-semibold")
                    _path_label(target.source_path)
                    for label, value in self._facts(target, source):
                        with ui.row().classes("facts-row no-wrap gap-2"):
                            ui.label(label).classes("muted text-xs facts-key")
                            ui.label(value).classes("text-xs")

    def _facts(self, row: MatrixRow, source: Path) -> list[tuple[str, str]]:
        facts: list[tuple[str, str]] = [("Engine", row.engine)]
        if row.engine == "opencv":
            size = _known_video_size(source)
            if size is None:
                facts.append(("Video", "unreadable"))
            else:
                frames, width, height = size
                facts += [("Frames", str(frames)), ("Size", f"{width} × {height} px")]
                file_fps = _file_fps(source)
                if file_fps:
                    facts.append(("File plays at", f"{file_fps:g} fps"))
                captured = _recorded_capture_fps(row)
                facts.append(("Captured at", f"{captured:g} fps (from the recording)" if captured
                              else "unknown: set FPS on 2. Analyse"))
                if captured and frames:
                    facts.append(("Duration", f"{frames / captured:.1f} s"))
                record = _video_record(source) or {}
                camera = record.get("camera") or {}
                if camera.get("exposure_us") is not None:
                    facts.append(("Exposure", f"{float(camera['exposure_us']):g} µs (from the video)"))
                if camera.get("pixel_size_um"):
                    facts.append(("Sensor pixel", f"{float(camera['pixel_size_um']):g} µm"))
                if record.get("frames_dropped"):
                    facts.append(("Dropped", f"{record['frames_dropped']} frames (the disk fell behind)"))
                if record.get("step_name"):
                    facts.append(("Step", f"{record.get('step')} {record['step_name']}"
                                  + (f", part {record['part']}" if record.get("part", 1) > 1 else "")))
        else:
            facts.append(("Images", str(_image_count(source))))
        analyzed = self._analyzed_at(row)
        facts.append(("Analysed", analyzed or "not yet"))
        return facts

    # -- 2. Analyse -----------------------------------------------------------------------

    def _render_analyse_table(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-3"):
            with ui.row().classes("items-center gap-2 no-wrap w-full"):
                ui.label("Columns").classes("text-xs muted")
                ui.select(dict(ANALYSE_COLUMN_LABELS), multiple=True, value=list(self.analyse_fields),
                          on_change=self._columns_changed).props(
                    "dense outlined options-dense use-chips").classes("columns-select grow")
            table = ui.table(columns=analyse_columns(self.analyse_fields), rows=self._analyse_rows(),
                             row_key="uid").classes("slim-table matrix-table w-full").props("dense flat hide-bottom")
            self._register_table("analyse", table)
            wire_analyse_table(table, self._handle_matrix_change, self.analyse_fields)
            self._render_analyse_headers(table)
            table.on("rowClick", self._select_row_event)
            if not self.matrix:
                ui.label("No files. Add them on 1. Files.").classes("muted text-xs")

    def _render_analyse_headers(self, table: Any) -> None:
        """The Use header ticks or clears every file; a setting column's header takes a value for every
        ticked file. They are NiceGUI elements in the header slots: a slot template cannot reach Python."""
        from nicegui import ui

        self._render_use_all_header(table)
        for name in self.analyse_fields:
            kind = matrix_field_kind(name)
            if kind is None:
                continue                                      # a read-only summary column
            with table.add_slot(f"header-cell-{name}"), ui.element("th").classes("text-left"), \
                    ui.element("div").classes("col-head"):
                ui.label(ANALYSE_COLUMN_LABELS[name]).classes("col-label")
                if kind is ParamKind.BOOLEAN:
                    with ui.element("div").classes("col-actions"):
                        ui.button("on", on_click=lambda _e, f=name: self._handle_header_set(f, True)).props(
                            "dense flat no-caps size=sm")
                        ui.button("off", on_click=lambda _e, f=name: self._handle_header_set(f, False)).props(
                            "dense flat no-caps size=sm")
                else:
                    entry = ui.number().props("dense outlined step=any input-class=matrix-num")
                    entry.on("keydown.enter", lambda _e, f=name, box=entry: self._header_entered(f, box))
                    entry.on("blur", lambda _e, f=name, box=entry: self._header_entered(f, box))

    def _header_entered(self, field: str, entry: Any) -> None:
        """What was typed in a header goes to the ticked files once, then the header is clear again."""
        value = entry.value
        if value is None or value == "":
            return
        entry.value = None
        self._handle_header_set(field, value)

    def _columns_changed(self, event: Any) -> None:
        """The setting columns to show, as ticked in the dropdown; remembered for next time."""
        chosen = analyse_fields(getattr(event, "value", None) or [])
        if chosen == self.analyse_fields:
            return
        self.analyse_fields = chosen or list(DEFAULT_ANALYSE_FIELDS)
        self.store.set_view_setting("analyse_columns", list(self.analyse_fields))
        self._refresh(force_mount=True)

    def _use_state(self) -> bool | None:
        """What the Use header shows: True when every file is ticked, False when none, None for some.
        On 1. Files the tick is whether a file goes on; on 2. Analyse whether it is run; on 3. Results
        whether it is in the plots and the export."""
        stage = self._stage_id()
        if stage == "files":
            ticked = [row.active for row in self.matrix]
        elif stage == "analyse":
            ticked = [row.to_run for row in self._listed()]
        else:
            ticked = [row.plotted for row in self._listed()]
        if not ticked or all(ticked):
            return bool(ticked)
        return False if not any(ticked) else None

    def _handle_use_all(self, value: bool) -> None:
        stage = self._stage_id()
        for row in self.matrix if stage == "files" else self._listed():
            self._tick(row, stage, bool(value))
        self._save_matrix()
        self._sync_tables()
        self._sync_action_box()
        self._notify(self._stage_summary(), "primary")
        self._mount_sidebar()
        if self._stage_id() != "files":
            self._remount_main()                      # the editor and the plots follow the ticked files

    def _handle_header_set(self, field: str, value: Any) -> None:
        """A value entered in a setting column's header: onto every ticked file the setting applies to."""
        if value is None or value == "":
            return
        changed = apply_setting(self.matrix, field, value)
        label = ANALYSE_COLUMN_LABELS.get(field, field)
        if not changed:
            self._notify(f"{label}: no ticked file takes it, or every one has it already.", "secondary")
            self._mount_sidebar()
            return
        shown = ("on" if value else "off") if isinstance(value, bool) else f"{value}"
        self._sync_previews()
        self._sync_tables()
        self._sync_action_box()
        self._save_matrix()
        self._notify(f"{label} = {shown} on {len(changed)} ticked file(s).", "positive")
        self._mount_sidebar()

    def _analyse_rows(self) -> list[dict[str, Any]]:
        rows = []
        for row in self._listed():                   # only files ticked on 1. Files go on
            if row.engine == "opencv":
                size = _known_video_size(Path(row.source_path))
                frames = size[0] if size else 0
                start, end = _row_int(row, "start_frame", 0), _row_int(row, "end_frame", 0) or frames
                range_text = f"{start}–{end}" if frames else "—"
                crop_text = _crop_text(row, (size[1], size[2]) if size else None)
            else:
                limit = _row_int(row, "frame_limit", 0)
                range_text = f"first {limit}" if limit else "all"
                crop_text = "—"
            rows.append(analyse_row(row, range_text=range_text, crop_text=crop_text,
                                    analyzed=self._analyzed_at(row)))
        return rows

    def _render_file_editor(self) -> None:
        from nicegui import ui

        target = self._selected_row()
        with ui.column().classes("panel w-full gap-2 p-3"):
            if target is None:
                ui.label("Set-up of the selected file").classes("section-title")
                ui.label("Select a file in the table above.").classes("muted text-xs")
                return
            ui.label(f"Set-up of {target.name}").classes("section-title")
            if not self._row_ready(target):
                ui.label("µm/px and FPS are missing: fill the red cells in the table above before running."
                         ).classes("editor-note editor-warning")
            if target.engine == "opencv":
                self._render_video_editor(target)
            else:
                self._render_image_editor(target)

    def _render_video_editor(self, target: MatrixRow) -> None:
        from nicegui import ui

        source = Path(target.source_path)
        size = _known_video_size(source)
        if size is None:
            ui.label(_friendly_video_error(f"cannot open video {source}")).classes("editor-note editor-warning")
            _path_label(target.source_path)
            return
        frames, width, height = size
        _path_label(target.source_path)
        frame_index = _preview_frame_index(target)
        frame = _read_video_frame(source, frame_index)
        with ui.element("div").classes("editor-grid w-full"):
            with ui.column().classes("gap-1 min-w-0"):
                image = ui.interactive_image(
                    frame["src"], content=self._overlay_svg(target, frame_index, width, height),
                    on_mouse=self._frame_mouse(target, width, height),
                    events=["mousedown", "mousemove", "mouseup"], cross=False,
                ).classes("admet-frame").style(f"max-width:{width}px")
                self._refs["frame_image"] = image
                with ui.row().classes("items-center gap-3 no-wrap"):
                    self._refs["frame_caption"] = ui.label(
                        f"frame {frame_index} of {frames} · {width}×{height} px").classes("muted text-xs")
                    self._refs["crop_label"] = ui.label(_crop_text(target, (width, height))).classes("text-xs")
                    ui.button("Full frame", on_click=lambda: self._set_roi(target, None)).props(
                        "dense no-caps flat size=sm")
                    ui.label("drag on the frame to crop").classes("muted text-xs")
                with ui.row().classes("items-center gap-x-4 gap-y-1"):
                    analysis = self._stored_analysis(target)
                    if analysis is not None:
                        ui.checkbox("Show the stored analysis", value=self.show_overlay,
                                    on_change=lambda event: self._toggle_overlay(event.value))
                        ui.label(f"from {analysis['analyzed']}").classes("muted text-xs")
                    ui.checkbox("Show detection stages", value=self.show_stages,
                                on_change=lambda event: self._toggle_stages(event.value))
                self._refs["stages"] = ui.html(self._stages_html(target)).classes("w-full")
                self._refs["editor_warnings"] = ui.html("").classes("w-full")
                self._render_timeline(target, frames)
            with ui.column().classes("gap-0 min-w-0 editor-settings"):
                ui.label("Detection").classes("muted text-xs font-semibold uppercase")
                self._slider_editor(target, "skip_left_pct", "Skip left, % (droplets form here)", 0, 90)
                self._slider_editor(target, "skip_right_pct", "Skip right, %", 0, 90)
                self._slider_editor(target, "gate_pct", "Gate, % (droplets counted here)", 0, 100, default=50)
                self._slider_editor(target, "satellite_px", "Satellite size, px", 3, 60, default=13)
                self._slider_editor(target, "min_area", "Min droplet area, px²", 0, 5000, default=800)
                self._slider_editor(target, "k_sigma", "Threshold, × noise", 1, 12, default=4)

    def _render_timeline(self, target: MatrixRow, frames: int) -> None:
        """Start–end on one bar and the preview frame on another, to the same scale."""
        from nicegui import ui

        start = _row_int(target, "start_frame", 0)
        end = _row_int(target, "end_frame", 0) or frames
        preview = _preview_frame_index(target)

        def commit_range(low: Any, high: Any) -> None:
            low = max(0, min(frames - 1, int(_numeric(low) or 0)))
            high = max(low + 1, min(frames, int(_numeric(high) or frames)))
            target.settings["start_frame"] = low
            target.settings["end_frame"] = high
            target.settings["preview_frame"] = max(low, min(high - 1, _row_int(target, "preview_frame", low)))
            bar.set_value({"min": low, "max": high - 1})
            start_box.set_value(low)
            end_box.set_value(high)
            preview_slider._props["min"], preview_slider._props["max"] = low, high - 1
            preview_slider.set_value(target.settings["preview_frame"])
            preview_slider.update()
            preview_box.set_value(target.settings["preview_frame"])
            self._settings_edited(target)

        def scrub(value: Any) -> None:
            number = int(_numeric(value) or 0)
            target.settings["preview_frame"] = number
            preview_box.set_value(number)
            now = time.monotonic()
            if now - last["t"] >= 0.12:
                last["t"] = now
                self._sync_previews()

        def commit_preview(value: Any) -> None:
            number = max(start_box.value or 0, min((end_box.value or frames) - 1, int(_numeric(value) or 0)))
            target.settings["preview_frame"] = number
            preview_slider.set_value(number)
            preview_box.set_value(number)
            self._settings_edited(target)

        last = {"t": 0.0}
        with ui.column().classes("timeline w-full gap-0"):
            with ui.row().classes("slider-label-row w-full items-center no-wrap"):
                ui.label("Frames analysed").classes("muted text-xs font-semibold grow")
                start_box = ui.number(value=start, min=0, max=frames - 1, step=1, format="%d").props(
                    "dense borderless input-class=text-right").classes("slider-number")
                ui.label("–").classes("muted text-xs")
                end_box = ui.number(value=end, min=1, max=frames, step=1, format="%d").props(
                    "dense borderless input-class=text-right").classes("slider-number")
                ui.label(f"of {frames} (end exclusive)").classes("muted text-xs")
            bar = ui.range(min=0, max=frames - 1, step=1, value={"min": start, "max": end - 1}).props(
                "dense").classes("w-full")
            bar.on("change", lambda e: commit_range(e.args["min"], e.args["max"] + 1))
            start_box.on("blur", lambda _e: commit_range(start_box.value, end_box.value))
            start_box.on("keydown.enter", lambda _e: commit_range(start_box.value, end_box.value))
            end_box.on("blur", lambda _e: commit_range(start_box.value, end_box.value))
            end_box.on("keydown.enter", lambda _e: commit_range(start_box.value, end_box.value))
            with ui.row().classes("slider-label-row w-full items-center no-wrap"):
                ui.label("Preview frame").classes("muted text-xs font-semibold grow")
                preview_box = ui.number(value=preview, min=0, max=frames - 1, step=1, format="%d").props(
                    "dense borderless input-class=text-right").classes("slider-number")
            preview_slider = ui.slider(min=start, max=end - 1, step=1, value=preview,
                                       on_change=lambda e: scrub(e.value)).props("dense").classes("w-full")
            preview_slider.on("change", lambda e: commit_preview(e.args))
            preview_box.on("blur", lambda _e: commit_preview(preview_box.value))
            preview_box.on("keydown.enter", lambda _e: commit_preview(preview_box.value))

    def _frame_mouse(self, target: MatrixRow, width: int, height: int):
        """Dragging on the frame draws the crop; a click leaves it alone."""
        drag: dict[str, Any] = {"start": None, "t": 0.0}

        def handler(event: Any) -> None:
            x = max(0, min(width, int(round(event.image_x))))
            y = max(0, min(height, int(round(event.image_y))))
            if event.type == "mousedown":
                drag["start"] = (x, y)
                return
            if drag["start"] is None:
                return
            x0, y0 = drag["start"]
            roi = _roi_from_drag(x0, y0, x, y, width, height)
            if event.type == "mousemove":
                now = time.monotonic()
                if roi is not None and now - drag["t"] >= 0.05:
                    drag["t"] = now
                    image = self._refs.get("frame_image")
                    if image is not None:
                        image.set_content(self._overlay_svg(target, _preview_frame_index(target), width, height,
                                                            draft=roi))
                return
            drag["start"] = None
            if roi is not None:
                self._set_roi(target, roi)
            else:
                self._sync_previews()

        return handler

    def _set_roi(self, target: MatrixRow, roi: tuple[int, int, int, int] | None) -> None:
        x, y, w, h = roi or (0, 0, 0, 0)
        target.settings.update({"roi_x": x, "roi_y": y, "roi_width": w, "roi_height": h})
        self._settings_edited(target)

    def _overlay_svg(self, target: MatrixRow, frame: int, width: int, height: int,
                     *, draft: tuple[int, int, int, int] | None = None) -> str:
        """The crop, the skipped ends, the gate, and the stored droplets of this frame, over it."""
        parts = []
        x, y, w, h = draft or _roi(target, width, height)
        if (x, y, w, h) != (0, 0, width, height):
            parts.append(f'<path class="ov-dim" fill-rule="evenodd" d="M0 0h{width}v{height}H0z '
                         f'M{x} {y}h{w}v{h}H{x}z"/>')
            parts.append(f'<rect class="ov-roi" x="{x}" y="{y}" width="{w}" height="{h}"/>')
        left = x + round(w * _row_float(target, "skip_left_pct", 0.0) / 100)
        right = x + w - round(w * _row_float(target, "skip_right_pct", 0.0) / 100)
        for line in (left, right):
            if x < line < x + w:
                parts.append(f'<line class="ov-window" x1="{line}" y1="{y}" x2="{line}" y2="{y + h}"/>')
        gate = x + round(w * _row_float(target, "gate_pct", 50.0) / 100)
        if x < gate < x + w:
            parts.append(f'<line class="ov-gate" x1="{gate}" y1="{y}" x2="{gate}" y2="{y + h}"/>')
        if self.show_overlay and draft is None:
            parts.append(self._analysis_overlay(target, frame, width, height))
        return (f'<svg class="admet-overlay" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
                + "".join(parts) + "</svg>")

    def _analysis_overlay(self, row: MatrixRow, frame: int, width: int, height: int) -> str:
        analysis = self._stored_analysis(row)
        if analysis is None or not analysis["rows_path"].is_file():
            return ""
        raw = analysis["rows_path"]
        index = _overlay_index(str(raw), raw.stat().st_mtime, analysis["file_id"])
        # Positions were measured inside the crop the analysis used; shift them back onto the frame.
        crop = analysis["settings"]
        if crop.get("roi_width") or crop.get("roi_height"):
            dx, dy = int(crop.get("roi_x") or 0), int(crop.get("roi_y") or 0)
        else:
            dx = dy = 0
        parts = []
        for detection in index["frames"].get(frame, []):
            droplet = detection.get("droplet_id")
            path = [(px + dx, py + dy) for f, px, py in index["paths"].get(int(droplet), []) if f <= frame] \
                if droplet is not None else []
            if len(path) > 1:
                parts.append('<polyline class="ov-path" points="'
                             + " ".join(f"{px:.1f},{py:.1f}" for px, py in path) + '"/>')
            outline = " ".join(f"{px + dx:.1f},{py + dy:.1f}" for px, py in _detection_outline(detection))
            parts.append(f'<polygon class="ov-droplet" points="{outline}"/>')
            cx, cy = float(detection.get("centroid_x", 0)) + dx, float(detection.get("centroid_y", 0)) + dy
            if droplet is not None:
                parts.append(f'<text class="ov-label" x="{cx + 4:.1f}" y="{cy - 4:.1f}">{int(droplet)}</text>')
        return "".join(parts)

    def _stages_html(self, target: MatrixRow) -> str:
        """Each detection stage of the preview frame, with this file's settings, in a grid."""
        if not self.show_stages:
            return ""
        from admet.engines.opencv.debug import detection_stages

        source = Path(target.source_path)
        try:
            stages = detection_stages(source, self._engine_settings(target), _preview_frame_index(target))
        except Exception as exc:
            stages = {"error": f"Detection stages failed: {exc}"}
        if stages["error"]:
            return f'<div class="editor-note editor-warning">{html.escape(stages["error"])}</div>'
        det = stages["detection"]
        captions = {
            "original": "1 · Original (crop)",
            "background": "2 · Background: the empty channel, from sampled frames",
            "matched": "3 · Frame matched to the background's brightness",
            "darker": "4 · Darker than the background",
            "threshold": f"5 · Threshold {stages['threshold']} = {det['k_sigma']:g} × noise {stages['noise_sigma']}",
            "bodies": f"6 · Bodies: satellites under {det['satellite']} px set aside",
            "droplets": (f"7 · Droplets: {stages['kept']} kept (green), {stages['dropped']} dropped (red), "
                         f"{stages['satellites']} satellites (yellow); window and gate lines"),
        }
        tiles = "".join(
            f'<figure class="stage-tile"><img src="{_png_src(stages["images"][name])}" alt="{html.escape(caption)}">'
            f"<figcaption>{html.escape(caption)}</figcaption></figure>"
            for name, caption in captions.items())
        return f'<div class="stage-grid">{tiles}</div>'

    def _render_image_editor(self, target: MatrixRow) -> None:
        """The image with the stored droplets over it; a click on a droplet corrects it."""
        from nicegui import ui

        source = Path(target.source_path)
        _path_label(target.source_path)
        index = _row_int(target, "image_frame", 1)
        shown = _read_image_source(source, index)
        with ui.element("div").classes("editor-grid w-full"):
            with ui.column().classes("gap-1 min-w-0"):
                if shown["error"]:
                    ui.label(shown["error"]).classes("editor-note editor-warning")
                else:
                    frame_id = _frame_id_at(source, index)
                    image = ui.interactive_image(
                        shown["src"], content=self._image_overlay_svg(target, frame_id, shown["width"], shown["height"]),
                        on_mouse=self._image_click(target), events=["click"], cross=False,
                    ).classes("admet-frame admet-frame-still").style(f"max-width:{shown['width']}px")
                    self._refs["image_image"] = image
                    self._refs["image_caption"] = ui.label(
                        f"image {index} of {_image_count(source)} · frame {frame_id} · "
                        f"{shown['width']}×{shown['height']} px").classes("muted text-xs")
                analysis = self._stored_analysis(target)
                with ui.row().classes("items-center gap-x-4 gap-y-1"):
                    if analysis is not None:
                        ui.checkbox("Show the stored analysis", value=self.show_overlay,
                                    on_change=lambda event: self._toggle_overlay(event.value))
                        ui.label(f"from {analysis['analyzed']}").classes("muted text-xs")
                if analysis is not None:
                    ui.label("Click a droplet to switch it off or on · shift-click adds an inclusion · "
                             "alt-click removes one").classes("muted text-xs")
                else:
                    ui.label("Run the file to see its droplets here and correct them.").classes("muted text-xs")
                self._slider_editor(target, "image_frame", "Image", 1, _image_count(source), default=1)
            with ui.column().classes("gap-2 min-w-0 editor-settings"):
                ui.label("Segmentation").classes("muted text-xs font-semibold uppercase")
                with ui.row().classes("slider-label-row w-full items-center no-wrap"):
                    ui.label("Frame limit (0 = all)").classes("muted text-xs font-semibold grow")
                    limit = ui.number(value=_row_int(target, "frame_limit", 0), min=0, step=1, format="%d").props(
                        "dense borderless input-class=text-right").classes("slider-number")
                    limit.on("blur", lambda _e: self._set_setting(target, "frame_limit", int(_numeric(limit.value) or 0)))
                ui.checkbox("Detect inclusions", value=_row_bool(target, "detect_inclusions", True),
                            on_change=lambda e: self._set_setting(target, "detect_inclusions", bool(e.value)))
                ui.label("Corrections").classes("muted text-xs font-semibold uppercase mt-2")
                self._refs["corrections_table"] = ui.table(
                    columns=[{"name": "item", "label": "Correction", "field": "item", "align": "left"},
                             {"name": "value", "label": "Count", "field": "value", "align": "right"}],
                    rows=_corrections_rows(target)).classes("w-full").props("dense flat hide-bottom")
                ui.button("Clear corrections", on_click=lambda: self._clear_corrections(target)).props(
                    "dense no-caps outline")
                ui.label("Corrections count on 3. Results and in the export; the stored rows stay as found."
                         ).classes("muted text-xs")

    def _image_overlay_svg(self, target: MatrixRow, frame_id: int, width: int, height: int) -> str:
        """The stored droplets of this image as circles with their inclusion counts, corrections applied."""
        parts = []
        analysis = self._stored_analysis(target) if self.show_overlay else None
        if analysis is not None and analysis["rows_path"].is_file():
            raw = analysis["rows_path"]
            fixes = target.settings.get("corrections") or {}
            for droplet in _overlay_index(str(raw), raw.stat().st_mtime, analysis["file_id"])["frames"].get(frame_id, []):
                cx, cy, radius = _droplet_circle(droplet)
                fix = fixes.get(correction_key(frame_id, droplet.get("droplet_id")))
                if fix and fix.get("disabled"):
                    parts.append(f'<circle class="ov-off" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}"/>')
                    continue
                parts.append(f'<circle class="ov-droplet" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}"/>')
                count = corrected_inclusions(droplet, fix)
                tag = "ov-count ov-count-fixed" if fix else "ov-count"
                parts.append(f'<text class="{tag}" x="{cx:.1f}" y="{cy:.1f}">{count}</text>')
        return (f'<svg class="admet-overlay" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
                + "".join(parts) + "</svg>")

    def _image_click(self, target: MatrixRow):
        """A click on a droplet switches it off or on; with shift or alt it changes its inclusions."""
        def handler(event: Any) -> None:
            analysis = self._stored_analysis(target)
            if analysis is None or not analysis["rows_path"].is_file():
                return
            source = Path(target.source_path)
            frame_id = _frame_id_at(source, _row_int(target, "image_frame", 1))
            raw = analysis["rows_path"]
            droplets = _overlay_index(str(raw), raw.stat().st_mtime, analysis["file_id"])["frames"].get(frame_id, [])
            hit = _droplet_at(droplets, event.image_x, event.image_y)
            if hit is None:
                return
            fixes = dict(target.settings.get("corrections") or {})
            key = correction_key(frame_id, hit.get("droplet_id"))
            fixes[key] = apply_click(fixes.get(key), shift=event.shift, alt=event.alt)
            if not fixes[key]:
                del fixes[key]
            target.settings["corrections"] = fixes
            self._settings_edited(target)

        return handler

    def _clear_corrections(self, target: MatrixRow) -> None:
        target.settings.pop("corrections", None)
        self._settings_edited(target)

    def _image_preview_html(self, target: MatrixRow) -> str:
        source = Path(target.source_path)
        image = _read_image_source(source, _row_int(target, "image_frame", 1))
        if image["error"]:
            return f'<div class="editor-note editor-warning">{html.escape(image["error"])}</div>'
        return (f'<img class="admet-still" src="{image["src"]}" style="max-width:{image["width"]}px" '
                f'alt="{html.escape(target.name)} image {image["index"]}">'
                f'<div class="muted text-xs">image {image["index"]} of {_image_count(source)}</div>')

    def _slider_editor(self, row: MatrixRow, key: str, label: str, minimum: int, maximum: int,
                       *, default: int | None = None) -> None:
        """A slider and a number box for one whole-number setting of a file.

        Both edit row.settings; a release redraws the preview and the file's table row, nothing else.
        """
        from nicegui import ui

        stored = _row_int(row, key, minimum if default is None else default)
        minimum, maximum = min(int(minimum), stored), max(int(maximum), int(minimum), stored)
        last = {"t": 0.0}

        def scrub(value: Any) -> None:
            number = int(_numeric(value) or 0)
            box.set_value(number)
            row.settings[key] = number
            if key == "image_frame":
                now = time.monotonic()
                if now - last["t"] >= 0.12:
                    last["t"] = now
                    self._sync_previews()

        def commit(number: Any) -> None:
            number = _numeric(number)
            if number is None:
                box.set_value(row.settings.get(key, stored))
                return
            value = max(int(minimum), min(maximum, int(round(number))))
            row.settings[key] = value
            box.set_value(value)
            slider.set_value(value)
            self._settings_edited(row)

        with ui.column().classes("slider-field w-full gap-0"):
            with ui.row().classes("slider-label-row w-full items-center no-wrap"):
                ui.label(label).classes("muted text-xs font-semibold grow")
                box = ui.number(value=stored, min=minimum, max=maximum, step=1, format="%d").props(
                    "dense borderless input-class=text-right").classes("slider-number")
                box.on("blur", lambda _event: commit(box.value))
                box.on("keydown.enter", lambda _event: commit(box.value))
            slider = ui.slider(min=minimum, max=maximum, step=1, value=stored,
                               on_change=lambda e: scrub(e.value)).props("dense").classes("w-full")
            slider.on("change", lambda e: commit(e.args))

    def _set_setting(self, row: MatrixRow, key: str, value: Any) -> None:
        row.settings[key] = value
        self._settings_edited(row)

    def _settings_edited(self, row: MatrixRow) -> None:
        """A setting of a file changed: show it on the preview and in its table row, and keep it."""
        self.selected_uid = row.uid
        self._sync_previews()
        self._sync_tables()
        self._sync_action_box()
        self._mount_sidebar()
        self._save_matrix()

    def _toggle_overlay(self, show: bool) -> None:
        self.show_overlay = bool(show)
        self._sync_previews()

    def _toggle_stages(self, show: bool) -> None:
        self.show_stages = bool(show)
        self._sync_previews()

    def _stored_analysis(self, row: MatrixRow) -> dict[str, Any] | None:
        """The stored analysis of this file: where its rows are and the crop it ran with."""
        stored = self._stored_result(row)
        if stored is None:
            return None
        return {"rows_path": stored.rows_path, "file_id": stored.file_id, "settings": stored.settings,
                "analyzed": local_time(stored.analyzed_at) if stored.analyzed_at else "an earlier run"}

    # -- 3. Results -----------------------------------------------------------------------

    def _render_results_table(self) -> None:
        """The one table of the stage: a line per file with anything to show, with Use, which every
        section below follows; an unticked file stays here but leaves the plots and export."""
        from nicegui import ui

        rows = self._aggregated_rows()
        with ui.column().classes("panel w-full gap-2 p-3"):
            with ui.row().classes("items-center gap-4 no-wrap"):
                ui.label("Data").classes("text-xs muted")
                for kind, label in RESULT_DATA.items():
                    box = ui.checkbox(label, value=kind in self.results_data).props("dense")
                    box.on("update:model-value", lambda event, kind=kind: self._set_results_data(kind, bool(event.args)),
                           [None])
                choices = self._results_set_choices()
                if choices:
                    ui.label("Sets").classes("text-xs muted")
                    ui.select(choices, multiple=True,
                              value=[n for n in (self.results_sets or choices) if n in choices],
                              on_change=self._set_results_sets).props(
                        "dense outlined options-dense use-chips").classes("sets-select")
            if not rows:
                ui.label("No ticked file is analysed yet or has the rig's live QC." if self.results_data
                         else "No data chosen.").classes("muted text-xs")
                return
            table = ui.table(columns=results_columns(AGGREGATED_FIELDS, RESULT_LABELS), rows=rows,
                             row_key="uid").classes("slim-table matrix-table results-table w-full").props(
                "dense flat hide-bottom")
            self._register_table("results", table)
            wire_results_table(table, self._handle_matrix_change, AGGREGATED_FIELDS)
            self._render_use_all_header(table)

    def _set_results_data(self, kind: str, chosen: bool) -> None:
        """A kind of data taken into 3. Results or left out, for every table, plot and export; remembered."""
        wanted = (self.results_data | {kind}) if chosen else (self.results_data - {kind})
        if wanted == self.results_data:
            return
        self.results_data = wanted
        self.store.set_view_setting("results_data", [kind for kind in RESULT_DATA if kind in wanted])
        self._refresh(force_mount=True)

    def _result_sections(self) -> list[str]:
        """The sections with something to show, in their order: aggregated, then the engines' own, then
        the rig's live QC with the fluidics; a section without data is not there."""
        results = self._results(ticked_only=False)
        lives = any(self._live_qc(row) is not None for row in self._for_results())
        present = {"aggregated": bool(results) or lives,
                   "opencv": any(r.engine == "opencv" for r in results),
                   "cellpose": any(r.engine == "cellpose" for r in results), "live": lives}
        return [section for section, _label in RESULT_SECTIONS if present[section]]

    def _styles(self) -> dict[tuple[str, str], Any]:
        """A style per file, its sample's hue and shade, over the ticked results and live lines; the
        hues follow the whole list's order, so a file keeps its colour whichever are ticked."""
        return plot_styles([*self._results(), *self._lives()], listed=[row.set or row.name for row in self.matrix])

    def _results_rows(self, engine: str | None = None) -> list[dict[str, Any]]:
        """One row per listed file with a stored result (of the engine), ticked or not; the ticked ones
        in their plot colour."""
        styles = self._styles()
        by_key = {_result_key(result): result for result in self._results(ticked_only=False)}
        rows = []
        for row in self._for_results():
            result = by_key.get(self._row_key(row))
            if result is None or (engine and result.engine != engine):
                continue
            style = styles.get((result.run, result.source_path))
            colour = style.color if style is not None else "var(--text-disabled)"
            rows.append({"uid": row.uid, "active": row.plotted, **results_cells(result, colour, active=row.plotted)})
        return rows

    def _live_rows(self) -> list[dict[str, Any]]:
        """One row per listed video the rig measured while recording, named as the file is."""
        styles = self._styles()
        rows = []
        for row in self._for_results():
            live = self._live_qc(row)
            if live is None:
                continue
            style = styles.get((live.run, live.source_path))
            colour = style.color if style is not None else "var(--text-disabled)"
            rows.append({"uid": f"{row.uid}:live", "active": row.plotted,
                         **live_cells(live, row.name, colour, active=row.plotted)})
        return rows

    def _aggregated_rows(self) -> list[dict[str, Any]]:
        """One row per listed file with anything to show: the analysis's numbers, the rig's live ones under."""
        styles = self._styles()
        by_key = {_result_key(result): result for result in self._results(ticked_only=False)}
        rows = []
        for row in self._for_results():
            result, live = by_key.get(self._row_key(row)), self._live_qc(row)
            if result is None and live is None:
                continue
            key = (result.run, result.source_path) if result is not None else (live.run, live.source_path)
            rows.append({"uid": row.uid, "active": row.plotted,
                         **aggregated_cells(row.name, styles.get(key), result, live, active=row.plotted,
                                            origin=row.origin)})
        return rows

    def _live_qc(self, row: MatrixRow):
        """The live QC the rig wrote for this video while recording it, when the file came from a run and
        3. Results takes live data."""
        if not row.run or row.engine != "opencv" or "live" not in self.results_data:
            return None
        try:
            mtime = (Path(row.run) / "run.csv").stat().st_mtime
        except OSError:
            return None
        return _live_qc_of(row.run, str(Path(row.source_path)), mtime)

    def _lives(self) -> list[LiveQC]:
        """The live QC of every ticked video that has one, named as the file is: data to view like a result."""
        lives = []
        for row in self._shown():
            live = self._live_qc(row)
            if live is not None:
                lives.append(replace(live, sample_id=row.name, replicate=row.replicate, set=row.set))
        return lives

    def _render_results_plots(self) -> None:
        """One page: the aggregated plots, then a section of plots per engine, then the rig's live QC
        and fluidics, each only when there is data for it; the one table above chooses for all."""
        from nicegui import ui

        sections = self._result_sections()
        if not sections:
            with ui.column().classes("panel w-full gap-2 p-3"):
                ui.label("Plots").classes("section-title")
                ui.label("Plots appear here once a ticked file is analysed or has the rig's live QC.").classes(
                    "muted text-xs")
            return
        listed = [row.set or row.name for row in self.matrix]
        results, lives = self._results(), self._lives()
        labels = dict(RESULT_SECTIONS)
        for section in sections:
            with ui.column().classes("panel w-full gap-2 p-3"):
                ui.label(labels[section]).classes("section-title")
                if section == "live":
                    self._render_live_charts()
                    continue
                charts = result_charts(results, lives, view=section, listed=listed)
                if charts:
                    self._render_charts(charts, "results-grid")
                else:
                    ui.label("Tick a file of this section to plot it.").classes("muted text-xs")

    def _render_charts(self, charts: list[tuple[str, dict[str, Any]]], grid: str) -> None:
        from nicegui import ui

        with ui.element("div").classes(f"{grid} w-full"):
            for name, options in charts:
                with ui.column().classes("plot-card"):
                    chart = ui.echart(options).classes("w-full h-full")
                self._chart_refs.append((name, chart))

    def _live_runs(self) -> list[tuple[str, str]]:
        """The runs of the ticked videos the rig measured, as (folder, name), in the list's order."""
        runs: dict[str, str] = {}
        for row in self._shown():
            if row.run and self._live_qc(row) is not None:
                runs.setdefault(row.run, run_label(row.run))
        return list(runs.items())

    def _run_plots(self) -> list[RunPlot]:
        """Every ticked run for the live plots, its ticked recordings named and styled as their files are."""
        styles = self._styles()
        plots = []
        for run_dir, label in self._live_runs():
            files = {}
            for row in self._shown():
                if row.run == run_dir and self._live_qc(row) is not None:
                    files[Path(row.source_path).stem] = (
                        row.name, styles.get((Path(row.run).name, str(Path(row.source_path)))))
            plots.append(RunPlot(run_dir, label, files))
        return plots

    def _render_live_charts(self) -> None:
        """The ticked recordings' steps, stacked, as the rig's Review stage shows them: pressure, flow,
        droplet rate and CV, every step from its own start; nothing to pick, the table above chooses."""
        from nicegui import ui

        charts = runs_charts(self._run_plots())
        if not charts:
            ui.label("Tick a video from a run to plot what the rig measured.").classes("muted text-xs")
            return
        self._render_charts(charts, "results-grid-2")

    def _results(self, *, ticked_only: bool = True) -> list[FileResult]:
        """Stored results of the listed files -- the ticked ones unless asked otherwise -- with the hand corrections.

        A file is known by its path, never by its sample name: two files may be given the same name.
        """
        listed = {self._row_key(row) for row in self.matrix
                  if row.active and (row.plotted or not ticked_only) and self._in_results_sets(row)}
        corrections = self._corrections()
        results = []
        for row in self.matrix:
            if self._row_key(row) not in listed:
                continue
            stored = self._stored_result(row)
            if stored is None:
                continue
            # Each file's rows are read with its own scale, fps, flow and corrections, so a change to one
            # file leaves the others' parsed results in the cache.
            fixes = json.dumps(corrections.get((row.engine, self._row_key(row)[2]), {}), sort_keys=True)
            scales = json.dumps({"scale": [*self._row_scale(row), self._row_flow(row)],
                                 "read": self._read_settings(row)}, sort_keys=True)
            # The name, the set and the replicate are the row's, stamped on after the parse: renaming or
            # moving a file between sets re-reads nothing.
            results.extend(replace(result, sample_id=row.name, set=row.set, replicate=row.replicate)
                           for result in _cached_results(stored, fixes, scales)
                           if _result_key(result) in listed and result.engine in self.results_data)
        return results

    def _read_settings(self, row: MatrixRow) -> dict[str, Any]:
        """The settings applied when the file's rows are read, not by a run: the window and the gate
        of a video, the frame limit and the inclusions of an image folder."""
        if row.engine == "cellpose":
            return {"frame_limit": _row_int(row, "frame_limit", 0) or None,
                    "detect_inclusions": _row_bool(row, "detect_inclusions", True)}
        return {"skip_left_pct": _row_float(row, "skip_left_pct", 0.0),
                "skip_right_pct": _row_float(row, "skip_right_pct", 0.0),
                "gate_pct": _row_float(row, "gate_pct", 50.0)}

    def _row_scale(self, row: MatrixRow) -> tuple[float, float]:
        """The µm per px and fps the file's rows are read with now: the table's values, not the run's."""
        if row.engine == "cellpose":
            return _row_float(row, "px_to_um", 0.0), 0.0
        return _row_float(row, "microns_per_pixel", 0.0), _row_float(row, "fps", 0.0)

    def _row_flow(self, row: MatrixRow) -> float:
        """The flow that made the file's droplets, µL/min, from the recording's own record."""
        if row.engine == "cellpose":
            return 0.0
        return dispersed_flow_ul_min(_video_record(Path(row.source_path)))

    def _row_key(self, row: MatrixRow) -> tuple[str, str, str]:
        return (row.run_name, row.engine, str(Path(row.source_path).resolve()))

    def _corrections(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Hand corrections by (engine, source path), the way the results are keyed."""
        return {(row.engine, self._row_key(row)[2]): row.settings["corrections"]
                for row in self.matrix if row.settings.get("corrections")}

    def _export_csv(self) -> None:
        from nicegui import ui

        results = self._results()
        if not results:
            self._notify("Nothing to export yet.", "warning")
            self._mount_sidebar()
            return
        live = {self._row_key(row): measured for row in self.matrix if (measured := self._live_qc(row)) is not None}
        ui.download.content(export_csv_zip(self._stored_results(), results, self._corrections(), live), "analysis.zip")
        self._log(f"export: {len(results)} result(s) as CSV")
        self._notify(f"Exported {_count(len(results), 'result')} as CSV.", "success")
        self._sync_stage()

    async def _export_plots(self) -> None:
        from nicegui import ui

        charts = [(name, chart) for name, chart in self._chart_refs if not chart.is_deleted]
        if not charts:
            self._notify("No plots to export.", "warning")
            self._mount_sidebar()
            return
        files = {}
        for name, chart in charts:
            url = await chart.run_chart_method("getDataURL", {"type": "png", "pixelRatio": 2,
                                                              "backgroundColor": "#ffffff"})
            if isinstance(url, str) and "," in url:
                files[f"{name}.png"] = base64.b64decode(url.split(",", 1)[1])
        ui.download.content(zip_bytes(files), "analysis_plots.zip")
        self._log(f"export: {len(files)} plot(s) as PNG")
        self._notify(f"Exported {_count(len(files), 'plot')} as PNG.", "success")
        self._sync_stage()

    # -- stored data ----------------------------------------------------------------------

    def _stored_result(self, row: MatrixRow) -> StoredResult | None:
        """This file's stored analysis for the settings it has now, if there is one.

        An image folder's run over all its frames serves any frame limit, the limit being applied
        when read. A video result made before the window was read-time, with a window baked in,
        does not serve: the file reads as not analysed and is run again over the whole width.
        """
        settings = self._engine_settings(row)
        source = Path(row.source_path)
        stored = load_stored_result(self.store.result_dir(source, settings), source_path=source, engine=row.engine,
                                    sample_id=row.name, run=row.run, replicate=row.replicate, group=row.set)
        if stored is None and row.engine == "cellpose" and settings.get("frame_limit"):
            stored = load_stored_result(self.store.result_dir(source, {**settings, "frame_limit": None}),
                                        source_path=source, engine=row.engine, sample_id=row.name, run=row.run,
                                        replicate=row.replicate, group=row.set)
        if stored is not None and row.engine == "opencv" and stale_window(stored.metadata):
            return None
        return stored

    def _stored_results(self) -> list[StoredResult]:
        return [stored for stored in (self._stored_result(row) for row in self.matrix) if stored is not None]

    def _analyzed_at(self, row: MatrixRow) -> str:
        """When this file was last analysed as it is set up now; empty if never."""
        stored = self._stored_result(row)
        if stored is None:
            return ""
        return local_time(stored.analyzed_at) if stored.analyzed_at else "yes"

    # -- dialogs --------------------------------------------------------------------------

    def _open_folder_browser(self) -> None:
        roots = self.store.roots()
        self._open_path_browser(title="Add a folder of runs", start=Path(roots[0]) if roots else Path.home(),
                                mode="folder")

    def _open_source_browser(self) -> None:
        roots = self.store.roots()
        self._open_path_browser(title="Add files", start=Path(roots[0]) if roots else Path.home(), mode="source")

    def _open_path_browser(self, *, title: str, start: Path, mode: str) -> None:
        from nicegui import ui

        state = {"path": _existing_dir(start)}
        selected: set[str] = set()
        multi = mode == "source"
        dialog = ui.dialog().classes("browser-dialog")
        with dialog, ui.card().classes("browser-card"):
            ui.label(title).classes("section-title")
            if multi:
                ui.label("Videos (.avi, .mp4, .mov, .mkv) and folders of images (.tif, .png, .jpg). "
                         "Press ＋ to pick, then Add.").classes("muted text-xs")
            else:
                ui.label("A data folder, an export folder on the server, or one run: every video of every run "
                         "in it is listed. Open the folder, then Add this folder.").classes("muted text-xs")
            path_label = ui.label(str(state["path"])).classes("browser-path")
            rows = ui.column().classes("browser-list w-full gap-1")
            with ui.row().classes("w-full gap-2 justify-end items-center"):
                status = ui.label("").classes("muted text-xs mr-auto")
                ui.button("Cancel", on_click=dialog.close).props("dense no-caps outline")
                if multi:
                    ui.button("Add", on_click=lambda: finish()).props("dense no-caps")
                else:
                    ui.button("Add this folder", on_click=lambda: choose_folder()).props("dense no-caps")

        def toggle(path: Path) -> None:
            key = str(path.resolve())
            selected.discard(key) if key in selected else selected.add(key)
            status.set_text(f"{len(selected)} picked")
            render_entries()

        def finish() -> None:
            added = [name for name in (self._append_source(Path(key)) for key in sorted(selected)) if name]
            dialog.close()
            if added:
                self._notify(f"Added {_count(len(added), 'file')}: {', '.join(added)}.", "success")
            else:
                self._notify("Nothing new was added.", "warning")
            self._refresh()

        def render_entries() -> None:
            current = state["path"]
            path_label.set_text(str(current))
            rows.clear()
            with rows:
                if current.parent != current:
                    ui.button("⬆  ..", on_click=lambda path=current.parent: navigate(path)).props(
                        "dense no-caps flat").classes("browser-row browser-name")
                entries = _browser_entries(current, mode)
                if not entries:
                    ui.label("Nothing here to add." if multi else "No folders here.").classes("muted text-xs")
                    return
                for entry in entries:
                    with ui.row().classes("browser-row w-full items-center no-wrap gap-2"):
                        if multi and _addable(entry):
                            picked = str(entry.resolve()) in selected
                            ui.button("✓" if picked else "＋", on_click=lambda path=entry: toggle(path)).props(
                                "dense no-caps " + ("unelevated color=primary" if picked else "outline")
                            ).classes("browser-select")
                        elif multi:
                            ui.element("span").classes("browser-select")
                        if entry.is_dir():
                            ui.button(entry.name + "/", on_click=lambda path=entry: navigate(path)).props(
                                "dense no-caps flat").classes("grow browser-name")
                        else:
                            ui.label(entry.name).classes("grow browser-name")

        def choose_folder() -> None:
            dialog.close()
            self._add_root(state["path"])

        def navigate(path: Path) -> None:
            state["path"] = _existing_dir(path)
            render_entries()

        render_entries()
        dialog.open()

    # -- the file list --------------------------------------------------------------------

    def _append_source(self, source: Path) -> str:
        """Add a file to the list and the registry; its name if it was new, empty otherwise."""
        source = source.expanduser().resolve()
        try:
            engine = infer_engine(source)
        except Exception as exc:
            self._notify(f"Not a video or image folder: {exc}", "warning")
            return ""
        if any(Path(item.source_path) == source for item in self.matrix):
            return ""
        row = MatrixRow(uid=_uid(), source_path=str(source), engine=engine,
                        sample_id="")                                 # named after the file until written over
        self.matrix.append(row)
        self.selected_uid = row.uid
        self.store.add_source(source, engine=engine, sample_id="")
        _name_rows(self.matrix)
        self._prefill_settings(row)
        self._log(f"files: added {source.name} → {engine}")
        return source.name

    def _handle_matrix_remove(self, event: Any) -> None:
        payload = getattr(event, "args", {})
        uid = str(payload.get("uid") or "") if isinstance(payload, dict) else ""
        row = next((item for item in self.matrix if item.uid == uid), None)
        if row is not None:
            self._remove_row(row)
            self._refresh()

    def _remove_row(self, row: MatrixRow) -> None:
        """Take a file off the list and out of the registry; its stored results stay on disk."""
        self.matrix.remove(row)
        if self.selected_uid == row.uid:
            self.selected_uid = ""
        self.store.remove_source(row.source_path)
        self._saved_matrix = None
        self._log(f"files: removed {Path(row.source_path).name}")
        self._notify(f"Removed {Path(row.source_path).name} from the list.", "success")

    def _clear_matrix(self) -> None:
        for row in list(self.matrix):
            self._remove_row(row)
        self._notify("List cleared; the stored results stay.", "success")
        self._log("files: cleared")
        self._refresh()

    def _reset_row_settings(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self._reset_settings(row)
        self._notify(f"Settings of {row.name} are back to the defaults.", "success")
        self._log(f"settings: reset {row.name}")
        self._refresh(force_mount=True)

    def _reset_ticked_settings(self) -> None:
        """Every ticked file back to the defaults and to the fps and µm/px its recording carries."""
        rows = self._targets()
        for row in rows:
            self._reset_settings(row)
        self._notify(f"{_count(len(rows), 'ticked file')} back to the defaults.", "success")
        self._log(f"settings: reset {len(rows)} ticked file(s)")
        self._refresh(force_mount=True)

    def _reset_settings(self, row: MatrixRow) -> None:
        """The defaults again, with the fps and µm/px the recording carries; whatever was typed goes."""
        row.settings = {}
        self._prefill_settings(row)

    def _handle_matrix_change(self, event: Any) -> None:
        payload = getattr(event, "args", {})
        if not isinstance(payload, dict):
            return
        uid = str(payload.get("uid") or "").removesuffix(":live")       # the live line ticks with its file
        field_name = str(payload.get("field") or "")
        value = payload.get("value")
        row = next((item for item in self.matrix if item.uid == uid), None)
        if row is None:
            return
        if field_name == "active":
            # Shift held: every row between the last box clicked and this one takes the same tick.
            shown = self.matrix if self._stage_id() == "files" else self._listed()
            targets = [row]
            if payload.get("shift") and self._last_tick:
                uids = [r.uid for r in shown]
                if row.uid in uids and self._last_tick in uids:
                    first, last = sorted((uids.index(row.uid), uids.index(self._last_tick)))
                    targets = shown[first:last + 1]
            for target in targets:
                self._tick(target, self._stage_id(), bool(value))
            self._last_tick = row.uid
            self._save_matrix()
            self._sync_use_all()
            self._sync_action_box()
            self._notify(self._stage_summary(), "primary")
            self._mount_sidebar()
            self._sync_tables()                          # past 1. Files the row greys out, or comes back
            if self._stage_id() != "files":
                self._remount_main()                     # the plots follow the ticked files
            return
        if field_name == "sample_id":
            row.sample_id = str(value or "").strip()     # written over, or cleared for the built name
            self._save_matrix()
            self._sync_tables()
            return
        if field_name == "set":
            row.set = str(value or "").strip()
            self._save_matrix()
            self._refresh(force_mount=True)              # the Set choices follow
            return
        kind = matrix_field_kind(field_name)
        if kind is None:
            return
        row.settings[setting_key(field_name, row.engine)] = cast_matrix_value(kind, value)
        self._settings_edited(row)

    def _select_row_event(self, event: Any) -> None:
        row = _event_row(event)
        uid = str(row.get("uid") or "")
        if uid and uid != self.selected_uid:
            self.selected_uid = uid
            self._refresh()

    def _selected_row(self) -> MatrixRow | None:
        """The file picked in the table; past 1. Files an unticked one is not on the page, so not picked."""
        row = next((row for row in self.matrix if row.uid == self.selected_uid), None)
        if row is not None and not row.active and self._stage_id() != "files":
            return None
        return row

    @staticmethod
    def _tick(row: MatrixRow, stage: str, value: bool) -> None:
        """Each stage's Use is its own: 1. Files passes a file on, 2. Analyse runs it, 3. Results plots it."""
        if stage == "files":
            row.active = value
        elif stage == "analyse":
            row.to_run = value
        else:
            row.plotted = value

    def _listed(self) -> list[MatrixRow]:
        """The files that went on from 1. Files: what the stages after it list."""
        return [row for row in self.matrix if row.active]

    def _targets(self, engine: str | None = None) -> list[MatrixRow]:
        """The files to run: listed past 1. Files and ticked on 2. Analyse."""
        return [row for row in self.matrix if row.active and row.to_run and (engine is None or row.engine == engine)]

    def _shown(self) -> list[MatrixRow]:
        """The files in the plots and the export: listed past 1. Files, of the sets chosen, ticked on 3. Results."""
        return [row for row in self.matrix if row.active and row.plotted and self._in_results_sets(row)]

    def _row_ready(self, row: MatrixRow) -> bool:
        """Whether the file has what a run cannot do without."""
        if row.engine == "cellpose":
            return _row_float(row, "px_to_um", 0.0) > 0
        return _row_float(row, "microns_per_pixel", 0.0) > 0 and _row_float(row, "fps", 0.0) > 0

    # -- the registry -----------------------------------------------------------------------

    def _prefill_settings(self, row: MatrixRow) -> None:
        """Fill what a file does not have yet: frames and crop from the video, detection defaults,
        the FPS control recorded with it, and the µm/px control measured on its preview."""
        if row.engine == "cellpose":
            for key, value in IMAGING_DEFAULTS.items():
                row.settings.setdefault(key, value)
            return
        for key, value in DETECTION_DEFAULTS.items():
            row.settings.setdefault(key, value)
        size = _known_video_size(Path(row.source_path))
        if size is not None:
            frames, width, height = size
            for key, value in {"start_frame": 0, "end_frame": frames, "roi_x": 0, "roi_y": 0,
                               "roi_width": 0, "roi_height": 0}.items():
                row.settings.setdefault(key, value)
        captured = _recorded_capture_fps(row)
        if captured and not _row_float(row, "fps", 0.0):
            row.settings["fps"] = round(captured, 3)
        measured = _recorded_scale(row)
        if measured and not _row_float(row, "microns_per_pixel", 0.0):
            row.settings["microns_per_pixel"] = round(measured, 4)

    def _load_matrix(self) -> list[MatrixRow]:
        rows = []
        for entry in self.store.sources():
            try:
                engine = str(entry.get("engine") or infer_engine(Path(entry["path"])))
            except ValueError:
                continue
            run = str(entry.get("run") or "")
            origin: dict[str, Any] = {"protocol": str(entry.get("protocol") or ""),
                                      "run_time": str(entry.get("run_time") or ""),
                                      "replicate": _int_or_none(entry.get("replicate"))}
            if run and (not any(origin.values()) or "replicate" not in entry):
                origin = _run_origin(run, Path(entry["path"]).name)      # listed before these were fields
            rows.append(MatrixRow(uid=_uid(), source_path=str(entry["path"]), engine=engine,
                                  sample_id=str(entry.get("sample_id") or ""),
                                  cache_policy=str(entry.get("cache_policy") or "use"),
                                  active=bool(entry.get("active", True)), to_run=bool(entry.get("to_run", True)),
                                  plotted=bool(entry.get("plotted", True)),
                                  settings=dict(entry.get("settings") or {}), run=run,
                                  set=str(entry.get("set") or ""), **origin))
        for row in rows:
            self._prefill_settings(row)
        _name_rows(rows)
        self._saved_matrix = self._matrix_record(rows)
        return rows

    @staticmethod
    def _matrix_record(rows: list[MatrixRow]) -> list[dict[str, Any]]:
        return [{"path": row.source_path, "engine": row.engine, "sample_id": row.sample_id,
                 "cache_policy": row.cache_policy, "active": row.active, "to_run": row.to_run, "plotted": row.plotted,
                 "settings": dict(row.settings), "run": row.run, "protocol": row.protocol, "run_time": row.run_time,
                 "replicate": row.replicate, "set": row.set}
                for row in rows]

    def _save_matrix(self) -> None:
        """Keep what the screen shows, so it comes back as left; an unchanged screen writes nothing."""
        _name_rows(self.matrix)
        record = self._matrix_record(self.matrix)
        if record == self._saved_matrix:
            return
        try:
            self.store.set_sources(record)
            self._saved_matrix = record
        except Exception as exc:
            self._log(f"registry: could not save the file list: {exc}")

    def _add_root(self, folder: Path) -> None:
        folder = Path(folder)
        new = self.store.add_root(folder)
        added = self.store.scan(folder)
        self._adopt(added)
        self._notify(f"{'Watching' if new else 'Rescanned'} {folder.name}: {_count(len(added), 'new video')}.",
                     "success")
        self._log(f"files: folder {folder} → {len(added)} new")
        self._refresh(force_mount=True)

    def _rescan(self) -> None:
        added = self.store.scan()
        self._adopt(added)
        self._notify(f"Rescanned {_count(len(self.store.roots()), 'folder')}: {_count(len(added), 'new video')}.",
                     "success")
        self._refresh(force_mount=True)

    def _adopt(self, entries: list[dict[str, Any]]) -> None:
        """New registry entries become rows on the screen."""
        known = {str(Path(row.source_path).resolve()) for row in self.matrix}
        for entry in entries:
            if str(Path(entry["path"]).resolve()) in known:
                continue
            row = MatrixRow(uid=_uid(), source_path=str(entry["path"]), engine=str(entry["engine"]),
                            sample_id=str(entry.get("sample_id") or ""), run=str(entry.get("run") or ""),
                            protocol=str(entry.get("protocol") or ""), run_time=str(entry.get("run_time") or ""),
                            replicate=_int_or_none(entry.get("replicate")))
            self._prefill_settings(row)
            self.matrix.append(row)
        _name_rows(self.matrix)
        if self.matrix and not self.selected_uid:
            self.selected_uid = self.matrix[0].uid
        if entries:
            self._saved_matrix = None

    # -- running --------------------------------------------------------------------------

    async def _run_rows(self, rows: list[MatrixRow]) -> None:
        from nicegui import run, ui

        rows = [row for row in rows if row.active and row.to_run and self._row_ready(row)]
        if not rows or self._running:
            return
        jobs = tuple(self._target_to_run(row) for row in rows)
        # Taken now: the refresh below deletes the button whose slot this handler runs in.
        page = ui.context.client
        self._running = True
        self._stop = AnalysisStop()
        self._run_progress = self._run_step_progress = 0.0
        self._notify(f"Analysing {_count(len(jobs), 'file')}…", "primary")
        self._log(f"analysis: running {', '.join(row.name for row in rows)}")
        self._refresh()
        with page.layout:                      # the page's outer layout, never redrawn
            timer = ui.timer(0.25, self._sync_run_progress)
        try:
            report = await run.io_bound(self._run_batch, jobs)
        except Exception as exc:
            self._notify(f"Analysis failed: {exc}", "danger")
            self._log(f"analysis: {type(exc).__name__}: {exc}")
            return
        finally:
            timer.cancel()
            self._running = False
            self._stop = None
            self._run_progress = self._run_step_progress = 0.0
        _cached_results.cache_clear()
        stopped = [job for job in report.jobs if job.status == "stopped"]
        failed = [job for job in report.jobs if job.status not in {"complete", "cached", "stopped"}]
        done = len(report.jobs) - len(failed) - len(stopped)
        if stopped:
            self._notify(f"Stopped: {_count(done, 'file')} analysed, {len(stopped)} not; run again to go on.", "warning")
        elif failed:
            self._notify(f"{_count(len(failed), 'file')} failed: "
                         + "; ".join(f"{job.sample_id}: {', '.join(job.warnings) or job.status}" for job in failed),
                         "danger")
        else:
            self._notify(f"Analysed {_count(len(report.jobs), 'file')} · see 3. Results.", "success")
        self._log(f"analysis: {'stopped' if stopped else 'done'}, {done} of {len(report.jobs)} complete")
        self._refresh(force_mount=True)

    def _stop_analysis(self) -> None:
        """Stop the run in hand at its next step: the file being analysed is left unfinished and is run
        again next time; the files already finished keep their results."""
        if not self._running or self._stop is None:
            return
        self._stop.now.set()
        self._notify("Stopping…", "warning")
        self._log("analysis: stop asked")

    def _run_batch(self, jobs: tuple[AnalyzeTarget, ...]) -> AnalyzeBatchReport:
        def on_progress(percent: float) -> None:
            self._run_progress = percent

        def on_file_progress(percent: float) -> None:
            self._run_step_progress = percent

        return AnalyzeBatchRunner(self.registry, results_dir=self.store.results_dir).run(
            jobs, on_progress=on_progress, on_file_progress=on_file_progress, stop=self._stop)

    def _sync_run_progress(self) -> None:
        progress = self._refs.get("action_progress")
        if progress is not None and not progress.is_deleted:
            progress.set_value(self._run_progress / 100.0)
        text = self._refs.get("action_progress_text")
        if text is not None and not text.is_deleted:
            text.set_text(self._progress()[1])

    def _target_to_run(self, row: MatrixRow) -> AnalyzeTarget:
        return AnalyzeTarget(source_path=Path(row.source_path), engine=row.engine, sample_id=row.name,
                             settings=self._engine_settings(row), cache_policy=row.cache_policy)

    def _engine_settings(self, row: MatrixRow) -> dict[str, Any]:
        if row.engine == "cellpose":
            return {"px_to_um": _row_float(row, "px_to_um", IMAGING_DEFAULTS["px_to_um"]),
                    "frame_limit": _row_int(row, "frame_limit", 0) or None,
                    "detect_inclusions": _row_bool(row, "detect_inclusions", True)}
        settings: dict[str, Any] = {
            "microns_per_pixel": _row_float(row, "microns_per_pixel", 0.0),
            "fps": _row_float(row, "fps", 0.0),
            "start_frame": _row_int(row, "start_frame", 0),
            "end_frame": _row_int(row, "end_frame", 0) or None,
            "roi_x": 0, "roi_y": 0, "roi_width": 0, "roi_height": 0,
        }
        for key, default in DETECTION_DEFAULTS.items():
            settings[key] = (_row_int if isinstance(default, int) else _row_float)(row, key, default)
        if _row_int(row, "roi_width", 0) or _row_int(row, "roi_height", 0):
            size = _known_video_size(Path(row.source_path)) or (0, 0, 0)
            settings.update({"roi_x": _row_int(row, "roi_x", 0), "roi_y": _row_int(row, "roi_y", 0),
                             "roi_width": _row_int(row, "roi_width", 0) or size[1],
                             "roi_height": _row_int(row, "roi_height", 0) or size[2]})
        return settings

    # -- actions, guards, stages ----------------------------------------------------------

    def _action_specs(self, stage_id: str) -> list[dict[str, Any]]:
        return [self._action_spec(action) for action in self._stage_by_id(stage_id).actions]

    def _action_spec(self, action: Any) -> dict[str, Any]:
        state = action_button_state(action, self._guard_value)
        return {"label": state.label, "handler": self._action_handler(action.action), "active": state.active,
                "warning": state.warning, "enabled": state.enabled, "action": action.action}

    def _action_handler(self, action: str | None) -> Any:
        if action == "browse_source":
            return self._open_source_browser
        if action == "browse_folder":
            return self._open_folder_browser
        if action == "clear_matrix":
            return self._clear_matrix
        if action == "reset_row_settings":
            return self._reset_row_settings
        if action == "reset_ticked_settings":
            return self._reset_ticked_settings
        if action == "analyze":
            return lambda: self._run_rows(self._targets())
        if action == "analyze_selected":
            return lambda: self._run_rows([row for row in [self._selected_row()] if row is not None])
        if action == "stop_analysis":
            return self._stop_analysis
        if action == "export_csv":
            return self._export_csv
        if action == "export_plots":
            return self._export_plots
        return lambda: None

    def _guard_value(self, guard: str) -> bool:
        ticked = self._targets()
        selected = self._selected_row()
        if guard == "has_matrix_rows":
            return bool(self.matrix)
        if guard == "has_targets":
            return bool(ticked)
        if guard == "targets_ready":
            return bool(ticked) and all(self._row_ready(row) for row in ticked)
        if guard == "has_selected":
            return selected is not None
        if guard == "selected_ready":
            return selected is not None and selected.active and self._row_ready(selected)
        if guard == "has_results":
            return bool(self._results())
        if guard == "running":
            return self._running
        return True

    def _activate(self, index: int) -> None:
        index = max(0, min(index, len(self.workflow.stages) - 1))
        if index == self.state.index:
            return
        self.state = replace(self.state, index=index)
        self._notify(self._stage_summary(), "primary")
        self._refresh()

    def _stage_summary(self) -> str:
        """Where this stage stands, for the notification card when it is opened."""
        stage_id = self._stage_id()
        ticked = self._targets()
        if stage_id == "files":
            return (f"{_count(len(self.matrix), 'file')} listed, {len(ticked)} ticked." if self.matrix
                    else "No files listed yet.")
        if stage_id == "analyse":
            if not ticked:
                return "No files ticked."
            analysed = sum(1 for row in ticked if self._analyzed_at(row))
            return f"{len(ticked)} ticked, {analysed} analysed."
        results, lives = self._results(), self._lives()
        live_note = f", {_count(len(lives), 'live line')}" if lives else ""
        return (f"{_count(len(results), 'result')}{live_note} for the ticked files." if results
                else f"Nothing analysed yet{live_note}.")

    def _stage_id(self) -> str:
        return self._stage().id

    def _stage(self) -> Any:
        return current_stage(self.workflow, self.state)

    def _stage_by_id(self, stage_id: str) -> Any:
        return stage_by_id(self.workflow, self.state, stage_id)

    def _instruction(self) -> str:
        return instruction_text(self._stage(), self._guard_value)

    def _notify(self, message: str, kind: str) -> None:
        self.notice = message
        self.notice_kind = kind

    def _log(self, message: str) -> None:
        self.action_log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def _refresh(self, *, force_mount: bool = False) -> None:
        self._save_matrix()
        self._render_current_stage(force_mount=force_mount)


# -- stored results, read once per version of the raw file ------------------------------

@lru_cache(maxsize=256)
def _results_of(result_dir: str, _mtime: float, source_path: str, engine: str, sample_id: str, run: str,
                fixes: str, scales: str) -> tuple[FileResult, ...]:
    """One file's results parsed from its rows: FIXES are its own corrections, SCALES its own
    (µm/px, fps, flow), as JSON, so the cache is keyed by what the parse depends on and no more."""
    stored = load_stored_result(Path(result_dir), source_path=Path(source_path), engine=engine, sample_id=sample_id,
                                run=run)
    if stored is None:
        return ()
    key = (engine, str(stored.source_path))
    own_fixes = json.loads(fixes) if fixes else {}
    corrections = {key: own_fixes} if own_fixes else {}
    reading = json.loads(scales) if scales else {}
    scale_of = {key: tuple(reading["scale"])} if reading.get("scale") else {}
    settings_of = {key: dict(reading["read"])} if reading.get("read") else {}
    return tuple(file_results(stored, iter_rows(stored.rows_path), corrections, scale_of, settings_of))


def _run_origin(run_dir: str, video_name: str) -> dict[str, str]:
    """A file's protocol, time and step from its run folder, for a file listed before these were fields."""
    from admet.core.registry import replicate_of, run_time
    from admet.core.runs import Run

    record = Run(run_dir).read()
    protocol = str(record.get("tag") or (record.get("normalized_settings") or {}).get("protocol", {}).get("name")
                   or record.get("name") or Path(run_dir).name[16:] or Path(run_dir).name)
    videos = [v for v in record.get("artifacts", {}).get("videos", []) if isinstance(v, dict) and v.get("video_path")]
    if isinstance(record.get("recording"), dict) and record["recording"].get("video_path"):
        videos.append(record["recording"])
    mine = next((v for v in videos if Path(str(v["video_path"])).name == video_name), None)
    return {"protocol": protocol, "run_time": run_time(run_dir),
            "replicate": replicate_of(mine, len(videos)) if mine is not None else None}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _name_rows(rows: list[MatrixRow]) -> None:
    """Every row's name in force: what the user wrote, else built. A file in a set is "<set>_rep<n>",
    the n-th of the set by run time and replicate; a file in none is its protocol with its replicate,
    or its file name."""
    by_set: dict[str, list[MatrixRow]] = {}
    for row in rows:
        by_set.setdefault(row.set, []).append(row)
    for group, members in by_set.items():
        if group:
            ordered = sorted(members, key=lambda r: (r.run_time, r.replicate or 0, rows.index(r)))
            for n, row in enumerate(ordered, start=1):
                row.built = f"{group}_rep{n}"
        else:
            for row in members:
                row.built = row.own_name


@lru_cache(maxsize=64)
def _live_qc_of(run_dir: str, source_path: str, _mtime: float):
    return live_qc(run_dir, source_path)


def _result_key(result: FileResult) -> tuple[str, str, str]:
    return (result.run, result.engine, str(Path(result.source_path).resolve()))


def _cached_results(stored: StoredResult, fixes: str = "", scales: str = "") -> tuple[FileResult, ...]:
    try:
        mtime = stored.rows_path.stat().st_mtime
    except OSError:
        return ()
    return _results_of(str(stored.result_dir), mtime, str(stored.source_path), stored.engine, "",   # named by the row
                       stored.run, fixes, scales)


_cached_results.cache_clear = _results_of.cache_clear  # type: ignore[attr-defined]


# -- video and images --------------------------------------------------------------------

@lru_cache(maxsize=64)
def _probe_video(path: str, _size: int, _mtime: float) -> tuple[int, int, int, float] | None:
    """Frame count, width, height and playback fps as the file reports them, or None if unreadable."""
    try:
        import cv2
    except Exception:
        return None
    capture = cv2.VideoCapture(path)
    try:
        if not capture.isOpened():
            return None
        frames, width, height = (int(capture.get(prop) or 0) for prop in (
            cv2.CAP_PROP_FRAME_COUNT, cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        return (frames, width, height, fps) if frames and width and height else None
    finally:
        capture.release()


def _known_video_size(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    probed = _probe_video(str(path), stat.st_size, stat.st_mtime)
    return probed[:3] if probed else None


def _video_size(path: Path) -> tuple[int, int, int]:
    return _known_video_size(path) or (1, 16, 9)


def _file_fps(path: Path) -> float:
    try:
        stat = path.stat()
    except OSError:
        return 0.0
    probed = _probe_video(str(path), stat.st_size, stat.st_mtime)
    return probed[3] if probed else 0.0


def _read_video_frame(path: Path, frame_index: int) -> dict[str, Any]:
    if not path.exists():
        return {"error": f"Video file is not readable: {path}", "src": "", "width": 0, "height": 0}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _decode_video_frame(str(path), mtime, int(frame_index))


@lru_cache(maxsize=128)
def _decode_video_frame(path_str: str, _mtime: float, frame_index: int) -> dict[str, Any]:
    try:
        import cv2
    except Exception as exc:
        return {"error": f"OpenCV is unavailable in this environment: {exc}", "src": "", "width": 0, "height": 0}
    capture = cv2.VideoCapture(path_str)
    try:
        if not capture.isOpened():
            return {"error": f"OpenCV cannot open video: {path_str}", "src": "", "width": 0, "height": 0}
        if frame_index > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok or frame is None:
            return {"error": f"OpenCV cannot read frame {frame_index} from {path_str}", "src": "",
                    "width": 0, "height": 0}
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            return {"error": f"OpenCV cannot encode frame {frame_index}", "src": "", "width": 0, "height": 0}
        height, width = frame.shape[:2]
        data = base64.b64encode(encoded.tobytes()).decode("ascii")
        return {"error": "", "src": f"data:image/jpeg;base64,{data}", "width": width, "height": height}
    finally:
        capture.release()


@lru_cache(maxsize=64)
def _count_images(path: str, _mtime: float) -> int:
    source = Path(path)
    if source.is_file():
        return 1 if source.suffix.lower() in IMAGE_SUFFIXES else 0
    if not source.is_dir():
        return 0
    groups = CellposeDetection(use_cache=False).load_and_group_images(source)
    if groups:
        return len(groups)
    return len([item for item in source.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES])


def _image_count(source: Path) -> int:
    try:
        return max(1, _count_images(str(source), source.stat().st_mtime))
    except OSError:
        return 1


def _read_image_source(path: Path, index: int) -> dict[str, Any]:
    try:
        image, label = _image_at(path, index)
    except Exception as exc:
        return {"error": f"Cannot read the image: {exc}", "src": "", "index": index}
    if image is None:
        return {"error": f"No readable image in {_compact_path(str(path))}", "src": "", "index": index}
    import cv2

    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        return {"error": f"Cannot encode the image {label}", "src": "", "index": index}
    height, width = image.shape[:2]
    data = base64.b64encode(encoded.tobytes()).decode("ascii")
    return {"error": "", "src": f"data:image/jpeg;base64,{data}", "index": index, "width": width, "height": height}


def _image_at(path: Path, index: int) -> tuple[Any | None, str]:
    if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
        return read_image_8bit(path), str(path)
    if not path.is_dir():
        return None, str(path)
    detector = CellposeDetection(use_cache=False)
    groups = detector.load_and_group_images(path)
    if groups:
        frame_ids = sorted(groups)
        frame_id = frame_ids[max(0, min(index - 1, len(frame_ids) - 1))]
        return detector.create_min_projection(groups[frame_id]), f"{path} frame {frame_id}"
    images = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        return None, str(path)
    image_path = images[max(0, min(index - 1, len(images) - 1))]
    return read_image_8bit(image_path), str(image_path)


def _frame_id_at(source: Path, index: int) -> int:
    """The frame number the engine gives the image at slider position INDEX (from 1)."""
    if source.is_dir():
        groups = CellposeDetection(use_cache=False).load_and_group_images(source)
        if groups:
            ids = sorted(groups)
            return ids[max(0, min(index - 1, len(ids) - 1))]
    return index


def _droplet_circle(droplet: dict[str, Any]) -> tuple[float, float, float]:
    """Centre and radius of a stored imaging droplet, in image pixels."""
    cx = float(droplet.get("center_x", droplet.get("centroid_x", 0)) or 0)
    cy = float(droplet.get("center_y", droplet.get("centroid_y", 0)) or 0)
    diameter = float(droplet.get("diameter_px") or 0) or 2 * math.sqrt(float(droplet.get("area_px") or 0) / math.pi)
    return cx, cy, diameter / 2


def _droplet_at(droplets: list[dict[str, Any]], x: float, y: float) -> dict[str, Any] | None:
    """The droplet under a point: the nearest one whose circle holds it."""
    best, best_distance = None, None
    for droplet in droplets:
        cx, cy, radius = _droplet_circle(droplet)
        distance = math.hypot(x - cx, y - cy)
        if distance <= radius and (best_distance is None or distance < best_distance):
            best, best_distance = droplet, distance
    return best


def apply_click(fix: dict[str, Any] | None, *, shift: bool = False, alt: bool = False) -> dict[str, Any]:
    """What a click does to one droplet's correction: shift adds an inclusion, alt removes one,
    a plain click switches the droplet off or back on. Empty means no correction is left."""
    fix = dict(fix or {})
    if shift or alt:
        fix["inclusions"] = int(fix.get("inclusions") or 0) + (1 if shift else -1)
        if not fix["inclusions"]:
            del fix["inclusions"]
    else:
        fix["disabled"] = not fix.get("disabled")
        if not fix["disabled"]:
            del fix["disabled"]
    return fix


def _corrections_rows(target: MatrixRow) -> list[dict[str, Any]]:
    summary = corrections_summary(target.settings.get("corrections") or {})
    return [{"item": "Droplets switched off", "value": summary["disabled"]},
            {"item": "Inclusions added", "value": summary["added"]},
            {"item": "Inclusions removed", "value": summary["removed"]}]


def _png_src(image: Any) -> str:
    """An image as a PNG data URL: lossless, so a binary stage stays crisp."""
    import cv2

    ok, encoded = cv2.imencode(".png", image)
    return f"data:image/png;base64,{base64.b64encode(encoded.tobytes()).decode('ascii')}" if ok else ""


def _recorded_capture_fps(row: MatrixRow) -> float:
    """The capture rate the recording carries in its own record."""
    return float((_video_record(Path(row.source_path).resolve()) or {}).get("acquisition_fps") or 0.0)


def _recorded_scale(row: MatrixRow) -> float:
    """The µm per pixel the recording carries, measured on control's preview; 0 when it has none."""
    optics = (_video_record(Path(row.source_path).resolve()) or {}).get("optics") or {}
    try:
        return float(optics.get("microns_per_pixel") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _video_record(source: Path) -> dict[str, Any] | None:
    """The record a video carries about itself, when it was made by control; read once per file version."""
    try:
        stat = Path(source).stat()
    except OSError:
        return None
    return _video_record_of(str(source), stat.st_mtime, stat.st_size)


@lru_cache(maxsize=256)
def _video_record_of(path: str, _mtime: float, _size: int) -> dict[str, Any] | None:
    return _read_video_record(Path(path))


def _read_video_record(source: Path) -> dict[str, Any] | None:
    from admet.engines.acquisition.camera.tags import read_recording_tag

    try:
        return read_recording_tag(source)
    except Exception:
        return None


# -- the stored analysis drawn over a frame ---------------------------------------------

@lru_cache(maxsize=4)
def _overlay_index(raw_path: str, _mtime: float, file_id: str) -> dict[str, Any]:
    """One file's droplets by frame and each tracked droplet's path, read once per version of the raw file."""
    frames: dict[int, list[dict[str, Any]]] = {}
    paths: dict[int, list[tuple[int, float, float]]] = {}
    marker = f'"file_id": "{file_id}"'
    with open(raw_path, encoding="utf-8") as handle:
        for line in handle:
            if marker not in line or ('"detection"' not in line and '"droplet"' not in line):
                continue
            row = json.loads(line)
            if row.get("kind") not in {"detection", "droplet"}:
                continue
            values = row.get("values") or {}
            frame = int(values.get("frame", -1))
            frames.setdefault(frame, []).append(values)
            if row.get("kind") == "detection" and values.get("droplet_id") is not None:
                paths.setdefault(int(values["droplet_id"]), []).append(
                    (frame, float(values.get("centroid_x", 0)), float(values.get("centroid_y", 0))))
    for path in paths.values():
        path.sort()
    return {"frames": frames, "paths": paths}


@lru_cache(maxsize=4)
def _contour_points(path: str) -> Any:
    import numpy as np

    with np.load(path) as data:
        return data["points"]


def _detection_outline(detection: dict[str, Any]) -> list[tuple[float, float]]:
    path, offset, length = (detection.get(key) for key in ("contour_path", "contour_offset", "contour_length"))
    if path and offset is not None and length:
        try:
            points = _contour_points(str(path))[int(offset):int(offset) + int(length)].reshape(-1, 2)
            return [(float(x), float(y)) for x, y in points]
        except Exception:
            pass
    x, y = float(detection.get("bbox_x", 0)), float(detection.get("bbox_y", 0))
    w, h = float(detection.get("bbox_width", 0)), float(detection.get("bbox_height", 0))
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


# -- row settings ------------------------------------------------------------------------

def _roi(row: MatrixRow, width: int, height: int) -> tuple[int, int, int, int]:
    """The crop in frame pixels; the whole frame when none is set."""
    x, y = _row_int(row, "roi_x", 0), _row_int(row, "roi_y", 0)
    w, h = _row_int(row, "roi_width", 0), _row_int(row, "roi_height", 0)
    if not w and not h:
        return 0, 0, width, height
    return x, y, (w or width - x), (h or height - y)


def _roi_from_drag(x0: int, y0: int, x1: int, y1: int, width: int, height: int) -> tuple[int, int, int, int] | None:
    """The rectangle between two drag points, clamped to the frame; None for a mere click."""
    left, right = sorted((max(0, min(width, x0)), max(0, min(width, x1))))
    top, bottom = sorted((max(0, min(height, y0)), max(0, min(height, y1))))
    if right - left < 4 or bottom - top < 4:
        return None
    return left, top, right - left, bottom - top


def _crop_text(row: MatrixRow, size: tuple[int, int] | None = None) -> str:
    """The crop in words; a crop the size of the frame is no crop."""
    x, y = _row_int(row, "roi_x", 0), _row_int(row, "roi_y", 0)
    w, h = _row_int(row, "roi_width", 0), _row_int(row, "roi_height", 0)
    if (not w and not h) or (size is not None and (x, y, w, h) == (0, 0, size[0], size[1])):
        return "full frame"
    return f"crop {x},{y} {w}×{h} px"


def _preview_frame_index(row: MatrixRow) -> int:
    """The frame shown: an absolute frame number kept between Start and End."""
    start = _row_int(row, "start_frame", 0)
    frame = max(_row_int(row, "preview_frame", start), start)
    end = _row_int(row, "end_frame", 0)
    return min(frame, end - 1) if end > start else frame


def _video_setting_warnings(row: MatrixRow, frames: int, width: int, height: int) -> list[str]:
    """What is odd about a file's frame and crop values, said plainly; nothing is changed."""
    warnings = []
    start = _row_int(row, "start_frame", 0)
    end = _row_int(row, "end_frame", 0) or frames
    if start >= frames:
        warnings.append(f"Start frame {start} is past the video's last frame ({frames - 1}).")
    if end <= start:
        warnings.append(f"End frame {end} is not after start frame {start}: no frames would be analysed.")
    elif end > frames:
        warnings.append(f"End frame {end} is past the video's {frames} frames; analysis stops at {frames}.")
    x, y = _row_int(row, "roi_x", 0), _row_int(row, "roi_y", 0)
    w, h = _row_int(row, "roi_width", 0), _row_int(row, "roi_height", 0)
    if x + w > width or y + h > height:
        warnings.append(f"The crop {x},{y} {w}×{h} runs past the {width}×{height} frame.")
    return warnings


def _row_setting(row: MatrixRow, key: str, default: Any) -> Any:
    value = row.settings.get(key, default)
    return default if value in {None, ""} else value


def _row_int(row: MatrixRow, key: str, default: Any) -> int:
    return int(_numeric(_row_setting(row, key, default)) or 0)


def _row_float(row: MatrixRow, key: str, default: Any) -> float:
    return float(_numeric(_row_setting(row, key, default)) or 0.0)


def _row_bool(row: MatrixRow, key: str, default: Any) -> bool:
    value = _row_setting(row, key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -- small helpers -----------------------------------------------------------------------

def _compact_path(value: str, *, max_parts: int = 4) -> str:
    parts = Path(value).parts
    if len(parts) <= max_parts:
        return value
    return str(Path("...").joinpath(*parts[-max_parts:]))


def _path_label(path: str):
    """The short path, with the full one on hover.

    The title is set, not passed in a props string: NiceGUI parses that as Python, and
    a Windows path's "\\U" in C:\\Users reads there as a broken unicode escape.
    """
    from nicegui import ui

    label = ui.label(_compact_path(path)).classes("muted text-xs path-label")
    label.props["title"] = path
    return label


def _friendly_video_error(message: str) -> str:
    if "cannot open video" in message.lower():
        return "OpenCV cannot open this video. Check the file or its codec."
    if "not readable" in message.lower():
        return "The video is missing or not readable. Check the file."
    return message


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _event_row(event: Any) -> dict[str, Any]:
    args = getattr(event, "args", {})
    if isinstance(args, list) and len(args) >= 2 and isinstance(args[1], dict):
        return args[1]
    if isinstance(args, dict):
        return args
    return {}


def _existing_dir(path: Path) -> Path:
    try:
        candidate = path.expanduser().resolve()
    except (OSError, RuntimeError):
        candidate = Path.cwd()
    if candidate.is_file():
        candidate = candidate.parent
    while not candidate.is_dir() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate if candidate.is_dir() else Path.cwd()


def _holds_images(path: Path) -> bool:
    try:
        return any(item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES for item in path.iterdir())
    except OSError:
        return False


def _addable(entry: Path) -> bool:
    """What Add files can take: a video, an image, or a folder with images in it."""
    if entry.is_dir():
        return _holds_images(entry)
    return entry.suffix.lower() in VIDEO_SUFFIXES | IMAGE_SUFFIXES


def _browser_entries(path: Path, mode: str) -> list[Path]:
    try:
        entries = [entry for entry in path.iterdir() if not entry.name.startswith(".")]
    except OSError:
        return []
    dirs = [entry for entry in entries if entry.is_dir()]
    files = [] if mode == "folder" else [entry for entry in entries if entry.is_file() and _addable(entry)]
    return sorted(dirs, key=lambda item: item.name.lower()) + sorted(files, key=lambda item: item.name.lower())


def _dot_class(status: StageStatus, selected: bool) -> str:
    if selected:
        return "toc-dot toc-dot-active"
    if status is StageStatus.COMPLETE:
        return "toc-dot toc-dot-done"
    return "toc-dot"


_uid_counter = itertools.count(1)


def _uid() -> str:
    # A process-unique, monotonic id. Rows are keyed by uid in the tables and looked up
    # by uid on every change, so two rows must never share one.
    return f"target_{next(_uid_counter)}"


def _style() -> str:
    return "<style>:root{" + theme.css_variables() + "}</style>" + """
    <style>
    body { background: var(--bg-app); }
    .shell { min-height: 100vh; color: var(--text); font-size: 13px; padding: 18px 20px 20px; gap: 8px; }
    .left-rail { width: 246px; align-self: flex-start; gap: 8px; }
    .topbar, .workflow-toc { background: var(--bg-control); border: 1px solid var(--border); border-radius: 8px; }
    .topbar { min-height: 58px; }
    .admet-panel { background: transparent; border: 0; border-radius: 0; gap: 4px; }
    .admet-box-title { font-size: 16px; line-height: 22px; font-weight: 650; color: var(--text); padding: 0; }
    .admet-panel-body { padding: 0; }
    .workflow-toc { padding: 10px 12px; gap: 3px; }
    .notification-card {
      background: var(--bg-control); border: 1px solid var(--accent); border-left: 5px solid var(--accent);
      border-radius: 2px; padding: 12px 14px; gap: 4px;
    }
    .notification-card-warning { border-color: var(--warning); border-left-color: var(--warning); }
    .notification-card-success { border-color: var(--success-hover); border-left-color: var(--success-hover); }
    .notification-card-danger { border-color: var(--danger-hover); border-left-color: var(--danger-hover); }
    .notification-title { color: var(--text-muted); font-size: 11px; font-weight: 650; text-transform: uppercase; }
    .notification-text { color: var(--text); font-size: 13px; font-weight: 600; line-height: 1.35; white-space: normal; overflow-wrap: anywhere; }
    .admet-process { background: var(--bg-raised); border: 1px solid var(--border); border-radius: 8px 8px 0 0; min-height: 24px; }
    .admet-process .q-linear-progress { height: 10px; border-radius: 8px 8px 0 0; overflow: hidden; }
    .admet-progress-text { font-size: 12px; color: var(--text-muted); padding: 2px 8px; }
    .admet-transport { background: transparent; border: 0; border-radius: 0; }
    .admet-transport-btn {
      background: var(--bg-control) !important; color: var(--text) !important; border: 0 !important;
      border-radius: 0 !important; min-height: 26px !important; height: 26px !important; padding: 0 !important; font-weight: 500;
    }
    .admet-transport-btn:hover { background: var(--bg-raised) !important; }
    .admet-transport-btn-active { color: var(--accent) !important; background: var(--bg-control-pressed) !important; font-weight: 650; }
    .admet-transport-btn-warning { color: var(--bg-control) !important; background: var(--warning) !important; font-weight: 700; }
    .admet-transport-separator { width: 1px; align-self: stretch; background: var(--border); }
    .panel { background: var(--bg-control); border: 1px solid var(--border); border-radius: 8px; }
    .toc-title { color: var(--text-muted); font-size: 11px; font-weight: 650; text-transform: uppercase; }
    .toc-row { min-height: 24px; border-radius: 6px; padding: 2px 4px; cursor: pointer; }
    .toc-row:hover { background: var(--bg-raised); }
    .toc-dot { width: 8px; height: 8px; border-radius: 999px; background: var(--text-disabled); border: 1px solid var(--border); }
    .toc-dot-active { background: var(--accent); border-color: var(--accent); }
    .toc-dot-done { background: var(--success-hover); border-color: var(--success-hover); }
    .root-hint { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .browser-card { width: min(760px, 92vw); max-height: 82vh; border-radius: 8px; box-shadow: none; border: 1px solid var(--border); gap: 8px; }
    .browser-path { color: var(--text-muted); font-family: Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
    .browser-list { max-height: 56vh; overflow-y: auto; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 6px 0; }
    .browser-row { justify-content: flex-start !important; width: 100%; border-radius: 4px !important; color: var(--text) !important; font-weight: 500 !important; }
    .browser-select { min-width: 34px !important; width: 34px; flex: 0 0 auto; padding: 0 !important; display: inline-block; }
    .browser-name { justify-content: flex-start !important; text-align: left; overflow-wrap: anywhere; }
    .facts-grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(220px, 2fr); gap: 12px; align-items: start; }
    .facts-image, .admet-still { display: block; width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--border); background: var(--text); }
    .facts-row { align-items: baseline; }
    .facts-key { width: 96px; flex: 0 0 auto; }
    .editor-grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(260px, 2fr); gap: 12px 18px; align-items: start; }
    .editor-settings { padding-top: 2px; }
    @media (max-width: 980px) { .editor-grid, .facts-grid { grid-template-columns: 1fr; } }
    .admet-frame { display: block; width: 100%; border-radius: 8px; border: 1px solid var(--border); background: var(--text); overflow: hidden; user-select: none; cursor: crosshair; }
    .admet-frame svg, .admet-frame img { display: block; width: 100%; height: auto; }
    .admet-overlay .ov-dim { fill: rgba(0, 0, 0, 0.38); }
    .admet-overlay .ov-roi { fill: none; stroke: var(--roi); stroke-width: 2; vector-effect: non-scaling-stroke; }
    .admet-overlay .ov-window { stroke: #4cc9f0; stroke-width: 1; stroke-dasharray: 5 4; vector-effect: non-scaling-stroke; }
    .admet-overlay .ov-gate { stroke: #ffd166; stroke-width: 2; vector-effect: non-scaling-stroke; }
    .admet-overlay .ov-droplet { fill: none; stroke: #39d353; stroke-width: 1; vector-effect: non-scaling-stroke; }
    .admet-overlay .ov-path { fill: none; stroke: #ffd166; stroke-width: 1.5; vector-effect: non-scaling-stroke; }
    .admet-overlay .ov-label { fill: #ffd166; font: 9px Menlo, Consolas, monospace; }
    .admet-overlay .ov-off { fill: rgba(255, 90, 95, 0.18); stroke: #ff5a5f; stroke-width: 2; stroke-dasharray: 6 4; vector-effect: non-scaling-stroke; }
    .admet-overlay .ov-count { fill: #ffd166; font: 600 22px Menlo, Consolas, monospace; text-anchor: middle; dominant-baseline: middle; paint-order: stroke; stroke: rgba(0,0,0,0.55); stroke-width: 3px; }
    .admet-overlay .ov-count-fixed { fill: #4cc9f0; }
    .admet-frame-still { cursor: pointer; }
    .stage-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 4px; }
    .stage-tile { margin: 0; min-width: 0; }
    .stage-tile img { display: block; width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--border); background: var(--text); image-rendering: pixelated; }
    .stage-tile figcaption { margin-top: 4px; font-size: 12px; color: var(--text-muted); overflow-wrap: anywhere; }
    @media (max-width: 900px) { .stage-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    .editor-note { border: 1px solid var(--border); border-left: 4px solid var(--accent); border-radius: 4px; padding: 8px 10px; color: var(--text-muted); background: var(--bg-app); font-size: 12px; font-weight: 600; }
    .editor-warning { border-left-color: var(--warning); color: var(--text); }
    .timeline { margin-top: 6px; }
    .slider-field { width: 100%; }
    .slider-field .q-slider, .timeline .q-slider { min-height: 24px; width: 100%; padding: 0 6px; }
    .slider-label-row { align-items: center; justify-content: space-between; gap: 8px; }
    .slider-number { width: 84px; font-family: Menlo, Consolas, monospace; font-size: 12px; }
    .shell .q-slider__pin { display: none !important; }
    .shell .q-field__bottom { display: none !important; }
    .shell .q-field__control { background: var(--bg-control) !important; box-shadow: none !important; }
    .shell .q-field__control:after, .shell .q-field__control:before { display: none !important; }
    .path-label { max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.3; }
    .log-text { background: var(--bg-raised); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; color: var(--text-muted); font-family: Menlo, Consolas, monospace; font-size: 12px; }
    .slim-table .q-table th, .slim-table .q-table td { height: 30px; padding: 2px 6px; font-size: 12px; white-space: nowrap; }
    .slim-table .q-table td:nth-child(2) { white-space: normal; overflow-wrap: anywhere; }       /* the sample */
    .matrix-table .q-table td:nth-child(6) { white-space: normal; overflow-wrap: anywhere; }     /* the file name */
    .matrix-table .q-table td { height: 38px; }
    .matrix-table .q-field__control { min-height: 28px; }
    .matrix-table .q-field { max-width: 104px; }
    .matrix-table .q-field.matrix-text { max-width: none; min-width: 260px; width: 100%; }   /* a sample name is read whole */
    .matrix-table .q-table td:nth-child(2) { min-width: 272px; }
    .matrix-missing .q-field__control { outline: 2px solid var(--danger-hover); outline-offset: -2px; border-radius: 8px; }
    .matrix-table th .col-head { display: flex; flex-direction: column; gap: 2px; padding: 2px 0; }
    .matrix-table th .col-label { font-weight: 600; }
    .matrix-table th .q-field { max-width: 104px; }
    .matrix-table th .q-field__control { min-height: 26px; outline: 1px solid var(--border); outline-offset: -1px;
      border-radius: 6px; }
    .matrix-table th .col-actions { display: flex; gap: 2px; }
    .columns-select { max-width: 760px; }
    .sets-select { min-width: 220px; max-width: 360px; }
    .matrix-num { text-align: right; }
    .results-wrap { overflow-x: auto; }
    table.results { border-collapse: collapse; width: 100%; font-size: 12px; }
    table.results th { color: var(--text-muted); font-weight: 600; text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
    table.results td { padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; white-space: nowrap; }
    table.results .n { font-family: Menlo, Consolas, monospace; margin-right: 6px; }
    table.results .sub { color: var(--text-muted); font-size: 11px; white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
    table.results .spark { fill: var(--accent); fill-opacity: 0.8; vertical-align: middle; }
    table.results .swatch { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 6px; }
    .results-table .q-table td { height: 44px; white-space: nowrap; }
    .results-table .q-table td:nth-child(2), .results-table .q-table td:nth-child(3) { white-space: nowrap; }
    .results-cell { line-height: 1.25; }
    .results-cell .n { font-family: Menlo, Consolas, monospace; margin-right: 6px; }
    .results-cell .sub { color: var(--text-muted); font-size: 11px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
    .results-cell .spark { fill: var(--accent); fill-opacity: 0.8; vertical-align: middle; }
    .results-cell .swatch { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 6px; }
    .results-off { opacity: 0.45; }
    .qc-state { font-weight: 700; }
    .results-grid, .results-grid-2 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .results-grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .results-grid-2 .plot-card { aspect-ratio: 16 / 9; min-height: 0; }
    .results-grid .plot-card { aspect-ratio: 4 / 3; min-height: 0; }
    @media (max-width: 1100px) { .results-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { .results-grid, .results-grid-2 { grid-template-columns: 1fr; } }
    .plot-card { border: 1px solid var(--border); border-radius: 8px; background: var(--bg-control); padding: 4px; }
    .muted { color: var(--text-muted); }
    .section-title { color: var(--text-muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .q-field__control { min-height: 34px; border-radius: 8px; }
    .q-btn { min-height: 30px; border-radius: 8px; text-transform: none; }
    .q-table__card { box-shadow: none; border: 1px solid var(--border); }
    .q-table th, .q-table td { padding: 4px 8px; }
    </style>
    """
