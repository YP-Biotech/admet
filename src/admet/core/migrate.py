"""Old data laid out as admet control writes it now: run folders, each with its run.db.

An old project, a .admetp folder:

    records/protocols/run_<id>/summary.json   ->  runs/<stamp>_<name>/run.db, its record with its events
    records/fluidics/run_<id>_<stamp>.csv     ->  run.db's timeline, the readings
    records/camera/run_<id>.qc.csv            ->  run.db's timeline too, the QC rows put on the polling clock
    records/camera/run_<id>_stepNN_<stamp>.avi -> the run folder, moved (or copied)

A run folder as control wrote it before run.db, with run.json and run.csv: its run.db is made
beside them from the two, and the two are left where they are, to be removed once checked.

A video named after a run, run_<id>_<stamp>.avi, belongs to that run even when its summary
does not list it. A video of no protocol run is a recording of its own. Only completed runs are
laid out: a failed or unfinished run stays in the project with its files. A run already laid out
is left alone, so a project can be migrated again after a stop. The analyze registry, when
given, watches the folder and lists every video. The project's rig settings go into the home's
rig.json only when asked, so one project's rig is the one kept.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from admet.core import rundb
from admet.core.home import RIG_KEYS
from admet.core.runs import RECORD, RUNS_DIR, Run, run_name

STAMP = re.compile(r"_(\d{8}_\d{6})\.csv$")


@dataclass
class Migration:
    """What a migration did: the run folders made, the ones found there already, the videos moved,
    the runs left in the project because they never completed."""
    into: Path
    made: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    left: dict[str, str] = field(default_factory=dict)       # run folder -> its state
    videos: int = 0
    registered: int = 0

    def lines(self) -> list[str]:
        lines = [f"{len(self.made)} run(s) laid out in {self.into / RUNS_DIR}, {self.videos} video(s) moved"]
        if self.skipped:
            lines.append(f"{len(self.skipped)} run(s) there already, left alone")
        if self.left:
            states = sorted(set(self.left.values()))
            counts = ", ".join(f"{sum(1 for s in self.left.values() if s == state)} {state}" for state in states)
            lines.append(f"{len(self.left)} run(s) not completed left in the project: {counts}")
        if self.registered:
            lines.append(f"{self.registered} video(s) listed for analyze")
        return lines


def rig_settings(project: str | Path) -> dict[str, Any]:
    """The project's rig settings as the home keeps them: the keys of rig.json found in its manifest."""
    project = Path(project)
    try:
        manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{project} is not a project: no manifest.json") from exc
    metadata = manifest.get("metadata") if isinstance(manifest, dict) else None
    if not isinstance(metadata, dict):
        return {}
    return {key: metadata[key] for key in RIG_KEYS if metadata.get(key) not in (None, "", [], {})}


def migrate_project(project: str | Path, into: str | Path, *, copy: bool = False, registry: Any = None) -> Migration:
    """Lay the project's runs out under INTO/runs; videos are moved, or copied with COPY."""
    project, into = Path(project), Path(into)
    if not (project / "manifest.json").is_file():
        raise ValueError(f"{project} is not a project: no manifest.json")
    report = Migration(into=into)
    runs_dir = into / RUNS_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    of_protocols: set[str] = set()
    for run_dir in sorted((project / "records" / "protocols").glob("*/")):
        summary_path = run_dir / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(summary, dict):
            continue
        artifacts = summary.get("artifacts") if isinstance(summary.get("artifacts"), dict) else {}
        videos = [v for v in artifacts.get("videos", []) if isinstance(v, dict) and v.get("video_path")]
        listed = {Path(str(v["video_path"])).name for v in videos}
        for path in sorted((project / "records" / "camera").glob(f"{run_dir.name}_*.avi")):
            if path.name not in listed:                      # named after the run, unlisted: the run's own
                videos.append({"video_path": f"records/camera/{path.name}"})
        of_protocols.update(str(v["video_path"]) for v in videos)
        state = str(summary.get("state") or "")
        if state != "completed":
            report.left[run_dir.name] = state or "unknown"
            continue
        _lay_out_protocol_run(project, run_dir, summary, artifacts, videos, runs_dir, report, copy)
    for entry in _loose_recordings(project, of_protocols):
        _lay_out_recording(project, entry, runs_dir, report, copy)
    if registry is not None:
        registry.add_root(into)
        report.registered = len(registry.scan(into))
    return report


