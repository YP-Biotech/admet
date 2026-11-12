"""Every stage of droplet detection on one frame, to see how a row's settings behave.

The same pipeline and settings as a run: background from sampled frames, the BTVS
threshold, morphology, then the contours kept and dropped. The background and the
threshold come from many frames, so they are worked out once per video, frame range
and crop; moving through frames only redoes that frame.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .engine import pipeline_config
from .pipeline import DropletPipeline

STAGES = ("original", "background", "removed", "threshold", "morphology", "contours")
KEPT, DROPPED = (60, 200, 90), (60, 60, 220)        # BGR: kept green, dropped red


def detection_stages(video_path: Path, settings: dict[str, Any], frame_index: int) -> dict[str, Any]:
    """Each stage of one frame as an 8-bit image, with the threshold and contour counts; or an error."""
    path = Path(video_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"error": f"Video file is not readable: {path}"}
    roi = tuple(int(settings.get(key) or 0) for key in ("roi_x", "roi_y", "roi_width", "roi_height"))
    key = (str(path), mtime, int(settings.get("start_frame") or 0), settings.get("end_frame"), roi)
    pipeline = _prepared(key, _frozen(settings))
    if isinstance(pipeline, str):
        return {"error": pipeline}
    reader = pipeline.video_reader
    if not reader.seek_frame(int(frame_index)):
        return {"error": f"Cannot reach frame {frame_index}"}
    frame = reader.read_frame()
    if frame is None:
        return {"error": f"Cannot read frame {frame_index}"}
    result = pipeline._process_frame(frame, int(frame_index))
    morphology = pipeline.detector._apply_morphology(result["binary"])
    kept, raw = result["contours"], result["raw_contours"]
    contours = cv2.cvtColor(_gray(result["original"]), cv2.COLOR_GRAY2BGR)
    cv2.drawContours(contours, raw, -1, DROPPED, 1)       # every contour found; the kept ones drawn over
    cv2.drawContours(contours, kept, -1, KEPT, 1)
    background = result["background"] if result["background"] is not None else np.zeros_like(_gray(frame))
    return {
        "error": "",
        "threshold": pipeline.threshold_value,
        "threshold_method": getattr(pipeline.threshold_finder, "method_used", ""),
        "kept": len(kept),
        "dropped": max(len(raw) - len(kept), 0),
        "segmentation": dict(pipeline.detector.config),
        "images": {
            "original": _gray(result["original"]),
            "background": _gray(background),
            "removed": _gray(result["processed"]),
            "threshold": _gray(result["binary"]),
            "morphology": _gray(morphology),
            "contours": contours,
        },
    }


def _frozen(settings: dict[str, Any]) -> tuple:
    return tuple(sorted((key, value) for key, value in settings.items() if key != "preview_frame"))


@lru_cache(maxsize=4)
def _prepared(key: tuple, frozen: tuple) -> DropletPipeline | str:
    """A pipeline with the video open, its background and its threshold; or why not."""
    settings = dict(frozen)
    pipeline = DropletPipeline(pipeline_config({**settings, "video_path": key[0]}))
    pipeline.set_video(key[0])
    if not pipeline.video_reader.open():
        return f"OpenCV cannot open video: {key[0]}"
    if not pipeline._compute_background():
        return "Background extraction failed"
    if not pipeline._determine_threshold():
        return "Threshold selection failed"
    return pipeline


def _gray(image: np.ndarray) -> np.ndarray:
    image = np.asarray(image)
    if image.dtype != np.uint8:
        image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
