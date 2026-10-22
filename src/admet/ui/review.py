"""Review runs: recorded pressure and flow from saved CSVs, run over run. Reads files only."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS
from admet.ui import theme as ui
from admet.ui.theme import Theme
from admet.workflows.calculation_inputs import recording_path
from admet.workflows.calculations import recorded_runs

CHANNEL_COLORS = (Theme.ACCENT, Theme.DANGER_HOVER, Theme.SUCCESS_HOVER)
# Runs are told apart by line style; channels keep the colours of the live plots.
RUN_STYLES = (
    (Qt.PenStyle.SolidLine, "━"), (Qt.PenStyle.DashLine, "╍"), (Qt.PenStyle.DotLine, "┅"),
    (Qt.PenStyle.DashDotLine, "─·"), (Qt.PenStyle.DashDotDotLine, "─··"),
)


@dataclass
class ReviewRun:
    name: str
    t: np.ndarray
    pressures: list[np.ndarray]
    flows: list[np.ndarray]
    markers: list[tuple[float, str, str]] = field(default_factory=list)


def read_fluidics_csv(path: Path) -> tuple[np.ndarray, list[np.ndarray], list[np.ndarray]]:
    """elapsed_s and every channel's pressure and flow, by channel number; missing or unreadable is NaN."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        if "elapsed_s" not in columns:
            raise ValueError(f"{Path(path).name}: no elapsed_s column")
        channels = range(len(FLUIDIC_CHANNEL_LABELS))
        rows = []
        for row in reader:
            rows.append([_number(row.get("elapsed_s"))]
                        + [_number(row.get(f"pressure_{i}_mbar")) for i in channels]
                        + [_number(row.get(f"flow_{i}_ul_min")) for i in channels])
    data = np.array(rows, dtype=float).reshape(-1, 1 + 2 * len(channels))
    data = data[np.isfinite(data[:, 0])]
    count = len(channels)
    return data[:, 0], [data[:, 1 + i] for i in range(count)], [data[:, 1 + count + i] for i in range(count)]


def _number(text) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return math.nan


def step_markers(events: list[dict], origin: float) -> list[tuple[float, str, str]]:
    """Step starts and ends on the CSV's clock, marked as on the live plots."""
    markers, current = [], None
    for event in events:
        if not event.get("step_name") or type(event.get("monotonic")) not in (int, float):
            current = None
            continue
        at, number, outcome = event["monotonic"] - origin, int(event.get("step_index", 0)) + 1, event.get("outcome")
        if outcome == "running" and not event.get("confirmation_message"):
            if current != number:
                current = number
                markers.append((at, f"▶ {number}", Theme.SUCCESS_HOVER))
        elif outcome != "running":
            current = None
            markers.append((at, f"■ {number}", Theme.TEXT_MUTED if outcome == "completed" else Theme.DANGER_HOVER))
    return markers


def project_run(directory: Path, name: str) -> ReviewRun:
    directory = Path(directory)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    t, pressures, flows = read_fluidics_csv(recording_path(directory, summary))
    origin = summary.get("artifacts", {}).get("polling_origin_monotonic")
    events = []
    if type(origin) in (int, float) and (directory / "events.jsonl").is_file():
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
                  if line.strip()]
    return ReviewRun(name, t, pressures, flows, step_markers(events, origin) if events else [])


def file_run(path: Path) -> ReviewRun:
    t, pressures, flows = read_fluidics_csv(path)
    return ReviewRun(Path(path).stem, t, pressures, flows)


def _bin(t: np.ndarray, y: np.ndarray, size: float) -> tuple[np.ndarray, np.ndarray]:
    keep = np.isfinite(y)
    t, y = t[keep], y[keep]
    if size <= 0 or len(t) < 2:
        return t, y
    index = ((t - t[0]) / size).astype(np.intp)
    counts = np.bincount(index)
    used = counts > 0
    return np.bincount(index, weights=t)[used] / counts[used], np.bincount(index, weights=y)[used] / counts[used]


