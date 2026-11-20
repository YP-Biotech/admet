"""Stored analysis results, summarised per file for the Results stage, and their export."""

from __future__ import annotations

import csv
import html
import io
import json
import math
import random
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Callable, Sequence

import numpy as np

from admet.core.droplets import dispersed_flow_ul_min, expected_rate_hz  # noqa: F401  (analyze's names for them)
from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS
from admet.ui import theme
from admet.ui.series_style import SeriesStyle, series_styles


@dataclass(frozen=True)
class StoredResult:
    """One source's stored analysis: where its rows and summary are, and what it was analysed as."""

    source_path: Path
    engine: str
    sample_id: str
    result_dir: Path
    metadata: dict[str, Any]           # result.json: the summary, when it was analysed, the settings it ran with
    run: str = ""                      # the run folder the source came from, if any

    @property
    def rows_path(self) -> Path:
        return self.result_dir / "rows.jsonl"

    @property
    def settings(self) -> dict[str, Any]:
        settings = self.metadata.get("settings")
        return dict(settings) if isinstance(settings, dict) else {}

    @property
    def analyzed_at(self) -> str:
        return str(self.metadata.get("analyzed_at") or "")

    @property
    def file_id(self) -> str:
        return str(self.metadata.get("file_id") or self.result_dir.name)

    @property
    def run_name(self) -> str:
        return Path(self.run).name if self.run else ""


@dataclass(frozen=True)
class FileResult:
    """One file's stored analysis: the numbers the Results stage shows and plots.

    Distributions hold one value per droplet (the median over the frames it was seen
    in), so a droplet seen for forty frames counts once, like one seen for four.
    """

    run: str                         # the run folder's name the file came from, empty for a loose file
    sample_id: str
    engine: str
    source_path: str
    file_id: str
    analyzed_at: str
    settings: dict[str, Any]
    fps: float
    scale: float                     # µm per px
    frames: int
    droplets: int
    rate_hz: float
    speed_mm_s: float
    rate_note: str = ""                                    # how the rate was had, when not from the gate
    expected_hz: float = 0.0                               # from the dispersed flow and the droplet size
    dispersed_ul_min: float = 0.0
    arrivals_s: tuple[float, ...] = ()                     # each droplet's first sighting, s from the first frame
    diameters: tuple[float, ...] = ()                      # µm
    areas: tuple[float, ...] = ()                          # µm²
    periods_s: tuple[float, ...] = ()                      # between one droplet's arrival and the next
    speeds_mm_s: tuple[float, ...] = ()
    detections: tuple[tuple[float, float, float, float], ...] = ()   # frame, x px, y px, area µm²
    timed_diameters: tuple[tuple[float, float], ...] = ()            # s from the first frame, µm, per tracked droplet
    spans: tuple[tuple[float, float, float], ...] = ()               # droplet id, first frame, last frame
    inclusion_counts: tuple[int, ...] = ()

    @property
    def mean_diameter(self) -> float:
        return mean(self.diameters) if self.diameters else 0.0

    @property
    def std_diameter(self) -> float:
        return pstdev(self.diameters) if len(self.diameters) > 1 else 0.0

    @property
    def cv_percent(self) -> float:
        return self.std_diameter / self.mean_diameter * 100.0 if self.mean_diameter else 0.0

    @property
    def mean_area(self) -> float:
        return mean(self.areas) if self.areas else 0.0

    @property
    def std_area(self) -> float:
        return pstdev(self.areas) if len(self.areas) > 1 else 0.0

    @property
    def inclusions(self) -> int:
        return sum(self.inclusion_counts)


def load_stored_result(result_dir: Path, *, source_path: Path, engine: str, sample_id: str,
                       run: str = "") -> StoredResult | None:
    """The stored analysis in a results folder, if the folder holds a finished one."""
    result_dir = Path(result_dir)
    if not (result_dir / "result.json").is_file() or not (result_dir / "rows.jsonl").is_file():
        return None
    try:
        metadata = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    return StoredResult(source_path=Path(source_path), engine=engine, sample_id=sample_id, result_dir=result_dir,
                        metadata=metadata, run=run)


def stale_window(metadata: dict[str, Any]) -> bool:
    """A video result made before the window was applied when read, with a window baked in: its
    droplets near the ends were never written, so it serves no other window and is run again."""
    settings = metadata.get("settings") if isinstance(metadata.get("settings"), dict) else {}
    return metadata.get("window") != "whole" and bool(settings.get("skip_left_pct") or settings.get("skip_right_pct"))


def iter_rows(path: Path, *, limit: int | None = None):
    """The rows of a rows file one at a time: a recording's rows run into the hundreds of thousands."""
    path = Path(path)
    if not path.is_file():
        return
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if limit is not None and count >= limit:
                return
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                count += 1
                yield value


