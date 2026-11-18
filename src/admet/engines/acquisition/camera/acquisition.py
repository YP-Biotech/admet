from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from collections.abc import Callable
from threading import Event, Thread
from typing import Any, Protocol

import numpy as np

from .constants import GRAB_BUFFER_MEMORY, GRAB_BUFFERS_MAX, GRAB_BUFFERS_MIN
from .qc import BURST_FRAMES, BURST_INTERVAL_S

log = logging.getLogger(__name__)


def bytes_per_pixel(pixel_format: Any) -> float:
    """Bytes one pixel takes on the wire: Mono8 1, Mono10 and Mono12 2, the packed Mono10p 1.25."""
    name = str(pixel_format or "Mono8")
    if name.endswith("p"):
        return int("".join(ch for ch in name if ch.isdigit()) or 8) / 8
    return 2.0 if any(bits in name for bits in ("10", "12", "16")) else 1.0


def grab_buffers(frame_bytes: int) -> int:
    """How many frames pylon keeps for us: the memory budget's worth, between the bounds."""
    return max(GRAB_BUFFERS_MIN, min(GRAB_BUFFERS_MAX, GRAB_BUFFER_MEMORY // max(int(frame_bytes), 1)))


def as_8bit(frame: np.ndarray) -> np.ndarray:
    """A frame as one byte a pixel; a 16-bit frame keeps its top byte.

    The camera hands over Mono8 whatever its pixel format (see Camera.grab_frame), so
    this only meets a true 16-bit frame from another source.
    """
    if frame.dtype == np.uint16:
        return (frame >> 8).astype(np.uint8)
    return frame


GRID_CELLS = 256        # cells the Nerd mode grid has, whatever the frame's size


def grid_edges(width: int, height: int, cells: int = GRID_CELLS) -> tuple[np.ndarray, np.ndarray]:
    """Row and column boundaries of a grid of about `cells` near-square cells over a width × height frame."""
    if width <= 0 or height <= 0:
        return np.zeros(1, dtype=int), np.zeros(1, dtype=int)
    side = max(np.sqrt(width * height / cells), 1.0)
    rows, cols = max(int(round(height / side)), 1), max(int(round(width / side)), 1)
    return np.linspace(0, height, rows + 1).round().astype(int), np.linspace(0, width, cols + 1).round().astype(int)


def cell_means(frame: np.ndarray, cells: int = GRID_CELLS) -> np.ndarray:
    """The mean level of each cell of the grid laid over the frame."""
    f = as_8bit(frame).astype(np.float32)
    if f.ndim == 3:
        f = f.mean(axis=2)
    if f.ndim != 2 or f.size == 0:
        return np.zeros((0, 0), dtype=np.float32)
    row_edges, col_edges = grid_edges(f.shape[1], f.shape[0], cells)
    sums = np.add.reduceat(np.add.reduceat(f, row_edges[:-1], axis=0), col_edges[:-1], axis=1)
    return sums / np.outer(np.diff(row_edges), np.diff(col_edges))


def sharpness(frame: np.ndarray) -> float:
    """How sharp a frame is: its edge energy (Tenengrad), the mean squared Sobel gradient after
    a 3×3 smoothing, divided by the squared mean level so a gain change leaves it alone.

    Only the comparison matters: the number rises as the focus improves and peaks when it is sharpest.
    """
    f = as_8bit(frame).astype(np.float32)
    if f.ndim == 3:
        f = f.mean(axis=2)
    if f.ndim != 2 or f.shape[0] < 5 or f.shape[1] < 5:
        return 0.0
    s = (f[:-2] + 2 * f[1:-1] + f[2:]) / 4
    s = (s[:, :-2] + 2 * s[:, 1:-1] + s[:, 2:]) / 4
    gx = (s[:-2, 2:] + 2 * s[1:-1, 2:] + s[2:, 2:]) - (s[:-2, :-2] + 2 * s[1:-1, :-2] + s[2:, :-2])
    gy = (s[2:, :-2] + 2 * s[2:, 1:-1] + s[2:, 2:]) - (s[:-2, :-2] + 2 * s[:-2, 1:-1] + s[:-2, 2:])
    level = max(float(s.mean()), 1.0)
    return float(np.mean(gx * gx + gy * gy)) / (level * level)


class CameraSource(Protocol):
    def start_grabbing(self, *, latest_only: bool = True) -> None:
        ...

    def stop_grabbing(self) -> None:
        ...

    def grab_frame(self, timeout_ms: int = 5) -> np.ndarray | None:
        ...


DEFAULT_TICK_HZ = 1_000_000_000.0     # a Basler USB camera's timestamp counts nanoseconds
CLOCK_SAMPLES = 40                    # consecutive frames the camera's clock is measured over


class FrameClock:
    """The camera's clock on the PC's monotonic one, and the frames the camera made that never arrived.

    Each frame comes with the camera's timestamp, in its own ticks, and its number. Over the first
    consecutive frames of a session the tick rate is measured against the camera's known frame rate,
    and the offset to the PC clock is the smallest latency seen, since a frame can only arrive after
    it was taken. From then on time_of() places any frame to a fraction of a millisecond, and a gap
    in the numbers counts the frames skipped on the camera's side, which no counter on the PC sees.
    """

    def __init__(self) -> None:
        self.tick_hz = DEFAULT_TICK_HZ
        self.offset_s: float | None = None          # PC monotonic = ticks / tick_hz + offset
        self.skipped = 0                            # frames the camera numbered that never arrived
        self.frames = 0
        self._last_number: int | None = None
        self._samples: list[tuple[int, int, float]] = []
        self.first: tuple[int, int, float] | None = None   # (ticks, number, arrived) of the first frame kept

    def observe(self, ticks: int, number: int, arrived: float, fps_hint: float = 0.0) -> None:
        """A frame arrived now with these camera stamps; fps_hint is the camera's own frame rate."""
        if not ticks and not number:
            return
        self.frames += 1
        if self._last_number is not None and number > self._last_number + 1:
            self.skipped += number - self._last_number - 1
        self._last_number = number
        if self.first is None:
            self.first = (ticks, number, arrived)
        if ticks and len(self._samples) < CLOCK_SAMPLES:
            self._samples.append((ticks, number, arrived))
            if len(self._samples) == CLOCK_SAMPLES:
                self._settle(fps_hint)
            elif self.offset_s is None:
                self.offset_s = arrived - ticks / self.tick_hz

    def _settle(self, fps_hint: float) -> None:
        steps = [(t1 - t0) / (n1 - n0) for (t0, n0, _), (t1, n1, _) in zip(self._samples, self._samples[1:])
                 if n1 > n0 and t1 > t0]
        if fps_hint and steps:
            measured = float(np.median(steps)) * fps_hint
            if 0.5 * DEFAULT_TICK_HZ <= measured <= 2 * DEFAULT_TICK_HZ or measured > 1e5:
                self.tick_hz = measured          # within reason: the camera's rate and its ticks agree
        self.offset_s = min(arrived - ticks / self.tick_hz for ticks, _n, arrived in self._samples)

    def time_of(self, ticks: int) -> float | None:
        """When a frame with these ticks was taken, on the PC's monotonic clock."""
        if self.offset_s is None or not ticks:
            return None
        return ticks / self.tick_hz + self.offset_s

    def reset_recording(self) -> None:
        """A recording starts: its first frame and its skipped count start over; the clock stays."""
        self.first = None
        self.skipped = 0
        self.frames = 0
        self._last_number = None

    def facts(self) -> dict[str, Any]:
        """What a recording's record keeps of the camera's time."""
        if self.first is None:
            return {}
        ticks, number, arrived = self.first
        taken = self.time_of(ticks)
        first_at = datetime.now() - timedelta(seconds=time.monotonic() - (taken if taken is not None else arrived))
        return {"first_frame_ticks": ticks, "first_frame_number": number,
                "first_frame_at": first_at.isoformat(timespec="milliseconds"),
                "camera_timestamp_hz": round(self.tick_hz), "frames_skipped": self.skipped}


class FrameWriter(Protocol):
    frame_count: int

    def start(self) -> bool:
        ...

    def write(self, frame: np.ndarray) -> bool:
        ...

    def stop(self) -> str:
        ...


FrameCallback = Callable[[np.ndarray], None]
StatsCallback = Callable[[dict], None]
RecordingCompleteCallback = Callable[[], None]


class CameraAcquisitionThread(Thread):
    def __init__(
        self,
        camera: CameraSource,
        *,
        preview_callback: FrameCallback | None = None,
        stats_callback: StatsCallback | None = None,
        recording_complete_callback: RecordingCompleteCallback | None = None,
        sleep_s: float = 0.001,
        stats_interval_s: float = 0.2,
    ):
        super().__init__(daemon=True, name="CameraAcquisitionThread")
        self.camera = camera
        self.preview_callback = preview_callback
        self.stats_callback = stats_callback
        self.recording_complete_callback = recording_complete_callback
        self.sleep_s = sleep_s
        self.stats_interval_s = stats_interval_s

        self.writer: FrameWriter | None = None
        self.last_recording_frames: int | None = None
        self.last_writer_frame_count: int | None = None
        self.frame_count = 0
        self.total_frames = 0
        self.live_started_monotonic: float | None = None
        self.latest_frame_monotonic: float | None = None
        self.start_time = 0.0
        self.last_stats_time = 0.0
        self.max_frames: int | None = None
        self.max_time: float | None = None
        self.preview_enabled = True
        # Live QC, while recording: every burst_interval_s the next burst_frames consecutive frames go to the sink.
        self.qc_sink: Callable[[list[np.ndarray], float, float], Any] | None = None
        self.burst_frames = BURST_FRAMES
        self.burst_interval_s = BURST_INTERVAL_S
        self.frame_rate_hint = 0.0                  # the camera's rate, for the burst's speed
        self._burst: list[np.ndarray] | None = None
        self._last_burst_at = 0.0
        self._last_ticks = 0
        self.on_recording_started: Callable[[Any], None] | None = None
        self.on_recording_stopped: Callable[[], None] | None = None
        self.clock = FrameClock()                   # the camera's time on the PC's

        self._stop_event = Event()
        self._recording_event = Event()
        self._recording_paused = Event()
        self._frame_pending = Event()

    @property
    def recording(self) -> bool:
        return self._recording_event.is_set()

    @property
    def recording_paused(self) -> bool:
        return self._recording_paused.is_set()

    def pause_recording(self) -> None:
        """Stop writing frames while keeping the recording open.

        The writer, frame count and limits are preserved, so resuming continues the
        same recording instead of starting a new one.
        """
        self._recording_paused.set()

    def resume_recording(self) -> None:
        self._recording_paused.clear()

    def run(self) -> None:
        self._stop_event.clear()
        self.live_started_monotonic = time.monotonic()
        self.last_stats_time = time.time()
        self.camera.start_grabbing(latest_only=True)
        grab = getattr(self.camera, "grab", None)
        while not self._stop_event.is_set():
            if grab is not None:
                grabbed = grab()
                if grabbed is None:
                    time.sleep(self.sleep_s)
                    continue
                self.process_frame(grabbed[0], ticks=grabbed[1], number=grabbed[2])
                continue
            frame = self.camera.grab_frame()
            if frame is None:
                time.sleep(self.sleep_s)
                continue
            self.process_frame(frame)
        self.camera.stop_grabbing()

    def process_frame(self, frame: np.ndarray, *, ticks: int = 0, number: int = 0) -> None:
        self.total_frames += 1
        self.latest_frame_monotonic = time.monotonic()
        if ticks or number:
            self.clock.observe(ticks, number, self.latest_frame_monotonic, self.frame_rate_hint)
        self._last_ticks = ticks
        if self._recording_event.is_set() and not self._recording_paused.is_set() and self.writer:
            if self.writer.write(frame):
                self.frame_count += 1
                if self._check_limits():
                    self.stop_recording(notify_complete=True)

        if self.preview_enabled and not self._frame_pending.is_set() and self.preview_callback:
            self._frame_pending.set()
            self.preview_callback(frame)
        if self.qc_sink is not None and self._recording_event.is_set() and not self._recording_paused.is_set():
            self._collect_burst(frame)           # the recording's frames are consecutive: the bursts are too

        current_time = time.time()
        if self.stats_callback and current_time - self.last_stats_time >= self.stats_interval_s:
            self.last_stats_time = current_time
            recording = self._recording_event.is_set()
            self.stats_callback(
                {
                    "recording": recording,
                    "frames": self.frame_count if recording else 0,
                    "elapsed": current_time - self.start_time if recording else 0,
                    "total_frames": self.total_frames,
                    "latest_frame_monotonic": self.latest_frame_monotonic,
                    "fps": (
                        self.total_frames
                        / max(time.monotonic() - self.live_started_monotonic, 1e-9)
                        if self.live_started_monotonic is not None
                        else None
                    ),
                }
            )

    def _collect_burst(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        if self._burst is None:
            if now - self._last_burst_at < self.burst_interval_s:
                return
            self._burst = []
        self._burst.append(frame)
        if len(self._burst) < self.burst_frames:
            return
        burst, self._burst = self._burst, None
        self._last_burst_at = now
        taken = self.clock.time_of(self._last_ticks) if self._last_ticks else None
        try:
            self.qc_sink(burst, self.frame_rate_hint, taken if taken is not None else now)
        except Exception:
            log.exception("live QC refused a burst")

    def start_recording(
        self,
        writer: FrameWriter,
        *,
        max_frames: int | None = None,
        max_time: float | None = None,
    ) -> bool:
        self.writer = writer
        self.max_frames = max_frames
        self.max_time = max_time
        self.frame_count = 0
        self.start_time = time.time()
        self._recording_paused.clear()
        self._burst, self._last_burst_at = None, 0.0        # the first burst comes at once
        self.clock.reset_recording()
        if not self.writer.start():
            self.writer = None
            return False
        self.camera.stop_grabbing()
        self.camera.start_grabbing(latest_only=False)
        self._recording_event.set()
        if self.on_recording_started is not None:
            try:
                self.on_recording_started(writer)
            except Exception:
                log.exception("recording start hook failed")
        return True

    def stop_recording(self, *, notify_complete: bool = False) -> int:
        frames = self.frame_count
        self._recording_event.clear()
        self._recording_paused.clear()
        if self.writer:
            self.last_writer_frame_count = getattr(self.writer, "frame_count", None)
            record = getattr(self.writer, "record", None)
            if isinstance(record, dict):
                record.update(self.clock.facts())     # the camera's own time, into the recording's record
            self.writer.stop()
            self.writer = None
        self.last_recording_frames = frames
        self._burst = None
        if self.on_recording_stopped is not None:
            try:
                self.on_recording_stopped()
            except Exception:
                log.exception("recording stop hook failed")
        if not self._stop_event.is_set():
            self.camera.stop_grabbing()
            self.camera.start_grabbing(latest_only=True)
        if notify_complete and self.recording_complete_callback:
            try:
                self.recording_complete_callback()
            except Exception:
                log.exception("recording completion callback failed")
        return frames

    def stop(self) -> None:
        self._stop_event.set()
        if self._recording_event.is_set():
            self.stop_recording()
        if self.is_alive():
            self.join(timeout=2.0)

    def set_preview_enabled(self, enabled: bool) -> None:
        self.preview_enabled = enabled

    def set_recording_complete_callback(self, callback: RecordingCompleteCallback | None) -> None:
        self.recording_complete_callback = callback

    def frame_processed(self) -> None:
        self._frame_pending.clear()

    def _check_limits(self) -> bool:
        if self.max_frames and self.frame_count >= self.max_frames:
            return True
        if self.max_time and time.time() - self.start_time >= self.max_time:
            return True
        return False
