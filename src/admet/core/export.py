"""Export: a run's folder copied whole to the server, each file checked, and its videos then let go.

The copy on the server is the one that lasts. The computer at the rig keeps every run's
run.db, its record and timeline, so Review and Calculations keep working there, and gives up
the videos once they are safely elsewhere. The database goes last, folded whole first, so the
copy needs nothing beside it. Exporting is a thing to do again: a record that changed since
(a result calculated later) is copied again; a video already there and whole is not touched.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from admet.core import rundb
from admet.core.runs import FINISHED, RECORD, Run, RunStore

CHUNK = 8 << 20
Progress = Callable[[str, str, int, int], None]        # run id, file name, bytes done, bytes of the file


class ExportError(RuntimeError):
    pass


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(source: Path, target: Path, note: Callable[[int], None] | None = None) -> str:
    """Copy source to target through a part file and read the copy back; the digest both share.

    A copy already there and whole is left alone. A copy that does not read back as the source
    is removed, and the error says which file.
    """
    expected = file_digest(source)
    if target.is_file() and target.stat().st_size == source.stat().st_size and file_digest(target) == expected:
        return expected
    part = target.with_name(target.name + ".part")
    try:
        with open(source, "rb") as reader, open(part, "wb") as writer:
            while chunk := reader.read(CHUNK):
                writer.write(chunk)
                if note is not None:
                    note(len(chunk))
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(part, target)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise ExportError(f"{source.name}: {exc}") from exc
    if file_digest(target) != expected:
        target.unlink(missing_ok=True)
        raise ExportError(f"{source.name} did not arrive whole; nothing was removed")
    return expected


def _exportable(run: Run) -> list[Path]:
    """The files of a run that go to the server: everything but the database (copied last) and work files."""
    return sorted(path for path in run.path.iterdir()
                  if path.is_file() and path.name != RECORD
                  and not path.name.endswith((".part", ".tmp", ".video.json", "-wal", "-shm", "-journal"))
                  and path.suffix != ".raw")


def export_run(run: Run, destination: str | Path, progress: Progress | None = None) -> dict[str, Any]:
    """Copy one finished run to destination/<run id>/, check every file, then let the local videos go.

    The run's record notes where it went, when, and the digest of each file sent; the copy on the
    server carries the same note. The videos leave this computer only after every file read back whole.
    """
    destination = Path(destination)
    record = run.read()
    if not record:
        raise ExportError(f"{run.path} is not a run")
    if record.get("state") not in FINISHED:
        raise ExportError(f"{run.run_id} is still going")
    if any(run.path.glob("*.video.json")):
        raise ExportError(f"{run.run_id} still has a video being encoded")
    if not destination.is_dir():
        raise ExportError(f"{destination} is not there; is the server mounted?")
    target = destination / run.run_id
    try:
        target.mkdir(exist_ok=True)
    except OSError as exc:
        raise ExportError(f"cannot write to {destination}: {exc}") from exc

    files = _exportable(run)
    sent: dict[str, dict[str, Any]] = {}
    copied = 0
    for path in files:
        size = path.stat().st_size
        done = 0

        def note(count: int, name: str = path.name, size: int = size) -> None:
            nonlocal done
            done += count
            if progress is not None:
                progress(run.run_id, name, done, size)

        was_there = (target / path.name).is_file()
        sent[path.name] = {"sha256": _copy_verified(path, target / path.name, note), "bytes": size}
        copied += 0 if was_there and done == 0 else size
    previous = record.get("export") if isinstance(record.get("export"), dict) else {}
    note_record = {
        "to": str(destination), "at": datetime.now().isoformat(timespec="seconds"),
        "files": {**(previous.get("files") or {}), **sent},
        "videos_removed": sorted(previous.get("videos_removed") or []),
    }
    record["export"] = note_record
    run.write(record)
    rundb.fold(run.db_path)
    _copy_verified(run.db_path, target / RECORD)

    videos = [path for path in files if path.suffix.lower() == ".avi"]
    for video in videos:                                 # every file read back whole: the local copies can go
        video.unlink()
    if videos:
        note_record["videos_removed"] = sorted(set(note_record["videos_removed"]) | {video.name for video in videos})
        record["export"] = note_record
        run.write(record)
        rundb.fold(run.db_path)
        _copy_verified(run.db_path, target / RECORD)
    return {"run_id": run.run_id, "to": str(target), "copied": [path.name for path in files],
            "removed": [video.name for video in videos], "bytes": copied}


def export_runs(store: RunStore, destination: str | Path, progress: Progress | None = None) -> list[dict[str, Any]]:
    """Every finished run of the store to the destination, one after another; what happened to each.

    A run still going or still encoding is reported and left for next time, and so is one whose
    copy failed; the others are not held up by it.
    """
    if not Path(destination).is_dir():
        raise ExportError(f"{destination} is not there; is the server mounted?")
    reports = []
    for entry in sorted(store.list(), key=lambda run: run["at"]):
        run = Run(entry["directory"])
        try:
            reports.append(export_run(run, destination, progress))
        except ExportError as exc:
            reports.append({"run_id": run.run_id, "error": str(exc)})
    return reports