def read_rows(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    return list(iter_rows(path, limit=limit))


PLOT_POINTS = 6000      # detections kept per file for the plots: a uniform sample, not the first ones


def file_results(stored: StoredResult, rows, corrections: dict[tuple[str, str], dict[str, Any]] | None = None,
                 scales: dict[tuple[str, str], tuple[float, float]] | None = None,
                 settings: dict[tuple[str, str], dict[str, Any]] | None = None) -> list[FileResult]:
    """The source's result from its stored rows, in one pass: distributions per droplet, rate and speed.

    CORRECTIONS, by (engine, source path), are what the user changed by hand on imaging droplets:
    a droplet switched off is left out, an inclusion count is taken as corrected. SCALES, by the
    same key, are the µm per px and fps to read the rows with: the engine measures in pixels and
    frames, so changing either needs no new run. SETTINGS, by the same key, are the file's
    read-time settings: for a video the window (skip left and right, % of the width) and the gate
    (% of the width), which choose among the droplets the whole-width run found and where they are
    counted; for an image folder the frame limit (the first frames only) and whether inclusions
    are shown. Rows are grouped by the file id they carry, so a rows file holding more than one
    file (as none written now does) still gives one result each.
    """
    source = str(stored.source_path)
    files: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = _row_key(row)
        kind = row.get("kind")
        acc = files.get(key)
        if acc is None:
            acc = files[key] = {"context": {}, "per_droplet": {}, "loose_d": [], "loose_a": [], "points": [],
                                "seen": 0, "inclusions": [], "frames": set(), "um": False,
                                "job": stored.metadata, "source": source, "rng": random.Random(0),
                                "fixes": (corrections or {}).get((stored.engine, source), {}),
                                "scale": tuple((scales or {}).get((stored.engine, source), (0.0, 0.0))),
                                "read": dict((settings or {}).get((stored.engine, source), {})),
                                "width": None, "frames_kept": []}
        if kind == "run_context":
            acc["context"] = _values(row)
            acc["width"] = _num(acc["context"].get("frame_width"))
            continue
        if kind not in {"detection", "droplet", "track"}:
            continue
        values = _values(row)
        frame = _num(values.get("frame"))
        if not _within_read_settings(acc, kind, values, frame):
            continue
        if frame is not None:
            acc["frames"].add(int(frame))
        fix = acc["fixes"].get(correction_key(frame, values.get("droplet_id"))) if kind == "droplet" and acc["fixes"] \
            else None
        if fix and fix.get("disabled"):
            continue
        # Pixels first, so the scale of the day applies; µm only when the row has nothing else.
        diameter = _num(values.get("diameter_px", values.get("equivalent_diameter", values.get("diameter"))))
        area = _num(values.get("area_px", values.get("area")))
        if diameter is None and area is None:
            diameter, area = _num(values.get("diameter_um")), _num(values.get("area_um2"))
            if diameter is not None or area is not None:
                acc["um"] = True
        if kind == "droplet":
            acc["inclusions"].append(corrected_inclusions(values, fix))
        cx = _num(values.get("centroid_x", values.get("center_x")))
        cy = _num(values.get("centroid_y", values.get("center_y")))
        if frame is not None and cx is not None and cy is not None:
            _reservoir(acc, (frame, round(cx, 1), round(cy, 1), area or 0.0))
        # A "droplet" row is one droplet in one image; its id only counts within that image,
        # so it is not followed across frames the way a tracked detection is.
        droplet_id = None if kind == "droplet" else _num(
            values.get("droplet_id") if values.get("droplet_id") is not None else values.get("track_id"))
        if droplet_id is None:
            if diameter:
                acc["loose_d"].append(diameter)
            if area:
                acc["loose_a"].append(area)
            continue
        entry = acc["per_droplet"].setdefault(int(droplet_id), {"d": [], "a": [], "f": [], "x": []})
        if diameter:
            entry["d"].append(diameter)
        if area:
            entry["a"].append(area)
        if frame is not None:
            entry["f"].append(frame)
            entry["x"].append(cx or 0.0)
    return [_finish(stored, key, acc) for key, acc in sorted(files.items())]


def _within_read_settings(acc: dict[str, Any], kind: str, values: dict[str, Any], frame: float | None) -> bool:
    """Whether a row is taken under the file's read-time settings: inside the window, within the frame limit."""
    read = acc["read"]
    if kind == "detection" and acc["width"]:
        left, right = float(read.get("skip_left_pct") or 0.0), float(read.get("skip_right_pct") or 0.0)
        if left or right:
            width = acc["width"]
            low, high = round(width * left / 100), width - round(width * right / 100)
            if low >= high:
                low, high = 0, width
            x, w = _num(values.get("bbox_x")), _num(values.get("bbox_width"))
            if x is not None and w is not None and not (x > low and x + w < high):
                return False
    if kind == "droplet" and frame is not None:
        limit = int(read.get("frame_limit") or 0)
        if limit:
            kept = acc["frames_kept"]                     # the rows come frame by frame, in order
            if int(frame) not in kept:
                if len(kept) >= limit:
                    return False
                kept.append(int(frame))
    return True


def gate_crossings(per_droplet: dict[int, dict[str, list]], gate_x: float) -> list[float]:
    """When each followed droplet passed the gate, in frames to a fraction, as the tracker counts them."""
    crossings = []
    for entry in per_droplet.values():
        order = sorted(range(len(entry["f"])), key=lambda i: entry["f"][i])
        previous: tuple[float, float] | None = None
        for i in order:
            frame, x = entry["f"][i], entry["x"][i]
            if previous is not None and (previous[1] < gate_x) != (x < gate_x) and x != previous[1]:
                crossings.append(frame - (x - gate_x) / (x - previous[1]) * (frame - previous[0]))
            previous = (frame, x)
    return sorted(crossings)


def _reservoir(acc: dict[str, Any], point: tuple[float, float, float, float]) -> None:
    """Keep PLOT_POINTS detections spread evenly over the file, whatever its length."""
    acc["seen"] += 1
    if len(acc["points"]) < PLOT_POINTS:
        acc["points"].append(point)
        return
    slot = acc["rng"].randrange(acc["seen"])
    if slot < PLOT_POINTS:
        acc["points"][slot] = point


def _finish(stored: StoredResult, key: tuple[str, str], acc: dict[str, Any]) -> FileResult:
    engine = stored.engine or key[0]
    job, context = acc["job"], acc["context"]
    analysis = _as_dict(context.get("analysis"))
    meta = _as_dict(job)                                  # result.json: the summary the engine returned
    scale = acc["scale"][0] or _num(analysis.get("microns_per_pixel")) or 1.0
    fps = acc["scale"][1] or _num(analysis.get("fps")) or 0.0
    to_um = 1.0 if acc["um"] else scale                  # engine rows in px are scaled; µm rows are kept
    diameters = [median(e["d"]) * to_um for e in acc["per_droplet"].values() if e["d"]] \
        + [d * to_um for d in acc["loose_d"]]
    areas = [median(e["a"]) * to_um * to_um for e in acc["per_droplet"].values() if e["a"]] \
        + [a * to_um * to_um for a in acc["loose_a"]]
    spans: list[tuple[float, float, float]] = []
    arrivals: list[float] = []
    speeds: list[float] = []
    timed: list[tuple[float, float]] = []                 # (first frame, diameter) per tracked droplet
    for droplet_id, entry in sorted(acc["per_droplet"].items()):
        if not entry["f"]:
            continue
        order = sorted(range(len(entry["f"])), key=lambda i: entry["f"][i])
        first, last = entry["f"][order[0]], entry["f"][order[-1]]
        spans.append((float(droplet_id), first, last))
        arrivals.append(first)
        if entry["d"]:
            timed.append((first, median(entry["d"]) * to_um))
        if last > first and fps and scale:
            travel = abs(entry["x"][order[-1]] - entry["x"][order[0]])
            speeds.append(travel / (last - first) * scale * fps / 1000.0)
    arrivals.sort()
    periods = [(b - a) / fps for a, b in zip(arrivals, arrivals[1:]) if fps and b > a]
    arrival_rate = ((len(arrivals) - 1) / ((arrivals[-1] - arrivals[0]) / fps)
                    if fps and len(arrivals) > 1 and arrivals[-1] > arrivals[0] else 0.0)
    generation = _as_dict(context.get("generation"))
    crossings = int(_num(generation.get("crossings")) or 0)
    period_frames = _num(generation.get("period_frames")) or 0.0
    # Read with a window or a frame limit, the droplets are the ones kept, not the run's total.
    filtered = any(acc["read"].get(key) for key in ("skip_left_pct", "skip_right_pct", "frame_limit"))
    gate_pct = _num(acc["read"].get("gate_pct"))
    if acc["width"] and gate_pct is not None and 0 < gate_pct < 100:
        # The gate of the day, counted over the stored tracks: no new run for a moved gate.
        passed = gate_crossings(acc["per_droplet"], acc["width"] * gate_pct / 100)
        gaps = [b - a for a, b in zip(passed, passed[1:])]
        crossings, period_frames = len(passed), (mean(gaps) if gaps else 0.0)
        if generation.get("method") != "spacing":
            generation = {**generation, "rate_hz": None}
    gate_rate = (fps / period_frames if fps and period_frames else
                 _num(generation.get("rate_hz")) or _num(meta.get("frequency_hz")) or _num(context.get("frequency_hz")))
    rate_note = ""
    method = str(generation.get("method") or "")
    if gate_rate and (method == "spacing" or crossings >= len(arrivals) / 2 or not arrival_rate):
        rate = gate_rate
        rate_note = "speed over spacing" if method == "spacing" else ""
    elif arrival_rate:
        # The gate saw too few of the droplets to be trusted (it sits where tracks start or end):
        # the rate is then droplets seen per second of analysed video.
        rate = arrival_rate
        rate_note = f"from arrivals; gate saw {crossings} of {len(arrivals)}" if gate_rate else "from arrivals"
    else:
        rate = 1.0 / mean(periods) if periods else 0.0
    speed_px = _num(context.get("speed_px_frame")) or 0.0
    speed = speed_px * scale * fps / 1000.0 if speed_px and fps else (mean(speeds) if speeds else 0.0)
    dispersed = float(acc["scale"][2]) if len(acc["scale"]) > 2 and acc["scale"][2] else 0.0
    expected = expected_rate_hz(dispersed, median(diameters)) if dispersed and diameters else 0.0
    analyzed_at = str(meta.get("analyzed_at") or "")
    points = sorted(acc["points"])
    settings = _run_settings(stored, analysis, _as_dict(context.get("detection")))
    settings.update({"microns_per_pixel": scale, "fps": fps} if engine != "cellpose" else {"px_to_um": scale})
    return FileResult(
        run=stored.run_name,
        sample_id=stored.sample_id or str(job.get("sample_id") or key[1]),
        engine=engine,
        source_path=acc["source"],
        file_id=key[1] if key[1] != "sample" else stored.file_id,
        analyzed_at=local_time(analyzed_at) if analyzed_at else "",
        settings=settings,
        fps=fps,
        scale=scale,
        frames=len(acc["frames"]),
        droplets=(len(acc["loose_d"]) if acc["fixes"] else
                  (len(acc["per_droplet"]) or len(acc["loose_d"])) if filtered else
                  int(_num(meta.get("total_droplets") or meta.get("droplet_count")) or 0)
                  or len(acc["per_droplet"]) or len(acc["loose_d"])),
        rate_hz=float(rate or 0.0),
        speed_mm_s=float(speed or 0.0),
        rate_note=rate_note,
        expected_hz=expected,
        dispersed_ul_min=dispersed,
        arrivals_s=tuple((a - arrivals[0]) / fps for a in arrivals) if arrivals and fps else (),
        diameters=tuple(diameters),
        areas=tuple(areas),
        periods_s=tuple(periods),
        speeds_mm_s=tuple(speeds),
        detections=tuple((f, x, y, round(a * to_um * to_um, 2)) for f, x, y, a in points),
        timed_diameters=tuple(sorted(((f - arrivals[0]) / fps, d) for f, d in timed)) if arrivals and fps else (),
        spans=tuple(spans),
        inclusion_counts=() if acc["read"].get("detect_inclusions") is False else tuple(acc["inclusions"]),
    )


def settings_summary(settings: dict[str, Any]) -> str:
    """The settings a result was made with, in one line."""
    parts = []
    for key, label, unit in (("microns_per_pixel", "µm/px", ""), ("fps", "fps", ""), ("k_sigma", "k", "")):
        if settings.get(key) not in {None, ""}:
            parts.append(f"{label} {_fmt(settings[key])}{unit}")
    if settings.get("start_frame") is not None or settings.get("end_frame"):
        parts.append(f"frames {int(_num(settings.get('start_frame')) or 0)}–{_fmt(settings.get('end_frame') or '')}")
    if settings.get("roi_width") or settings.get("roi_height"):
        parts.append(f"crop {int(_num(settings.get('roi_x')) or 0)},{int(_num(settings.get('roi_y')) or 0)} "
                     f"{int(_num(settings.get('roi_width')) or 0)}×{int(_num(settings.get('roi_height')) or 0)}")
    for key, label in (("skip_left_pct", "skip left"), ("skip_right_pct", "skip right"), ("gate_pct", "gate")):
        if settings.get(key) not in {None, ""}:
            parts.append(f"{label} {_fmt(settings[key])} %")
    for key, label, unit in (("satellite_px", "satellite", " px"), ("min_area", "min area", " px²"),
                             ("px_to_um", "px→µm", ""), ("frame_limit", "frame limit", "")):
        if settings.get(key) not in {None, ""}:
            parts.append(f"{label} {_fmt(settings[key])}{unit}")
    return " · ".join(parts)


def correction_key(frame: Any, droplet_id: Any) -> str:
    """How a correction names one imaging droplet: its image and its number in that image."""
    return f"{int(_num(frame) or 0)}:{int(_num(droplet_id) or 0)}"


def corrected_inclusions(values: dict[str, Any], fix: dict[str, Any] | None) -> int:
    count = int(_num(values.get("inclusions")) or 0)
    return max(0, count + int(_num((fix or {}).get("inclusions")) or 0))


def corrections_summary(fixes: dict[str, Any]) -> dict[str, int]:
    """How many droplets were switched off and how many inclusions were added or removed."""
    deltas = [int(_num(fix.get("inclusions")) or 0) for fix in fixes.values() if not fix.get("disabled")]
    return {"disabled": sum(1 for fix in fixes.values() if fix.get("disabled")),
            "added": sum(d for d in deltas if d > 0), "removed": -sum(d for d in deltas if d < 0)}


# -- the per-file table ----------------------------------------------------------------

def spark(values, width: int = 72, height: int = 16, bins: int = 24) -> str:
    """A tiny histogram of the values as inline SVG; no axes, just the shape."""
    import numpy as np

    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    if len(array) < 2:
        return ""
    low, high = np.percentile(array, (0.5, 99.5))
    if high <= low:
        low, high = array.min() - 0.5, array.max() + 0.5
    counts, _ = np.histogram(np.clip(array, low, high), bins=bins, range=(low, high))
    top = counts.max() or 1
    bar = width / bins
    rects = "".join(
        f'<rect x="{i * bar:.1f}" y="{height - height * c / top:.1f}" width="{bar - 0.4:.1f}" '
        f'height="{height * c / top:.1f}"/>'
        for i, c in enumerate(counts) if c)
    return f'<svg class="spark" width="{width}" height="{height}">{rects}</svg>'


@dataclass(frozen=True)
class LiveQC:
    """What the rig measured while a video was recorded: the QC rows of its run's timeline for that video."""

    rate_hz: float                   # the median over the bursts
    rate_half: float                 # the last burst's half interval
    diameter: float
    diameter_half: float
    diameter_unit: str
    cv_percent: float                # the last burst's, the one over the whole recording
    cv_half: float
    count: float                     # droplets made, as counted live
    bursts: int
    state: str
    run: str = ""                    # the run folder's name
    source_path: str = ""
    sample_id: str = ""              # the file's, given by the view
    # Burst by burst: s since the first burst, droplets counted, rate /s, diameter, CV %.
    points: tuple[tuple[float, float, float, float, float], ...] = ()

    @property
    def file_id(self) -> str:
        return "live"


def live_qc(run_dir: str | Path, video_path: str | Path) -> LiveQC | None:
    """The live QC of one video, from the rows of its run's timeline that carry its recording id."""
    path = Path(run_dir) / "run.csv"
    stem = Path(video_path).stem
    if not path.is_file():
        return None
    rows = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("state") or "") and str(row.get("recording_id") or "") == stem:
                    rows.append(row)
    except OSError:
        return None
    if not rows:
        return None
    last = rows[-1]
    rates = [value for value in (_num(row.get("rate_hz")) for row in rows) if value]
    diameters = [value for value in (_num(row.get("diameter")) for row in rows) if value]
    counts = [value for value in (_num(row.get("count")) for row in rows) if value is not None]
    times = [_num(row.get("elapsed_s")) for row in rows]
    if any(t is None for t in times):
        times = [float(i) for i in range(len(rows))]                     # no clock: one tick a burst
    points = tuple((round(t - times[0], 3), _num(row.get("count")) or 0.0, _num(row.get("rate_hz")) or 0.0,
                    _num(row.get("diameter")) or 0.0, _num(row.get("cv_percent")) or 0.0)
                   for t, row in zip(times, rows))
    return LiveQC(
        rate_hz=float(median(rates)) if rates else 0.0, rate_half=_num(last.get("rate_half")) or 0.0,
        diameter=float(median(diameters)) if diameters else 0.0, diameter_half=_num(last.get("diameter_half")) or 0.0,
        diameter_unit=str(last.get("diameter_unit") or "px"),
        cv_percent=_num(last.get("cv_percent")) or 0.0, cv_half=_num(last.get("cv_half")) or 0.0,
        count=max(counts) if counts else 0.0, bursts=len(rows), state=str(last.get("state") or ""),
        run=Path(run_dir).name, source_path=str(video_path), points=points,
    )


