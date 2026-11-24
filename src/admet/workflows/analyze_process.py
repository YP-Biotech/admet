"""An analysis run in a process of its own, so the web server never waits for it.

A run in a thread shares Python's interpreter lock with the server: a heavy pipeline starves the
page, which then stops updating and drops its connection. In its own process, at a lower priority,
the run cannot hold the server up. Progress and the stop request cross over shared memory; the
report comes back on a queue when the run ends.
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import threading
from pathlib import Path
from typing import Any

from admet.workflows.analyze_runner import AnalysisStop, AnalyzeBatchReport, AnalyzeTarget

_CONTEXT = multiprocessing.get_context("spawn")       # never fork a process that runs a web server
_LIVE: set["AnalysisProcess"] = set()
_LIVE_LOCK = threading.Lock()


class AnalysisProcess:
    """One batch of analyses in a child process. Poll it; it is done when result() is not None."""

    def __init__(self, jobs: tuple[AnalyzeTarget, ...], results_dir: str | Path, *, niceness: int = 10) -> None:
        self.jobs = tuple(jobs)
        self.results_dir = str(results_dir)
        self.niceness = niceness
        self.after_file = _CONTEXT.Event()             # the same two requests an AnalysisStop carries
        self.now = _CONTEXT.Event()
        self._progress = _CONTEXT.Array("d", 2)       # the batch's percent, the current file's
        self._queue = _CONTEXT.Queue()
        self._process: Any = None
        self._outcome: tuple[str, Any] | None = None

    def start(self) -> None:
        self._process = _CONTEXT.Process(
            target=_work, args=(self.jobs, self.results_dir, self.after_file, self.now, self._progress,
                                self._queue, self.niceness), daemon=False)
        self._process.start()
        with _LIVE_LOCK:
            _LIVE.add(self)

    def requested(self) -> bool:
        return self.after_file.is_set() or self.now.is_set()

    @property
    def progress(self) -> float:
        return float(self._progress[0])

    @property
    def file_progress(self) -> float:
        return float(self._progress[1])

    def poll(self) -> bool:
        """Whether the run has ended, with a report or an error; never blocks."""
        if self._outcome is not None:
            return True
        try:
            self._outcome = self._queue.get_nowait()
        except queue.Empty:
            if self._process is not None and not self._process.is_alive():
                try:                                  # the last message may land just after the exit
                    self._outcome = self._queue.get(timeout=1.0)
                except queue.Empty:
                    self._outcome = ("error", f"the analysis process ended without a report "
                                              f"(exit code {self._process.exitcode})")
        if self._outcome is not None:
            self._finish()
        return self._outcome is not None

    def result(self) -> AnalyzeBatchReport:
        """The report of an ended run; raises the run's error as a RuntimeError."""
        kind, value = self._outcome or ("error", "the run has not ended")
        if kind == "error":
            raise RuntimeError(value)
        return value

    def wait(self, timeout: float | None = None) -> bool:
        if self._process is not None:
            self._process.join(timeout)
        return self.poll()

    def _finish(self) -> None:
        if self._process is not None:
            self._process.join(5)
        with _LIVE_LOCK:
            _LIVE.discard(self)


def live() -> int:
    """How many analysis processes are running."""
    with _LIVE_LOCK:
        return len(_LIVE)


def stop_all(*, now: bool = False) -> None:
    """Ask every running analysis to stop: after the file in hand, or at once."""
    with _LIVE_LOCK:
        runs = list(_LIVE)
    for run in runs:
        (run.now if now else run.after_file).set()
    for run in runs:
        run.poll()


def _work(jobs, results_dir, after_file, now, progress, out, niceness) -> None:
    """The child: run the batch, report progress, put the report or the error on OUT."""
    try:
        if niceness and hasattr(os, "nice"):
            os.nice(niceness)                          # the server and the rig come first
    except OSError:
        pass
    try:
        from admet.engines import create_engine_registry
        from admet.workflows.analyze_runner import AnalyzeBatchRunner

        stop = AnalysisStop()
        stop.after_file, stop.now = after_file, now

        def on_progress(percent: float) -> None:
            progress[0] = percent

        def on_file_progress(percent: float) -> None:
            progress[1] = percent

        report = AnalyzeBatchRunner(create_engine_registry("analyze"), results_dir=results_dir).run(
            jobs, on_progress=on_progress, on_file_progress=on_file_progress, stop=stop)
        progress[0] = 100.0
        out.put(("report", report))
    except BaseException as exc:                       # noqa: BLE001 - whatever ends the child is reported
        out.put(("error", f"{type(exc).__name__}: {exc}"))
