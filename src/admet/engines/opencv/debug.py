"""Every stage of droplet detection on one frame, to see how a row's settings behave.

The same pipeline and settings as a run: the background from sampled frames, the frame
matched to it, the difference, the threshold, the bodies, then what was kept, dropped
or set aside as a satellite. The background comes from many frames, so it is built once
per video, frame range and crop; moving through frames only redoes that frame.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .engine import pipeline_config
from .pipeline import DropletPipeline

STAGES = ("original", "background", "matched", "darker", "threshold", "bodies", "droplets")
KEPT, DROPPED, SATELLITE, LINE, GATE = (60, 200, 90), (60, 60, 220), (0, 200, 230), (200, 200, 200), (220, 120, 60)


def detection_stages(video_path: Path, settings: dict[str, Any], frame_index: int) -> dict[str, Any]:
    """Each stage of one frame as an 8-bit image, with the threshold and counts; or an error."""
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
    height, width = frame.shape[:2]
    view = cv2.cvtColor(_gray(frame), cv2.COLOR_GRAY2BGR)
    low, high = result["window"]
    for x in (low, high):
        cv2.line(view, (x, 0), (x, height), LINE, 1)
    gate = int(round(width * float(pipeline.detection.get("gate_pct", 50.0)) / 100))
    if 0 < gate < width:
        cv2.line(view, (gate, 0), (gate, height), GATE, 2)
    dropped = [r["contour"] for r in result["rejected"] if r["why"] != "outside window"]
    for contours, color in ((dropped, DROPPED), ([s["contour"] for s in result["satellites"]], SATELLITE),
                            (result["contours"], KEPT)):
        if contours:                                   # OpenCV refuses an empty list
            cv2.drawContours(view, contours, -1, color, 2)
    return {
        "error": "",
        "threshold": round(float(pipeline.threshold_value or 0), 1),
        "noise_sigma": round(float(pipeline.noise_sigma or 0), 2),
        "kept": len(result["properties"]),
        "dropped": len(dropped),
        "satellites": len(result["satellites"]),
        "detection": dict(pipeline.detection),
        "images": {
            "original": _gray(frame),
            "background": _gray(pipeline.background),
            "matched": _gray(detection_matched(frame, pipeline.background)),
            "darker": _stretch(result["processed"]),
            "threshold": result["binary"],
            "bodies": result["bodies"],
            "droplets": view,
        },
    }


def detection_matched(frame: np.ndarray, background: np.ndarray) -> np.ndarray:
    from .detection import match_brightness

    return match_brightness(_gray(frame), background)


def _frozen(settings: dict[str, Any]) -> tuple:
    return tuple(sorted((key, value) for key, value in settings.items() if key != "preview_frame"))


@lru_cache(maxsize=4)
def _prepared(key: tuple, frozen: tuple) -> DropletPipeline | str:
    """A pipeline with the video open and its background built; or why not."""
    settings = dict(frozen)
    pipeline = DropletPipeline(pipeline_config({**settings, "video_path": key[0]}))
    pipeline.set_video(key[0])
    if not pipeline.video_reader.open():
        return f"OpenCV cannot open video: {key[0]}"
    if not pipeline._compute_background():
        return "Background extraction failed"
    return pipeline


def _gray(image: np.ndarray) -> np.ndarray:
    image = np.asarray(image)
    if image.dtype != np.uint8:
        image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _stretch(image: np.ndarray) -> np.ndarray:
    return cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
