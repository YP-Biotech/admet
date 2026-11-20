"""Run the analysis engines over sources, one file at a time, into analyze's results folder.

Each source analysed with given detection settings has one folder, named by the content key of
the file and those settings: rows.jsonl holds every detection the engine wrote, result.json the
summary and when it was made. The same file analysed the same way again is read from there.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from admet.core.engine import EngineRegistry, action_spec
from admet.core.registry import content_key, infer_engine
from admet.core.run import JsonlRunSink, RunJob, RunResult
from admet.core.session import write_atomic

__all__ = ["AnalysisStop", "AnalysisStopped", "AnalyzeBatchReport", "AnalyzeBatchRunner", "AnalyzeJobReport",
           "AnalyzeTarget", "SHUTDOWN", "infer_engine", "running_batches"]


class AnalysisStop:
    """How running analyses are asked to stop: after the file in hand, or at once.

    A file stopped at once leaves no result.json in its folder, so it reads as not done and
    is analysed again on the next run. Files already finished keep their results either way.
    """

    def __init__(self) -> None:
        self.after_file = threading.Event()
        self.now = threading.Event()

    def requested(self) -> bool:
        return self.after_file.is_set() or self.now.is_set()


class AnalysisStopped(BaseException):
    """Raised from an engine's progress report; BaseException so a broad except in an engine cannot swallow it."""


# Set by the analysis server when it is shut down (Ctrl+C).
SHUTDOWN = AnalysisStop()
_running = 0
_running_lock = threading.Lock()


def running_batches() -> int:
    with _running_lock:
        return _running


CACHE_POLICIES = {"use", "discard", "skip"}
ROWS = "rows.jsonl"
RESULT = "result.json"


@dataclass(frozen=True)
class AnalyzeTarget:
    source_path: Path
    engine: str = ""
    sample_id: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    cache_policy: str = "use"


@dataclass(frozen=True)
class AnalyzeJobReport:
    job_id: str
    engine: str
    sample_id: str
    source_path: str
    status: str
    result_dir: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalyzeBatchReport:
    jobs: tuple[AnalyzeJobReport, ...]


class AnalyzeBatchRunner:
    def __init__(self, registry: EngineRegistry, *, results_dir: str | Path) -> None:
        self.registry = registry
        self.results_dir = Path(results_dir)

    def run(
        self,
        targets: list[AnalyzeTarget] | tuple[AnalyzeTarget, ...],
        *,
        on_progress: Callable[[float], None] | None = None,
        on_file_progress: Callable[[float], None] | None = None,
        stop: AnalysisStop | None = None,
    ) -> AnalyzeBatchReport:
        """Run every target; ON_PROGRESS gets the batch percent, ON_FILE_PROGRESS the current file's.

        STOP (by default the server's shutdown request) ends the batch early; see AnalysisStop.
        """
        global _running
        stop = stop or SHUTDOWN
        with _running_lock:
            _running += 1
        try:
            return self._run(tuple(targets), on_progress=on_progress, on_file_progress=on_file_progress, stop=stop)
        finally:
            with _running_lock:
                _running -= 1

    def _run(self, targets, *, on_progress, on_file_progress, stop: AnalysisStop) -> AnalyzeBatchReport:
        total = len(targets)
        state = {"index": 0}

        def begin_file() -> Callable[[int, str], None]:
            index = state["index"]
            state["index"] += 1
            if on_progress is not None and total:
                on_progress(index / total * 100.0)
            if on_file_progress is not None:
                on_file_progress(0.0)

            def report(percent: int, message: str = "") -> None:
                if stop.now.is_set():
                    raise AnalysisStopped()
                percent = max(0.0, min(100.0, float(percent)))
                if on_file_progress is not None:
                    on_file_progress(percent)
                if on_progress is not None and total:
                    on_progress((index + percent / 100.0) / total * 100.0)

            return report

        reports: list[AnalyzeJobReport] = []
        for index, target in enumerate(targets, start=1):
            source = Path(target.source_path)
            sample_id = target.sample_id or source.stem
            if stop.requested():
                reports.append(_stopped_report(index, target, sample_id))
                continue
            file_progress = begin_file()
            cache_policy = _cache_policy(target.cache_policy)
            try:
                engine_id = target.engine or infer_engine(source)
            except ValueError as exc:
                reports.append(_skipped_report(index, target.engine, sample_id, str(source), str(exc)))
                continue
            if engine_id not in self.registry.ids():
                reports.append(_skipped_report(index, engine_id, sample_id, str(source),
                                               f"unknown analyze engine: {engine_id}"))
                continue
            job_id = f"{_safe(sample_id)}_{index}_{engine_id}"
            if cache_policy == "skip":
                reports.append(AnalyzeJobReport(job_id=job_id, engine=engine_id, sample_id=sample_id,
                                                source_path=str(source), status="skipped",
                                                metadata={"cache_policy": cache_policy}))
                continue

            engine = self.registry.create(engine_id)
            action = action_spec(engine.actions, "run_analysis")
            action_settings = {key: value for key, value in target.settings.items()
                               if key in set(action.params) - {"video_path", "input_dir"}}
            key = content_key(source, action_settings)
            result_dir = self.results_dir / key
            if cache_policy == "discard" and result_dir.exists():
                shutil.rmtree(result_dir)
            if cache_policy == "use" and engine_id == "cellpose" and action_settings.get("frame_limit") \
                    and not _serves(result_dir, engine_id):
                # A run over every frame serves a frame limit: the limit is applied when the rows are read.
                whole = self.results_dir / content_key(source, {**action_settings, "frame_limit": None})
                if _serves(whole, engine_id):
                    result_dir = whole
            rows_path, result_path = result_dir / ROWS, result_dir / RESULT
            if cache_policy == "use" and _serves(result_dir, engine_id):
                metadata = dict(_read_json(result_path))
                metadata["cached"] = True
                result = RunResult(job_id=job_id, engine=engine_id, action="run_analysis", status="cached",
                                   metadata=metadata)
            else:
                result_dir.mkdir(parents=True, exist_ok=True)
                try:
                    with JsonlRunSink(rows_path) as sink:
                        result = engine.run(RunJob(
                            id=job_id, engine=engine_id, action="run_analysis", settings=action_settings,
                            inputs=_job_inputs(engine_id, source), cache_dir=result_dir, sink=sink,
                            progress=file_progress,
                            metadata={"sample_id": sample_id, "item_id": sample_id, "file_id": key,
                                      "source_path": str(source), "cache_policy": cache_policy},
                        ))
                    result = replace(result, metadata={
                        **result.metadata,
                        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "source_path": str(source), "engine": engine_id, "sample_id": sample_id, "file_id": key,
                        "settings": dict(target.settings),
                    })
                    _write_json(result_path, dict(result.metadata))
                except AnalysisStopped:
                    # Stopped mid-file: no result.json, so the folder reads as not done.
                    reports.append(_stopped_report(index, target, sample_id))
                    continue
            reports.append(_job_report(result, sample_id=sample_id, source_path=str(source),
                                       cache_policy=cache_policy, result_dir=str(result_dir)))
        if on_progress is not None:
            on_progress(100.0)
        return AnalyzeBatchReport(jobs=tuple(reports))


