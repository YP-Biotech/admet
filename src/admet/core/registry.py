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
from functools import lru_cache
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from admet.core.experiments import find_sample_id
from admet.core.rundb import read_record
from admet.core.runs import RECORD
from admet.core.files import write_atomic

log = logging.getLogger(__name__)

REGISTRY = "registry.json"
RESULTS_DIR = "results"
# The settings that change what is detected, and so what a result is of. Scale and fps are
# applied when a result is read, so they are not part of the key; so are the window (skip left
# and right) and the gate, which only choose among the droplets found: the key carries them at
# their defaults whatever a file says, so a result made with the whole width serves every window.
KEY_FIELDS = ("start_frame", "end_frame", "max_frames", "frame_limit", "roi_x", "roi_y", "roi_width", "roi_height",
              "k_sigma", "satellite_px", "min_area", "skip_left_pct", "skip_right_pct", "gate_pct")
READ_TIME_DEFAULTS = {"skip_left_pct": 0.0, "skip_right_pct": 0.0, "gate_pct": 50.0}
VIDEO_SUFFIXES = {".avi", ".mp4", ".mov", ".mkv"}
IMAGE_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp"}


@lru_cache(maxsize=4096)
def _file_identity(path: str, size: int, _mtime_ns: int) -> "hashlib._Hash":
    """The digest of a file's size and its first and last megabyte, kept while the file stays as it is."""
    digest = hashlib.sha256()
    digest.update(str(size).encode())
    with open(path, "rb") as handle:
        digest.update(handle.read(1 << 20))
        if size > 2 << 20:
            handle.seek(-(1 << 20), 2)
            digest.update(handle.read(1 << 20))
    return digest


