"""An experiment: the runs it is made of, designed in one table, each with an id short enough to type.

An experiment has a code of three characters (no 0/O, 1/I/L to mistake), and each of its runs a
number: run 3 of experiment K7Q is K7Q-03. A run collects its replicates one after another, and
each collected replicate is a sample: K7Q-03-r2. That id goes into the run's record, and onto
whatever is collected from it, such as the folder of microscopy images, so analyze can put the two
together without being told.

A run is a row, and the row is all of it: how the channels are driven (flow or pressure), the oil,
cells and beads setpoints, what ends each collection (oil delivered, everything delivered, time;
the first met), how many replicates, whether each is filmed, its condition, a note, and where it
got to. The steps it runs are made from the row here, not from a protocol. The table can come whole
from a JSON document and goes back out as one; the store keeps it in the data folder's control.db,
beside the runs it made.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"     # no 0 O 1 I L: nothing to mistake when typed
CODE_LENGTH = 3
STATES = ("planned", "running", "done", "failed")
_CODE = f"[{ALPHABET}]{{{CODE_LENGTH}}}"
RUN_ID = re.compile(rf"{_CODE}-\d{{2,3}}")
# A sample's id as typed into a folder's name: any case, the parts joined by -, _, . or a space, the
# replicate as r2 or rep2; not part of a longer run of letters and digits.
SAMPLE_IN_TEXT = re.compile(rf"(?<![A-Za-z0-9])({_CODE})[-_. ]?(\d{{2,3}})[-_. ]?r(?:ep)?(\d{{1,2}})(?![0-9])",
                            re.IGNORECASE)

def new_code(taken: set[str] | None = None) -> str:
    """A code no experiment in TAKEN has."""
    taken = taken or set()
    for _ in range(1000):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if code not in taken:
            return code
    raise RuntimeError("no free experiment code left")


def sample_id(run_id: str, replicate: int) -> str:
    return f"{run_id}-r{int(replicate)}"


def find_sample_id(text: str) -> str | None:
    """The sample's id written in TEXT (a folder's name), as ADMET writes it: K7Q-03-r2; None when
    there is none. k7q_03_rep2 and K7Q03r2 are the same id."""
    match = SAMPLE_IN_TEXT.search(str(text or ""))
    if not match:
        return None
    code, run, replicate = match.groups()
    return sample_id(f"{code.upper()}-{int(run):02d}", int(replicate))


MODES = ("flow", "pressure")
CHANNELS = {"oil": 0, "cells": 1, "beads": 2}            # the Fluigent channel of each liquid
STOPS = ("stop_oil_ul", "stop_total_ul", "stop_s")
MAX_REPLICATES = 20
UNTIMED_TIMEOUT_S = 600.0       # a pressure-driven collection with no time stop gives up after this
# A row's values as JSON and the table name them, with what a new experiment starts from.
DEFAULTS: dict[str, Any] = {"mode": "flow", "oil": 300.0, "cells": 40.0, "beads": 40.0, "stop_oil_ul": 150.0,
                            "stop_total_ul": None, "stop_s": None, "replicates": 3, "video": False, "video_s": 100.0}
FIELDS = tuple(DEFAULTS)


# What names a run, given beside its protocol when it is planned: its name, its generation condition,
# and, for a run of an experiment, its id (K7Q-03, which its samples carry as K7Q-03-r2) and the
# experiment's name.
RUN_FIELDS = ("name", "condition", "id", "experiment")


def run_identity(value: dict[str, Any] | None) -> dict[str, str]:
    """A run's names as kept: text, trimmed, empty ones dropped; an id as an experiment writes it."""
    value = value or {}
    if not isinstance(value, dict) or set(value) - set(RUN_FIELDS):
        raise ValueError(f"a run is named by any of {', '.join(RUN_FIELDS)}")
    if value.get("id") and (not isinstance(value["id"], str) or not RUN_ID.fullmatch(value["id"].strip())):
        raise ValueError("a run's id is an experiment's run id, like K7Q-03")
    result = {}
    for key in RUN_FIELDS:
        text = value.get(key)
        if text is None:
            continue
        if not isinstance(text, str) or len(text) > 80:
            raise ValueError(f"a run's {key} is text of at most 80 characters")
        if text.strip():
            result[key] = text.strip()
    return result


