"""The file tables of the analyze stages: their columns, rows and editable cells."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import ParamKind

# The settings a file can carry, in the order their columns go: the field, its label, its kind, the
# engines it applies to. Each is a column of the Analyse table when chosen, with a value in its header
# that lands on every ticked file.
SETTING_FIELDS: tuple[tuple[str, str, ParamKind, tuple[str, ...]], ...] = (
    ("microns_per_pixel", "µm/px", ParamKind.FLOAT, ("opencv", "cellpose")),
    ("fps", "FPS", ParamKind.FLOAT, ("opencv",)),
    ("start_frame", "First frame", ParamKind.INTEGER, ("opencv",)),
    ("end_frame", "Last frame", ParamKind.INTEGER, ("opencv",)),
    ("k_sigma", "Threshold, × noise", ParamKind.FLOAT, ("opencv",)),
    ("satellite_px", "Satellite size, px", ParamKind.INTEGER, ("opencv",)),
    ("min_area", "Min droplet area, px²", ParamKind.FLOAT, ("opencv",)),
    ("skip_left_pct", "Skip left, %", ParamKind.FLOAT, ("opencv",)),
    ("skip_right_pct", "Skip right, %", ParamKind.FLOAT, ("opencv",)),
    ("gate_pct", "Gate, %", ParamKind.FLOAT, ("opencv",)),
    ("frame_limit", "Image limit", ParamKind.INTEGER, ("cellpose",)),
    ("detect_inclusions", "Detect inclusions", ParamKind.BOOLEAN, ("cellpose",)),
    ("slide_height_um", "Slide height, µm", ParamKind.FLOAT, ("cellpose",)),
)
MASS_EDIT_FIELDS = SETTING_FIELDS
# The columns the Analyse table can show, in order: every setting, and two read-only summaries.
ANALYSE_COLUMN_LABELS: dict[str, str] = {**{field: label for field, label, _kind, _applies in SETTING_FIELDS},
                                         "range": "Frames", "crop": "Crop"}
DEFAULT_ANALYSE_FIELDS = ("microns_per_pixel", "fps", "range", "crop")
# What a row's table cells edit, and as which kind.
FIELD_KINDS = {field: kind for field, _label, kind, _applies in SETTING_FIELDS}
FIELD_KINDS["px_to_um"] = ParamKind.FLOAT


def files_columns() -> list[dict[str, Any]]:
    """The Files list: one row per sample under its condition, a file shown as a chip in its column."""
    return [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "sample", "label": "Sample", "field": "rep", "align": "left"},
        {"name": "video", "label": "Video", "field": "video", "align": "left"},
        {"name": "images", "label": "Images", "field": "images", "align": "left"},
        {"name": "live", "label": "Live", "field": "live", "align": "left"},
        {"name": "note", "label": "Note", "field": "note", "align": "left"},
    ]


def condition_row(key: str, alias: str, auto: str, count: str, *, active: bool, mixed: bool,
                  fixed: bool = False) -> dict[str, Any]:
    """A condition's line in the Files list: its alias to type over, the derived condition when the alias
    differs from it, how many samples it holds. FIXED is the Unassigned group, which has no tick and no alias."""
    return {"uid": f"condition:{key}", "kind": "condition", "alias": alias, "auto": auto, "count": count,
            "active": active, "mixed": mixed, "fixed": fixed}


def sample_row(name: str, rep: str, *, active: bool, mixed: bool, video: str, images: str, live: str,
               note: str, file_uids: list[str]) -> dict[str, Any]:
    """A sample's line: its files as chips, the live QC that came with its video, its note."""
    return {"uid": f"sample:{name}", "kind": "sample", "rep": rep, "active": active, "mixed": mixed,
            "video": video, "images": images, "live": live, "note": note, "file_uids": list(file_uids)}


