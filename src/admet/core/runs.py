"""A run: its record and its timeline in the data folder's control.db, its videos in its folder.

control.db (see rundb) keeps every run control recorded: the plan and the protocol as they ran,
the state and the times, the rig mapping and the corrections in force, the step events, the
measurements typed during the run, the videos it made, and the results calculated from it
afterwards; and its timeline, fluidics and QC rows on one clock. Each change to a record is one
transaction. The run's folder, data/runs/<date>_<name>/, holds its videos; once the run has
finished it holds its run.db too, the run sealed on its own, sealed again whenever its record
changes. That folder is what goes to the server and what analyze reads: nothing in it points
outside it, so a folder copied elsewhere is whole.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from admet.core import rundb
from admet.core.files import write_atomic

RECORD = rundb.DB_NAME             # a sealed run's file in its folder
CONTROL_DB = rundb.CONTROL_DB      # every run of a data folder, in the data folder
RUNS_DIR = "runs"
RUN_ID = re.compile(r"\d{8}_\d{6}_[a-z0-9_]+")
FINISHED = frozenset({"completed", "failed", "cancelled"})
# An event is kept when one of these changed since the last one kept; the ticks between only carry progress.
EVENT_KEYS = ("state", "outcome", "step_index", "step_name", "confirmation_message", "error")


def run_name(value: Any) -> str:
    """A folder-safe name: lowercase letters, digits and single underscores."""
    text = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    name = "_".join(part for part in text.split("_") if part)
    return name[:60] or "run"


def write_json(path: str | Path, payload: Any) -> None:
    write_atomic(path, json.dumps(payload, indent=2, allow_nan=False) + "\n")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sealed_record(folder: str | Path) -> dict[str, Any]:
    """A finished run's record as its folder carries it, in its run.db: what analyze and the server
    read, wherever the folder was copied; empty when the folder holds none."""
    return rundb.read_record(Path(folder) / RECORD)


class Run:
    """One run of a data folder: its folder, data/runs/<id>/, and its record in control.db."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.run_id = self.path.name
        self.database = self.path.parent.parent / CONTROL_DB

    @property
    def ref(self) -> str:
        """Where the run's record and timeline are: its locator in control.db."""
        return rundb.locator(self.database, self.run_id)

    @property
    def sealed_path(self) -> Path:
        """The run on its own, in its folder, once it has finished."""
        return self.path / RECORD

    def read(self) -> dict[str, Any]:
        return rundb.read_record(self.ref)

    def write(self, record: dict[str, Any]) -> None:
        rundb.write_record(self.ref, record)
        self._seal_if_finished(record)

    def update(self, **fields: Any) -> dict[str, Any]:
        return self._changed(rundb.update_record(self.ref, lambda record: {**record, **fields}))

    def seal(self) -> Path:
        """The run written on its own into its folder as run.db: what goes to the server and analyze."""
        self.path.mkdir(parents=True, exist_ok=True)
        return rundb.seal(self.ref, self.sealed_path)

    def _changed(self, record: dict[str, Any]) -> dict[str, Any]:
        self._seal_if_finished(record)
        return record

    def _seal_if_finished(self, record: dict[str, Any]) -> None:
        if record.get("state") in FINISHED:
            self.seal()

    @property
    def name(self) -> str:
        return str(self.read().get("name") or self.run_id.split("_", 2)[-1])

    def event(self, described: dict[str, Any]) -> bool:
        """Keep a protocol event when it changes what the run is doing; a progress tick is not kept."""
        kept = []

        def add(record: dict[str, Any]) -> dict[str, Any] | None:
            events = record.setdefault("events", [])
            last = events[-1] if events else None
            if last is not None and all(last.get(key) == described.get(key) for key in EVENT_KEYS):
                return None
            events.append(described)
            kept.append(True)
            return record

        self._changed(rundb.update_record(self.ref, add))
        return bool(kept)

    def measurements(self, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        """The values typed during the run; with changes, the ones declared are updated and the revision moves."""
        from admet.workflows.json_protocol import validate_measurement

        payload = self.read().get("measurements")
        if not isinstance(payload, dict):
            raise ValueError("this run declares no measurements")
        if changes is None:
            return payload
        if not isinstance(changes, dict) or set(changes) - set(payload["fields"]):
            raise ValueError("undeclared measurement")
        for key, value in changes.items():
            validate_measurement(value, payload["fields"][key])

        def change(record: dict[str, Any]) -> dict[str, Any]:
            kept = record["measurements"]
            kept["values"].update(changes)
            kept["revision"] += 1
            return record

        return self._changed(rundb.update_record(self.ref, change))["measurements"]

    def inputs_digest(self) -> str:
        """What a calculation reads from the record, hashed: everything but the results themselves."""
        record = {key: value for key, value in self.read().items() if key != "calculations"}
        return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    def results(self) -> list[dict[str, Any]]:
        results = self.read().get("calculations")
        return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

    def result(self, result_id: str) -> dict[str, Any] | None:
        return next((item for item in self.results() if item.get("id") == result_id), None)

    def add_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        kept = {**payload, "id": uuid.uuid4().hex}

        def add(record: dict[str, Any]) -> dict[str, Any]:
            results = record.get("calculations")
            record["calculations"] = [*(results if isinstance(results, list) else []), kept]
            return record

        self._changed(rundb.update_record(self.ref, add))
        return kept

    def video_finished(self, video_name: str, encoded: bool, reason: str) -> bool:
        """A video of this run finished encoding, or could not; its entry says so."""
        found = []

        def mark(record: dict[str, Any]) -> dict[str, Any] | None:
            videos = list(record.get("artifacts", {}).get("videos", []))
            recording = record.get("recording")
            if isinstance(recording, dict):
                videos.append(recording)
            for video in videos:
                if Path(str(video.get("video_path") or "")).name == video_name:
                    video["encoded"] = bool(encoded)
                    if not encoded:
                        video["encode_error"] = reason
                    found.append(True)
            return record if found else None

        self._changed(rundb.update_record(self.ref, mark))
        return bool(found)


class RunStore:
    """The runs of one data folder: their records in its control.db, their folders under runs/."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir)
        self.runs_dir = self.root / RUNS_DIR
        self.database = self.root / CONTROL_DB

    def exists(self, run_id: Any) -> bool:
        return isinstance(run_id, str) and RUN_ID.fullmatch(run_id) is not None \
            and bool(rundb.read_record(rundb.locator(self.database, run_id)))

    def open(self, run_id: Any) -> Run:
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise ValueError("invalid run ID")
        if not self.exists(run_id):
            raise LookupError(f"unknown run {run_id!r}")
        return Run(self.runs_dir / run_id)

    def _new(self, name: str) -> Run:
        base = f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
        taken = set(rundb.run_ids(self.database))
        folder, n = base, 2
        while (self.runs_dir / folder).exists() or folder in taken:
            folder, n = f"{base}_{n}", n + 1
        (self.runs_dir / folder).mkdir(parents=True)
        return Run(self.runs_dir / folder)

    def begin(self, plan: dict[str, Any]) -> Run:
        """A protocol run starts: its folder, named after the run's tag or the protocol, and its record."""
        from admet.workflows.json_protocol import resolve

        protocol = (plan.get("normalized_settings") or {}).get("protocol") or {}
        name = run_name(plan.get("tag") or protocol.get("name") or plan.get("operation_id"))
        run = self._new(name)
        fields = resolve(protocol).get("measurements", {}) if protocol else {}
        run.write({
            **plan, "run_id": run.run_id, "name": name, "kind": "protocol", "artifacts": {}, "events": [],
            "measurements": ({"fields": fields, "values": {key: None for key in fields}, "revision": 0}
                             if fields else None),
            "calculations": [],
        })
        return run

    def begin_recording(self, label: str) -> Run:
        """A recording started by hand is a run of its own."""
        name = run_name(label)
        run = self._new(name)
        run.write({"run_id": run.run_id, "name": name, "kind": "recording", "state": "recording",
                   "started_at": _now(), "artifacts": {}, "events": [], "measurements": None, "calculations": []})
        return run

    def list(self) -> list[dict[str, Any]]:
        """Every run, newest first: where it is, what it is called, how it ended."""
        entries = []
        for run_id in rundb.run_ids(self.database):
            record = rundb.read_record(rundb.locator(self.database, run_id))
            if not record:
                continue
            protocol = (record.get("normalized_settings") or {}).get("protocol") or {}
            entries.append({
                "run_id": run_id,
                "directory": str(self.runs_dir / run_id),
                "name": record.get("tag") or protocol.get("name") or record.get("name") or run_id,
                "condition": str(record.get("condition") or ""),
                "kind": record.get("kind", "protocol"),
                "state": record.get("state", ""),
                "at": record.get("executed_at") or record.get("started_at") or "",
                "completed_at": record.get("completed_at") or record.get("stopped_at") or "",
                "measurements": isinstance(record.get("measurements"), dict),
            })
        return sorted(entries, key=lambda entry: entry["at"], reverse=True)
