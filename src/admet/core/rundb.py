"""A run's record and timeline: in control.db while control keeps it, in run.db when it goes out.

The record is the run as one document (the plan and the protocol as they ran, the state and the
times, the events, the measurements, the videos, the results calculated later), kept whole in
one row. The timeline is two tables on one clock, elapsed_s from the moment polling began:
fluidics, ten readings a second, and qc, one row a second per burst of a recording; their
columns are the ones run.csv had, so the CSV the export writes reads as it always did.

Control keeps every run in one database, control.db, beside the runs' folders: a run there is
named by its locator, "<control.db>#<run id>", and its rows carry its id. A run that has finished
is sealed into its own folder as run.db, the same tables for that run alone, beside its videos:
that is what goes to the server and what analyze reads. Every function here takes either, a
locator or a run.db's path.

While a run records the database is in WAL mode: the poll thread and the live QC hand their rows
to one writer thread that commits them once a second, and the record can be written at any time
beside it. A sealed run.db is left whole on its own, its WAL folded back, so a copy of it, or a
reader on a network share where WAL cannot work, sees everything.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

DB_NAME = "run.db"
CONTROL_DB = "control.db"
SCHEMA_VERSION = 2
BUSY_TIMEOUT_S = 10.0
COMMIT_INTERVAL_S = 1.0
TEXT_QC_COLUMNS = {"timestamp", "recording_id", "step", "part", "diameter_unit", "state"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS record (id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (code TEXT PRIMARY KEY, body TEXT NOT NULL);
"""

Target = str | Path       # a run.db's path, or a run in control.db: "<control.db>#<run id>"


def locator(database: str | Path, run_id: str) -> str:
    """How a run in control.db is named wherever a run's file used to be."""
    return f"{Path(database)}#{run_id}"


def locate(target: Target) -> tuple[Path, str | None]:
    """The database's path and the run's id; the id is None for a run.db, which holds one run."""
    text = str(target)
    if "#" in text:
        path, run_id = text.rsplit("#", 1)
        return Path(path), run_id
    return Path(text), None


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """A connection to a database of runs. Read-only when asked; a file on a share that cannot be
    locked is then read as it stands."""
    path = Path(path)
    if readonly:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S)
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return connection
        except sqlite3.OperationalError:
            return sqlite3.connect(uri + "&immutable=1", uri=True)
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    connection.executescript(_SCHEMA)
    connection.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    connection.commit()
    return connection


# -- the record ------------------------------------------------------------------------------