def unassigned_row(uid: str, folder: str, options: list[dict[str, str]]) -> dict[str, Any]:
    """An image folder with no sample yet, and the samples it can join."""
    return {"uid": uid, "kind": "unassigned", "images": folder, "options": options}


FILES_BODY = """
<q-tr :props="props" :class="'kind-' + props.row.kind" @click="$parent.$emit('pick', {uid: props.row.uid})">
  <template v-if="props.row.kind === 'condition'">
    <q-td class="text-center">
      <q-checkbox v-if="!props.row.fixed" dense :model-value="props.row.mixed ? null : props.row.active"
        @click.stop @mousedown.stop
        @update:model-value="(val, evt) => $parent.$emit('tick', {uid: props.row.uid, value: !!val, shift: !!(evt && evt.shiftKey)})" />
    </q-td>
    <q-td colspan="5">
      <div class="cond-line">
        <q-input v-if="!props.row.fixed" dense borderless class="alias-input" v-model="props.row.alias"
          @click.stop @mousedown.stop
          @blur="$parent.$emit('alias', {uid: props.row.uid, value: props.row.alias})"
          @keyup.enter="$event.target.blur()" />
        <span v-else class="cond-alias">{{ props.row.alias }}</span>
        <span v-if="props.row.auto" class="cond-auto">{{ props.row.auto }}</span>
        <span class="cond-auto">{{ props.row.count }}</span>
      </div>
    </q-td>
  </template>
  <template v-else-if="props.row.kind === 'sample'">
    <q-td class="text-center">
      <q-checkbox dense :model-value="props.row.mixed ? null : props.row.active"
        @click.stop @mousedown.stop
        @update:model-value="(val, evt) => $parent.$emit('tick', {uid: props.row.uid, value: !!val, shift: !!(evt && evt.shiftKey)})" />
    </q-td>
    <q-td class="sample-cell">{{ props.row.rep }}</q-td>
    <q-td><span v-if="props.row.video" class="chip">&#9654; {{ props.row.video }}</span><span v-else class="chip off">no video</span></q-td>
    <q-td><span v-if="props.row.images" class="chip">&#9635; {{ props.row.images }}</span><span v-else class="chip off">no images</span></q-td>
    <q-td><span v-if="props.row.live" class="chip">&#9679; {{ props.row.live }}</span><span v-else class="muted">—</span></q-td>
    <q-td>
      <q-input dense borderless class="note-input" v-model="props.row.note" placeholder="—"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('note', {uid: props.row.uid, value: props.row.note})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
  </template>
  <template v-else>
    <q-td></q-td>
    <q-td class="muted">—</q-td>
    <q-td class="muted">no video</q-td>
    <q-td><span class="chip">&#9635; {{ props.row.images }}</span></q-td>
    <q-td class="muted">—</q-td>
    <q-td>
      <q-select dense outlined options-dense emit-value map-options class="sample-picker"
        :options="props.row.options" :model-value="null" label="pick a sample…"
        @click.stop @mousedown.stop
        @update:model-value="val => $parent.$emit('assign', {uid: props.row.uid, sample: val})" />
    </q-td>
  </template>
</q-tr>
"""


def wire_files_list(table: Any, *, on_tick: Any, on_alias: Any, on_note: Any, on_assign: Any, on_pick: Any) -> None:
    """The Files list draws its own rows: conditions, samples and unassigned folders look different."""
    table.add_slot("body", FILES_BODY)
    table.on("tick", on_tick)
    table.on("alias", on_alias)
    table.on("note", on_note)
    table.on("assign", on_assign)
    table.on("pick", on_pick)


def analyse_fields(chosen) -> list[str]:
    """The chosen columns in their canonical order, unknown names dropped."""
    wanted = set(chosen or ())
    return [field for field in ANALYSE_COLUMN_LABELS if field in wanted]


