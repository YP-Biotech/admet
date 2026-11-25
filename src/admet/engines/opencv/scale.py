"""The frame's scale from the channel itself: its two walls, found on the empty channel.

A wall seen from above is a dark line along the channel. On the background (the bright side of
each pixel over many frames, so droplets are gone) the darkest row in the top half and the
darkest row in the bottom half are the two walls; their distance in px against the channel's
known width is the µm per px. Each wall is placed to a fraction of a pixel by the parabola
through its darkest row and the rows either side. A frame whose walls are not clearly darker
than the channel, sit on the frame's edge, or are too close together gives no scale at all,
rather than a wrong one.
"""

from __future__ import annotations

from typing import Any

import numpy as np

MIN_CONTRAST = 0.05      # a wall is at least this much darker than the channel between the walls
MIN_SPAN = 0.4           # the walls are at least this share of the frame's height apart
EDGE_COLUMNS = 0.1       # the share of the width left out at each end, where the nozzle or outlet may be


def background(frames: list[np.ndarray], percentile: float = 85) -> np.ndarray:
    """The empty channel from frames spread over a recording: the bright side of each pixel."""
    return np.percentile(np.stack([_gray(frame) for frame in frames]), percentile, axis=0)


def wall_rows(image: np.ndarray) -> tuple[float, float] | None:
    """The two walls' rows to a fraction of a pixel, top first; None when the frame shows no clear pair."""
    image = _gray(image).astype(float)
    height, width = image.shape
    if height < 8:
        return None
    skip = int(width * EDGE_COLUMNS)
    profile = np.median(image[:, skip:width - skip] if width - 2 * skip >= 8 else image, axis=1)
    half = height // 2
    top, bottom = int(np.argmin(profile[:half])), half + int(np.argmin(profile[half:]))
    if top in (0, height - 1) or bottom in (0, height - 1):
        return None                                     # a wall on the frame's edge may be cut off
    inside = profile[top + 2:bottom - 1]
    if bottom - top < MIN_SPAN * height or len(inside) < 3:
        return None
    channel = float(np.median(inside))
    if channel <= 0 or min(channel - profile[top], channel - profile[bottom]) < MIN_CONTRAST * channel:
        return None
    return _vertex(profile, top), _vertex(profile, bottom)


def scale_from_background(image: np.ndarray, channel_width_um: float) -> dict[str, Any] | None:
    """The µm per px the walls give, with how it was found; None without a clear pair of walls."""
    if not channel_width_um or channel_width_um <= 0:
        return None
    walls = wall_rows(image)
    if walls is None:
        return None
    top, bottom = walls
    height, width = _gray(image).shape
    return {"microns_per_pixel": round(float(channel_width_um) / (bottom - top), 4),
            "channel_width_um": float(channel_width_um), "wall_px": [round(top, 2), round(bottom, 2)],
            "frame": [int(width), int(height)], "method": "walls_auto"}


def _vertex(profile: np.ndarray, index: int) -> float:
    a, b, c = float(profile[index - 1]), float(profile[index]), float(profile[index + 1])
    curvature = a - 2 * b + c
    return index + 0.5 * (a - c) / curvature if curvature > 0 else float(index)


def _gray(frame: np.ndarray) -> np.ndarray:
    frame = np.asarray(frame)
    return frame.mean(axis=2) if frame.ndim == 3 else frame