def default_condition(run: Run) -> str:
    """A row's condition when none is given: its setpoints, 300/40/40, in mbar when pressure-driven."""
    numbers = "/".join(f"{value:g}" for value in (run.oil, run.cells, run.beads))
    return numbers + (" mbar" if run.mode == "pressure" else "")


@dataclass
class Run:
    number: int
    mode: str = DEFAULTS["mode"]
    oil: float = DEFAULTS["oil"]
    cells: float = DEFAULTS["cells"]
    beads: float = DEFAULTS["beads"]
    stop_oil_ul: float | None = DEFAULTS["stop_oil_ul"]
    stop_total_ul: float | None = None
    stop_s: float | None = None
    replicates: int = DEFAULTS["replicates"]
    video: bool = False
    video_s: float = DEFAULTS["video_s"]
    condition: str = ""
    note: str = ""
    state: str = "planned"
    run_folder: str = ""             # the run's folder name once it ran
    protocol: str = ""               # a custom run's protocol; its values are not what it ran

    def run_id(self, code: str) -> str:
        return f"{code}-{self.number:02d}"

    def values(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in FIELDS}

    def set(self, **values: Any) -> None:
        """Change some of the row's values; refused, and nothing changed, if the row would not run."""
        unknown = set(values) - set(FIELDS)
        if unknown:
            raise ValueError(f"a run has no {', '.join(sorted(unknown))}; it has {', '.join(FIELDS)}")
        checked = check_values({**self.values(), **values})
        for name, value in checked.items():
            setattr(self, name, value)

    def condition_text(self) -> str:
        return self.condition or default_condition(self)


def check_values(values: dict[str, Any]) -> dict[str, Any]:
    """A row's values as they will run: numbers as numbers, an empty stop off; ValueError naming the value
    that cannot run."""
    out = dict(values)
    if out["mode"] not in MODES:
        raise ValueError(f"mode is {' or '.join(MODES)}")
    for name in ("oil", "cells", "beads"):
        out[name] = _number(out[name], name)
        if out[name] < 0:
            raise ValueError(f"{name} cannot be below 0")
    for name in STOPS:
        value = out[name]
        out[name] = None if value in (None, "", 0) else _number(value, name)
        if out[name] is not None and out[name] < 0:
            raise ValueError(f"{name} cannot be below 0")
    if all(out[name] is None for name in STOPS):
        raise ValueError("a run needs a stop: oil µL, total µL or time s")
    replicates = out["replicates"]
    if isinstance(replicates, bool) or not isinstance(replicates, (int, float)) or int(replicates) != replicates \
            or not 1 <= replicates <= MAX_REPLICATES:
        raise ValueError(f"replicates is a whole number from 1 to {MAX_REPLICATES}")
    out["replicates"] = int(replicates)
    if not isinstance(out["video"], bool):
        raise ValueError("video is true or false")
    from admet.engines.acquisition.pipeline import RECORD_CAP_S

    out["video_s"] = _number(out["video_s"], "video_s")
    if not 1 <= out["video_s"] <= RECORD_CAP_S:
        raise ValueError(f"video_s is from 1 to {RECORD_CAP_S:g} s")
    return out


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is a number")
    return float(value)


def collection_stop(run: Run) -> tuple[str, dict[str, Any], float]:
    """What ends each of the run's collections, as a step's trigger, and how long before it gives up."""
    stops = []
    if run.stop_oil_ul is not None:
        stops.append(("volume", {"sensor_index": CHANNELS["oil"], "target_volume_ul": run.stop_oil_ul}))
    if run.stop_total_ul is not None:
        stops.append(("total_volume", {"sensors": list(CHANNELS.values()), "target_volume_ul": run.stop_total_ul}))
    if run.stop_s is not None:
        stops.append(("time", {"duration_s": run.stop_s}))
    if run.stop_s is not None:
        timeout = run.stop_s + 30
    else:
        nominal = []
        if run.mode == "flow" and run.stop_oil_ul is not None and run.oil > 0:
            nominal.append(run.stop_oil_ul / run.oil * 60)
        if run.mode == "flow" and run.stop_total_ul is not None and run.oil + run.cells + run.beads > 0:
            nominal.append(run.stop_total_ul / (run.oil + run.cells + run.beads) * 60)
        timeout = 30 + 3 * min(nominal) if nominal else UNTIMED_TIMEOUT_S
    if len(stops) == 1:
        return stops[0][0], stops[0][1], timeout
    return "any", {"stops": [{"trigger_type": kind, "trigger_params": params} for kind, params in stops]}, timeout


