"""The Export data page: where the runs go, what is here and what is on the server, how the export goes.

The Export button sits in the stage's action bar like every other action; this page holds the folder,
the runs and the line that reports progress.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidgetItem, QVBoxLayout, QWidget

from admet.core.runs import Run
from admet.ui import theme as ui
from admet.ui.tables import GridTable, fit_table_height
from admet.ui.tasks import Tasks

COLUMNS = ("Run", "Recorded", "State", "Videos", "Size", "Exported")


def _size(count: int) -> str:
    if count >= 1e9:
        return f"{count / 1e9:.1f} GB"
    if count >= 1e6:
        return f"{count / 1e6:.0f} MB"
    return f"{count / 1e3:.0f} kB" if count else "—"


class ExportPanel(QWidget):
    changed = Signal()                 # the folder or the busy state changed; the action bar follows

    def __init__(self, api):
        super().__init__()
        self.api = api
        self.tasks = Tasks(self)
        self._progress: tuple[str, str, int, int] | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(ui.spacing("default"))

        row = QHBoxLayout()
        row.setSpacing(ui.spacing("control"))
        row.addWidget(QLabel("Export to"))
        self.path = QLineEdit(self.api.service.export_path())
        self.path.setPlaceholderText("The server folder that holds the runs, as mounted on this computer")
        self.path.editingFinished.connect(self._path_edited)
        row.addWidget(self.path, 1)
        browse = ui.button("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        root.addLayout(row)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.table = GridTable(0, len(COLUMNS))
        self.table.setObjectName("RawConfigTable")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(GridTable.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(GridTable.SelectionMode.NoSelection)
        root.addWidget(self.table)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._ticker = QTimer(self)
        self._ticker.setInterval(300)
        self._ticker.timeout.connect(self._show_progress)
        self.refresh()

    # -- the folder -------------------------------------------------------------
    def _path_edited(self) -> None:
        self.api.service.set_export_path(self.path.text())
        self.refresh()
        self.changed.emit()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Export to", self.path.text() or str(Path.home()))
        if chosen:
            self.path.setText(chosen)
            self._path_edited()

    # -- the runs ---------------------------------------------------------------
    def refresh(self) -> None:
        rows, here_videos, here_bytes, on_server = [], 0, 0, 0
        for entry in self.api.service.runs.list():
            folder = Path(entry["directory"])
            record = Run(folder).read()
            videos = [video for video in record.get("artifacts", {}).get("videos", []) if video.get("video_path")]
            if isinstance(record.get("recording"), dict) and record["recording"].get("video_path"):
                videos.append(record["recording"])
            here = [folder / Path(video["video_path"]).name for video in videos
                    if (folder / Path(video["video_path"]).name).is_file()]
            size = sum(path.stat().st_size for path in folder.iterdir() if path.is_file())
            export = record.get("export") if isinstance(record.get("export"), dict) else {}
            if not videos:
                where = "none"
            elif len(here) == len(videos):
                where = f"{len(videos)} here"
            elif here:
                where = f"{len(videos) - len(here)} on the server, {len(here)} here"
            else:
                where = f"{len(videos)} on the server"
            here_videos += len(here)
            here_bytes += sum(path.stat().st_size for path in here)
            on_server += 1 if export else 0
            rows.append((entry["name"], (entry["at"] or "")[:16].replace("T", " "), entry["state"], where, _size(size),
                         (export.get("at") or "")[:16].replace("T", " ")))
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        fit_table_height(self.table, max_rows=20)
        self.summary.setText(self._summary(len(rows), here_videos, here_bytes, on_server))

    def _summary(self, runs: int, here_videos: int, here_bytes: int, on_server: int) -> str:
        if not runs:
            return "No runs recorded yet."
        parts = [f"{runs} run(s)", f"{here_videos} video(s) here" + (f", {_size(here_bytes)} to send" if here_bytes else ""),
                 f"{on_server} exported"]
        folder = self.path.text().strip()
        if not folder:
            parts.append("choose the server folder to export")
        elif not Path(folder).is_dir():
            parts.append("the server folder is not there; is it mounted?")
        return " · ".join(parts)

    # -- exporting --------------------------------------------------------------
    def export(self) -> bool:
        """Send every finished run; False when there is nowhere to send them or an export is running."""
        if not self.path.text().strip() or self.tasks.busy:
            return False
        self.api.service.set_export_path(self.path.text())
        self._progress = None
        self.status.setText("Exporting…")
        self._ticker.start()
        started = self.tasks.submit(lambda: self.api.service.export_runs(progress=self._note), self._done, self._failed)
        self.changed.emit()
        return started

    def _note(self, run_id: str, name: str, done: int, size: int) -> None:
        self._progress = (run_id, name, done, size)

    def _show_progress(self) -> None:
        if self._progress is None:
            return
        run_id, name, done, size = self._progress
        self.status.setText(f"Exporting {run_id}: {name}, {done / 1e6:.0f} of {size / 1e6:.0f} MB")

    def _done(self, reports) -> None:
        self._ticker.stop()
        sent = [report for report in reports if "error" not in report]
        errors = [report for report in reports if "error" in report]
        moved = sum(len(report["removed"]) for report in sent)
        text = f"{len(sent)} run(s) exported, {moved} video(s) now on the server only."
        if errors:
            text += " Left for next time: " + "; ".join(f"{report['run_id']}: {report['error']}" for report in errors)
        self.status.setText(text)
        self.refresh()
        self.changed.emit()

    def _failed(self, exc) -> None:
        self._ticker.stop()
        self.status.setText(f"Export failed: {exc}")
        self.changed.emit()