def state_html(state: str) -> str:
    """The rig's state of a recording in its colour: STABLE green, UNSTABLE red, SETTLING amber."""
    colour = theme.QC_STATE_COLORS.get(str(state).upper(), theme.PALETTE.text_muted)
    return f'<span class="qc-state" style="color:{colour}">{html.escape(str(state))}</span>'


def live_cells(live: LiveQC, sample_id: str, color: str, *, active: bool = True) -> dict[str, str]:
    """The live QC as a row of the results table, under the analysis of the same video."""
    off = "" if active else " results-off"

    def cell(inner: str) -> str:
        return f'<div class="results-cell results-live{off}">{inner}</div>'

    def pm(value: float, half: float, digits: int) -> str:
        text = f"{value:.{digits}f}" + (f" ± {half:.{digits}f}" if half else "")
        return f'<span class="n">{text}</span>'

    swatch = f'<span class="swatch" style="background:{color}"></span>' if sample_id else "↳ "
    return {
        "sample": cell(f'{swatch}{html.escape(sample_id) if sample_id else "live"}'
                       f'<div class="sub">on the rig, {live.bursts} bursts</div>'),
        "droplets": cell(f'{live.count:,.0f}'.replace(",", " ") + '<div class="sub">counted live</div>'),
        "inclusions": cell("—"),
        "diameter": cell(pm(live.diameter, live.diameter_half, 1) + f'<div class="sub">{html.escape(live.diameter_unit)}</div>'
                         if live.diameter else "—"),
        "area": cell("—"),
        "cv": cell(pm(live.cv_percent, live.cv_half, 1) if live.cv_percent else "—"),
        "rate": cell(pm(live.rate_hz, live.rate_half, 0) if live.rate_hz else "—"),
        "speed": cell("—"),
        "analysed": cell(f'{state_html(live.state)}<div class="sub">while recording</div>'),
    }


