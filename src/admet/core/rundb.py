"""A run's database, run.db: its record and its timeline in one file, each write a transaction.

The record is the run as one document (the plan and the protocol as they ran, the state and the
times, the events, the measurements, the videos, the results calculated later), kept whole in
one row. The timeline is two tables on one clock, elapsed_s from the moment polling began:
fluidics, ten readings a second, and qc, one row a second per burst of a recording; their
columns are the ones run.csv had, so the CSV the export writes reads as it always did.

While a run records, the file is in WAL mode: the poll thread and the live QC hand their rows to
one writer thread that commits them once a second, and the record can be written at any time
beside it. When the timeline closes the WAL is folded back and the file left whole on its own,
so a copy of it, or a reader on a network share where WAL cannot work, sees everything.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

DB_NAME = "run.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 10.0
COMMIT_INTERVAL_S = 1.0
TEXT_QC_COLUMNS = {"timestamp", "recording_id", "step", "part", "diameter_unit", "state"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS record (id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL);
"""


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """A connection to a run's database. Read-only when asked; a file on a share that cannot be
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


def read_record(path: str | Path) -> dict[str, Any]:
    """The run's record; empty when the file is missing or holds none."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        connection = connect(path, readonly=True)
        try:
            row = connection.execute("SELECT body FROM record WHERE id = 1").fetchone()
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


def write_record(path: str | Path, record: dict[str, Any]) -> None:
    """The run's record replaced whole, in one transaction."""
    body = json.dumps(record, allow_nan=False, default=str)
    connection = connect(path)
    try:
        with connection:
            connection.execute("INSERT OR REPLACE INTO record (id, body) VALUES (1, ?)", (body,))
    finally:
        connection.close()


