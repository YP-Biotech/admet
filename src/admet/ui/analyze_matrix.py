"""The file tables of the analyze stages: their columns, rows and editable cells."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import ParamKind

# What a row's table cells edit, and as which kind.
FIELD_KINDS = {
    "microns_per_pixel": ParamKind.FLOAT,
    "fps": ParamKind.FLOAT,
    "px_to_um": ParamKind.FLOAT,
}


def files_columns() -> list[dict[str, Any]]:
    return [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "sample_id", "label": "Sample", "field": "sample_id", "align": "left"},
        {"name": "source", "label": "File", "field": "source", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "count", "label": "Frames / images", "field": "count", "align": "right"},
        {"name": "analyzed", "label": "Analysed", "field": "analyzed", "align": "left"},
        {"name": "remove", "label": "", "field": "remove", "align": "center"},
    ]


def analyse_columns() -> list[dict[str, Any]]:
    return [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "sample_id", "label": "Sample", "field": "sample_id", "align": "left"},
        {"name": "source", "label": "File", "field": "source", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "microns_per_pixel", "label": "µm/px", "field": "microns_per_pixel", "align": "left"},
        {"name": "fps", "label": "FPS", "field": "fps", "align": "left"},
        {"name": "range", "label": "Frames", "field": "range", "align": "left"},
        {"name": "crop", "label": "Crop", "field": "crop", "align": "left"},
        {"name": "analyzed", "label": "Analysed", "field": "analyzed", "align": "left"},
    ]


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


def files_row(row: Any, *, count: str, analyzed: str, removable: bool) -> dict[str, Any]:
    return {
        "uid": row.uid,
        "active": row.active,
        "sample_id": row.sample_id,
        "source": Path(row.source_path).name or row.source_path,
        "engine": row.engine,
        "count": count,
        "analyzed": analyzed or "—",
        "remove": removable,
    }


def analyse_row(row: Any, *, range_text: str, crop_text: str, analyzed: str) -> dict[str, Any]:
    if row.engine == "cellpose":
        scale, fps = _number(row.settings.get("px_to_um")), None
    else:
        scale, fps = _number(row.settings.get("microns_per_pixel")), _number(row.settings.get("fps"))
    return {
        "uid": row.uid,
        "active": row.active,
        "sample_id": row.sample_id,
        "source": Path(row.source_path).name or row.source_path,
        "engine": row.engine,
        "microns_per_pixel": scale,
        "fps": fps,
        "fps_applies": row.engine != "cellpose",
        "range": range_text,
        "crop": crop_text,
        "analyzed": analyzed or "—",
    }


def wire_files_table(table: Any, on_change: Any, on_remove: Any) -> None:
    table.add_slot("body-cell-active", _bool_cell_slot("active"))
    table.add_slot("body-cell-sample_id", _text_cell_slot("sample_id"))
    table.add_slot("body-cell-remove", """
    <q-td :props="props">
      <q-btn v-if="props.row.remove" flat dense round size="sm" icon="close" title="Remove from the list"
        @click.stop="$parent.$emit('matrix-remove', {uid: props.row.uid})" />
    </q-td>
    """)
    table.on("matrix-change", on_change)
    table.on("matrix-remove", on_remove)


def wire_analyse_table(table: Any, on_change: Any) -> None:
    table.add_slot("body-cell-active", _bool_cell_slot("active"))
    table.add_slot("body-cell-microns_per_pixel", _required_number_slot("microns_per_pixel"))
    table.add_slot("body-cell-fps", _required_number_slot("fps", applies="props.row.fps_applies"))
    table.on("matrix-change", on_change)


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


def _required_number_slot(field: str, *, applies: str = "true") -> str:
    # Empty or zero is "missing": the cell is outlined red until a value is in.
    return f"""
    <q-td :props="props">
      <q-input v-if="{applies}" dense outlined type="number" step="any" input-class="matrix-num"
        :class="props.row.{field} > 0 ? '' : 'matrix-missing'"
        v-model.number="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
      <span v-else class="muted">—</span>
    </q-td>
    """


def _text_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-input dense outlined v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
    """


def _bool_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-checkbox dense v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @update:model-value="val => $parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: val}})" />
    </q-td>
    """


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
