from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from admet.engines.acquisition.camera.acquisition import bytes_per_pixel  # noqa: F401  (the UI's name for it)

from admet.workflows import Stage


@dataclass(frozen=True)
class PanelSpec:
    key: str
    title: str
    object_name: str = "Panel"


@dataclass(frozen=True)
class LogState:
    lines: tuple[str, ...]
    empty_text: str = "No actions yet."


CORE_PANELS = (
    PanelSpec("action_box", "Action Box"),
    PanelSpec("action_panel", "Action Panel"),
    PanelSpec("main", "Main Window", "MainPanel"),
    PanelSpec("results", "Results"),
    PanelSpec("log", "Action Log"),
)

CONTROL_PANELS = (
    PanelSpec("action_box", "Action Box"),
    PanelSpec("action_panel", "Action Panel"),
    PanelSpec("main", "Main Window", "MainPanel"),
    PanelSpec("channel_manager", "Channel Manager"),
    PanelSpec("results", "Results"),
    PanelSpec("log", "Action Log"),
)


def panel_specs(*, channel_manager: bool = False, include_results: bool = True) -> tuple[PanelSpec, ...]:
    panels = CONTROL_PANELS if channel_manager else CORE_PANELS
    if include_results:
        return panels
    return tuple(panel for panel in panels if panel.key != "results")


def log_state(entries: list[str] | tuple[str, ...], *, limit: int = 80, empty_text: str = "No actions yet.") -> LogState:
    return LogState(lines=tuple(entries[-limit:]), empty_text=empty_text)


def connected_devices(
    *,
    camera: bool = False,
    camera_live: bool = False,
    fluidics: bool = False,
    recording: bool = False,
    protocol: str = "",
) -> tuple[str, ...]:
    """What is still live, named the way an operator would name it.

    Closing the window disconnects everything on the way out, so this is the list
    that has to be shown before it happens -- a stray click should not take the
    hardware down in the middle of an experiment.
    """
    devices = []
    if camera:
        devices.append("Camera (live preview)" if camera_live else "Camera")
    if fluidics:
        devices.append("Fluigent pressure controller")
    if recording:
        devices.append("Recording in progress")
    if protocol:
        devices.append(f"Protocol running ({protocol})")
    return tuple(devices)


def live_stats(metadata: dict[str, Any]) -> str:
    """What changes while the camera runs, in one line for the frame: state, time, fps, frames.

    Only the live numbers; the camera's settings are in the parameter table already.
    """
    if not metadata.get("camera_connected"):
        return ""
    fps = metadata.get("camera_fps")
    rate = f"{float(fps):.0f} fps" if fps else "fps —"
    if metadata.get("camera_recording"):
        frames = int(metadata.get("camera_recorded_frames") or 0)
        parts = [f"REC {_clock(float(metadata.get('camera_record_elapsed') or 0))}", rate,
                 f"{frames:,} frames".replace(",", " ")]
        dropped = int(metadata.get("camera_dropped_frames") or 0)
        if dropped:
            parts.append(f"{dropped:,} dropped".replace(",", " "))
        return " · ".join(parts)
    if metadata.get("camera_live"):
        return f"LIVE · {rate}"
    return "connected · not live"


class FocusTracker:
    """The sharpness of the last half second of frames against the best seen since the reset.

    Turn the focus until the fraction reads 1: it falls on either side of the sharpest position.
    """

    def __init__(self, window_s: float = 0.5) -> None:
        self.window_s = window_s
        self.samples: list[tuple[float, float]] = []
        self.best = 0.0

    def add(self, score: float, now: float) -> None:
        self.samples.append((now, score))
        self.samples = [(t, s) for t, s in self.samples if now - t <= self.window_s]
        self.best = max(self.best, self.current)

    @property
    def current(self) -> float:
        return sum(s for _, s in self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def fraction(self) -> float | None:
        """The current sharpness as a share of the best, None before any frame."""
        if not self.samples or self.best <= 0:
            return None
        return min(self.current / self.best, 1.0)

    def reset(self) -> None:
        self.samples = []
        self.best = 0.0


NERD_SEPARATOR = "  "


def heat_rgb(t: float) -> tuple[int, int, int]:
    """Blue for the lowest level through green to red for the highest, t in 0..1."""
    t = min(max(t, 0.0), 1.0)
    stops = [(0, 0, 255), (0, 200, 255), (0, 220, 0), (255, 220, 0), (255, 0, 0)]
    position = t * (len(stops) - 1)
    i = min(int(position), len(stops) - 2)
    f = position - i
    return tuple(int(round(a + (b - a) * f)) for a, b in zip(stops[i], stops[i + 1]))


def heat_range(cells: Any, min_span: float = 4.0) -> tuple[float, float]:
    """The levels the colours span: the cells' lowest and highest, at least min_span apart so a
    flat picture does not paint its noise."""
    values = [float(v) for row in cells for v in row]
    if not values:
        return (0.0, min_span)
    low, high = min(values), max(values)
    if high - low < min_span:
        middle = (low + high) / 2
        low, high = middle - min_span / 2, middle + min_span / 2
    return (low, high)


def nerd_line(metadata: dict[str, Any], mean: float | None = None, focus: float | None = None,
              cells: tuple[float, float] | None = None) -> str:
    """The camera's numbers for the curious, short: size, format, mean level, grid span, exposure, gain, data rate,
    focus. Fields are two spaces apart; a field holds one space at most."""
    if not metadata.get("camera_connected"):
        return ""
    parameters = metadata.get("camera_parameters") or {}

    def value(name: str) -> Any:
        return (parameters.get(name) or {}).get("value")

    shape = metadata.get("camera_frame_shape") or []
    width, height = (shape[1], shape[0]) if len(shape) >= 2 else (value("Width"), value("Height"))
    parts = []
    if width and height:
        parts.append(f"{int(width)}x{int(height)}")
    fmt = value("PixelFormat")
    if fmt:
        parts.append(str(fmt))
    if mean is not None:
        parts.append(f"avg {mean:.1f}")
    if cells is not None:
        parts.append(f"grid {cells[0]:.0f}-{cells[1]:.0f}")      # blue at the first level, red at the second
    if value("ExposureTime") is not None:
        parts.append(f"exp {float(value('ExposureTime')):.0f}us")
    if value("Gain") is not None:
        parts.append(f"gain {float(value('Gain')):g}dB")
    fps = metadata.get("camera_fps")
    if fps and width and height:
        parts.append(f"{float(fps) * int(width) * int(height) * bytes_per_pixel(fmt) / 1e6:.1f}MB/s")
    if focus is not None:
        parts.append(f"focus {focus * 100:.0f}%")        # last, so the focus bar sits next to it
    return NERD_SEPARATOR.join(parts)


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def structure_signature(*parts: Any) -> tuple[Any, ...]:
    return tuple(parts)


def structure_changed(current: tuple[Any, ...] | None, next_signature: tuple[Any, ...]) -> bool:
    return current != next_signature


@dataclass(frozen=True)
class SettingsPanelState:
    kind: str
    source: str = ""
    stage_id: str = ""


def settings_panel_state(stage: Stage) -> SettingsPanelState:
    panel = stage.settings_panel
    if panel is None:
        return SettingsPanelState(kind="none", stage_id=stage.id)
    return SettingsPanelState(
        kind=panel.kind,
        source=str(panel.options.get("source") or ""),
        stage_id=stage.id,
    )