def analyse_columns(fields) -> list[dict[str, Any]]:
    """The Analyse table's columns: Use, the sample, the file, the chosen setting columns, then its status.
    Where the file came from is on 1. Files, under Details, not here."""
    columns = [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "sample_id", "label": "Sample", "field": "sample_id", "align": "left"},
        {"name": "source", "label": "File", "field": "source", "align": "left"},
    ]
    columns += [{"name": field, "label": ANALYSE_COLUMN_LABELS[field], "field": field, "align": "left"}
                for field in analyse_fields(fields)]
    columns.append({"name": "status", "label": "Status", "field": "status", "align": "left"})
    return columns


def results_columns(fields: tuple[str, ...], labels: dict[str, str]) -> list[dict[str, Any]]:
    columns = [{"name": "active", "label": "Use", "field": "active", "align": "center"}]
    columns += [{"name": field, "label": labels[field], "field": field, "align": "left"} for field in fields]
    return columns


def wire_results_table(table: Any, on_change: Any, html_fields: tuple[str, ...]) -> None:
    """Use toggles like the other tables; every other cell is HTML made server-side."""
    table.add_slot("body-cell-active", _bool_cell_slot("active"))
    for field in html_fields:
        table.add_slot(f"body-cell-{field}", f"""
        <q-td :props="props"><div v-html="props.row.{field}"></div></q-td>
        """)
    table.on("matrix-change", on_change)


STATUS_TITLES = {"new": "not run", "running": "running", "done": "analysed", "stale": "settings changed since"}


def status_html(status: str, when: str = "") -> str:
    """A dot for a file's state on 2. Analyse: grey not run, blue running, green done, amber stale."""
    title = STATUS_TITLES.get(status, status) + (f", {when}" if when else "")
    return f'<span class="dot dot-{status}" title="{title}"></span>'


def analyse_condition_row(key: str, alias: str, auto: str) -> dict[str, Any]:
    """A condition's line in the Analyse table: its name across the row, no cell to edit."""
    cells: dict[str, Any] = {"uid": f"condition:{key}", "kind": "condition", "active": None, "sample_id": alias,
                             "auto": auto, "engine": "", "range": "", "crop": "", "status": ""}
    for field, _label, _kind, _applies in SETTING_FIELDS:
        cells[field] = None
        cells[f"{field}_applies"] = False
    return cells


def analyse_row(row: Any, *, range_text: str, crop_text: str, status: str, analyzed: str = "") -> dict[str, Any]:
    """A file's row: every setting it can carry (None and "<field>_applies" False where its engine has none)."""
    cells: dict[str, Any] = {
        "uid": row.uid,
        "kind": "file",
        "active": row.to_run,                 # the tick of 2. Analyse: run it
        "sample_id": row.rep,                 # "rep 2" under its condition's line; the full name is on 1. Files
        "auto": "",
        "condition": row.condition,
        "protocol": row.protocol,
        "run_time": row.run_time,
        "replicate": f"rep {row.replicate}" if row.replicate else "",
        "source": Path(row.source_path).name or row.source_path,
        "engine": row.engine,
        "range": range_text,
        "crop": crop_text,
        "status": status_html(status, analyzed),
    }
    for field, _label, kind, applies in SETTING_FIELDS:
        takes = row.engine in applies
        value = row.settings.get(setting_key(field, row.engine)) if takes else None
        cells[field] = (bool(value) if kind is ParamKind.BOOLEAN else _number(value)) if takes else None
        cells[f"{field}_applies"] = takes
    return cells