RESULT_FIELDS = ("sample", "droplets", "inclusions", "diameter", "area", "cv", "rate", "speed", "analysed")
RESULT_LABELS = {"sample": "Sample", "droplets": "Droplets", "inclusions": "Inclusions", "diameter": "Diameter, µm",
                 "area": "Area, µm²", "cv": "CV, %", "rate": "Rate, /s", "speed": "Speed, mm/s",
                 "analysed": "Analysed"}


def results_cells(result: FileResult, color: str, *, active: bool = True) -> dict[str, str]:
    """One file's row of the results table, each cell as HTML: the number with its distribution."""
    off = "" if active else " results-off"

    def cell(inner: str) -> str:
        return f'<div class="results-cell{off}">{inner}</div>'

    with_inclusions = sum(1 for count in result.inclusion_counts if count)
    summary = settings_summary(result.settings)
    return {
        "sample": cell(f'<span class="swatch" style="background:{color}"></span>{html.escape(result.sample_id)}'
                       f'<div class="sub">{html.escape(result.engine)}</div>'),
        "droplets": cell(f'{result.droplets}<div class="sub">{result.frames} frames</div>'),
        "inclusions": cell(f'{result.inclusions}<div class="sub">in {with_inclusions} droplets</div>'
                           + spark(result.inclusion_counts)) if result.inclusion_counts else cell("—"),
        "diameter": cell(_distribution(result.mean_diameter, result.std_diameter, result.diameters, 1)),
        "area": cell(_distribution(result.mean_area, result.std_area, result.areas, 0)),
        "cv": cell(f"{result.cv_percent:.1f}" if result.diameters else "—"),
        "rate": cell((_distribution(result.rate_hz, 0.0, [1.0 / p for p in result.periods_s if p > 0], 1)
                      + (f'<div class="sub" title="{html.escape(result.rate_note)}">{html.escape(result.rate_note)}</div>'
                         if result.rate_note else "")
                      + (f'<div class="sub">expected {result.expected_hz:.0f} from {result.dispersed_ul_min:g} µL/min'
                         '</div>' if result.expected_hz else "")) if result.rate_hz else "—"),
        "speed": cell(_distribution(result.speed_mm_s, 0.0, result.speeds_mm_s, 2) if result.speed_mm_s else "—"),
        "analysed": cell(f'{html.escape(result.analyzed_at or "earlier")}'
                         f'<div class="sub" title="{html.escape(summary)}">{html.escape(_short(summary, 48))}</div>'),
    }


def results_table_html(results: list[FileResult]) -> str:
    """The results as one HTML table (what the per-file rows hold, without the Use column)."""
    if not results:
        return ""
    fields = [f for f in RESULT_FIELDS if f != "inclusions" or any(r.inclusion_counts for r in results)]
    head = "".join(f"<th>{html.escape(RESULT_LABELS[f])}</th>" for f in fields)
    body = "".join("<tr>" + "".join(f"<td>{results_cells(r, chart_color(i))[f]}</td>" for f in fields) + "</tr>"
                   for i, r in enumerate(results))
    return f'<table class="results"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _distribution(value: float, sd: float, samples, digits: int) -> str:
    if not value and not samples:
        return "—"
    text = f"{value:.{digits}f} ± {sd:.{digits}f}" if sd else f"{value:.{digits}f}"
    return f'<span class="n">{text}</span>{spark(samples)}'


# -- plots -----------------------------------------------------------------------------

def chart_color(index: int) -> str:
    return theme.CHART_COLORS[index % len(theme.CHART_COLORS)]


def short_names(names: list[str], *, keep: int = 12) -> dict[str, str]:
    """Each name without the start they all share, so plots say what differs between the samples.

    Recordings are named run_<id>_step01_<stamp>: with the run id gone, step01 and its stamp remain.
    """
    distinct = list(dict.fromkeys(names))
    if len(distinct) < 2:
        return {name: name for name in names}
    prefix = 0
    while all(len(name) > prefix and name[prefix] == distinct[0][prefix] for name in distinct):
        prefix += 1
    shared = distinct[0][:prefix]
    prefix = min(len(shared), min(len(name) for name in distinct) - keep)
    prefix = max(shared[:prefix].rfind(sep) + 1 for sep in "_ -")    # cut at a word, not mid-way through one
    if prefix < 4:
        return {name: name for name in names}
    return {name: name[prefix:] for name in names}


def _name_key(result: Any) -> tuple[str, str, str, str]:
    return (result.run, result.source_path, result.sample_id, result.file_id)


def _file_key(item: Any) -> tuple[str, str]:
    """The file an analysed result or a live line is of: the two share one style."""
    return (item.run, item.source_path)


