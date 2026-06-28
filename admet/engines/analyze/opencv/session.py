"""
Save and load ADM analysis bundles and batch project files.
"""

from __future__ import annotations

import copy
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .beo import BackgroundExtractor

SCHEMA_VERSION = 1


def bundle_path_for_video(video_path: str, parent_dir: str | Path = None) -> Path:
    video = Path(video_path)
    base = Path(parent_dir) if parent_dir is not None else video.parent
    return base / f"{video.stem}.admet"


def analysis_cache_dir(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir)


def save_analysis(
    bundle_dir: str | Path,
    results: dict,
    config: dict,
    video_path: str,
    background: np.ndarray,
    video_id: str = None,
) -> dict:
    bundle = Path(bundle_dir)
    contours_dir = bundle / "contours"
    bundle.mkdir(parents=True, exist_ok=True)
    contours_dir.mkdir(exist_ok=True)

    slim_results = _slim_results(results)
    _make_contour_paths_portable(slim_results)

    meta = _metadata(
        bundle=bundle,
        video_path=video_path,
        video_id=video_id or Path(video_path).stem,
        config=config,
        results=slim_results,
    )

    with (bundle / "meta.json").open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True)
    with (bundle / "results.pkl").open("wb") as handle:
        pickle.dump(slim_results, handle, protocol=pickle.HIGHEST_PROTOCOL)
    np.save(bundle / "background.npy", background)
    return meta


def load_analysis(bundle_dir: str | Path) -> dict:
    bundle = Path(bundle_dir)
    with (bundle / "meta.json").open("r", encoding="utf-8") as handle:
        meta = json.load(handle)
    with (bundle / "results.pkl").open("rb") as handle:
        results = pickle.load(handle)
    background = np.load(bundle / "background.npy")

    _rebind_contour_paths(results, bundle / "contours")
    config = meta.get("config", {})
    extractor = BackgroundExtractor(config.get("background", {}))
    extractor.background = background

    return {
        "results": results,
        "config": config,
        "video_path": meta.get("video_path"),
        "video_id": meta.get("video_id"),
        "background": background,
        "background_extractor": extractor,
        "bundle_path": str(bundle),
        "meta": meta,
    }


def save_project(project_path: str | Path, items: list[dict]) -> dict:
    project = {
        "schema_version": SCHEMA_VERSION,
        "items": items,
    }
    path = Path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(project, handle, indent=2, sort_keys=True)
    return project


def load_project(project_path: str | Path) -> dict:
    with Path(project_path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_analysis_meta(bundle_dir: str | Path) -> dict:
    with (Path(bundle_dir) / "meta.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def analysis_matches_config(
    bundle_dir: str | Path,
    config: dict,
    video_path: str = None,
) -> bool:
    try:
        meta = load_analysis_meta(bundle_dir)
    except Exception:
        return False

    if video_path and meta.get("video_path") and str(video_path) != meta.get("video_path"):
        return False

    return _normalized_config(config) == _normalized_config(meta.get("config", {}))


def _metadata(
    bundle: Path,
    video_path: str,
    video_id: str,
    config: dict,
    results: dict,
) -> dict:
    analysis = config.get("analysis", {})
    summary_keys = [
        "total_droplets",
        "total_detections",
        "frames_processed",
        "mean_diameter_um",
        "std_diameter_um",
        "mean_speed_mm_s",
        "frequency_hz",
        "threshold",
        "true_stats",
    ]
    summary = {key: results.get(key) for key in summary_keys if key in results}
    return {
        "schema_version": SCHEMA_VERSION,
        "video_path": str(video_path),
        "video_id": video_id,
        "bundle_path": str(bundle),
        "config": config,
        "fps": analysis.get("fps"),
        "um_per_pixel": analysis.get("microns_per_pixel"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "frame_count": results.get("frames_processed", 0),
        "summary": summary,
    }


def _normalized_config(config: dict) -> dict:
    normalized = copy.deepcopy(config or {})
    processing = normalized.get("processing")
    if isinstance(processing, dict):
        processing.pop("cache_dir", None)
        processing.pop("store_preview_frames", None)
    return _json_normalized(normalized)


def _json_normalized(value):
    if isinstance(value, dict):
        return {
            str(key): _json_normalized(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_json_normalized(item) for item in value]
    return value


def _slim_results(results: dict) -> dict:
    slim = copy.deepcopy(results)
    for key in ("stored_frames", "background", "extractor", "finder"):
        slim.pop(key, None)
    return slim


def _make_contour_paths_portable(results: dict):
    for prop in _iter_properties(results):
        contour_path = prop.get("contour_path")
        if contour_path:
            prop["contour_path"] = Path(contour_path).name


def _rebind_contour_paths(results: dict, contours_dir: Path):
    for prop in _iter_properties(results):
        contour_path = prop.get("contour_path")
        if contour_path:
            prop["contour_path"] = str(contours_dir / Path(contour_path).name)


def _iter_properties(results: dict):
    for frame in results.get("frame_data", []) or []:
        for prop in frame.get("properties", []) or []:
            yield prop
    for track in results.get("tracks", []) or []:
        for prop in track.get("properties", []) or []:
            yield prop
