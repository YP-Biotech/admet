"""Run databases for tests: a run's timeline set or extended from CSV text, as a run records it."""

import csv
import io
from pathlib import Path

from admet.core import rundb


def set_timeline(db, text):
    """The run's timeline replaced by the rows of CSV text, its header naming the columns."""
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    db = Path(db)
    if db.is_file():
        connection = rundb.connect(db)
        try:
            with connection:
                connection.execute("DROP TABLE IF EXISTS fluidics")
                connection.execute("DROP TABLE IF EXISTS qc")
        finally:
            connection.close()
    rundb.write_timeline(db, rows, *rundb.channel_counts(reader.fieldnames or []))
    return db


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