def _names(results: list[Any]) -> dict[tuple[str, str, str, str], str]:
    """A name per plotted series, analysed or live: the sample's, shortened to what differs, "live" added
    for the rig's line, and told apart by the run's time and then a number when two files share one."""
    short = short_names([result.sample_id for result in results])
    names: dict[tuple[str, str, str, str], str] = {}
    taken: dict[str, int] = {}
    for result in results:
        name = short[result.sample_id] + (" live" if isinstance(result, LiveQC) else "")
        if name in taken.values() or name in taken:
            stamp = result.run[:15]
            if len(stamp) == 15 and stamp[8] == "_" and stamp[9:11].isdigit() and f"{stamp[9:11]}:{stamp[11:13]}" not in name:
                name = f"{name} {stamp[9:11]}:{stamp[11:13]}"
        base, n = name, 2
        while name in taken:
            name, n = f"{base} #{n}", n + 1
        taken[name] = 1
        names[_name_key(result)] = name
    return names


def plot_styles(items: Sequence[Any], listed: Sequence[str] | None = None) -> dict[tuple[str, str], SeriesStyle]:
    """A style per file among the items: its sample's group hue and replicate shade (see series_style).

    Two files given one sample name are replicates of it. LISTED is every sample name on the screen,
    in order, so a group keeps its hue whichever files are ticked.
    """
    labels: dict[tuple[str, str], str] = {}
    seen: dict[str, int] = {}
    for item in items:
        key = _file_key(item)
        if key in labels:
            continue
        n = seen.get(item.sample_id, 0) + 1
        seen[item.sample_id] = n
        labels[key] = item.sample_id if n == 1 else f"{item.sample_id} #{n}"
    styles = series_styles(list(labels.values()), listed=list(listed or []))
    return {key: styles[label] for key, label in labels.items()}


def histogram_edges(values: list[float], *, max_bins: int = 40, min_bins: int = 8) -> list[float]:
    """Bin edges by the Freedman-Diaconis rule, between a sensible minimum and maximum of bins."""
    array = np.asarray(values, dtype=float)
    low, high = float(array.min()), float(array.max())
    if high <= low:
        low, high = low - 0.5, high + 0.5
    q1, q3 = np.percentile(array, (25, 75))
    width = 2 * (q3 - q1) * len(array) ** (-1 / 3)
    bins = int(math.ceil((high - low) / width)) if width > 0 else max_bins
    bins = max(min_bins, min(max_bins, bins))
    return [low + (high - low) * i / bins for i in range(bins + 1)]


def bin_labels(edges: list[float]) -> list[str]:
    """Bin centres written to the precision the bin width needs, so neighbours read differently."""
    width = edges[1] - edges[0] if len(edges) > 1 else 1.0
    digits = max(0, -math.floor(math.log10(width))) if width > 0 else 0
    return [f"{(a + b) / 2:.{digits}f}" for a, b in zip(edges, edges[1:])]


def _plotted(results: Sequence[FileResult], lives: Sequence[LiveQC] = (),
             listed: Sequence[str] | None = None) -> list[tuple[Any, SeriesStyle, str]]:
    """Every series to draw with its style and its name: the analysed files first, then the live lines."""
    items: list[Any] = [*results, *lives]
    names, styles = _names(items), plot_styles(items, listed)
    return [(item, styles[_file_key(item)], names[_name_key(item)]) for item in items]


def diameter_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                   listed: Sequence[str] | None = None) -> dict[str, Any]:
    plotted = [(item, style, name) for item, style, name in _plotted(results, (), listed) if item.diameters]
    values = [value for item, _style, _name in plotted for value in item.diameters]
    if not values:
        return _empty_chart("Diameter distribution (µm)")
    edges = histogram_edges(values)
    labels = bin_labels(edges)
    width = edges[1] - edges[0]
    series = []
    for item, style, name in plotted:
        counts = [0] * len(labels)
        for value in item.diameters:
            counts[min(len(labels) - 1, int((value - edges[0]) / width))] += 1
        series.append({"name": name, "type": "bar", "data": counts, "barMaxWidth": 18,
                       "itemStyle": {**style.item(), "opacity": 0.82}, "emphasis": {"focus": "series"}})
    return {
        "title": _title("Diameter distribution (µm)"),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend(),
        "grid": _grid(bottom=58),
        "xAxis": {"type": "category", "data": labels,
                  "axisLabel": {"rotate": 45, "fontSize": 10, "hideOverlap": True}},
        "yAxis": _value_axis("droplets"),
        "series": series,
    }


def timeline_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                   listed: Sequence[str] | None = None) -> dict[str, Any]:
    """Droplets counted against time since each file's first frame: the slope is the rate.

    Files start at different frames of different recordings, so each is laid from its own
    start; a steady train is a straight line, a change of rate a bend, a stop a plateau.
    The rig's live count runs over the whole recording, thinner, from its first burst.
    """
    series = []
    for item, style, name in _plotted(results, lives, listed):
        if isinstance(item, LiveQC):
            points = [[t, count] for t, count, _rate, _d, _cv in item.points]
        else:
            points = [[round(t, 4), n] for n, t in enumerate(item.arrivals_s, start=1)]
        if not points:
            continue
        if len(points) > 4000:
            step = len(points) // 4000 + 1
            points = points[::step] + [points[-1]]
        series.append(_line(name, points, style, live=isinstance(item, LiveQC)))
    return _time_chart("Droplets over time", "droplets", series)


def diameter_time_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                        listed: Sequence[str] | None = None) -> dict[str, Any]:
    """The diameter as the recording went on: the analysis's median per second of video, the rig's
    per burst (thinner); a live line in px, from a rig without a scale, is left out."""
    series = []
    for item, style, name in _plotted(results, lives, listed):
        if isinstance(item, LiveQC):
            points = [[t, d] for t, _count, _rate, d, _cv in item.points if d] if item.diameter_unit == "um" else []
        else:
            points = _per_second(item.timed_diameters)
        if points:
            series.append(_line(name, points, style, live=isinstance(item, LiveQC)))
    return _time_chart("Diameter over time (µm)", "µm", series)


def rate_time_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                    listed: Sequence[str] | None = None) -> dict[str, Any]:
    """The generation rate as the recording went on: droplets the analysis saw arrive per second of
    video, and the rate the rig measured at each burst (thinner)."""
    series = []
    for item, style, name in _plotted(results, lives, listed):
        if isinstance(item, LiveQC):
            points = [[t, rate] for t, _count, rate, _d, _cv in item.points if rate]
        else:
            points = _per_second([(t, 1.0) for t in item.arrivals_s], reduce=len) if len(item.arrivals_s) > 1 else []
        if points:
            series.append(_line(name, points, style, live=isinstance(item, LiveQC)))
    return _time_chart("Rate over time (/s)", "/s", series)


def _line(name: str, points: list, style: SeriesStyle, *, live: bool = False) -> dict[str, Any]:
    return {"name": name, "type": "line", "showSymbol": style.marked, "symbol": style.symbol, "symbolSize": 6,
            "silent": True, "data": points, "lineStyle": style.line(live=live),
            "itemStyle": {"color": style.color, **({"opacity": 0.8} if live else {})}}