def _stops_text(run: Run) -> str:
    parts = ([f"{run.stop_oil_ul:g} µL oil"] if run.stop_oil_ul is not None else []) \
        + ([f"{run.stop_total_ul:g} µL in all"] if run.stop_total_ul is not None else []) \
        + ([f"{run.stop_s:g} s"] if run.stop_s is not None else [])
    return " or ".join(parts)


@dataclass
class Experiment:
    code: str
    name: str = ""
    runs: list[Run] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    custom: bool = False             # a day's custom runs, each a protocol run on its own

    # -- the table ----------------------------------------------------------------------------
    def add(self, *, condition: str = "", note: str = "", **values: Any) -> Run:
        """A new row: the values given, the rest as the last row has them (or the defaults)."""
        number = max((run.number for run in self.runs), default=0) + 1
        base = self.runs[-1].values() if self.runs else dict(DEFAULTS)
        run = Run(number, condition=condition, note=note)
        for name, value in base.items():
            setattr(run, name, value)
        run.set(**values)
        self.runs.append(run)
        return run

    def run(self, run_id: str) -> Run:
        for run in self.runs:
            if run.run_id(self.code) == run_id:
                return run
        raise LookupError(f"no run {run_id} in experiment {self.code}")

    def next_planned(self) -> Run | None:
        return next((run for run in self.runs if run.state == "planned"), None)

    def mark(self, run_id: str, state: str, run_folder: str = "") -> Run:
        if state not in STATES:
            raise ValueError(f"a run is one of {', '.join(STATES)}")
        run = self.run(run_id)
        run.state = state
        if run_folder:
            run.run_folder = run_folder
        return run

    # -- what a run is given --------------------------------------------------------------------
    def identity(self, run: Run) -> dict[str, str]:
        """What names the row's run when it is planned: its id, its condition, the experiment's name."""
        run_id = run.run_id(self.code)
        return run_identity({"name": run_id, "id": run_id, "condition": run.condition_text()[:80],
                             "experiment": self.name[:80]})

    def document(self, run: Run) -> dict[str, Any]:
        """The protocol the row runs: for each replicate, a fresh collection confirmed, then the
        channels at the row's setpoints until its first stop."""
        run_id = run.run_id(self.code)
        kind, params, timeout = collection_stop(run)
        key = "sensor_setpoints" if run.mode == "flow" else "pressure_setpoints"
        unit = "µL/min" if run.mode == "flow" else "mbar"
        setpoints = {str(CHANNELS[name]): getattr(run, name) for name in CHANNELS}
        steps = []
        for replicate in range(1, run.replicates + 1):
            step = {"name": f"{run_id} rep{replicate:02d}", key: dict(setpoints), "trigger_type": kind,
                    "trigger_params": deepcopy(params), "timeout_s": timeout, "on_complete": "zero",
                    "confirm_message": (f"Replicate {replicate} of {run.replicates}, sample {sample_id(run_id, replicate)}: "
                                        f"fresh collection in place? Oil {run.oil:g}, cells {run.cells:g}, "
                                        f"beads {run.beads:g} {unit}; stops at {_stops_text(run)}.")}
            if run.video:
                step.update(record=True, record_max_s=run.video_s)
            steps.append(step)
        return {"name": "experiment_run", "steps": steps}

    # -- JSON -------------------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {"experiment": self.name, "code": self.code, "created_at": self.created_at,
                **({"custom": True} if self.custom else {}),
                "runs": [{"id": run.run_id(self.code), **({"protocol": run.protocol} if run.protocol else {}),
                          **run.values(), "condition": run.condition, "note": run.note, "state": run.state,
                          "run_folder": run.run_folder} for run in self.runs]}

    @classmethod
    def from_json(cls, document: dict[str, Any], *, taken: set[str] | None = None) -> Experiment:
        """An experiment from its document: {"experiment": name, <any row value for every row>,
        "runs": [{<row values>, "condition"?, "note"?}, ...]}. A value a row leaves out is the
        experiment's, else the default; a row (or the experiment) that names any stop has only the
        stops it names. Every run is checked before any is taken, and an error names the run it is in."""
        if not isinstance(document, dict) or not isinstance(document.get("runs"), list):
            raise ValueError("an experiment is an object with a list of runs")
        code = str(document.get("code") or "") or new_code(taken)
        if not re.fullmatch(_CODE, code):
            raise ValueError(f"an experiment's code is {CODE_LENGTH} of {ALPHABET}")
        experiment = cls(code, str(document.get("experiment") or ""), custom=bool(document.get("custom")))
        if document.get("created_at"):
            experiment.created_at = str(document["created_at"])
        shared = _row_values(DEFAULTS, document)
        known = {*FIELDS, "id", "condition", "note", "state", "run_folder", *(("protocol",) if experiment.custom else ())}
        for index, entry in enumerate(document["runs"], 1):
            where = f"run {index}"
            if not isinstance(entry, dict):
                raise ValueError(f"{where}: each run is an object")
            unknown = set(entry) - known
            if unknown:
                raise ValueError(f"{where}: no {', '.join(sorted(unknown))}; a run has {', '.join(sorted(known))}")
            run = Run(0, condition=str(entry.get("condition") or ""), note=str(entry.get("note") or ""),
                      protocol=str(entry.get("protocol") or ""))
            try:
                run.set(**_row_values(shared, entry))
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from exc
            run.number = max((r.number for r in experiment.runs), default=0) + 1
            if entry.get("id"):
                number = re.fullmatch(rf"{code}-(\d{{2,3}})", str(entry["id"]))
                if not number:
                    raise ValueError(f"{where}: id {entry['id']} is not one of experiment {code}")
                run.number = int(number.group(1))
            state = str(entry.get("state") or "planned")
            if state not in STATES:
                raise ValueError(f"{where}: state is one of {', '.join(STATES)}")
            run.state, run.run_folder = state, str(entry.get("run_folder") or "")
            experiment.runs.append(run)
        numbers = [run.number for run in experiment.runs]
        if len(set(numbers)) != len(numbers):
            raise ValueError("two runs share an id")
        return experiment


