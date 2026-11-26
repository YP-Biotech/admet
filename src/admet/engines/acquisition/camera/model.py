"""A camera modelled in software, to see the recording path at the rig's rate on any computer.

pylon's camera emulator makes about 90 frames a second whatever it is asked, so it cannot show
whether a recording keeps up at 4000. This one makes frames on the PC's clock at the rate given,
numbered and stamped as a Basler camera does, and keeps them the way pylon does: in a buffer of a
fixed number of frames. When Python takes them slower than they come and the buffer is full,
the oldest are lost, as the real camera loses them, and the gap shows in the numbers. The frames
are drawn (a channel with dark plugs moving along it and the sensor's noise) or replayed from a
recording.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_BUFFER = 1000            # frames pylon keeps for Python on a small frame (see grab_buffers)
CYCLE = 256                      # distinct frames made, then shown again in turn


class ModelCamera:
    def __init__(self, fps: float, width: int = 320, height: int = 60, *, buffer_frames: int = DEFAULT_BUFFER,
                 video: str | Path | None = None, seed: int = 0) -> None:
        if fps <= 0:
            raise ValueError("a modelled camera needs a frame rate")
        self.fps = float(fps)
        self.buffer_frames = max(1, int(buffer_frames))
        self.frames = video_frames(video, CYCLE) if video else drawn_frames(width, height, CYCLE, seed)
        self.height, self.width = self.frames[0].shape
        self.connected = True
        self._started: float | None = None
        self._next = 0                   # the number of the next frame to hand over
        self.lost = 0                    # frames made but pushed out of the buffer before Python took them

    # -- what the acquisition thread asks of a camera ----------------------------------------
    def start_grabbing(self, *, latest_only: bool = True) -> None:
        if self._started is None:
            self._started, self._next = time.perf_counter(), 0

    def stop_grabbing(self) -> None:
        """Nothing to do: the modelled sensor keeps its clock, as a camera between two grabbing sessions."""

    def grab(self, timeout_ms: int = 5) -> tuple[np.ndarray, int, int] | None:
        """The next frame with its time in nanoseconds and its number, waiting for it to be made; the
        frames the buffer could not hold are skipped, so their numbers never arrive."""
        if self._started is None:
            return None
        now = time.perf_counter()
        made = int((now - self._started) * self.fps)         # frames made so far: numbers 0 .. made-1
        oldest_kept = made - self.buffer_frames
        if self._next < oldest_kept:
            self.lost += oldest_kept - self._next
            self._next = oldest_kept
        due = self._started + (self._next + 1) / self.fps    # when frame _next is done being made
        wait = due - now
        if wait > timeout_ms / 1000:
            time.sleep(timeout_ms / 1000)
            return None
        if wait > 0:
            # Asleep, not spinning: a camera waits in pylon's code, not in Python's, and the writer
            # and the preview run meanwhile. Sleeping past the frame only lets the next ones wait
            # in the buffer, as they do on the real camera; their numbers and times stay exact.
            time.sleep(wait)
        number = self._next
        self._next += 1
        ticks = int((due - self._started) * 1e9)
        return self.frames[number % len(self.frames)].copy(), ticks, number

    def grab_frame(self, timeout_ms: int = 5) -> np.ndarray | None:
        grabbed = self.grab(timeout_ms)
        return grabbed[0] if grabbed is not None else None

    def get_resulting_framerate(self) -> float:
        return self.fps


class ModelController:
    """Stands for the camera controller: the bench connects, asks the frame's size, disconnects."""

    def __init__(self, camera: ModelCamera) -> None:
        self.camera = camera

    def connect(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {"camera_connect_ok": True, "camera_settings_ok": True}

    def recording_frame_size(self, settings: dict[str, Any]) -> tuple[int, int]:
        return self.camera.width, self.camera.height

    def disconnect(self) -> dict[str, Any]:
        return {}


def drawn_frames(width: int, height: int, count: int, seed: int = 0, *, step_px: float = 44.5,
                 spacing_px: float = 175.0, noise: float = 4.2) -> list[np.ndarray]:
    """A channel 210 bright between two dark walls, dark-rimmed plugs moving STEP_PX a frame, and the
    sensor's noise: the frames the rig records, near enough for the writer and the live QC."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:height, :width].astype(np.float32)
    top, bottom = 0.08 * height, 0.92 * height
    channel = 210 - 45 * (np.exp(-((yy - top) / 1.5) ** 2) + np.exp(-((yy - bottom) / 1.5) ** 2))
    radius = (bottom - top) / 2
    frames = []
    for k in range(count):
        image = channel.copy()
        first = (k * step_px) % spacing_px - spacing_px
        for x0 in np.arange(first, width + spacing_px, spacing_px):
            ring = np.abs(np.hypot((xx - x0) / 1.15, yy - height / 2) - (radius - 3)) < 3
            image[ring] -= 60
        image += rng.normal(0, noise, image.shape)
        frames.append(np.clip(image, 0, 255).astype(np.uint8))
    return frames


def video_frames(path: str | Path, count: int) -> list[np.ndarray]:
    """The first COUNT frames of a recording, grey, to be shown in turn."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    frames = []
    try:
        while len(frames) < count:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    finally:
        capture.release()
    if not frames:
        raise ValueError(f"no frames could be read from {path}")
    return frames