@lru_cache(maxsize=1024)
def _folder_identity(path: str, _dir_mtime_ns: int) -> "hashlib._Hash":
    """The digest of a folder's files by name, size and time, no bytes read; kept while the folder's
    own time stays, which changes when a file is added or taken away."""
    digest = hashlib.sha256()
    root = Path(path)
    for file in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = file.stat()
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
    return digest


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
    """What a result is of: the source's identity and the settings that change what is detected.

    A file is known by its size and its first and last megabyte, enough to tell videos apart
    without reading gigabytes; an image folder by the names, sizes and times of its files, so
    listing a folder reads no image. Either is worked out once per process while the source
    stays as it is, so a table redraw costs one stat, not a read."""
    path = Path(source)
    if path.is_file():
        stat = path.stat()
        digest = _file_identity(str(path), stat.st_size, stat.st_mtime_ns).copy()
    elif path.is_dir():
        stat = path.stat()
        digest = _folder_identity(str(path), stat.st_mtime_ns).copy()
    else:
        digest = hashlib.sha256(str(path).encode("utf-8"))
    kept = {key: settings[key] for key in KEY_FIELDS if settings and key in settings}
    kept.update({key: value for key, value in READ_TIME_DEFAULTS.items() if key in kept})
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
                   settings: dict[str, Any] | None = None, protocol: str = "", run_time: str = "",
                   replicate: int | None = None, condition: str = "", note: str = "",
                   sample_code: str = "") -> dict[str, Any] | None:
        """Know a file; None when it was known already. SAMPLE_ID empty means the name is built;
        CONDITION is the generation condition the file starts in, the one its run was planned with;
        SAMPLE_CODE the id of the sample it is of (K7Q-03-r2), from its run or its folder's name."""
        source = Path(path).expanduser().resolve()
        record = self.read()
        if any(_canonical(entry.get("path")) == str(source) for entry in record["sources"] if isinstance(entry, dict)):
            return None
        entry = {"path": str(source), "engine": engine or infer_engine(source), "sample_id": sample_id,
                 "cache_policy": "use", "active": True, "settings": dict(settings or {}),
                 "run": str(Path(run).resolve()) if run else "", "protocol": protocol, "run_time": run_time,
                 "replicate": replicate, "condition": str(condition or "").strip(), "note": str(note or ""),
                 "sample_code": str(sample_code or ""), "added_at": _now()}
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
        """What is new in the folder (or in all the watched folders): archives of runs taken in first,
        then every video of every run, and every folder of images named with a sample's id."""
        folders = [Path(folder)] if folder is not None else [Path(root) for root in self.roots()]
        added = []
        for root in folders:
            self.ingest(root)
            for record_path in sorted(_run_records(root)):
                record = read_record(record_path)
                if not record:
                    continue
                run_dir = record_path.parent
                protocol = str(record.get("tag") or (record.get("normalized_settings") or {}).get("protocol", {}).get("name")
                               or record.get("name") or run_dir.name)
                videos = [video for video in record.get("artifacts", {}).get("videos", [])
                          if isinstance(video, dict) and video.get("video_path")]
                if isinstance(record.get("recording"), dict) and record["recording"].get("video_path"):
                    videos.append(record["recording"])
                for video in videos:
                    path = run_dir / Path(str(video["video_path"])).name
                    if not path.is_file():
                        continue
                    # The protocol, the run's time and the replicate are fields of their own; the sample name
                    # is built from them and the file's condition unless the user writes one over it. The condition is
                    # the one the run was planned with in control, else the protocol with its setpoints,
                    # until the user puts the file elsewhere.
                    replicate = replicate_of(video, len(videos))
                    entry = self.add_source(path, engine="opencv", sample_id="", run=run_dir,
                                            protocol=protocol, run_time=run_time(run_dir), replicate=replicate,
                                            condition=str(record.get("condition") or "") or protocol_condition(record),
                                            sample_code=run_sample_code(record, replicate, len(videos)))
                    if entry is not None:
                        added.append(entry)
            for images in _named_image_folders(root):
                entry = self.add_source(images, engine="cellpose", sample_code=find_sample_id(images.name) or "")
                if entry is not None:
                    added.append(entry)
        return added

    def ingest(self, folder: str | Path) -> list[str]:
        """Archives of runs (admet export --tar) dropped into the folder, unpacked into it once each:
        their run folders join the folder's runs. The archives themselves are left as they are."""
        import tarfile

        root = Path(folder)
        if not root.is_dir():
            return []
        record = self.read()
        done = {(entry.get("path"), entry.get("size"), entry.get("mtime_ns"))
                for entry in record.get("ingested", []) if isinstance(entry, dict)}
        taken = []
        for archive in sorted(root.glob("*.tar")):
            stat = archive.stat()
            key = (str(archive.resolve()), stat.st_size, stat.st_mtime_ns)
            if key in done:
                continue
            try:
                with tarfile.open(archive) as tar:
                    tar.extractall(root, filter="data")      # nothing outside the folder, no links, no devices
            except (OSError, tarfile.TarError) as exc:
                log.warning("could not take in %s: %s", archive, exc)
                continue
            record.setdefault("ingested", []).append({"path": key[0], "size": key[1], "mtime_ns": key[2],
                                                      "at": _now()})
            taken.append(archive.name)
        if taken:
            self.write(record)
        return taken

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


def run_sample_code(record: dict[str, Any], replicate: int | None, videos: int) -> str:
    """The id of the sample a run's video is of: the run's id in its experiment and the replicate
    (K7Q-03-r2); the run's id alone for its one video; empty for a run of no experiment."""
    from admet.core.experiments import sample_id

    code = str(record.get("run_code") or "")
    if not code:
        return ""
    if replicate:
        return sample_id(code, replicate)
    return code if videos == 1 else ""


def _named_image_folders(root: Path, depth: int = 3) -> list[Path]:
    """Folders of images under ROOT (DEPTH levels at most) whose names carry a sample's id."""
    from admet.core.experiments import find_sample_id

    found: list[Path] = []
    if not root.is_dir():
        return found

    def walk(folder: Path, level: int) -> None:
        try:
            children = list(folder.iterdir())
        except OSError:
            return
        if find_sample_id(folder.name) and any(child.suffix.lower() in IMAGE_SUFFIXES for child in children
                                               if child.is_file()):
            found.append(folder)
            return
        if level < depth:
            for child in sorted(children):
                if child.is_dir() and not child.name.startswith("."):
                    walk(child, level + 1)

    walk(root, 0)
    return found


