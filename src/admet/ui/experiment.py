"""The Experiment page: an experiment designed as a table of runs, each run with its id.

A row is a run: its id (K7Q-03), its protocol, the values of that protocol's parameters, its
condition, a note, and where it got to. A parameter left empty takes its protocol's default, shown
greyed. Rows that ran are read-only. The table comes whole from a JSON document and goes back out
as one; every change is kept in the data folder's control.db as it is made. Run next hands the next
planned row to the window, which runs it as any protocol is run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from admet.core.experiments import Experiment, Run, new_code
from admet.ui import theme as ui
from admet.ui.tables import GridTable, fit_table_height
from admet.ui.theme import Theme

FIXED = ("Id", "Protocol", "Condition")
TRAILING = ("Note", "State", "Run folder")
NEW = "New experiment…"


class ExperimentPanel(QWidget):
    run_requested = Signal(object, object)     # (experiment, run): the window runs it
    changed = Signal()

    def __init__(self, api, *, notify: Callable[[str, str], None] | None = None) -> None:
        super().__init__()
        self.api = api
        self.notify = notify or (lambda text, kind="primary": None)
        self.experiment: Experiment | None = None
        self._columns: list[str] = []
        self._filling = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(ui.spacing("default"))

        top = QHBoxLayout()
        top.setSpacing(ui.spacing("control"))
        top.addWidget(QLabel("Experiment"))
        self.chooser = QComboBox()
        self.chooser.activated.connect(self._chosen)
        top.addWidget(self.chooser, 1)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Its name, e.g. ratio sweep 26 Nov")
        self.name.editingFinished.connect(self._renamed)
        top.addWidget(self.name, 2)
        root.addLayout(top)

        buttons = QHBoxLayout()
        buttons.setSpacing(ui.spacing("control"))
        for label, slot in (("Add run", self.add_run), ("Duplicate", self.duplicate_run), ("Remove", self.remove_run),
                            ("Import JSON…", self._import_dialog), ("Export JSON…", self._export_dialog)):
            button = ui.button(label)
            button.clicked.connect(lambda _checked=False, slot=slot: slot())
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.run_next_button = ui.button("Run next", variant="primary")
        self.run_next_button.clicked.connect(lambda _checked=False: self.run_next())
        buttons.addWidget(self.run_next_button)
        root.addLayout(buttons)

        self.table = GridTable(0, 0)
        self.table.setObjectName("RawConfigTable")
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(GridTable.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(GridTable.SelectionMode.SingleSelection)
        self.table.itemChanged.connect(self._cell_edited)
        root.addWidget(self.table)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.refresh()

    # -- what there is ------------------------------------------------------------------
    @property
    def store(self):
        return self.api.service.experiments

    def protocol(self, name: str) -> dict[str, Any]:
        return self.api.service.protocol_document(name)

    def protocol_names(self) -> list[str]:
        from admet.workflows.json_protocol import template_documents

        saved = [entry["name"] for entry in self.api.service.protocol_store().list()["protocols"]
                 if not entry.get("error")]
        return sorted(set(template_documents()) | set(saved))

    def refresh(self) -> None:
        """The list of experiments and the table of the one shown."""
        entries = self.store.list()
        self.chooser.blockSignals(True)
        self.chooser.clear()
        for entry in entries:
            label = f"{entry['code']} · {entry['name'] or 'unnamed'} · {entry['done']}/{entry['runs']} done"
            self.chooser.addItem(label, entry["code"])
        self.chooser.addItem(NEW, "")
        if self.experiment is None and entries:
            self.experiment = self.store.load(entries[0]["code"], self.protocol)
        if self.experiment is not None:
            index = self.chooser.findData(self.experiment.code)
            self.chooser.setCurrentIndex(max(index, 0))
        self.chooser.blockSignals(False)
        self._fill()

    def _chosen(self, index: int) -> None:
        code = self.chooser.itemData(index)
        if not code:
            self.new_experiment()
            return
        self.experiment = self.store.load(code, self.protocol)
        self._fill()

    # -- the experiment -------------------------------------------------------------------
    def new_experiment(self, name: str = "") -> Experiment:
        self.experiment = Experiment(new_code(self.store.codes()), name)
        self._save()
        self.refresh()
        return self.experiment

    def _renamed(self) -> None:
        if self.experiment is not None and self.name.text().strip() != self.experiment.name:
            self.experiment.name = self.name.text().strip()
            self._save()
            self.refresh()

    def _save(self) -> None:
        if self.experiment is not None:
            self.store.save(self.experiment)
            self.changed.emit()

    # -- rows ----------------------------------------------------------------------------
    def add_run(self, protocol: str = "", parameters: dict[str, Any] | None = None) -> Run:
        if self.experiment is None:
            self.new_experiment()
        last = self.experiment.runs[-1] if self.experiment.runs else None
        protocol = protocol or (last.protocol if last else "dropseq_3n")
        run = self.experiment.add(protocol, dict(parameters if parameters is not None else
                                                 (last.parameters if last else {})))
        self._save()
        self._fill()
        return run

    def duplicate_run(self) -> Run | None:
        run = self._selected()
        if run is None:
            self.notify("Select the run to duplicate.", "warning")
            return None
        copy = self.experiment.add(run.protocol, dict(run.parameters), condition=run.condition, note=run.note)
        self._save()
        self._fill()
        return copy

    def remove_run(self) -> bool:
        run = self._selected()
        if run is None or run.state != "planned":
            self.notify("Only a planned run can be removed.", "warning")
            return False
        self.experiment.runs.remove(run)
        self._save()
        self._fill()
        return True

    def mark(self, run_id: str, state: str, run_folder: str = "") -> None:
        """Where a run got to, as the window saw it."""
        if self.experiment is None:
            return
        self.experiment.mark(run_id, state, run_folder)
        self._save()
        self.refresh()

    def run_next(self) -> Run | None:
        """The next planned run, handed to the window to run; None when every run has been run."""
        if self.experiment is None:
            return None
        run = self.experiment.next_planned()
        if run is None:
            self.notify(f"Every run of {self.experiment.code} has been run.", "success")
            return None
        try:
            self.experiment.document(run, self.protocol)
        except (LookupError, ValueError) as exc:
            self.notify(f"{run.run_id(self.experiment.code)}: {exc}", "danger")
            return None
        self.run_requested.emit(self.experiment, run)
        return run

    # -- JSON -------------------------------------------------------------------------------
    def import_json(self, path: str | Path) -> Experiment:
        """An experiment from a JSON document, kept as a new one: a code already used gets a fresh one."""
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        taken = self.store.codes()
        if isinstance(document, dict) and document.get("code") in taken:
            document = {**document, "code": new_code(taken), "runs": [
                {key: value for key, value in run.items() if key != "id"} for run in document.get("runs", [])
                if isinstance(run, dict)]}
        experiment = Experiment.from_json(document, self.protocol, taken=taken)
        self.experiment = experiment
        self._save()
        self.refresh()
        return experiment

    def export_json(self, path: str | Path) -> None:
        if self.experiment is not None:
            Path(path).write_text(json.dumps(self.experiment.to_json(), indent=2) + "\n", encoding="utf-8")

    def _import_dialog(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Import an experiment", "", "JSON (*.json)")
        if path:
            try:
                experiment = self.import_json(path)
            except (OSError, ValueError) as exc:
                self.notify(f"Not imported: {exc}", "danger")
                return
            self.notify(f"Experiment {experiment.code}: {len(experiment.runs)} runs.", "success")

    def _export_dialog(self) -> None:
        if self.experiment is None:
            return
        path, _filter = QFileDialog.getSaveFileName(self, "Export the experiment", f"{self.experiment.code}.json",
                                                    "JSON (*.json)")
        if path:
            self.export_json(path)

    # -- the table --------------------------------------------------------------------------
    def _declarations(self, protocol: str) -> dict[str, dict[str, Any]]:
        from admet.workflows.json_protocol import parameter_declarations

        try:
            return parameter_declarations(self.protocol(protocol))
        except (LookupError, ValueError):
            return {}

    def _fill(self) -> None:
        experiment = self.experiment
        self._filling = True
        self.name.setText(experiment.name if experiment else "")
        runs = experiment.runs if experiment else []
        names: list[str] = []
        for run in runs:
            for name in self._declarations(run.protocol):
                if name not in names:
                    names.append(name)
        self._columns = [*FIXED, *names, *TRAILING]
        self.table.clear()
        self.table.setColumnCount(len(self._columns))
        self.table.setHorizontalHeaderLabels(self._columns)
        self.table.setRowCount(len(runs))
        protocols = self.protocol_names()
        for row, run in enumerate(runs):
            editable = run.state == "planned"
            declarations = self._declarations(run.protocol)
            self._set(row, "Id", run.run_id(experiment.code), editable=False)
            combo = QComboBox()
            combo.addItems(protocols if run.protocol in protocols else [run.protocol, *protocols])
            combo.setCurrentText(run.protocol)
            combo.setEnabled(editable)
            combo.currentTextChanged.connect(lambda text, run=run: self._protocol_changed(run, text))
            self.table.setCellWidget(row, self._columns.index("Protocol"), combo)
            self._set(row, "Condition", run.condition, editable=editable)
            for name in names:
                if name not in declarations:
                    self._set(row, name, "", editable=False)
                elif name in run.parameters:
                    self._set(row, name, _text(run.parameters[name]), editable=editable)
                else:
                    self._set(row, name, _text(declarations[name]["default"]), editable=editable, default=True)
            self._set(row, "Note", run.note, editable=editable)
            self._set(row, "State", run.state, editable=False)
            self._set(row, "Run folder", run.run_folder, editable=False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._columns.index("Note"), QHeaderView.ResizeMode.Stretch)
        self.table.resizeRowsToContents()
        fit_table_height(self.table, max_rows=20)
        planned = sum(1 for run in runs if run.state == "planned")
        self.summary.setText(f"{len(runs)} runs, {planned} planned. Samples are labelled with the run's id and "
                             f"the replicate: {experiment.code}-01-r1." if experiment else
                             "No experiment yet: choose New experiment… or import one.")
        self.run_next_button.setEnabled(bool(planned))
        self._filling = False

    def _set(self, row: int, column: str, text: str, *, editable: bool, default: bool = False) -> None:
        item = QTableWidgetItem(text)
        flags = item.flags() | Qt.ItemFlag.ItemIsEditable if editable else item.flags() & ~Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        if default or not editable:
            item.setForeground(QColor(Theme.TEXT_MUTED))
        self.table.setItem(row, self._columns.index(column), item)

    def _selected(self) -> Run | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or self.experiment is None:
            return None
        return self.experiment.runs[rows[0].row()]

    def _protocol_changed(self, run: Run, protocol: str) -> None:
        if self._filling or protocol == run.protocol:
            return
        declared = self._declarations(protocol)
        run.protocol = protocol
        run.parameters = {name: value for name, value in run.parameters.items() if name in declared}
        self._save()
        self._fill()

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        if self._filling or self.experiment is None:
            return
        run = self.experiment.runs[item.row()]
        column = self._columns[item.column()]
        text = item.text().strip()
        if column == "Condition":
            run.condition = text
        elif column == "Note":
            run.note = text
        else:
            declaration = self._declarations(run.protocol).get(column)
            if declaration is None:
                return
            previous = dict(run.parameters)
            if text == "":
                run.parameters.pop(column, None)
            else:
                run.parameters[column] = _parse(text, declaration)
            try:
                self.experiment.document(run, self.protocol)
            except ValueError as exc:
                run.parameters = previous
                self.notify(f"{column}: {exc}", "danger")
                self._fill()
                return
        self._save()
        self._fill()


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _parse(text: str, declaration: dict[str, Any]) -> Any:
    """A cell's text as its parameter's kind: a number, true/false, one of the choices, or text."""
    kind = declaration["type"]
    if kind == "number":
        try:
            number = float(text)
        except ValueError:
            return text                                    # refused by the protocol's own check
        return int(number) if number.is_integer() else number
    if kind == "boolean":
        return text.lower() in {"true", "yes", "1", "on"}
    return text
