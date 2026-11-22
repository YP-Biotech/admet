"""A run is a folder: data/runs/<date>_<name>/ with run.json, run.csv and its videos.

run.json is the run's one record: the plan and the protocol as they ran, the state and
the times, the rig mapping and the corrections in force, the step events, the measurements
typed during the run, the videos it made, and the results calculated from it afterwards.
run.csv is the timeline, fluidics and QC rows on one clock. Nothing in the folder points
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

from admet.core.files import write_atomic

RECORD = "run.json"
TIMELINE = "run.csv"
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


class Run:
    """One run folder and its record."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.run_id = self.path.name

    @property
    def record_path(self) -> Path:
        return self.path / RECORD

    @property
    def csv_path(self) -> Path:
        return self.path / TIMELINE

    def read(self) -> dict[str, Any]:
        try:
            record = read_json(self.record_path)
        except (OSError, ValueError):
            return {}
        return record if isinstance(record, dict) else {}

    def write(self, record: dict[str, Any]) -> None:
        write_json(self.record_path, record)

    def update(self, **fields: Any) -> dict[str, Any]:
        record = self.read()
        record.update(fields)
        self.write(record)
        return record

    @property
    def name(self) -> str:
        return str(self.read().get("name") or self.run_id.split("_", 2)[-1])

    def event(self, described: dict[str, Any]) -> bool:
        """Keep a protocol event when it changes what the run is doing; a progress tick is not kept."""
        record = self.read()
        events = record.setdefault("events", [])
        last = events[-1] if events else None
        if last is not None and all(last.get(key) == described.get(key) for key in EVENT_KEYS):
            return False
        events.append(described)
        self.write(record)
        return True

    def measurements(self, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        """The values typed during the run; with changes, the ones declared are updated and the revision moves."""
        from admet.workflows.json_protocol import validate_measurement

        record = self.read()
        payload = record.get("measurements")
        if not isinstance(payload, dict):
            raise ValueError("this run declares no measurements")
        if changes is not None:
            if not isinstance(changes, dict) or set(changes) - set(payload["fields"]):
                raise ValueError("undeclared measurement")
            for key, value in changes.items():
                validate_measurement(value, payload["fields"][key])
            payload["values"].update(changes)
            payload["revision"] += 1
            record["measurements"] = payload
            self.write(record)
        return payload

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
        record = self.read()
        kept = {**payload, "id": uuid.uuid4().hex}
        results = record.get("calculations")
        record["calculations"] = [*(results if isinstance(results, list) else []), kept]
        self.write(record)
        return kept

    def video_finished(self, video_name: str, encoded: bool, reason: str) -> bool:
        """A video of this run finished encoding, or could not; its entry says so."""
        record = self.read()
        videos = list(record.get("artifacts", {}).get("videos", []))
        recording = record.get("recording")
        if isinstance(recording, dict):
            videos.append(recording)
        found = False
        for video in videos:
            if Path(str(video.get("video_path") or "")).name == video_name:
                video["encoded"] = bool(encoded)
                if not encoded:
                    video["encode_error"] = reason
                found = True
        if found:
            self.write(record)
        return found


class RunStore:
    """The runs of one data folder."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir)
        self.runs_dir = self.root / RUNS_DIR

    def exists(self, run_id: Any) -> bool:
        return isinstance(run_id, str) and RUN_ID.fullmatch(run_id) is not None \
            and (self.runs_dir / run_id / RECORD).is_file()

    def open(self, run_id: Any) -> Run:
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise ValueError("invalid run ID")
        path = self.runs_dir / run_id
        if not (path / RECORD).is_file():
            raise LookupError(f"unknown run {run_id!r}")
        return Run(path)

    def _new(self, name: str) -> Run:
        base = f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
        folder, n = base, 2
        while (self.runs_dir / folder).exists():
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
        for record_path in self.runs_dir.glob(f"*/{RECORD}"):
            try:
                record = read_json(record_path)
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            protocol = (record.get("normalized_settings") or {}).get("protocol") or {}
            entries.append({
                "run_id": record_path.parent.name,
                "directory": str(record_path.parent),
                "name": record.get("tag") or protocol.get("name") or record.get("name") or record_path.parent.name,
                "set": str(record.get("set") or ""),
                "kind": record.get("kind", "protocol"),
                "state": record.get("state", ""),
                "at": record.get("executed_at") or record.get("started_at") or "",
                "completed_at": record.get("completed_at") or record.get("stopped_at") or "",
                "measurements": isinstance(record.get("measurements"), dict),
            })
        return sorted(entries, key=lambda entry: entry["at"], reverse=True)