CHANNEL_LETTERS = ("o", "c", "b")           # oil, cells, beads: the rig's channels 0, 1, 2
CHANNEL_LABELS = ("Oil L", "Cells M", "Beads M")


def condition_channels(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Each channel of the run's recording step as set: index, label, mode ("flow" or "pressure") and
    value, in µL/min or mbar. The recording step is the first that records, else the first with a
    setpoint; a run whose protocol is unknown has none."""
    from admet.workflows.json_protocol import resolve

    protocol = (record.get("normalized_settings") or {}).get("protocol")
    if not isinstance(protocol, dict):
        return []
    try:
        steps = resolve(protocol).get("steps", [])
    except (TypeError, ValueError):
        steps = [s for s in protocol.get("steps", []) if isinstance(s, dict)]
    with_setpoints = [s for s in steps if s.get("sensor_setpoints") or s.get("pressure_setpoints")]
    recording = [s for s in with_setpoints if s.get("record") is True]
    step = (recording or with_setpoints or [None])[0]
    if step is None:
        return []
    channels = []
    for index, label in enumerate(CHANNEL_LABELS):
        flow = _setpoint(step.get("sensor_setpoints"), index)
        pressure = _setpoint(step.get("pressure_setpoints"), index)
        if flow is not None:
            channels.append({"index": index, "label": label, "mode": "flow", "value": flow})
        elif pressure is not None:
            channels.append({"index": index, "label": label, "mode": "pressure", "value": pressure})
    return channels


def protocol_condition(record: dict[str, Any]) -> str:
    """The generation condition of a run, by what it did: its protocol and the oil, cells and beads setpoints of
    its recording step, "dropseq_3n o300 c40 b40"; a pressure setpoint says so, "o600mbar". Nothing known is ""."""
    protocol = (record.get("normalized_settings") or {}).get("protocol")
    if not isinstance(protocol, dict):
        return ""
    name = str(record.get("tag") or protocol.get("name") or "").strip()
    parts = [f"{CHANNEL_LETTERS[c['index']]}{c['value']:g}" + ("mbar" if c["mode"] == "pressure" else "")
             for c in condition_channels(record)]
    return " ".join([name, *parts]).strip()


def _setpoint(setpoints: Any, index: int) -> float | None:
    if not isinstance(setpoints, dict):
        return None
    value = setpoints.get(str(index))
    try:
        return float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def run_time(run_dir: str | Path) -> str:
    """When a run folder's run began, from its name: "2026-11-17 19:30"; empty when the name has no stamp."""
    stamp = Path(run_dir).name[:15]
    if re.fullmatch(r"\d{8}_\d{6}", stamp):
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
    return ""


REPLICATE = re.compile(r"rep(?:licate)?[ _-]*0*(\d+)", re.IGNORECASE)


def replicate_of(video: dict[str, Any], videos: int) -> int | None:
    """Which replicate of its run a video is: the number its step's name carries ("set01_rep02" is
    the second; a protocol runs one set, its steps are replicates), else the step's number when the
    run made several videos; None for a lone video."""
    match = REPLICATE.search(str(video.get("step_name") or ""))
    if match:
        return int(match.group(1))
    step = video.get("step")
    try:
        return int(step) if step is not None and videos > 1 else None
    except (TypeError, ValueError):
        return None


def _canonical(path: Any) -> str:
    try:
        return str(Path(str(path)).expanduser().resolve())
    except OSError:
        return str(path)


def _run_records(root: Path) -> list[Path]:
    """The run databases under a folder: a data folder (runs/*/run.db), an export folder (*/run.db), or one run."""
    if (root / RECORD).is_file():
        return [root / RECORD]
    found = list((root / "runs").glob(f"*/{RECORD}")) if (root / "runs").is_dir() else []
    found += list(root.glob(f"*/{RECORD}"))
    return list(dict.fromkeys(found))
