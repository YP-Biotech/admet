"""How a plotted series is told apart: a hue per sample group, a shade per replicate, never the same twice.

A sample name ends in a replicate marker or not: "oil rep 2", "oil_rep02", "oil r2", "oil #2" and
"oil step 2" are replicates of "oil"; a name without one is a group of its own. Groups take the chart
hues in the order they are first listed, so a group keeps its hue when others are unticked. Within a
group each replicate is a lighter or darker shade of the hue; past five shades the line pattern
changes, past four patterns the marker. Past eight groups the hues come round again with another line
pattern, so no two series share both colour and pattern.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from admet.ui import theme

# Lightness is OKLCH L; plots sit on a white surface, where this band keeps a hue readable.
L_MIN, L_MAX, L_STEP = 0.43, 0.77, 0.065
SHADES = 5
DASHES: tuple[Any, ...] = ("solid", "dashed", "dotted", [12, 4, 3, 4])
SYMBOLS = ("circle", "triangle", "rect", "diamond")
_MARKERS = (
    re.compile(r"^(?P<group>.+?)[ _-]*rep(?:licate)?[ _-]*0*(?P<rep>\d+)$", re.IGNORECASE),
    re.compile(r"^(?P<group>.+?)[ _-]+r0*(?P<rep>\d+)$", re.IGNORECASE),
    re.compile(r"^(?P<group>.+?)\s*#0*(?P<rep>\d+)$"),
    re.compile(r"^(?P<group>.+?)\s+step\s+0*(?P<rep>\d+)$", re.IGNORECASE),
)


def sample_identity(name: str) -> tuple[str, str]:
    """A sample name as (group, replicate): "oil rep 2" is replicate "2" of "oil"; "oil" is its own group."""
    text = str(name or "").strip()
    for marker in _MARKERS:
        match = marker.match(text)
        if match:
            return match.group("group").strip(" _-"), match.group("rep")
    return text, ""


@dataclass(frozen=True)
class SeriesStyle:
    color: str                       # the shade, as hex
    hue: int                         # which chart hue
    shade: int                       # 0 the hue itself, then darker and lighter by turns
    dash: Any = "solid"              # an ECharts lineStyle.type: the hues' round
    symbol: str = "circle"           # an ECharts symbol: the shades' round
    marked: bool = False             # a line shows its symbol once the shades have come round
    decal: dict[str, Any] | None = None   # an ECharts itemStyle.decal for a bar, past the first rounds

    def line(self, *, width: float = 2.0, live: bool = False) -> dict[str, Any]:
        style: dict[str, Any] = {"color": self.color, "width": width, "type": self.dash}
        if live:
            style["width"] = max(1.0, width - 0.5)
        return style

    def item(self, *, live: bool = False) -> dict[str, Any]:
        style: dict[str, Any] = {"color": self.color}
        if live:
            style["decal"] = LIVE_DECAL
            style["opacity"] = 0.85
        elif self.decal is not None:
            style["decal"] = self.decal
        return style


def _stripes(rotation: float, gap: int = 4) -> dict[str, Any]:
    return {"symbol": "rect", "dashArrayX": [1, 0], "dashArrayY": [3, gap], "rotation": rotation,
            "color": "rgba(255,255,255,0.55)"}


# Bars past the first eight groups, or past the fifth replicate, wear a hatch along with the hue;
# a live bar wears a finer one the other way.
_DECALS: tuple[dict[str, Any] | None, ...] = (
    None, _stripes(math.pi / 4), _stripes(0), _stripes(math.pi / 2), _stripes(-math.pi / 4),
    _stripes(math.pi / 4, 8), _stripes(0, 8), _stripes(math.pi / 2, 8),
    {"symbol": "circle", "symbolSize": 0.5, "dashArrayX": [6, 0], "dashArrayY": [6, 0], "color": "rgba(255,255,255,0.6)"},
)
LIVE_DECAL = {"symbol": "rect", "dashArrayX": [1, 0], "dashArrayY": [2, 4], "rotation": -math.pi / 4,
              "color": "rgba(255,255,255,0.6)"}


def series_styles(names: list[str], *, listed: list[str] | None = None,
                  groups: dict[str, str] | None = None) -> dict[str, SeriesStyle]:
    """A style per sample name, by its group and replicate.

    A name's group is the one GROUPS gives it (a file's set), else what its replicate marker leaves.
    LISTED, when given, is every name or group on the screen in order: hues go to groups in that
    order, so a group keeps its hue whichever files are ticked. NAMES are the ones to style.
    """
    def group_of(name: str) -> str:
        return (groups or {}).get(name) or sample_identity(name)[0]

    order: list[str] = []
    for name in [*(listed or []), *names]:
        group = group_of(name)
        if group not in order:
            order.append(group)
    hues = len(theme.CHART_COLORS)
    replicates: dict[str, list[str]] = {}
    for name in names:
        group = group_of(name)
        replicates.setdefault(group, [])
        if name not in replicates[group]:
            replicates[group].append(name)
    styles: dict[str, SeriesStyle] = {}
    for name in names:
        group = group_of(name)
        index = order.index(group)
        hue, group_round = index % hues, index // hues
        position = replicates[group].index(name)
        shade, shade_round = position % SHADES, position // SHADES
        styles[name] = SeriesStyle(
            color=shade_of(theme.CHART_COLORS[hue], shade), hue=hue, shade=shade,
            dash=DASHES[group_round % len(DASHES)], symbol=SYMBOLS[shade_round % len(SYMBOLS)],
            marked=shade_round > 0, decal=_DECALS[(group_round * 3 + shade_round) % len(_DECALS)],
        )
    return styles


def shade_of(color: str, shade: int) -> str:
    """The hue itself (0), then darker and lighter by turns, five even steps that stay in the band."""
    if shade <= 0:
        return color
    lightness, chroma, hue = oklch(color)
    fits = [k for k in range(-(SHADES - 1), SHADES) if L_MIN <= lightness + k * L_STEP <= L_MAX]
    start = max(min(fits), min(-(SHADES // 2), max(fits) - (SHADES - 1)))    # five around the hue, inside the band
    window = [k for k in range(start, start + SHADES) if k in fits]
    order = sorted(window, key=lambda k: (abs(k), k))                            # 0, darker, lighter, ...
    return from_oklch(lightness + order[min(shade, len(order) - 1)] * L_STEP, chroma, hue)


# -- OKLCH, so lightness steps look even ----------------------------------------------------

def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _encode(channel: float) -> float:
    channel = min(1.0, max(0.0, channel))
    return channel * 12.92 if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055


def oklab(color: str) -> tuple[float, float, float]:
    text = color.lstrip("#")
    r, g, b = (_linear(int(text[i:i + 2], 16) / 255.0) for i in (0, 2, 4))
    long = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    medium = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    short = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    long, medium, short = (math.copysign(abs(v) ** (1 / 3), v) for v in (long, medium, short))
    return (0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short,
            1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short,
            0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short)


def _rgb(lightness: float, a: float, b: float) -> tuple[float, float, float]:
    long = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    medium = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    short = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
            -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
            -0.0041960863 * long - 0.7034186147 * medium + 1.7076147010 * short)


def oklch(color: str) -> tuple[float, float, float]:
    lightness, a, b = oklab(color)
    return lightness, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def from_oklch(lightness: float, chroma: float, hue: float) -> str:
    """The sRGB hex nearest the colour: chroma is let down until the colour fits the gamut."""
    for _ in range(40):
        a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
        rgb = _rgb(lightness, a, b)
        if all(-0.002 <= channel <= 1.002 for channel in rgb):
            break
        chroma *= 0.95
    return "#" + "".join(f"{round(_encode(channel) * 255):02x}" for channel in rgb)
