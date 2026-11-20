"""Analyze's registry: the folders it watches, the sources it knows, and where their results are.

    analyze/registry.json      the roots (folders of runs), every source by path with its sample
                               name, engine, settings and the run it came from, as the user left it
    analyze/results/<key>/     one folder per source and detection settings: rows.jsonl, the
                               detections, and result.json, the summary; the key is a digest of the
                               file's content and those settings

Analyze tracks where the raw files are; it records nothing about how they were made. A video
analysed the same way twice is analysed once, and a copy of the file finds the same result.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from admet.core.runs import RECORD
from admet.core.session import write_atomic

REGISTRY = "registry.json"
RESULTS_DIR = "results"
# The settings that change what is detected, and so what a result is of. Scale and fps are
# applied when a result is read, so they are not part of the key.
KEY_FIELDS = ("start_frame", "end_frame", "max_frames", "frame_limit", "roi_x", "roi_y", "roi_width", "roi_height",
              "k_sigma", "satellite_px", "min_area", "skip_left_pct", "skip_right_pct", "gate_pct")
VIDEO_SUFFIXES = {".avi", ".mp4", ".mov", ".mkv"}
IMAGE_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp"}


def infer_engine(path: str | Path) -> str:
    source = Path(path)
    if source.is_dir():
        return "cellpose"
    suffix = source.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "opencv"
    if suffix in IMAGE_SUFFIXES:
        return "cellpose"
    raise ValueError(f"cannot infer analyze engine for {source}")


def content_key(source: str | Path, settings: dict[str, Any] | None = None) -> str:
    """What a result is of: the file's content (its size and its first and last megabyte, enough to
    tell videos apart without reading gigabytes) and the settings that change what is detected."""
    path = Path(source)
    digest = hashlib.sha256()
    if path.is_file():
        size = path.stat().st_size
        digest.update(str(size).encode())
        with path.open("rb") as handle:
            digest.update(handle.read(1 << 20))
            if size > 2 << 20:
                handle.seek(-(1 << 20), 2)
                digest.update(handle.read(1 << 20))
    elif path.is_dir():
        for file in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(file.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with file.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    else:
        digest.update(str(path).encode("utf-8"))
    kept = {key: settings[key] for key in KEY_FIELDS if settings and key in settings}
    digest.update(json.dumps(kept, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()[:24]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Registry:
    """One registry per analyze room."""

    def __init__(self, analyze_dir: str | Path) -> None:
        self.root = Path(analyze_dir)
        self.path = self.root / REGISTRY
        self.results_dir = self.root / RESULTS_DIR

    # -- the record ----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        if not isinstance(record, dict):
            record = {}
        record.setdefault("roots", [])
        record.setdefault("sources", [])
        return record

    def write(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n")

    # -- roots: folders of runs ------------------------------------------------
    def roots(self) -> list[str]:
        return [str(root) for root in self.read()["roots"]]

    def add_root(self, folder: str | Path) -> bool:
        """Watch a folder of runs; False when it was watched already."""
        text = str(Path(folder).expanduser().resolve())
        record = self.read()
        if text in record["roots"]:
            return False
        record["roots"].append(text)
        self.write(record)
        return True

    def remove_root(self, folder: str | Path) -> None:
        text = str(Path(folder).expanduser().resolve())
        record = self.read()
        record["roots"] = [root for root in record["roots"] if root != text]
        self.write(record)

    # -- sources ----------------------------------------------------------------
    def sources(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.read()["sources"] if isinstance(entry, dict) and entry.get("path")]

    def source(self, path: str | Path) -> dict[str, Any] | None:
        text = _canonical(path)
        return next((entry for entry in self.sources() if _canonical(entry["path"]) == text), None)

    def add_source(self, path: str | Path, *, engine: str = "", sample_id: str = "", run: str | Path | None = None,
                   settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Know a file; None when it was known already."""
        source = Path(path).expanduser().resolve()
        record = self.read()
        if any(_canonical(entry.get("path")) == str(source) for entry in record["sources"] if isinstance(entry, dict)):
            return None
        entry = {"path": str(source), "engine": engine or infer_engine(source), "sample_id": sample_id or source.stem,
                 "cache_policy": "use", "active": True, "settings": dict(settings or {}),
                 "run": str(Path(run).resolve()) if run else "", "added_at": _now()}
        record["sources"].append(entry)
        self.write(record)
        return dict(entry)

    def update_source(self, path: str | Path, **fields: Any) -> dict[str, Any] | None:
        text = _canonical(path)
        record = self.read()
        for entry in record["sources"]:
            if isinstance(entry, dict) and _canonical(entry.get("path")) == text:
                entry.update(fields)
                self.write(record)
                return dict(entry)
        return None

    def remove_source(self, path: str | Path) -> bool:
        text = _canonical(path)
        record = self.read()
        kept = [entry for entry in record["sources"] if not (isinstance(entry, dict) and _canonical(entry.get("path")) == text)]
        if len(kept) == len(record["sources"]):
            return False
        record["sources"] = kept
        self.write(record)
        return True

    def set_sources(self, sources: list[dict[str, Any]]) -> None:
        """The whole list as the screen has it, in its order."""
        record = self.read()
        known = {_canonical(entry.get("path")): entry for entry in record["sources"] if isinstance(entry, dict)}
        record["sources"] = [{**known.get(_canonical(entry["path"]), {"added_at": _now()}), **entry} for entry in sources]
        self.write(record)

    # -- runs in the watched folders ------------------------------------------------
    def scan(self, folder: str | Path | None = None) -> list[dict[str, Any]]:
        """Every video of every run in the folder (or in all the watched folders) that is not known yet."""
        folders = [Path(folder)] if folder is not None else [Path(root) for root in self.roots()]
        added = []
        for root in folders:
            for record_path in sorted(_run_records(root)):
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                run_dir = record_path.parent
                name = str(record.get("tag") or (record.get("normalized_settings") or {}).get("protocol", {}).get("name")
                           or record.get("name") or run_dir.name)
                stamp = run_dir.name[:15]
                if re.fullmatch(r"\d{8}_\d{6}", stamp):
                    name = f"{name} {stamp[9:11]}:{stamp[11:13]}"            # the run's time tells runs apart
                videos = [video for video in record.get("artifacts", {}).get("videos", [])
                          if isinstance(video, dict) and video.get("video_path")]
                if isinstance(record.get("recording"), dict) and record["recording"].get("video_path"):
                    videos.append(record["recording"])
                for video in videos:
                    path = run_dir / Path(str(video["video_path"])).name
                    if not path.is_file():
                        continue
                    step = video.get("step")
                    sample = f"{name} step {step}" if step is not None and len(videos) > 1 else name
                    entry = self.add_source(path, engine="opencv", sample_id=sample, run=run_dir)
                    if entry is not None:
                        added.append(entry)
        return added

    # -- what the screen remembers -------------------------------------------------------
    def view_setting(self, key: str, default: Any = None) -> Any:
        view = self.read().get("view")
        return view.get(key, default) if isinstance(view, dict) else default

    def set_view_setting(self, key: str, value: Any) -> None:
        record = self.read()
        view = record.get("view") if isinstance(record.get("view"), dict) else {}
        record["view"] = {**view, key: value}
        self.write(record)

    # -- results ------------------------------------------------------------------------
    def result_dir(self, source: str | Path, settings: dict[str, Any] | None = None) -> Path:
        return self.results_dir / content_key(source, settings)


def _canonical(path: Any) -> str:
    try:
        return str(Path(str(path)).expanduser().resolve())
    except OSError:
        return str(path)


def _run_records(root: Path) -> list[Path]:
    """The run records under a folder: a data folder (runs/*/run.json), an export folder (*/run.json), or one run."""
    if (root / RECORD).is_file():
        return [root / RECORD]
    found = list((root / "runs").glob(f"*/{RECORD}")) if (root / "runs").is_dir() else []
    found += list(root.glob(f"*/{RECORD}"))
    return list(dict.fromkeys(found))
