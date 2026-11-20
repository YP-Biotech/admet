"""A run's timeline, run.csv, read for plots: the fluidics readings and the droplet QC rows on one clock.

A reading row carries the channels' pressures and flows; a QC row carries a recording's burst (its
id, rate, size, CV, count, state) and no readings. Both carry elapsed_s, seconds since polling began.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from admet.core.droplets import dispersed_channels, expected_rate_hz
from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS

QC_LIMIT_PERCENT = 5.0           # the droplet CV above which the rig calls a recording unstable


@dataclass
class QCSeries:
    """The run's droplet QC rows on the fluidics clock: one row a burst, each from one recording."""
    t: np.ndarray
    rate: np.ndarray
    rate_half: np.ndarray
    cv: np.ndarray
    cv_half: np.ndarray
    diameter: np.ndarray
    diameter_unit: str
    count: np.ndarray
    state: list[str]
    recording: list[str]
    expected: np.ndarray            # from the flows at that moment and the size then; NaN when unknown

    def chunks(self, mask: np.ndarray) -> list[np.ndarray]:
        """The indices in mask, one array per recording, in order."""
        indices = np.flatnonzero(mask)
        pieces: list[list[int]] = []
        for index in indices:
            if pieces and self.recording[pieces[-1][-1]] == self.recording[index]:
                pieces[-1].append(int(index))
            else:
                pieces.append([int(index)])
        return [np.array(piece) for piece in pieces]


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
            rows.append([number(row.get("elapsed_s"))]
                        + [number(row.get(f"pressure_{i}_mbar")) for i in channels]
                        + [number(row.get(f"flow_{i}_ul_min")) for i in channels])
    data = np.array(rows, dtype=float).reshape(-1, 1 + 2 * len(channels))
    data = data[np.isfinite(data[:, 0]) & np.isfinite(data[:, 1:]).any(axis=1)]   # a QC row is not a reading
    count = len(channels)
    return data[:, 0], [data[:, 1 + i] for i in range(count)], [data[:, 1 + count + i] for i in range(count)]


def number(text: Any) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return math.nan


def read_qc_csv(path: Path) -> QCSeries | None:
    """The QC rows of a run's timeline file, on its clock: the rows that carry a state."""
    columns: dict[str, list] = {key: [] for key in ("t", "rate", "rate_half", "cv", "cv_half", "diameter", "count")}
    states, recordings, units = [], [], []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not str(row.get("state") or ""):
                continue                                   # a reading
            t = number(row.get("elapsed_s"))
            if not math.isfinite(t):
                continue
            columns["t"].append(t)
            for key, column in (("rate", "rate_hz"), ("rate_half", "rate_half"), ("cv", "cv_percent"),
                                ("cv_half", "cv_half"), ("diameter", "diameter"), ("count", "count")):
                columns[key].append(number(row.get(column)))
            states.append(str(row.get("state") or ""))
            recordings.append(str(row.get("recording_id") or ""))
            units.append(str(row.get("diameter_unit") or ""))
    if not columns["t"]:
        return None
    arrays = {key: np.array(values, dtype=float) for key, values in columns.items()}
    unit = next((u for u in units if u), "")
    return QCSeries(arrays["t"], arrays["rate"], arrays["rate_half"], arrays["cv"], arrays["cv_half"],
                    arrays["diameter"], unit, arrays["count"], states, recordings,
                    np.full(len(states), math.nan))


def expected_rates(qc: QCSeries, t: np.ndarray, flows: list[np.ndarray]) -> np.ndarray:
    """At each QC row, the rate the flows then measured must make at the size then: every channel
    but the oil over a sphere of the row's diameter. NaN until the size is in µm."""
    if qc.diameter_unit != "um" or not len(t):
        return np.full(len(qc.t), math.nan)
    dispersed = np.zeros(len(qc.t))
    for channel in dispersed_channels(FLUIDIC_CHANNEL_LABELS):
        if channel < len(flows):
            dispersed += np.interp(qc.t, t, np.nan_to_num(flows[channel]))
    return np.array([expected_rate_hz(float(q), float(d)) if q > 0 and d > 0 else math.nan
                     for q, d in zip(dispersed, qc.diameter)])


def step_markers(events: list[dict], origin: float) -> list[tuple[float, str, str]]:
    """Step starts and ends on the CSV's clock: (seconds, label, kind), kind start, completed or failed."""
    markers, current = [], None
    for event in events:
        if not event.get("step_name") or type(event.get("monotonic")) not in (int, float):
            current = None
            continue
        at, number_, outcome = event["monotonic"] - origin, int(event.get("step_index", 0)) + 1, event.get("outcome")
        if outcome == "running" and not event.get("confirmation_message"):
            if current != number_:
                current = number_
                markers.append((at, f"▶ {number_}", "start"))
        elif outcome != "running":
            current = None
            markers.append((at, f"■ {number_}", "completed" if outcome == "completed" else "failed"))
    return markers
