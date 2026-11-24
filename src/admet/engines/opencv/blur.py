"""How much longer than it is a droplet reads along the flow, because it moved while the shutter was open.

A droplet moving v px a frame with the shutter open for a share e of the frame interval draws a
streak b = v × e px long: its image along the flow is its still image averaged over a window b
wide. The outline is where the image is darker than the threshold, and the smeared ends fade,
so the outline grows by less than b: by s × b, where s, the share, depends on how dark the
droplet's edge is against the threshold. The share is found on the droplets themselves: each
one's profile along the flow is unsmeared (least squares, with the window known and a little
smoothing), and the lengths at the threshold before and after give s.
"""

from __future__ import annotations

from typing import Any

import numpy as np

SMOOTHING = 0.005        # weight of the smoothness term when unsmearing a profile
MARGIN_PX = 4            # px either side of a droplet's box taken into its profile
PROFILE_ROWS = 3         # rows about the centroid averaged into the profile


def blur_px(speed_px_frame: float | None, exposure_us: float | None, camera_fps: float | None) -> float:
    """How far a droplet moves while the shutter is open, px: its step between two of the camera's
    frames times the share of that interval the exposure takes. 0 when any of the three is unknown."""
    if not speed_px_frame or not exposure_us or not camera_fps or speed_px_frame <= 0:
        return 0.0
    return float(speed_px_frame) * float(exposure_us) * float(camera_fps) / 1e6


def smear_matrix(length: int, blur: float) -> np.ndarray:
    """The smear of a profile LENGTH px long as a matrix: each pixel the mean over a window BLUR px wide,
    its last pixel weighted by the fraction of it the window covers. The smeared profile is longer by
    the window, less one pixel."""
    whole, part = int(np.floor(blur)), float(blur - np.floor(blur))
    weights = np.array([1.0] * max(whole, 1) + ([part] if part > 1e-9 and whole >= 1 else []))
    weights /= weights.sum()
    matrix = np.zeros((length + len(weights) - 1, length))
    for column in range(length):
        matrix[column:column + len(weights), column] = weights
    return matrix


def unsmear(profile: np.ndarray, blur: float, smoothing: float = SMOOTHING) -> np.ndarray:
    """The still profile whose smear is PROFILE: non-negative least squares with a light smoothness
    term. It is shorter than the smeared one by the window, less one pixel."""
    from scipy.optimize import nnls

    profile = np.asarray(profile, dtype=float)
    smear = smear_matrix(len(profile) - int(np.ceil(blur)) + 1, blur)
    n = smear.shape[1]
    difference = np.diff(np.eye(n), axis=0)
    system = np.vstack([smear, np.sqrt(smoothing) * difference])
    return nnls(system, np.r_[profile, np.zeros(n - 1)], maxiter=20 * n)[0]


def extent(profile: np.ndarray, threshold: float) -> int:
    """px from the first to the last pixel at or above the threshold; 0 when none is."""
    above = np.flatnonzero(np.asarray(profile) >= threshold)
    return int(above[-1] - above[0] + 1) if len(above) else 0


def droplet_share(darker: np.ndarray, props: dict[str, Any], blur: float, threshold: float) -> float | None:
    """For one droplet on the darker-than-background image: how much of the window BLUR its outline grew by."""
    if blur < 1.0:
        return None
    x, w = int(props["bbox_x"]), int(props["bbox_width"])
    cy = int(round(props["centroid_y"]))
    low, high = max(x - MARGIN_PX, 0), min(x + w + MARGIN_PX, darker.shape[1])
    rows = darker[max(cy - PROFILE_ROWS // 2, 0):cy + PROFILE_ROWS // 2 + 1, low:high].astype(float)
    if rows.size == 0 or high - low <= blur + 4:
        return None
    seen = rows.mean(axis=0)
    long, short = extent(seen, threshold), extent(unsmear(seen, blur), threshold)
    if not long or not short:
        return None
    return (long - short) / blur


def measure_share(frames: list[np.ndarray], background: np.ndarray, detection_config: dict[str, Any],
                  blur: float, *, max_droplets: int = 300) -> dict[str, Any] | None:
    """The share over droplets found in FRAMES: the median and its spread, and how many droplets it is of.
    Only droplets wholly inside the frame, away from its ends, count."""
    from .detection import detect

    shares = []
    for frame in frames:
        found = detect(frame, background, detection_config)
        darker, threshold = found["images"]["darker"], found["threshold"]
        width = darker.shape[1]
        for props in found["properties"]:
            if props["bbox_x"] < 0.1 * width or props["bbox_x"] + props["bbox_width"] > 0.9 * width:
                continue
            share = droplet_share(darker, props, blur, threshold)
            if share is not None:
                shares.append(share)
        if len(shares) >= max_droplets:
            break
    if len(shares) < 20:
        return None
    shares_array = np.array(shares)
    low, high = np.percentile(shares_array, [25, 75])
    return {"share": round(float(np.median(shares_array)), 3), "share_iqr": [round(float(low), 3), round(float(high), 3)],
            "droplets": len(shares), "blur_px": round(float(blur), 2)}