def _per_second(points: Sequence[tuple[float, float]], *, reduce: Callable = median) -> list[list[float]]:
    """Points (s, value) folded into one a second: the median of the second (or its count), at its middle."""
    seconds: dict[int, list[float]] = {}
    for t, value in points:
        seconds.setdefault(int(t), []).append(value)
    return [[second + 0.5, round(float(reduce(values)), 3)] for second, values in sorted(seconds.items())]


def _time_chart(title: str, unit: str, series: list[dict[str, Any]], *,
                xname: str = "s since the first frame") -> dict[str, Any]:
    return {
        "title": _title(title),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend(),
        "grid": _grid(right=28),
        "xAxis": _value_axis(xname),
        "yAxis": _value_axis(unit),
        "series": series,
    }


def positions_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                    listed: Sequence[str] | None = None) -> dict[str, Any]:
    return scatter_chart("Droplet positions (px)", "x", "y", results, lambda d: [d[1], d[2]], invert_y=True,
                         listed=listed)


def area_x_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                 listed: Sequence[str] | None = None) -> dict[str, Any]:
    return scatter_chart("Area along x (µm²)", "x px", "µm²", results, lambda d: [d[1], d[3]] if d[3] else None,
                         listed=listed)


def scatter_chart(title: str, xname: str, yname: str, results: list[FileResult],
                  project: Callable[[tuple[float, float, float, float]], list[float] | None],
                  *, invert_y: bool = False, listed: Sequence[str] | None = None) -> dict[str, Any]:
    series = []
    for item, style, name in _plotted(results[:8], (), listed):
        data = [point for point in (project(detection) for detection in item.detections) if point is not None]
        if data:
            series.append({"name": name, "type": "scatter", "symbol": style.symbol, "symbolSize": 4, "data": data,
                           "itemStyle": {"color": style.color}})
    return {
        "title": _title(title),
        "animation": False,
        "tooltip": {},
        "legend": _legend(),
        "grid": _grid(),
        "xAxis": _value_axis(xname),
        "yAxis": _value_axis(yname, inverse=invert_y),
        "series": series,
    }


def per_sample_chart(title: str, unit: str, results: Sequence[FileResult], lives: Sequence[LiveQC],
                     analysed: Callable[[FileResult], float | None], measured: Callable[[LiveQC], float | None],
                     *, listed: Sequence[str] | None = None, digits: int = 1) -> dict[str, Any]:
    """One place per ticked file: the analysis's value as a bar and, hatched beside it, what the rig
    measured live; a file with only one of them shows that one."""
    files: dict[tuple[str, str], dict[str, Any]] = {}
    for item, style, name in _plotted(results, lives, listed):
        entry = files.setdefault(_file_key(item), {"name": name.removesuffix(" live"), "style": style,
                                                   "analysed": None, "live": None})
        if isinstance(item, LiveQC):
            entry["live"] = measured(item)
        else:
            entry["name"], entry["analysed"] = name, analysed(item)
    shown = [entry for entry in files.values() if entry["analysed"] is not None or entry["live"] is not None]
    if not shown:
        return _empty_chart(title)

    def bars(kind: str) -> dict[str, Any]:
        return {"name": kind, "type": "bar", "barMaxWidth": 28, "barGap": "10%",
                "data": [{"value": round(entry[kind], digits) if entry[kind] is not None else None,
                          "itemStyle": entry["style"].item(live=kind == "live")} for entry in shown],
                "label": {"show": True, "position": "top", "fontSize": 9, "formatter": "{c}"}}

    series = [bars("analysed")] if any(entry["analysed"] is not None for entry in shown) else []
    if any(entry["live"] is not None for entry in shown):
        series.append(bars("live"))
    return {
        "title": _title(title),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend() if len(series) > 1 else {"show": False},
        "grid": _grid(bottom=48),
        "xAxis": {"type": "category", "data": [entry["name"] for entry in shown],
                  "axisLabel": {"rotate": 25, "fontSize": 10, "hideOverlap": True, "width": 90,
                                "overflow": "truncate"}},
        "yAxis": _value_axis(unit),
        "series": series,
    }


def diameter_per_sample_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                              listed: Sequence[str] | None = None) -> dict[str, Any]:
    return per_sample_chart("Diameter per sample (µm)", "µm", results, lives,
                            lambda r: r.mean_diameter if r.diameters else None,
                            lambda live: live.diameter if live.diameter and live.diameter_unit == "um" else None,
                            listed=listed)


def cv_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
             listed: Sequence[str] | None = None) -> dict[str, Any]:
    """One place per sample: the spread of its droplet diameters as CV in percent, and the CV the rig
    had over the whole recording."""
    return per_sample_chart("CV of diameter per sample (%)", "CV %", results, lives,
                            lambda r: r.cv_percent if r.diameters else None, lambda live: live.cv_percent or None,
                            listed=listed, digits=2)


def rate_per_sample_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                          listed: Sequence[str] | None = None) -> dict[str, Any]:
    return per_sample_chart("Rate per sample (/s)", "/s", results, lives, lambda r: r.rate_hz or None,
                            lambda live: live.rate_hz or None, listed=listed, digits=0)


def inclusions_chart(results: list[FileResult], lives: Sequence[LiveQC] = (), *,
                     listed: Sequence[str] | None = None) -> dict[str, Any]:
    """How many droplets of each sample hold 0, 1, 2 ... inclusions; empty for samples without any."""
    keys = sorted({count for result in results for count in result.inclusion_counts})
    series = []
    for item, style, name in _plotted(results, (), listed):
        if not item.inclusion_counts:
            continue
        counts = {key: 0 for key in keys}
        for value in item.inclusion_counts:
            counts[value] += 1
        series.append({"name": name, "type": "bar", "barMaxWidth": 24,
                       "data": [counts[key] for key in keys], "itemStyle": style.item()})
    return {
        "title": _title("Inclusions per droplet"),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend(),
        "grid": _grid(bottom=44),
        "xAxis": {"type": "category", "data": [str(key) for key in keys], "name": "inclusions",
                  "nameLocation": "middle", "nameGap": 22, "axisLabel": {"fontSize": 10}},
        "yAxis": _value_axis("droplets"),
        "series": series,
    }


def has_inclusions(results: list[FileResult]) -> bool:
    return any(result.inclusion_counts for result in results)


RESULT_SECTIONS = (("aggregated", "Aggregated"), ("opencv", "OpenCV"), ("cellpose", "Cellpose"),
                   ("live", "Live QC + Fluidics"))
_CHARTS = {
    "diameter_distribution": diameter_chart, "cv_per_sample": cv_chart, "droplet_timeline": timeline_chart,
    "diameter_over_time": diameter_time_chart, "rate_over_time": rate_time_chart,
    "droplet_positions": positions_chart, "area_along_x": area_x_chart,
    "inclusions_per_droplet": inclusions_chart, "diameter_per_sample": diameter_per_sample_chart,
    "rate_per_sample": rate_per_sample_chart,
}
_VIEW_CHARTS = {
    "all": ("diameter_distribution", "cv_per_sample", "droplet_timeline", "diameter_over_time", "rate_over_time",
            "droplet_positions", "area_along_x", "inclusions_per_droplet"),
    "aggregated": ("diameter_per_sample", "cv_per_sample", "rate_per_sample"),
    "opencv": ("diameter_distribution", "droplet_timeline", "diameter_over_time", "rate_over_time",
               "droplet_positions", "area_along_x"),
    "cellpose": ("diameter_distribution", "inclusions_per_droplet"),
}


