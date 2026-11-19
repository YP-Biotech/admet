"""The Export data page: where the runs go, which ones went, and the button that sends them."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidgetItem, QVBoxLayout, QWidget

from admet.core.runs import Run
from admet.ui import theme as ui
from admet.ui.tables import GridTable, fit_table_height
from admet.ui.tasks import Tasks

COLUMNS = ("Run", "Recorded", "State", "Videos", "Exported")


class ExportPanel(QWidget):
    """A path, an Export button, a line saying what is happening, and the runs with where their videos are."""

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
        self.path.setPlaceholderText("The server folder that holds the runs, mounted on this computer")
        self.path.editingFinished.connect(self._path_edited)
        row.addWidget(self.path, 1)
        browse = ui.button("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        self.export_button = ui.button("Export", variant="primary")
        self.export_button.clicked.connect(self.export)
        row.addWidget(self.export_button)
        root.addLayout(row)

        self.status = QLabel("Finished runs are copied whole and checked; their videos then leave this computer.")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

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

        self._ticker = QTimer(self)
        self._ticker.setInterval(300)
        self._ticker.timeout.connect(self._show_progress)
        self.refresh()

    # -- the path -------------------------------------------------------------
    def _path_edited(self) -> None:
        self.api.service.set_export_path(self.path.text())
        self._sync_buttons()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Export to", self.path.text() or str(Path.home()))
        if chosen:
            self.path.setText(chosen)
            self._path_edited()

    # -- the runs -------------------------------------------------------------
    def refresh(self) -> None:
        rows = []
        for entry in self.api.service.runs.list():
            record = Run(entry["directory"]).read()
            videos = [video for video in record.get("artifacts", {}).get("videos", []) if video.get("video_path")]
            if isinstance(record.get("recording"), dict) and record["recording"].get("video_path"):
                videos.append(record["recording"])
            here = [video for video in videos if (Path(entry["directory"]) / Path(video["video_path"]).name).is_file()]
            export = record.get("export") if isinstance(record.get("export"), dict) else {}
            if not videos:
                where = "none"
            elif here and len(here) == len(videos):
                where = f"{len(videos)} here"
            elif here:
                where = f"{len(videos) - len(here)} on the server, {len(here)} here"
            else:
                where = f"{len(videos)} on the server"
            rows.append((entry["name"], (entry["at"] or "")[:16].replace("T", " "), entry["state"], where,
                         (export.get("at") or "")[:16].replace("T", " ")))
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        fit_table_height(self.table, max_rows=12)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.export_button.setEnabled(bool(self.path.text().strip()) and not self.tasks.busy)

    # -- exporting ------------------------------------------------------------
    def export(self) -> None:
        if not self.path.text().strip():
            self.status.setText("Choose the server folder first.")
            return
        self.api.service.set_export_path(self.path.text())
        self._progress = None
        self.status.setText("Exporting…")
        self._ticker.start()
        self._sync_buttons()
        self.tasks.submit(lambda: self.api.service.export_runs(progress=self._note), self._done, self._failed)
        self._sync_buttons()

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

    def _failed(self, exc) -> None:
        self._ticker.stop()
        self.status.setText(f"Export failed: {exc}")
        self._sync_buttons()
