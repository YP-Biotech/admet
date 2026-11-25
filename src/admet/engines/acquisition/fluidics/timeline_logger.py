"""The run's timeline as it is recorded: every fluidics reading and every QC burst, into run.db.

A fluidics row comes from the poll thread ten times a second; a QC row comes from the live QC
once a second while a recording runs, stamped with the time its frames were taken. Both carry
elapsed_s from the same origin, the moment polling began, so the two series need nothing but
the run's database to be laid on one axis. Neither thread waits for the disk: the rows are
handed to the database's writer, which commits them once a second (see admet.core.rundb).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from admet.core.rundb import TimelineWriter
from admet.engines.acquisition.camera.qc import QCSummary


class TimelineLogger:
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
        self._writer: TimelineWriter | None = None
        self._row_count = 0
        self._filepath: str | None = None
        self._origin_monotonic: float | None = None
        self._lock = threading.Lock()              # the poll thread and the QC worker both write

    @property
    def filepath(self) -> str | None:
        return self._filepath

    @property
    def row_count(self) -> int:
        return self._row_count

    def start(self, pressure_count: int, sensor_count: int, *, origin_monotonic: float | None = None) -> str:
        """Open the timeline; origin_monotonic is the moment elapsed_s counts from, the poll thread's start."""
        filename = self._filename or f"{self._prefix}_{datetime.now():%Y%m%d_%H%M%S}.db"
        self._filepath = str(self._output_dir / filename)
        self._origin_monotonic = origin_monotonic if origin_monotonic else time.monotonic()
        self._writer = TimelineWriter(self._filepath, pressure_count, sensor_count)
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
        values: dict[str, Any] = {"timestamp": timestamp, "elapsed_s": round(float(elapsed_s), 3)}
        values.update({f"pressure_{index}_mbar": float(value) for index, value in enumerate(pressures)})
        values.update({f"flow_{index}_ul_min": float(value) for index, value in enumerate(flows)})
        values.update({f"volume_{index}_ul": float(value) for index, value in enumerate(volumes or ())})
        values.update({f"stable_{index}": 1.0 if stable else 0.0 for index, stable in enumerate(stability or ())})
        self._add("fluidics", values)

    def write_qc(self, at_monotonic: float, recording: dict[str, Any] | None, summary: QCSummary) -> None:
        """A QC burst's summary as a row of its own, stamped with the time its frames were taken, on
        the timeline's clock."""
        if self._origin_monotonic is None:
            return
        wall = datetime.now() - timedelta(seconds=max(0.0, time.monotonic() - at_monotonic))
        recording = recording or {}
        self._add("qc", {
            "timestamp": wall.strftime("%H:%M:%S.%f")[:-3], "elapsed_s": round(at_monotonic - self._origin_monotonic, 3),
            "recording_id": recording.get("recording_id", ""), "step": _text(recording.get("step")),
            "part": _text(recording.get("part")), "rate_hz": summary.rate_hz, "rate_half": _finite(summary.rate_half),
            "count": summary.count, "diameter": summary.diameter, "diameter_unit": summary.diameter_unit,
            "diameter_half": _finite(summary.diameter_half), "cv_percent": summary.cv_percent,
            "cv_half": _finite(summary.cv_half), "n": summary.n, "bursts": summary.bursts,
            "speed_px_frame": summary.speed_px_frame, "spacing_px": summary.spacing_px, "state": summary.state,
        })

    def _add(self, table: str, values: dict[str, Any]) -> None:
        with self._lock:
            if self._writer is None:
                return
            (self._writer.add_qc if table == "qc" else self._writer.add_fluidics)(values)
            self._row_count += 1

    def stop(self) -> None:
        """Every row committed and the file left whole."""
        with self._lock:
            writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()


def _finite(value: float) -> float | None:
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value)