def _row_values(base: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """BASE with what GIVEN says; GIVEN naming any stop sets all three, the ones it leaves out off."""
    values = {**base, **{name: given[name] for name in FIELDS if name in given}}
    if any(name in given for name in STOPS):
        values.update({name: given.get(name) for name in STOPS})
    return values


class ExperimentStore:
    """The experiments of one data folder, in its control.db beside the runs they made."""

    def __init__(self, data_dir: str | Path) -> None:
        from admet.core.rundb import CONTROL_DB

        self.database = Path(data_dir) / CONTROL_DB

    def _rows(self, where: str = "", args: tuple = ()) -> list[tuple[str, str]]:
        from admet.core.rundb import connect

        if not self.database.is_file():
            return []
        connection = connect(self.database, readonly=True)
        try:
            return list(connection.execute(f"SELECT code, body FROM experiments {where}", args))
        except sqlite3.DatabaseError:
            return []
        finally:
            connection.close()

    def codes(self) -> set[str]:
        return {code for code, _body in self._rows()}

    def save(self, experiment: Experiment) -> None:
        from admet.core.rundb import connect

        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = connect(self.database)
        try:
            with connection:
                connection.execute("INSERT OR REPLACE INTO experiments (code, body) VALUES (?, ?)",
                                   (experiment.code, json.dumps(experiment.to_json(), allow_nan=False)))
        finally:
            connection.close()

    def load(self, code: str) -> Experiment:
        rows = self._rows("WHERE code = ?", (str(code),))
        if not rows:
            raise LookupError(f"no experiment {code}")
        return Experiment.from_json(json.loads(rows[0][1]))

    def custom(self, day: str) -> Experiment:
        """The experiment that holds the custom runs of DAY (2026-11-26), new if there is none yet."""
        name = f"Custom {day}"
        entry = next((entry for entry in self.list() if entry["custom"] and entry["name"] == name), None)
        if entry is not None:
            return self.load(entry["code"])
        experiment = Experiment(new_code(self.codes()), name, custom=True)
        self.save(experiment)
        return experiment

    def list(self) -> list[dict[str, Any]]:
        """Every experiment, newest first: its code, name, and how many of its runs are done."""
        entries = []
        for code, body in self._rows():
            try:
                document = json.loads(body)
            except ValueError:
                continue
            runs = document.get("runs") or []
            entries.append({"code": code, "name": document.get("experiment", ""), "custom": bool(document.get("custom")),
                            "created_at": document.get("created_at", ""), "runs": len(runs),
                            "done": sum(1 for run in runs if run.get("state") == "done")})
        return sorted(entries, key=lambda entry: entry["created_at"], reverse=True)
