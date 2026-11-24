"""The export written by a process of its own into a folder on the server, as a .tar.gz.

Building the export in the web server held its event loop, and sending it to the browser over the
page's connection could run the server out of memory. The child builds droplet.csv, summary.csv and
details.json from the results it is given and writes them as one archive, through a part file that is
renamed only when whole; the page only polls it.
"""

from __future__ import annotations

import io
import multiprocessing
import os
import queue
import tarfile
import time
from pathlib import Path
from typing import Any

_CONTEXT = multiprocessing.get_context("spawn")       # never fork a process that runs a web server


def archive_name(stamp: float | None = None) -> str:
    return time.strftime("analysis_%Y%m%d_%H%M%S", time.localtime(stamp)) + ".tar.gz"


class ExportProcess:
    """Write the export of RESULTS, LIVES and DETAILS into FOLDER; poll() until it has ended."""

    def __init__(self, folder: str | Path, results: list[Any], lives: list[Any], details: dict[str, Any]) -> None:
        self.folder = Path(folder)
        self.path = self.folder / archive_name()
        self._args = (str(self.path), list(results), list(lives), dict(details))
        self._queue = _CONTEXT.Queue()
        self._process: Any = None
        self._outcome: tuple[str, str] | None = None

    def start(self) -> None:
        self._process = _CONTEXT.Process(target=_write, args=(*self._args, self._queue), daemon=False)
        self._process.start()

    def poll(self) -> bool:
        if self._outcome is not None:
            return True
        try:
            self._outcome = self._queue.get_nowait()
        except queue.Empty:
            if self._process is not None and not self._process.is_alive():
                try:
                    self._outcome = self._queue.get(timeout=1.0)
                except queue.Empty:
                    self._outcome = ("error", f"the export process ended without a word (exit code {self._process.exitcode})")
        if self._outcome is not None and self._process is not None:
            self._process.join(5)
        return self._outcome is not None

    def result(self) -> Path:
        """The archive written; raises the child's error as a RuntimeError."""
        kind, value = self._outcome or ("error", "the export has not ended")
        if kind == "error":
            raise RuntimeError(value)
        return Path(value)

    def wait(self, timeout: float | None = None) -> bool:
        if self._process is not None:
            self._process.join(timeout)
        return self.poll()


def write_archive(path: str | Path, files: dict[str, bytes]) -> Path:
    """FILES as one .tar.gz in a folder of the archive's own name, written whole or not at all."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    folder = path.name.removesuffix(".tar.gz")
    part = path.with_name(path.name + ".part")
    try:
        with tarfile.open(part, "w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(f"{folder}/{name}")
                info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
                archive.addfile(info, io.BytesIO(data))
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)
    return path


def _write(path, results, lives, details, out) -> None:
    try:
        if hasattr(os, "nice"):
            os.nice(10)
    except OSError:
        pass
    try:
        from admet.ui.analyze_results import export_files

        out.put(("done", str(write_archive(path, export_files(results, lives, details)))))
    except BaseException as exc:                       # noqa: BLE001 - whatever ends the child is reported
        out.put(("error", f"{type(exc).__name__}: {exc}"))