class ReviewPanel(QWidget):
    """Pick runs on the left; their pressure and flow overlay on two linked plots on the right."""

    def __init__(self):
        super().__init__()
        import pyqtgraph as pg

        self._pg = pg
        self.project = None
        self._runs: dict[str, ReviewRun] = {}
        self._sources: dict[str, tuple[str, str]] = {}
        self._reference = None
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(ui.spacing("default"))

        side = QVBoxLayout()
        side.setSpacing(ui.spacing("control"))
        side.addWidget(_title("Runs"))
        self.run_list = QListWidget()
        self.run_list.setMinimumHeight(220)
        self.run_list.itemChanged.connect(self._run_toggled)
        side.addWidget(self.run_list)
        load = ui.button("Load file…")
        load.clicked.connect(self._load_files)
        side.addWidget(load)
        side.addWidget(_title("Channels"))
        self.channel_boxes = []
        for index, label in enumerate(FLUIDIC_CHANNEL_LABELS):
            box = QCheckBox(label)
            box.setChecked(True)
            box.setStyleSheet(f"color: {CHANNEL_COLORS[index]};")
            box.toggled.connect(self.redraw)
            side.addWidget(box)
            self.channel_boxes.append(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Bin"))
        self.bin = QDoubleSpinBox()
        self.bin.setRange(0.0, 60.0)
        self.bin.setSingleStep(0.5)
        self.bin.setSuffix(" s")
        self.bin.setSpecialValueText("off")
        self.bin.valueChanged.connect(self.redraw)
        row.addWidget(self.bin, 1)
        side.addLayout(row)
        reset = ui.button("Reset view")
        reset.clicked.connect(self._reset_view)
        side.addWidget(reset)
        self.readout = QLabel("Hover a plot; click to pin a reference, double-click to clear it.")
        self.readout.setObjectName("MutedText")
        self.readout.setWordWrap(True)
        side.addWidget(self.readout)
        side.addStretch(1)
        sidebar = QWidget()
        sidebar.setLayout(side)
        sidebar.setFixedWidth(240)
        root.addWidget(sidebar)

        plots = QVBoxLayout()
        plots.setSpacing(ui.spacing("control"))
        self.pressure_plot = self._plot("Pressure", "mbar")
        self.flow_plot = self._plot("Flow", "µL/min")
        self.flow_plot.setXLink(self.pressure_plot)
        plots.addWidget(self.pressure_plot)
        plots.addWidget(self.flow_plot)
        self.status = QLabel()
        self.status.setObjectName("MutedText")
        plots.addWidget(self.status)
        root.addLayout(plots, 1)
        self.setMinimumHeight(560)

    def _plot(self, title: str, unit: str):
        pg = self._pg
        plot = pg.PlotWidget(title=title, labels={"left": unit, "bottom": "s"})
        plot.setBackground(Theme.BG_DARK)
        plot.showGrid(x=True, y=True, alpha=0.15)
        line = pg.InfiniteLine(angle=90, pen=pg.mkPen(Theme.TEXT_MUTED, width=1, style=Qt.PenStyle.DotLine))
        plot.addItem(line, ignoreBounds=True)
        plot.cursor = line
        plot.scene().sigMouseMoved.connect(lambda pos, plot=plot, unit=unit: self._hover(plot, unit, pos))
        plot.scene().sigMouseClicked.connect(lambda event, plot=plot, unit=unit: self._click(plot, unit, event))
        return plot

    def set_project(self, project) -> None:
        if project == self.project and self.run_list.count():
            return
        self.project = project
        self._runs.clear()
        self._sources = {key: value for key, value in self._sources.items() if value[0] == "file"}
        self.refresh()

    def refresh(self) -> None:
        """List the project's recorded runs, newest first, then any files loaded by hand."""
        checked = {item.data(Qt.ItemDataRole.UserRole) for item in self._items() if item.checkState() == Qt.CheckState.Checked}
        self.run_list.blockSignals(True)
        self.run_list.clear()
        for run in recorded_runs(self.project):
            parts = (run["at"][:16].replace("T", " "), run["name"], run["state"])
            self._sources[run["directory"]] = ("project", " · ".join(part for part in parts if part))
        for key, (_kind, label) in self._sources.items():
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
            self.run_list.addItem(item)
        self.run_list.blockSignals(False)
        self.redraw()

    def _items(self):
        return [self.run_list.item(index) for index in range(self.run_list.count())]

    def _load_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "Load fluidics CSV", "", "CSV files (*.csv)")
        self.add_files(paths)

    def add_files(self, paths) -> None:
        for path in paths:
            self._sources.setdefault(str(Path(path).resolve()), ("file", Path(path).name))
        self.refresh()
        for item in self._items():
            if item.data(Qt.ItemDataRole.UserRole) in {str(Path(p).resolve()) for p in paths}:
                item.setCheckState(Qt.CheckState.Checked)

    def _run_toggled(self, item) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked and key not in self._runs:
            try:
                kind, label = self._sources[key]
                self._runs[key] = project_run(Path(key), label) if kind == "project" else file_run(Path(key))
            except (OSError, ValueError, KeyError) as exc:
                self.run_list.blockSignals(True)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.run_list.blockSignals(False)
                self.status.setText(f"Could not read {item.text()}: {exc}")
                return
        self.redraw()

    def shown_runs(self) -> list[tuple[str, ReviewRun]]:
        return [(item.data(Qt.ItemDataRole.UserRole), self._runs[item.data(Qt.ItemDataRole.UserRole)])
                for item in self._items()
                if item.checkState() == Qt.CheckState.Checked and item.data(Qt.ItemDataRole.UserRole) in self._runs]

    def redraw(self, *_args) -> None:
        pg = self._pg
        for plot in (self.pressure_plot, self.flow_plot):
            for item in list(plot.listDataItems()) + [i for i in plot.items() if getattr(i, "step_marker", False)]:
                plot.removeItem(item)
        shown, points = self.shown_runs(), 0
        for number, (_key, run) in enumerate(shown):
            style = RUN_STYLES[number % len(RUN_STYLES)][0]
            for plot, series in ((self.pressure_plot, run.pressures), (self.flow_plot, run.flows)):
                for channel, values in enumerate(series):
                    if not self.channel_boxes[channel].isChecked():
                        continue
                    t, y = _bin(run.t, values, self.bin.value())
                    if not len(t):
                        continue
                    points += len(t)
                    curve = plot.plot(t, y, pen=pg.mkPen(CHANNEL_COLORS[channel], width=1.5, style=style))
                    curve.setDownsampling(auto=True, method="peak")
                    curve.setClipToView(True)
                    curve.setToolTip(f"{run.name} · {FLUIDIC_CHANNEL_LABELS[channel]}")
                for at, label, color in run.markers:
                    line = pg.InfiniteLine(at, pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
                                           label=label if plot is self.flow_plot else None,
                                           labelOpts={"position": 0.92, "color": color})
                    line.step_marker = True
                    plot.addItem(line)
        # Each shown run's line style leads its name; relabelling must not re-trigger a redraw.
        styles = {key: RUN_STYLES[number % len(RUN_STYLES)][1] for number, (key, _run) in enumerate(shown)}
        self.run_list.blockSignals(True)
        for item in self._items():
            key = item.data(Qt.ItemDataRole.UserRole)
            item.setText(f"{styles[key]}  {self._sources[key][1]}" if key in styles else self._sources[key][1])
        self.run_list.blockSignals(False)
        bin_text = "off" if self.bin.value() <= 0 else f"{self.bin.value():g} s"
        self.status.setText(f"{len(shown)} run(s) · {points:,} points · bin {bin_text}"
                            if shown else "Tick a run to plot it.")

    def _reset_view(self) -> None:
        for plot in (self.pressure_plot, self.flow_plot):
            plot.enableAutoRange()

    def _hover(self, plot, unit: str, pos) -> None:
        if not plot.sceneBoundingRect().contains(pos):
            return
        point = plot.getViewBox().mapSceneToView(pos)
        for each in (self.pressure_plot, self.flow_plot):
            each.cursor.setPos(point.x())
        text = f"t = {point.x():.2f} s · {point.y():.2f} {unit}"
        if self._reference and self._reference[2] == unit:
            text += f"\nΔt = {point.x() - self._reference[0]:.2f} s · Δ = {point.y() - self._reference[1]:.2f} {unit}"
        self.readout.setText(text)

    def _click(self, plot, unit: str, event) -> None:
        if not plot.sceneBoundingRect().contains(event.scenePos()):
            return
        if event.double():
            self._reference = None
            self.readout.setText("Reference cleared.")
            return
        if event.button() == Qt.MouseButton.LeftButton:
            point = plot.getViewBox().mapSceneToView(event.scenePos())
            self._reference = (point.x(), point.y(), unit)
            self.readout.setText(f"Reference pinned at t = {point.x():.2f} s · {point.y():.2f} {unit}")


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("FieldLabel")
    return label