def result_charts(results: list[FileResult], lives: Sequence[LiveQC] = (), *, view: str = "all",
                  listed: Sequence[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """The plots of a Results section, named for export: only those the ticked files have data for.

    aggregated sets every file side by side, one number each, the rig's live number beside the
    analysis's; opencv has the video analyses' distributions and time series; cellpose the image
    folders' diameters and inclusions. The live section is per run: see runs_charts. A plot with
    nothing to show is not shown.
    """
    if view == "opencv":
        results, lives = [r for r in results if r.engine == "opencv"], ()
    elif view == "cellpose":
        results, lives = [r for r in results if r.engine == "cellpose"], ()
    charts = []
    for name in _VIEW_CHARTS.get(view, _VIEW_CHARTS["all"]):
        chart = _CHARTS[name](results, lives, listed=listed)
        if any(series.get("data") for series in chart.get("series", ())):
            charts.append((name, chart))
    return charts


CHANNEL_DASHES: tuple[Any, ...] = ("solid", "dashed", [12, 4, 3, 4])     # Oil L, Cells M, Beads M, as on the rig's plots


@dataclass(frozen=True)
class RunPlot:
    """One run to draw on the live plots: its folder, its name, and its ticked recordings by id, each
    with its sample's name and style."""
    run_dir: str
    label: str = ""
    files: dict[str, tuple[str, SeriesStyle]] = field(default_factory=dict)


def run_label(run_dir: str | Path) -> str:
    """A run folder's name as the screen says it: the name with the run's time, "dropseq_1n 16:43"."""
    name = Path(run_dir).name
    stamp, _sep, rest = name.partition("_")
    if len(name) > 15 and name[8] == "_" and name[:8].isdigit() and name[9:15].isdigit():
        return f"{name[16:]} {name[9:11]}:{name[11:13]}"
    return name


def run_charts(run_dir: str | Path, files: dict[str, tuple[str, SeriesStyle]] | None = None, *,
               label: str = "") -> list[tuple[str, dict[str, Any]]]:
    return runs_charts([RunPlot(str(run_dir), label or run_label(run_dir), dict(files or {}))])


def runs_charts(runs: Sequence[RunPlot]) -> list[tuple[str, dict[str, Any]]]:
    """The ticked recordings' steps, stacked, as the rig's Review stage shows them: every step from its
    own start, so steps and runs overlay; pressure and flow per channel, then the droplet rate and CV.

    A recording is drawn in its sample's colour, a channel in its line pattern. Its step is the one
    its bursts fall in; a recording outside any step (a run without events) is its bursts' span.
    """
    from admet.core.runs import Run
    from admet.core.timeline import QC_LIMIT_PERCENT, read_fluidics_csv, read_qc_csv, step_segments

    xname = "s since the step began"
    readings: dict[str, list[dict[str, Any]]] = {"pressure": [], "flow": []}
    rate_series: list[dict[str, Any]] = []
    cv_series: list[dict[str, Any]] = []
    for run in runs:
        path = Path(run.run_dir) / "run.csv"
        if not path.is_file():
            continue
        try:
            t, pressures, flows = read_fluidics_csv(path)
            qc = read_qc_csv(path)
        except (OSError, ValueError):
            continue
        if qc is None:
            continue                                          # no recording, nothing a ticked file is of
        record = Run(run.run_dir).read()
        polling = record.get("artifacts", {}).get("polling_origin_monotonic")
        events = [e for e in record.get("events", []) if isinstance(e, dict)] if type(polling) in (int, float) else []
        segments = step_segments(events, polling or 0.0) if events else []
        for chunk in qc.chunks(np.ones(len(qc.t), dtype=bool)):
            recording = qc.recording[chunk[0]]
            if recording not in run.files:
                continue                                      # not ticked
            name, style = run.files[recording]
            style = style or series_styles([name])[name]
            first, last = float(qc.t[chunk[0]]), float(qc.t[chunk[-1]])
            step = next(((s, e) for _n, s, e in segments if s <= last and first <= e), None)
            start, end = step if step else (first, last)
            inside = (t >= start) & (t <= end)
            for key, values_by_channel in (("pressure", pressures), ("flow", flows)):
                for channel, values in enumerate(values_by_channel):
                    points = _thin([[round(float(a) - start, 3), round(float(b), 3)]
                                    for a, b in zip(t[inside], values[inside]) if math.isfinite(b)], 2000)
                    if not points:
                        continue
                    label = FLUIDIC_CHANNEL_LABELS[channel] if channel < len(FLUIDIC_CHANNEL_LABELS) else f"channel {channel}"
                    readings[key].append({"name": f"{name} · {label}", "type": "line", "showSymbol": False,
                                          "silent": True, "data": points,
                                          "lineStyle": {"color": style.color, "width": 1.5,
                                                        "type": CHANNEL_DASHES[channel % len(CHANNEL_DASHES)]},
                                          "itemStyle": {"color": style.color}})
            times = [round(float(value) - start, 3) for value in qc.t[chunk]]
            rate = [[at, round(float(v), 1)] for at, v in zip(times, qc.rate[chunk]) if math.isfinite(v) and v > 0]
            cv = [[at, round(float(v), 2)] for at, v in zip(times, qc.cv[chunk]) if math.isfinite(v) and v > 0]
            if rate:
                rate_series.append(_line(name, rate, style))
            if cv:
                cv_series.append(_line(name, cv, style))
    if cv_series:
        cv_series[0]["markLine"] = {"silent": True, "symbol": "none",
                                    "lineStyle": {"color": theme.PALETTE.danger_hover, "type": "dashed", "width": 1},
                                    "label": {"formatter": f"{QC_LIMIT_PERCENT:g} %", "fontSize": 9},
                                    "data": [{"yAxis": QC_LIMIT_PERCENT}]}
    charts = [("pressure", _time_chart("Pressure (mbar)", "mbar", readings["pressure"], xname=xname)),
              ("flow", _time_chart("Flow (µL/min)", "µL/min", readings["flow"], xname=xname)),
              ("droplets", _time_chart("Droplets (/s)", "/s", rate_series, xname=xname)),
              ("droplet_cv", _time_chart("Droplet CV (%)", "%", cv_series, xname=xname))]
    ends = [point[0] for _name, chart in charts for series in chart["series"] for point in series["data"]]
    for _name, chart in charts:                                   # the four on one clock, as linked plots
        chart["xAxis"].update({"min": 0, "max": math.ceil(max(ends)) if ends else 1})
    return [(name, chart) for name, chart in charts if any(s.get("data") for s in chart["series"])]


def _thin(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    step = len(points) // limit + 1
    return points[::step] + [points[-1]]


AGGREGATED_FIELDS = ("sample", "droplets", "diameter", "cv", "rate", "analysed")


def aggregated_cells(name: str, style: SeriesStyle | None, result: FileResult | None, live: LiveQC | None,
                     *, active: bool = True) -> dict[str, str]:
    """One file's line of the aggregated table: the analysis's number, with the rig's live one under it."""
    off = "" if active else " results-off"
    colour = style.color if style is not None else "var(--text-disabled)"

    def cell(main: str, sub: str = "") -> str:
        inner = f'<span class="n">{main}</span>' + (f'<div class="sub">{sub}</div>' if sub else "")
        return f'<div class="results-cell{off}">{inner}</div>'

    unit = live.diameter_unit if live is not None else "um"
    return {
        "sample": cell(f'<span class="swatch" style="background:{colour}"></span>{html.escape(name)}',
                       html.escape(result.engine if result is not None else "not analysed")),
        "droplets": cell(f"{result.droplets}" if result is not None else "—",
                         f"{live.count:,.0f} live".replace(",", " ") if live is not None else ""),
        "diameter": cell(f"{result.mean_diameter:.1f} ± {result.std_diameter:.1f}"
                         if result is not None and result.diameters else "—",
                         f"{live.diameter:.1f} {html.escape(unit)} live" if live is not None and live.diameter else ""),
        "cv": cell(f"{result.cv_percent:.1f}" if result is not None and result.diameters else "—",
                   f"{live.cv_percent:.1f} live" if live is not None and live.cv_percent else ""),
        "rate": cell(f"{result.rate_hz:.0f}" if result is not None and result.rate_hz else "—",
                     f"{live.rate_hz:.0f} live" if live is not None and live.rate_hz else ""),
        "analysed": cell(html.escape(result.analyzed_at or "earlier") if result is not None else "—",
                         f"{state_html(live.state)} while recording" if live is not None else ""),
    }


def _title(text: str) -> dict[str, Any]:
    return {"text": text, "left": 8, "top": 4, "textStyle": {"fontSize": 13}}


def _legend() -> dict[str, Any]:
    """Under the plot, scrolling, each name cut to fit: a sample name can be a whole file name."""
    return {"bottom": 0, "left": 8, "right": 8, "type": "scroll", "itemWidth": 12, "itemHeight": 8,
            "textStyle": {"fontSize": 9, "width": 120, "overflow": "truncate"}}


def _grid(*, left: int = 58, right: int = 18, top: int = 36, bottom: int = 60) -> dict[str, Any]:
    return {"left": left, "right": right, "top": top, "bottom": bottom, "containLabel": True}


def _value_axis(name: str, *, inverse: bool = False) -> dict[str, Any]:
    axis: dict[str, Any] = {"type": "value", "name": name, "nameLocation": "middle",
                            "nameGap": 34 if name else 22, "axisLabel": {"fontSize": 10, "hideOverlap": True}}
    if inverse:
        axis["inverse"] = True
    return axis


def _empty_chart(title: str) -> dict[str, Any]:
    return {"title": _title(title), "grid": _grid(), "xAxis": {"type": "category", "data": []},
            "yAxis": _value_axis(""), "series": []}


# -- export ----------------------------------------------------------------------------

def export_csv_zip(stored: list[StoredResult], results: list[FileResult],
                   corrections: dict[tuple[str, str], dict[str, Any]] | None = None,
                   live: dict[tuple[str, str, str], LiveQC] | None = None) -> bytes:
    """A zip: summary.csv over the results, and every droplet row of each file as its own CSV.

    Imaging droplets switched off by hand are left out; corrected inclusion counts are written
    with the raw count beside them.
    """
    wanted = {(result.run, result.engine, result.source_path): result.sample_id for result in results}
    files: dict[str, bytes] = {"summary.csv": _summary_csv(results, live)}
    for item in stored:
        source = str(item.source_path)
        sample_id = wanted.get((item.run_name, item.engine, source))
        if sample_id is None:
            continue
        rows = [row for row in iter_rows(item.rows_path)
                if row.get("kind") in {"detection", "droplet", "track", "droplet_geometry"}]
        fixes = (corrections or {}).get((item.engine, source), {})
        files[f"droplets/{_safe(item.run_name or 'files')}_{_safe(sample_id)}_{item.engine}.csv"] = _rows_csv(rows, fixes)
    return zip_bytes(files)


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _summary_csv(results: list[FileResult], live: dict[tuple[str, str, str], LiveQC] | None = None) -> bytes:
    """One line per result; the live QC of the same video, when the rig measured one, in the last columns."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["run", "sample", "engine", "source", "analysed_at", "frames", "droplets", "inclusions",
                     "mean_diameter_um", "sd_diameter_um", "cv_percent", "mean_area_um2", "sd_area_um2",
                     "rate_hz", "expected_hz", "dispersed_ul_min", "speed_mm_s", "fps", "um_per_px", "settings",
                     "live_rate_hz", "live_diameter", "live_diameter_unit", "live_cv_percent", "live_state"])
    for result in results:
        measured = (live or {}).get((result.run, result.engine, result.source_path))
        writer.writerow([result.run, result.sample_id, result.engine, result.source_path, result.analyzed_at,
                         result.frames, result.droplets, result.inclusions,
                         _fmt(result.mean_diameter, 3), _fmt(result.std_diameter, 3), _fmt(result.cv_percent, 3),
                         _fmt(result.mean_area, 2), _fmt(result.std_area, 2), _fmt(result.rate_hz, 3),
                         _fmt(result.expected_hz, 1), _fmt(result.dispersed_ul_min, 2),
                         _fmt(result.speed_mm_s, 3), _fmt(result.fps, 3), _fmt(result.scale, 4),
                         settings_summary(result.settings),
                         *((_fmt(measured.rate_hz, 3), _fmt(measured.diameter, 1), measured.diameter_unit,
                            _fmt(measured.cv_percent, 1), measured.state) if measured else ("", "", "", "", ""))])
    return out.getvalue().encode("utf-8")


def _rows_csv(rows: list[dict[str, Any]], fixes: dict[str, Any] | None = None) -> bytes:
    keys: list[str] = []
    for row in rows:
        for key in _values(row):
            if key not in keys and not key.startswith("contour"):
                keys.append(key)
    if fixes and "inclusions" in keys:
        keys.append("inclusions_raw")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["kind", *keys])
    for row in rows:
        values = dict(_values(row))
        if fixes and row.get("kind") == "droplet":
            fix = fixes.get(correction_key(values.get("frame"), values.get("droplet_id")))
            if fix and fix.get("disabled"):
                continue
            if fix:
                values["inclusions_raw"] = values.get("inclusions")
                values["inclusions"] = corrected_inclusions(values, fix)
        writer.writerow([row.get("kind", ""), *(_cell(values.get(key)) for key in keys)])
    return out.getvalue().encode("utf-8")


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    return "" if value is None else value


# -- small helpers ---------------------------------------------------------------------

def local_time(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    """A row's file: its id, or the sample it was written for when rows carry no file id."""
    return (str(row.get("engine") or ""),
            str(row.get("file_id") or row.get("item_id") or row.get("sample_id") or "sample"))


def _run_settings(stored: StoredResult, analysis: dict[str, Any], detection: dict[str, Any]) -> dict[str, Any]:
    settings = dict(stored.settings)
    for key in ("microns_per_pixel", "fps"):
        if analysis.get(key) not in {None, ""}:
            settings[key] = analysis[key]
    for key, name in (("k_sigma", "k_sigma"), ("satellite", "satellite_px"), ("min_area", "min_area"),
                      ("skip_left_pct", "skip_left_pct"), ("skip_right_pct", "skip_right_pct"),
                      ("gate_pct", "gate_pct")):
        if detection.get(key) not in {None, ""}:
            settings.setdefault(name, detection[key])
    return settings


def _values(row: dict[str, Any]) -> dict[str, Any]:
    values = row.get("values")
    return values if isinstance(values, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any, digits: int = 2) -> str:
    number = _num(value)
    if number is None:
        return str(value)
    if number == int(number) and digits <= 2:
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value) or "file"