def _lay_out_protocol_run(project: Path, run_dir: Path, summary: dict[str, Any], artifacts: dict[str, Any],
                          videos: list[dict[str, Any]], runs_dir: Path, report: Migration, copy: bool) -> None:
    protocol = (summary.get("normalized_settings") or {}).get("protocol") or {}
    name = run_name(summary.get("tag") or protocol.get("name") or run_dir.name)
    fluidics = project / str(artifacts.get("fluidics_csv")) if artifacts.get("fluidics_csv") else None
    if fluidics is None or not fluidics.is_file():           # the summary's path is stale: the file is named after the run
        fluidics = next(iter(sorted((project / "records" / "fluidics").glob(f"{run_dir.name}_*.csv"))), fluidics)
    stamp = _stamp(fluidics, summary.get("executed_at") or summary.get("created_at"))
    folder = runs_dir / f"{stamp}_{name}"
    if (folder / RECORD).is_file():
        report.skipped.append(folder.name)
        return
    folder.mkdir(exist_ok=True)
    kept_videos = []
    for video in videos:
        source = project / str(video["video_path"])
        if not source.is_file():
            continue
        _place(source, folder / source.name, copy)
        kept_videos.append({**video, "video_path": source.name, "encoded": True})
        report.videos += 1
    record = {key: value for key, value in summary.items() if key != "artifacts"}
    record.update({
        "run_id": folder.name, "name": name, "kind": "protocol", "events": [],
        "measurements": _measurements(run_dir), "calculations": _calculations(run_dir),
        "artifacts": {"timeline": RECORD, "recording_closed": True,
                      "polling_origin_monotonic": artifacts.get("polling_origin_monotonic"), "videos": kept_videos},
    })
    run = Run(folder)
    run.write(record)
    for line in _lines(run_dir / "events.jsonl"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            run.event(event)
    qc = project / str(artifacts["qc_csv"]) if artifacts.get("qc_csv") else project / "records" / "camera" / f"{run_dir.name}.qc.csv"
    _write_timeline(run.db_path, fluidics, qc)
    report.made.append(folder)


def _loose_recordings(project: Path, of_protocols: set[str]) -> list[dict[str, Any]]:
    """The manifest's videos that belong to no protocol run: recordings started by hand."""
    try:
        manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    files = manifest.get("files") if isinstance(manifest, dict) else None
    loose = []
    for entry in files if isinstance(files, list) else []:
        if not isinstance(entry, dict) or entry.get("role") != "control_video":
            continue
        if str(entry.get("path") or "") in of_protocols:
            continue
        loose.append(entry)
    return loose


def _lay_out_recording(project: Path, entry: dict[str, Any], runs_dir: Path, report: Migration, copy: bool) -> None:
    source = project / str(entry.get("path") or "")
    if not source.is_file():
        return
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    fluidics = project / str(metadata["fluidics_csv"]) if metadata.get("fluidics_csv") else None
    stem = source.stem
    name = run_name(re.sub(r"_\d{8}_\d{6}$", "", stem) or stem)
    stamp = _stamp(fluidics, metadata.get("started_at")) if fluidics and fluidics.is_file() else \
        _stamp(None, metadata.get("started_at")) if metadata.get("started_at") else _stamp(Path(stem + ".csv"), None)
    folder = runs_dir / f"{stamp}_{name}"
    if (folder / RECORD).is_file():
        report.skipped.append(folder.name)
        return
    folder.mkdir(exist_ok=True)
    _place(source, folder / source.name, copy)
    report.videos += 1
    recording = {key: value for key, value in metadata.items() if key != "fluidics_csv"}
    recording.update({"video_path": source.name, "timeline": RECORD, "recording_id": stem, "encoded": True})
    run = Run(folder)
    run.write({"run_id": folder.name, "name": name, "kind": "recording", "state": "completed",
               "started_at": metadata.get("started_at") or "", "stopped_at": metadata.get("stopped_at") or "",
               "artifacts": {"timeline": RECORD, "recording_closed": True}, "recording": recording,
               "events": [], "measurements": None, "calculations": []})
    _write_timeline(run.db_path, fluidics, project / "records" / "camera" / f"{stem}.qc.csv")
    report.made.append(folder)


def _write_timeline(path: Path, fluidics: Path | None, qc: Path | None) -> None:
    """The readings, then the QC rows with their elapsed_s put on the readings' clock by their timestamps."""
    rows: list[dict[str, str]] = []
    columns: list[str] = []
    origin: tuple[float, float] | None = None
    for source in (fluidics, qc):
        if source is None or not source.is_file():
            continue
        with source.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns += [c for c in reader.fieldnames or [] if c not in columns]
            for row in reader:
                clock = _seconds_of_day(row.get("timestamp"))
                if source is fluidics and origin is None and clock is not None and _number(row.get("elapsed_s")) is not None:
                    origin = (clock, float(row["elapsed_s"]))
                if source is qc and origin is not None and clock is not None:
                    row["elapsed_s"] = f"{origin[1] + clock - origin[0]:.3f}"
                rows.append(row)
    if not columns:
        return
    _timeline_into(path, columns, rows)


def _timeline_into(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """CSV rows into the run's database, as many channels as the columns name."""
    rundb.write_timeline(path, rows, *rundb.channel_counts(columns))


@dataclass
class RunsMigration:
    """What converting run folders did: the run.db made, the folders that had one already, the ones that
    could not be read."""
    made: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def lines(self) -> list[str]:
        lines = [f"{len(self.made)} run(s) given a run.db; their run.json and run.csv are left beside it"]
        if self.skipped:
            lines.append(f"{len(self.skipped)} run(s) had a run.db already, left alone")
        lines += [f"{name}: {why}" for name, why in sorted(self.failed.items())]
        return lines


def run_folders(path: str | Path) -> list[Path]:
    """The run folders as control wrote them before run.db, under PATH: PATH itself, runs/*/ or */."""
    path = Path(path)
    if (path / "run.json").is_file():
        return [path]
    found = list((path / RUNS_DIR).glob("*/run.json")) if (path / RUNS_DIR).is_dir() else []
    found += list(path.glob("*/run.json"))
    return sorted({record.parent for record in found})


def migrate_runs(path: str | Path) -> RunsMigration:
    """Each run folder under PATH with a run.json and no run.db given its run.db: the record from
    run.json, the timeline from run.csv. Nothing is moved or removed."""
    report = RunsMigration()
    for folder in run_folders(path):
        if (folder / RECORD).is_file():
            report.skipped.append(folder.name)
            continue
        try:
            record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("run.json holds no record")
            for holder in (record.get("artifacts"), record.get("recording"), *(record.get("artifacts") or {}).get("videos", [])):
                if isinstance(holder, dict) and "fluidics_csv" in holder:
                    holder.pop("fluidics_csv")
                    holder["timeline"] = RECORD
            target = folder / (RECORD + ".part")
            target.unlink(missing_ok=True)
            rundb.write_record(target, record)
            if (folder / "run.csv").is_file():
                with (folder / "run.csv").open(newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    _timeline_into(target, list(reader.fieldnames or []), list(reader))
            rundb.fold(target)
            target.rename(folder / RECORD)
        except (OSError, ValueError) as exc:
            (folder / (RECORD + ".part")).unlink(missing_ok=True)
            report.failed[folder.name] = str(exc)
            continue
        report.made.append(folder)
    return report


def _place(source: Path, target: Path, copy: bool) -> None:
    if target.exists():
        return
    if copy:
        shutil.copy2(source, target)
    else:
        shutil.move(str(source), str(target))


def _stamp(fluidics: Path | None, moment: Any) -> str:
    """When the run began, as a folder stamp: from its fluidics file's name, else its record's time."""
    if fluidics is not None:
        match = STAMP.search(fluidics.name)
        if match:
            return match.group(1)
    try:
        return datetime.fromisoformat(str(moment)).strftime("%Y%m%d_%H%M%S")
    except (TypeError, ValueError):
        return "00000000_000000"


def _measurements(run_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((run_dir / "measurements.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and "values" in payload else None


def _calculations(run_dir: Path) -> list[dict[str, Any]]:
    results = []
    for path in sorted((run_dir / "calculations").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            results.append(payload)
    return results


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _seconds_of_day(text: Any) -> float | None:
    """A timestamp's time of day in seconds: "16:48:29.998" or "2026-11-17T16:48:33.923"."""
    clock = str(text or "").split("T")[-1].strip()
    parts = clock.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None


def _number(text: Any) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None
