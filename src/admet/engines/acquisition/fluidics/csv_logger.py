"""The run's timeline: every fluidics reading and every QC burst, one CSV, one clock.

A fluidics row comes from the poll thread ten times a second; a QC row comes from the live QC
once a second while a recording runs, on its own columns, stamped with the time its frames
were taken. Both carry elapsed_s from the same origin, the moment polling began, so the two
series need nothing but this file to be laid on one axis.
"""

from __future__ import annotations

import csv
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from admet.engines.acquisition.camera.qc import QC_COLUMNS, QCSummary

QC_ROW_COLUMNS = tuple(column for column in QC_COLUMNS if column not in ("timestamp", "elapsed_s"))


class CsvLogger:
    def __init__(
        self,
        output_dir: str | Path = "logs",
        *,
        prefix: str = "fluidics",
        filename: str = "",
    ):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._prefix = prefix
        self._filename = filename
        self._file = None
        self._writer = None
        self._row_count = 0
        self._filepath: str | None = None
        self._fluidics_width = 0
        self._origin_monotonic: float | None = None
        self._lock = threading.Lock()              # the poll thread and the QC worker both write

    @property
    def filepath(self) -> str | None:
        return self._filepath

    @property
    def row_count(self) -> int:
        return self._row_count

    def start(self, pressure_count: int, sensor_count: int, *, origin_monotonic: float | None = None) -> str:
        """Open the file; origin_monotonic is the moment elapsed_s counts from, the poll thread's start."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = self._filename or f"{self._prefix}_{timestamp}.csv"
        self._filepath = str(self._output_dir / filename)

        self._file = open(self._filepath, "w", encoding="utf-8", newline="")
        headers = ["timestamp", "elapsed_s"]
        headers.extend(f"pressure_{index}_mbar" for index in range(pressure_count))
        headers.extend(f"flow_{index}_ul_min" for index in range(sensor_count))
        headers.extend(f"volume_{index}_ul" for index in range(sensor_count))
        headers.extend(f"stable_{index}" for index in range(sensor_count))
        self._fluidics_width = len(headers) - 2
        headers.extend(QC_ROW_COLUMNS)
        self._origin_monotonic = origin_monotonic if origin_monotonic else time.monotonic()
        self._writer = csv.writer(self._file)
        self._writer.writerow(headers)
        self._row_count = 0
        return self._filepath

    @property
    def origin_monotonic(self) -> float | None:
        return self._origin_monotonic

    def write_row(
        self,
        timestamp: str,
        elapsed_s: float,
        pressures: list[float],
        flows: list[float],
        volumes: list[float] | None = None,
        stability: list[bool] | None = None,
    ) -> None:
        if self._writer is None:
            return
        row = [timestamp, f"{elapsed_s:.3f}"]
        row.extend(f"{pressure:.2f}" for pressure in pressures)
        row.extend(f"{flow:.3f}" for flow in flows)
        if volumes:
            row.extend(f"{volume:.3f}" for volume in volumes)
        if stability:
            row.extend("1" if stable else "0" for stable in stability)
        row.extend("" for _ in QC_ROW_COLUMNS)              # a reading has no QC columns
        self._emit(row)

    def write_qc(self, at_monotonic: float, recording: dict[str, Any] | None, summary: QCSummary) -> None:
        """A QC burst's summary as a row of its own: fluidics columns empty, stamped with the
        time its frames were taken, on the file's clock."""
        if self._writer is None or self._origin_monotonic is None:
            return
        wall = datetime.now() - timedelta(seconds=max(0.0, time.monotonic() - at_monotonic))
        recording = recording or {}
        row = [wall.strftime("%H:%M:%S.%f")[:-3], f"{at_monotonic - self._origin_monotonic:.3f}"]
        row.extend("" for _ in range(self._fluidics_width))
        row.extend([recording.get("recording_id", ""), recording.get("step", ""), recording.get("part", ""),
                    f"{summary.rate_hz:.2f}", _finite(summary.rate_half), f"{summary.count:.1f}",
                    f"{summary.diameter:.3f}", summary.diameter_unit, _finite(summary.diameter_half),
                    f"{summary.cv_percent:.3f}", _finite(summary.cv_half), summary.n, summary.bursts,
                    f"{summary.speed_px_frame:.3f}", f"{summary.spacing_px:.2f}", summary.state])
        self._emit(row)

    def _emit(self, row: list) -> None:
        with self._lock:
            if self._writer is None:
                return
            self._writer.writerow(row)
            self._row_count += 1
            if self._row_count % 10 == 0 and self._file:
                self._file.flush()

    def stop(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
                self._writer = None


def _finite(value: float) -> str:
    return f"{value:.4f}" if value == value and value not in (float("inf"), float("-inf")) else ""