def _serves(result_dir: Path, engine_id: str) -> bool:
    """Whether a results folder holds a finished result that can be read with today's settings."""
    if not (result_dir / RESULT).is_file() or not (result_dir / ROWS).is_file():
        return False
    metadata = _read_json(result_dir / RESULT)
    if engine_id == "opencv":
        from admet.ui.analyze_results import stale_window

        return not stale_window(metadata)
    return True


def _cache_policy(value: str) -> str:
    policy = str(value or "use").strip().lower()
    if policy not in CACHE_POLICIES:
        raise ValueError(f"unknown cache policy: {value!r}")
    return policy


def _job_inputs(engine_id: str, source_path: Path) -> dict[str, Path]:
    if engine_id == "opencv":
        return {"video": source_path}
    if engine_id == "cellpose":
        return {"input_dir": source_path}
    raise LookupError(f"unknown analyze engine: {engine_id}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(data, default=str) + "\n")


def _job_report(result: RunResult, *, sample_id: str, source_path: str, cache_policy: str,
                result_dir: str) -> AnalyzeJobReport:
    metadata = dict(result.metadata)
    metadata.setdefault("cache_policy", cache_policy)
    return AnalyzeJobReport(job_id=result.job_id, engine=result.engine, sample_id=sample_id, source_path=source_path,
                            status=result.status, result_dir=result_dir, metadata=metadata, warnings=result.warnings)


def _stopped_report(index: int, target: AnalyzeTarget, sample_id: str) -> AnalyzeJobReport:
    return AnalyzeJobReport(job_id=f"{_safe(sample_id)}_{index}_{_safe(target.engine or 'unknown')}",
                            engine=target.engine, sample_id=sample_id, source_path=str(target.source_path),
                            status="stopped",
                            metadata={"reason": "analysis stopped before this file finished; run again to continue"})


def _skipped_report(index: int, engine: str, sample_id: str, source_path: str, reason: str) -> AnalyzeJobReport:
    return AnalyzeJobReport(job_id=f"{_safe(sample_id)}_{index}_{_safe(engine or 'unknown')}", engine=engine,
                            sample_id=sample_id, source_path=source_path, status="skipped",
                            metadata={"reason": reason}, warnings=(reason,))


def _safe(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-") or "file"