def _select_record(connection: sqlite3.Connection, run_id: str | None):
    if run_id is None:
        return connection.execute("SELECT body FROM record WHERE id = 1").fetchone()
    return connection.execute("SELECT body FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def _store_record(connection: sqlite3.Connection, run_id: str | None, body: str) -> None:
    if run_id is None:
        connection.execute("INSERT OR REPLACE INTO record (id, body) VALUES (1, ?)", (body,))
    else:
        connection.execute("INSERT OR REPLACE INTO runs (run_id, body) VALUES (?, ?)", (run_id, body))


def read_record(target: Target) -> dict[str, Any]:
    """The run's record; empty when the file is missing or holds none."""
    path, run_id = locate(target)
    if not path.is_file():
        return {}
    try:
        connection = connect(path, readonly=True)
        try:
            row = _select_record(connection, run_id)
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return {}
    if row is None:
        return {}
    try:
        record = json.loads(row[0])
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def write_record(target: Target, record: dict[str, Any]) -> None:
    """The run's record replaced whole, in one transaction."""
    path, run_id = locate(target)
    body = json.dumps(record, allow_nan=False, default=str)
    connection = connect(path)
    try:
        with connection:
            _store_record(connection, run_id, body)
    finally:
        connection.close()


def update_record(target: Target, change) -> dict[str, Any]:
    """Read, change and write the record in one transaction, so two writers never lose each other's
    change; CHANGE takes the record and returns it, or None to leave it as it was."""
    path, run_id = locate(target)
    connection = connect(path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _select_record(connection, run_id)
            record = json.loads(row[0]) if row else {}
            changed = change(record)
            if changed is not None:
                _store_record(connection, run_id, json.dumps(changed, allow_nan=False, default=str))
                record = changed
        return record
    finally:
        connection.close()


def run_ids(database: str | Path) -> list[str]:
    """Every run control.db keeps."""
    path = Path(database)
    if not path.is_file():
        return []
    connection = connect(path, readonly=True)
    try:
        return [row[0] for row in connection.execute("SELECT run_id FROM runs ORDER BY run_id")]
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()


def fold(path: str | Path) -> None:
    """The WAL folded back into the file and the file left in rollback mode: whole on its own."""
    if not Path(path).is_file():
        return
    connection = connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()


# -- the timeline ---------------------------------------------------------------------------------

def fluidics_columns(pressure_count: int, sensor_count: int) -> list[str]:
    columns = ["timestamp", "elapsed_s"]
    columns += [f"pressure_{index}_mbar" for index in range(pressure_count)]
    columns += [f"flow_{index}_ul_min" for index in range(sensor_count)]
    columns += [f"volume_{index}_ul" for index in range(sensor_count)]
    columns += [f"stable_{index}" for index in range(sensor_count)]
    return columns


def qc_columns() -> list[str]:
    from admet.engines.acquisition.camera.qc import QC_COLUMNS

    return list(QC_COLUMNS)


def create_timeline(connection: sqlite3.Connection, pressure_count: int, sensor_count: int, *,
                    keyed: bool = False) -> None:
    """The two timeline tables, if the file has none yet, and any column a wider rig adds; KEYED
    tables (control.db's) carry each row's run id."""
    wanted = {"fluidics": fluidics_columns(pressure_count, sensor_count), "qc": qc_columns()}
    with connection:
        for table, columns in wanted.items():
            text = {"timestamp"} if table == "fluidics" else TEXT_QC_COLUMNS
            declared = [f'"{name}" {"TEXT" if name in text else "REAL"}' for name in columns]
            if keyed:
                declared.insert(0, '"run_id" TEXT NOT NULL')
            connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(declared)})")
            have = set(_table_columns(connection, table))
            for name in columns:
                if name not in have:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" '
                                       f'{"TEXT" if name in text else "REAL"}')
            if keyed:
                connection.execute(f"CREATE INDEX IF NOT EXISTS {table}_run ON {table} (run_id)")


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _data_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [name for name in _table_columns(connection, table) if name != "run_id"]


def timeline_columns(target: Target) -> list[str]:
    """The columns of the timeline as a CSV: the fluidics ones, then the QC ones after elapsed_s."""
    path, _run_id = locate(target)
    if not path.is_file():
        return []
    connection = connect(path, readonly=True)
    try:
        fluidics = _data_columns(connection, "fluidics")
        qc = _data_columns(connection, "qc")
    finally:
        connection.close()
    if not fluidics and not qc:
        return []
    return [*(fluidics or ["timestamp", "elapsed_s"]), *[name for name in qc if name not in ("timestamp", "elapsed_s")]]


def has_timeline(target: Target) -> bool:
    """Whether the run has any timeline row: a reading or a QC row."""
    path, run_id = locate(target)
    if not path.is_file():
        return False
    connection = connect(path, readonly=True)
    try:
        for table in ("fluidics", "qc"):
            if not _table_columns(connection, table):
                continue
            where, args = ("WHERE run_id = ?", (run_id,)) if run_id is not None else ("", ())
            if connection.execute(f"SELECT 1 FROM {table} {where} LIMIT 1", args).fetchone():
                return True
        return False
    finally:
        connection.close()