def wire_analyse_table(table: Any, on_change: Any, fields) -> None:
    """Each setting column edits per row; a cell is dimmed when the row's engine has no such setting.
    The headers' controls are NiceGUI elements, added by the view into the header-cell slots."""
    table.add_slot("body-cell-active", _bool_cell_slot("active", applies="props.row.kind !== 'condition'"))
    table.add_slot("body-cell-sample_id", """
    <q-td :props="props" :class="props.row.kind === 'condition' ? 'cond-cell' : 'sample-cell'">
      <span :class="props.row.kind === 'condition' ? 'cond-alias' : ''">{{ props.row.sample_id }}</span>
      <span v-if="props.row.auto" class="cond-auto">{{ props.row.auto }}</span>
    </q-td>
    """)
    table.add_slot("body-cell-source", """
    <q-td :props="props"><span v-if="props.row.kind !== 'condition'" class="chip"
      :title="props.row.source">{{ props.row.engine === 'cellpose' ? '\u25A3' : '\u25B6' }} {{ props.row.source }}</span></q-td>
    """)
    table.add_slot("body-cell-status", """
    <q-td :props="props"><div v-html="props.row.status"></div></q-td>
    """)
    kinds = dict(FIELD_KINDS)
    for field in analyse_fields(fields):
        if field not in kinds:
            continue                                         # a read-only summary column
        if kinds[field] is ParamKind.BOOLEAN:
            table.add_slot(f"body-cell-{field}", _bool_cell_slot(field, applies=f"props.row.{field}_applies"))
        else:
            required = field in ("microns_per_pixel", "fps")
            table.add_slot(f"body-cell-{field}", _required_number_slot(field, applies=f"props.row.{field}_applies",
                                                                        required=required))
    table.on("matrix-change", on_change)


def setting_key(field: str, engine: str) -> str:
    """The settings key a table field means for an engine: the imaging engine names the scale px_to_um."""
    return "px_to_um" if field == "microns_per_pixel" and engine == "cellpose" else field


def apply_setting(rows: list[Any], field: str, value: Any) -> list[Any]:
    """Set one field on every ticked row whose engine takes it; the rows that changed."""
    spec = next((item for item in MASS_EDIT_FIELDS if item[0] == field), None)
    if spec is None:
        return []
    _field, _label, kind, applies = spec
    changed = []
    for row in rows:
        if not (row.active and row.to_run) or row.engine not in applies:
            continue
        key = setting_key(field, row.engine)
        new = cast_matrix_value(kind, value)
        if row.settings.get(key) != new:
            row.settings[key] = new
            changed.append(row)
    return changed


def cast_matrix_value(kind: ParamKind, value: Any) -> Any:
    if kind is ParamKind.BOOLEAN:
        return bool(value)
    if kind is ParamKind.INTEGER:
        return int(_number(value) or 0)
    if kind is ParamKind.FLOAT:
        return float(_number(value) or 0.0)
    return str(value or "")


def matrix_field_kind(field: str) -> ParamKind | None:
    return FIELD_KINDS.get(field)


def _required_number_slot(field: str, *, applies: str = "true", required: bool = True) -> str:
    # For a required field, empty or zero is "missing": the cell is outlined red until a value is in.
    missing = f"props.row.{field} > 0 ? '' : 'matrix-missing'" if required else "''"
    return f"""
    <q-td :props="props">
      <q-input v-if="{applies}" dense outlined type="number" step="any" input-class="matrix-num"
        :class="{missing}"
        v-model.number="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
      <span v-else-if="props.row.kind !== 'condition'" class="muted">—</span>
    </q-td>
    """


def _text_cell_slot(field: str, *, placeholder: str = "") -> str:
    """A text field per row; PLACEHOLDER names the row field shown greyed when nothing is written."""
    hint = f' :placeholder="props.row.{placeholder}"' if placeholder else ""
    return f"""
    <q-td :props="props">
      <q-input dense outlined class="matrix-text" v-model="props.row.{field}"{hint}
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
    """


def _bool_cell_slot(field: str, *, applies: str = "true") -> str:
    return f"""
    <q-td :props="props">
      <q-checkbox v-if="{applies}" dense v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @update:model-value="(val, evt) => $parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: val,
                                                                           shift: !!(evt && evt.shiftKey)}})" />
      <span v-else-if="props.row.kind !== 'condition'" class="muted">—</span>
    </q-td>
    """


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
