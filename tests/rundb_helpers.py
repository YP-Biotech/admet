"""Run databases for tests: a run's timeline set or extended from CSV text, as a run records it."""

import csv
import io
from pathlib import Path

from admet.core import rundb


def set_timeline(db, text):
    """The run's timeline replaced by the rows of CSV text, its header naming the columns; DB is a
    run.db's path or a run's locator in control.db."""
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    path, run_id = rundb.locate(db)
    if path.is_file():
        connection = rundb.connect(path)
        try:
            with connection:
                for table in ("fluidics", "qc"):
                    if run_id is None:
                        connection.execute(f"DROP TABLE IF EXISTS {table}")
                    elif rundb._table_columns(connection, table):
                        connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        finally:
            connection.close()
    rundb.write_timeline(db, rows, *rundb.channel_counts(reader.fieldnames or []))
    return db if run_id is not None else Path(db)


def add_to_timeline(db, text):
    """Rows of CSV text, its header naming the columns, added to the end of the run's timeline."""
    rundb.write_timeline(db, list(csv.DictReader(io.StringIO(text))), *rundb.channel_counts(rundb.timeline_columns(db)))


def timeline_text(rows, columns):
    """CSV text from rows as tuples in the order of COLUMNS."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    writer.writerows(rows)
    return out.getvalue()


def set_timeline_rows(db, rows):
    """The run's timeline replaced by ROWS, dicts by column name, as the timeline's rows came."""
    columns = list(rows[0]) if rows else ["elapsed_s"]
    return set_timeline(db, timeline_text([[row.get(column, "") for column in columns] for row in rows], columns))