def timeline_rows(target: Target) -> list[dict[str, str]]:
    """Every timeline row in time order, as the CSV reader gave them: each column's value as text,
    empty where the row has none (a reading has no QC values, a QC row no readings). A calculation
    reads the timeline many times over, so the rows are kept per version of the file, and each
    caller gets its own copies."""
    path, run_id = locate(target)
    if not path.is_file():
        return []
    return [dict(row) for row in _timeline_rows(str(path.resolve()), run_id, _version(path))]


def _version(path: Path) -> tuple:
    """What changes whenever the file's content can: its size and time, and its WAL's."""
    versions = []
    for part in (path, path.with_name(path.name + "-wal")):
        try:
            stat = part.stat()
            versions.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            versions.append(None)
    return tuple(versions)


@lru_cache(maxsize=8)
def _timeline_rows(path_text: str, run_id: str | None, _version_key: tuple) -> tuple[dict[str, str], ...]:
    path = Path(path_text)
    columns = timeline_columns(path)
    if not columns:
        return ()
    connection = connect(path, readonly=True)
    try:
        rows: list[tuple[float, int, dict[str, str]]] = []
        for order, table in enumerate(("fluidics", "qc")):
            names = _data_columns(connection, table)
            if not names:
                continue
            quoted = ", ".join(f'"{name}"' for name in names)
            where, args = ("WHERE run_id = ?", (run_id,)) if run_id is not None else ("", ())
            for values in connection.execute(f"SELECT {quoted} FROM {table} {where} ORDER BY rowid", args):
                row = {column: "" for column in columns}
                row.update({name: _text(value) for name, value in zip(names, values)})
                elapsed = values[names.index("elapsed_s")] if "elapsed_s" in names else None
                rows.append((float(elapsed) if elapsed is not None else float("inf"), order, row))
    finally:
        connection.close()
    rows.sort(key=lambda item: (item[0], item[1]))
    return tuple(row for _elapsed, _order, row in rows)


def timeline_digest(target: Target) -> str:
    """A digest of the timeline's content, whatever else the file holds."""
    digest = hashlib.sha256()
    for row in timeline_rows(target):
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def _text(value: Any) -> str:
    """A stored value as the CSV held it: empty for none, a number in its shortest exact form."""
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)