def update_record(path: str | Path, change) -> dict[str, Any]:
    """Read, change and write the record in one transaction, so two writers never lose each other's
    change; CHANGE takes the record and returns it, or None to leave it as it was."""
    connection = connect(path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT body FROM record WHERE id = 1").fetchone()
            record = json.loads(row[0]) if row else {}
            changed = change(record)
            if changed is not None:
                connection.execute("INSERT OR REPLACE INTO record (id, body) VALUES (1, ?)",
                                   (json.dumps(changed, allow_nan=False, default=str),))
                record = changed
        return record
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


def create_timeline(connection: sqlite3.Connection, pressure_count: int, sensor_count: int) -> None:
    """The two timeline tables, if the file has none yet."""
    fluidics = ", ".join(f'"{name}" {"TEXT" if name == "timestamp" else "REAL"}'
                         for name in fluidics_columns(pressure_count, sensor_count))
    qc = ", ".join(f'"{name}" {"TEXT" if name in TEXT_QC_COLUMNS else "REAL"}' for name in qc_columns())
    with connection:
        connection.execute(f"CREATE TABLE IF NOT EXISTS fluidics ({fluidics})")
        connection.execute(f"CREATE TABLE IF NOT EXISTS qc ({qc})")


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def timeline_columns(path: str | Path) -> list[str]:
    """The columns of the timeline as a CSV: the fluidics ones, then the QC ones after elapsed_s."""
    connection = connect(path, readonly=True)
    try:
        fluidics = _table_columns(connection, "fluidics")
        qc = _table_columns(connection, "qc")
    finally:
        connection.close()
    if not fluidics and not qc:
        return []
    return [*(fluidics or ["timestamp", "elapsed_s"]), *[name for name in qc if name not in ("timestamp", "elapsed_s")]]


def timeline_rows(path: str | Path) -> list[dict[str, str]]:
    """Every timeline row in time order, as the CSV reader gave them: each column's value as text,
    empty where the row has none (a reading has no QC values, a QC row no readings). A calculation
    reads the timeline many times over, so the rows are kept per version of the file, and each
    caller gets its own copies."""
    path = Path(path)
    if not path.is_file():
        return []
    return [dict(row) for row in _timeline_rows(str(path.resolve()), _version(path))]


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
def _timeline_rows(path_text: str, _version_key: tuple) -> tuple[dict[str, str], ...]:
    path = Path(path_text)
    columns = timeline_columns(path)
    if not columns:
        return ()
    connection = connect(path, readonly=True)
    try:
        rows: list[tuple[float, int, dict[str, str]]] = []
        for order, table in enumerate(("fluidics", "qc")):
            names = _table_columns(connection, table)
            if not names:
                continue
            quoted = ", ".join(f'"{name}"' for name in names)
            for values in connection.execute(f"SELECT {quoted} FROM {table} ORDER BY rowid"):
                row = {column: "" for column in columns}
                row.update({name: _text(value) for name, value in zip(names, values)})
                elapsed = values[names.index("elapsed_s")] if "elapsed_s" in names else None
                rows.append((float(elapsed) if elapsed is not None else float("inf"), order, row))
    finally:
        connection.close()
    rows.sort(key=lambda item: (item[0], item[1]))
    return tuple(row for _elapsed, _order, row in rows)


def timeline_digest(path: str | Path) -> str:
    """A digest of the timeline's content, whatever else the file holds."""
    digest = hashlib.sha256()
    for row in timeline_rows(path):
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

    def __init__(self, path: str | Path, pressure_count: int, sensor_count: int) -> None:
        self.path = Path(path)
        self._fluidics = fluidics_columns(pressure_count, sensor_count)
        self._qc = qc_columns()
        self._pending: list[tuple[str, tuple]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.rows_written = 0
        connection = connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            create_timeline(connection, pressure_count, sensor_count)
            self._fluidics = [name for name in _table_columns(connection, "fluidics")]
        finally:
            connection.close()
        self._thread = threading.Thread(target=self._run, name="TimelineWriter", daemon=True)
        self._thread.start()

    def add_fluidics(self, values: dict[str, Any]) -> None:
        self._add("fluidics", tuple(values.get(name) for name in self._fluidics))

    def add_qc(self, values: dict[str, Any]) -> None:
        self._add("qc", tuple(values.get(name) for name in self._qc))

    def _add(self, table: str, row: tuple) -> None:
        with self._lock:
            self._pending.append((table, row))

    def close(self) -> None:
        """Every row taken so far committed, the writer stopped and the file folded whole."""
        self._stop.set()
        self._wake.set()
        self._thread.join(BUSY_TIMEOUT_S + 5)
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
        fluidics = [row for table, row in pending if table == "fluidics"]
        qc = [row for table, row in pending if table == "qc"]
        for attempt in range(3):
            try:
                with connection:
                    if fluidics:
                        connection.executemany(self._insert("fluidics", self._fluidics), fluidics)
                    if qc:
                        connection.executemany(self._insert("qc", self._qc), qc)
                self.rows_written += len(pending)
                return
            except sqlite3.OperationalError:
                time.sleep(0.2 * (attempt + 1))
        with self._lock:                                    # still locked out: try again next second
            self._pending[:0] = pending

    @staticmethod
    def _insert(table: str, columns: list[str]) -> str:
        return (f"INSERT INTO {table} ({', '.join(chr(34) + name + chr(34) for name in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})")


# -- export -----------------------------------------------------------------------------------

def export_csv(path: str | Path, target: str | Path) -> int:
    """The timeline as run.csv was: one header, every row in time order; how many rows."""
    rows = timeline_rows(path)
    columns = timeline_columns(path)
    with Path(target).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def export_json(path: str | Path, target: str | Path) -> None:
    """The record as run.json was."""
    Path(target).write_text(json.dumps(read_record(path), indent=2, allow_nan=False) + "\n", encoding="utf-8")


def channel_counts(columns: Iterable[str]) -> tuple[int, int]:
    """How many pressure and sensor channels timeline columns name: one past the highest index of each."""
    columns = list(columns)

    def highest(pattern: str) -> int:
        found = [int(match.group(1)) for match in (re.fullmatch(pattern, column) for column in columns) if match]
        return max(found) + 1 if found else 0

    return highest(r"pressure_(\d+)_mbar"), max(highest(r"flow_(\d+)_ul_min"), highest(r"volume_(\d+)_ul"),
                                                highest(r"stable_(\d+)"))


def write_timeline(path: str | Path, rows: Iterable[dict[str, Any]], pressure_count: int, sensor_count: int) -> int:
    """Timeline rows written at once, a reading or a QC row each as its columns say (for migrated runs
    and tests); how many."""
    connection = connect(path)
    count = 0
    try:
        create_timeline(connection, pressure_count, sensor_count)
        fluidics, qc = _table_columns(connection, "fluidics"), _table_columns(connection, "qc")
        readings, bursts = [], []
        for row in rows:
            is_qc = bool(str(row.get("state") or ""))
            names = qc if is_qc else fluidics
            (bursts if is_qc else readings).append(tuple(_value(row.get(name), name) for name in names))
        with connection:
            if readings:
                connection.executemany(TimelineWriter._insert("fluidics", fluidics), readings)
            if bursts:
                connection.executemany(TimelineWriter._insert("qc", qc), bursts)
        count = len(readings) + len(bursts)
    finally:
        connection.close()
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
