from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


def camera_strip(metadata: dict[str, Any], camera_name: str = "") -> str:
    """What the camera is and is doing, in one line for the top bar.

    The preview used to carry this in a box over the frame, where a 320 x 60 frame
    left no room for it; here it has the width of the window.
    """
    if not metadata.get("camera_connected"):
        return "Camera: not connected"
    parameters = metadata.get("camera_parameters") or {}

    def value(name: str) -> Any:
        return (parameters.get(name) or {}).get("value")

    parts = [camera_name or "Camera"]
    shape = metadata.get("camera_frame_shape") or []
    width, height = (shape[1], shape[0]) if len(shape) >= 2 else (value("Width"), value("Height"))
    if width and height:
        size = f"{int(width)} × {int(height)} px"
        if value("PixelFormat"):
            size += f" · {value('PixelFormat')}"
        parts.append(size)
    binning_h, binning_v = value("BinningHorizontal"), value("BinningVertical")
    if binning_h and binning_v and (int(binning_h) > 1 or int(binning_v) > 1):
        parts.append(f"binning {int(binning_h)}×{int(binning_v)}")
    fps, set_fps = metadata.get("camera_fps"), value("AcquisitionFrameRate")
    if fps:
        text = f"{float(fps):.0f} fps"
        if set_fps and abs(float(set_fps) - float(fps)) > 0.5:
            text += f" (set {float(set_fps):.0f})"
        parts.append(text)
    if value("ExposureTime") is not None:
        parts.append(f"exposure {float(value('ExposureTime')):.0f} µs")
    if value("Gain") is not None:
        parts.append(f"gain {float(value('Gain')):g}")
    if metadata.get("camera_recording"):
        frames = int(metadata.get("camera_recorded_frames") or 0)
        parts.append(f"REC {_clock(float(metadata.get('camera_record_elapsed') or 0))} · {frames:,} frames"
                     .replace(",", " "))
        dropped = int(metadata.get("camera_dropped_frames") or 0)
        if dropped:
            parts.append(f"{dropped:,} dropped".replace(",", " "))
    elif metadata.get("camera_live"):
        parts.append("live")
    else:
        parts.append("connected, not live")
    return " · ".join(parts)


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