class TimelineWriter:
    """The timeline of one run as it is recorded: rows taken from any thread, committed by one."""

    def __init__(self, target: Target, pressure_count: int, sensor_count: int) -> None:
        self.path, self.run_id = locate(target)
        self._pending: list[tuple[str, tuple]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.rows_written = 0
        connection = connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            create_timeline(connection, pressure_count, sensor_count, keyed=self.run_id is not None)
            self._fluidics = _data_columns(connection, "fluidics")
            self._qc = _data_columns(connection, "qc")
        finally:
            connection.close()
        self._thread = threading.Thread(target=self._run, name="TimelineWriter", daemon=True)
        self._thread.start()

    def add_fluidics(self, values: dict[str, Any]) -> None:
        self._add("fluidics", self._row(values, self._fluidics))

    def add_qc(self, values: dict[str, Any]) -> None:
        self._add("qc", self._row(values, self._qc))

    def _row(self, values: dict[str, Any], columns: list[str]) -> tuple:
        row = tuple(values.get(name) for name in columns)
        return (self.run_id, *row) if self.run_id is not None else row

    def _add(self, table: str, row: tuple) -> None:
        with self._lock:
            self._pending.append((table, row))

    def close(self) -> None:
        """Every row taken so far committed and the writer stopped; a run.db is folded whole."""
        self._stop.set()
        self._wake.set()
        self._thread.join(BUSY_TIMEOUT_S + 5)
        if self.run_id is None:
            fold(self.path)

    def _run(self) -> None:
        connection = connect(self.path)
        try:
            while True:
                stopping = self._stop.wait(COMMIT_INTERVAL_S)
                self._commit(connection)
                if stopping:
                    break
        finally:
            connection.close()

    def _commit(self, connection: sqlite3.Connection) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        if not pending:
            return
        keyed = self.run_id is not None
        fluidics = [row for table, row in pending if table == "fluidics"]
        qc = [row for table, row in pending if table == "qc"]
        for attempt in range(3):
            try:
                with connection:
                    if fluidics:
                        connection.executemany(_insert("fluidics", self._fluidics, keyed), fluidics)
                    if qc:
                        connection.executemany(_insert("qc", self._qc, keyed), qc)
                self.rows_written += len(pending)
                return
            except sqlite3.OperationalError:
                time.sleep(0.2 * (attempt + 1))
        with self._lock:                                    # still locked out: try again next second
            self._pending[:0] = pending


def _insert(table: str, columns: list[str], keyed: bool = False) -> str:
    names = (["run_id"] if keyed else []) + columns
    return (f"INSERT INTO {table} ({', '.join(chr(34) + name + chr(34) for name in names)}) "
            f"VALUES ({', '.join('?' for _ in names)})")


# -- sealing, export -----------------------------------------------------------------------------

def seal(target: Target, run_db: str | Path) -> Path:
    """The run written out on its own as RUN_DB: its record and its timeline, the file whole, so it
    can be copied and read anywhere; written beside and swapped in, never half there."""
    run_db = Path(run_db)
    part = run_db.with_name(run_db.name + ".part")
    part.unlink(missing_ok=True)
    rows = timeline_rows(target)
    columns = timeline_columns(target) if rows else []
    write_record(part, read_record(target))
    if rows:
        write_timeline(part, rows, *channel_counts(columns))
    fold(part)
    os.replace(part, run_db)
    return run_db


def export_csv(target: Target, out: str | Path) -> int:
    """The timeline as run.csv was: one header, every row in time order; how many rows."""
    rows = timeline_rows(target)
    columns = timeline_columns(target)
    with Path(out).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def export_json(target: Target, out: str | Path) -> None:
    """The record as run.json was."""
    Path(out).write_text(json.dumps(read_record(target), indent=2, allow_nan=False) + "\n", encoding="utf-8")


def channel_counts(columns: Iterable[str]) -> tuple[int, int]:
    """How many pressure and sensor channels timeline columns name: one past the highest index of each."""
    columns = list(columns)

    def highest(pattern: str) -> int:
        found = [int(match.group(1)) for match in (re.fullmatch(pattern, column) for column in columns) if match]
        return max(found) + 1 if found else 0

    return highest(r"pressure_(\d+)_mbar"), max(highest(r"flow_(\d+)_ul_min"), highest(r"volume_(\d+)_ul"),
                                                highest(r"stable_(\d+)"))


def write_timeline(target: Target, rows: Iterable[dict[str, Any]], pressure_count: int, sensor_count: int) -> int:
    """Timeline rows written at once, a reading or a QC row each as its columns say; how many."""
    path, run_id = locate(target)
    keyed = run_id is not None
    connection = connect(path)
    count = 0
    try:
        create_timeline(connection, pressure_count, sensor_count, keyed=keyed)
        fluidics, qc = _data_columns(connection, "fluidics"), _data_columns(connection, "qc")
        readings, bursts = [], []
        for row in rows:
            is_qc = bool(str(row.get("state") or ""))
            names = qc if is_qc else fluidics
            values = tuple(_value(row.get(name), name) for name in names)
            (bursts if is_qc else readings).append((run_id, *values) if keyed else values)
        with connection:
            if readings:
                connection.executemany(_insert("fluidics", fluidics, keyed), readings)
            if bursts:
                connection.executemany(_insert("qc", qc, keyed), bursts)
        count = len(readings) + len(bursts)
    finally:
        connection.close()
    if not keyed:
        fold(path)
    return count


def _value(value: Any, name: str) -> Any:
    if value is None or value == "":
        return None
    if name in TEXT_QC_COLUMNS:
        return str(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
