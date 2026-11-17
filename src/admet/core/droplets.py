"""What control and analyze both say about droplets, so the two never disagree."""

from __future__ import annotations

import math
from typing import Any


def dispersed_flow_ul_min(record: dict[str, Any] | None) -> float:
    """The flow that becomes droplets, from a recording's record: every flow-controlled channel
    but the oil, in µL/min; 0 when the record says nothing."""
    total = 0.0
    for channel in (record or {}).get("channels") or []:
        if not isinstance(channel, dict) or "oil" in str(channel.get("label", "")).lower():
            continue
        total += float(channel.get("flow_ul_min") or 0.0)
    return total


def dispersed_channels(labels: tuple[str, ...] | list[str]) -> tuple[int, ...]:
    """The channel numbers whose liquid becomes droplets: every one but the oil."""
    return tuple(index for index, label in enumerate(labels) if "oil" not in str(label).lower())


def expected_rate_hz(dispersed_ul_min: float, diameter_um: float) -> float:
    """Droplets a second the flow must make at this size: the dispersed flow over a sphere's volume."""
    if dispersed_ul_min <= 0 or diameter_um <= 0:
        return 0.0
    volume_um3 = math.pi / 6 * diameter_um ** 3
    return dispersed_ul_min / 60 * 1e9 / volume_um3


def time_of_day(stamp: Any) -> float | None:
    """Seconds since midnight of an ISO stamp with a date, or of a bare HH:MM:SS.mmm; None when unreadable."""
    text = str(stamp or "").strip()
    if not text:
        return None
    clock = text.split("T", 1)[1] if "T" in text else text
    try:
        hours, minutes, seconds = clock.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None
