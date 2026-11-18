"""Live quality control from bursts of consecutive frames, while a recording runs.

What analyze measures, measured as the video is made, without following a droplet: the
detection is the OpenCV engine's own, the speed is the train's common shift between one
frame and the next, the rate is speed over the spacing between neighbours, the size and
its CV come from every whole droplet seen. Each number carries a 95 % interval, and the
state follows the CV's interval against the limit.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from statistics import mean, median, pstdev
from typing import Any, Callable

import numpy as np

log = logging.getLogger(__name__)

BURST_FRAMES = 8            # consecutive frames a burst holds
BURST_INTERVAL_S = 1.0      # one burst this often
WINDOW_S = 10.0             # the summary looks back this far
BACKGROUND_FRAMES = 64      # frames the background is taken over
BACKGROUND_EVERY = 4        # bursts between two takes of the background; it drifts slowly
CV_LIMIT_PERCENT = 5.0      # the CV the state is judged against
MIN_DROPLETS = 30           # measured droplets before a CV is judged
MIN_BURSTS = 3              # bursts with a rate before a rate is judged


@dataclass(frozen=True)
class BurstSample:
    at: float                              # monotonic seconds
    fps: float
    speed_px_frame: float                  # 0 when the burst gave no shift
    spacing_px: float                      # 0 when no neighbours were seen
    rate_hz: float                         # 0 when either is missing
    diameters_px: tuple[float, ...]        # every whole droplet in the burst


@dataclass(frozen=True)
class QCSummary:
    at: float
    rate_hz: float
    rate_half: float                       # half the 95 % interval; inf before enough bursts
    count: float                           # droplets made since the count was reset
    diameter: float                        # µm when the scale is known, else px
    diameter_half: float
    diameter_unit: str
    cv_percent: float
    cv_half: float
    n: int                                 # droplets behind the size and the CV
    bursts: int                            # bursts behind the rate
    speed_px_frame: float
    spacing_px: float
    state: str                             # STABLE, UNSTABLE or SETTLING


def interval(values: list[float]) -> tuple[float, float]:
    """The mean and half the 95 % interval of the mean; infinite half-width below two values."""
    if not values:
        return 0.0, math.inf
    if len(values) < 2:
        return float(values[0]), math.inf
    return float(mean(values)), 1.96 * pstdev(values) / math.sqrt(len(values))


def cv_interval(values: list[float]) -> tuple[float, float]:
    """The coefficient of variation in percent and half its 95 % interval, CV over root 2n."""
    if len(values) < 2 or mean(values) <= 0:
        return 0.0, math.inf
    cv = pstdev(values) / mean(values) * 100.0
    return cv, 1.96 * cv / math.sqrt(2 * len(values))


def qc_state(cv_percent: float, cv_half: float, n: int, bursts: int, *, limit: float = CV_LIMIT_PERCENT) -> str:
    """STABLE when the CV's whole interval sits below the limit, UNSTABLE when it sits above,
    SETTLING while it straddles the limit or the sample is still small."""
    if n < MIN_DROPLETS or bursts < MIN_BURSTS or not math.isfinite(cv_half):
        return "SETTLING"
    if cv_percent + cv_half < limit:
        return "STABLE"
    if cv_percent - cv_half > limit:
        return "UNSTABLE"
    return "SETTLING"


def qc_as_dict(summary: QCSummary | None) -> dict[str, Any] | None:
    """The summary as plain values for the status metadata; an infinite half-width becomes None."""
    if summary is None:
        return None
    return {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
            for key, value in asdict(summary).items()}


def qc_line(summary: QCSummary | dict[str, Any] | None) -> str:
    """One compact line for the frame: rate, count, size, CV, state, each with its interval."""
    if not summary:
        return ""

    def field(name: str) -> Any:
        return summary.get(name) if isinstance(summary, dict) else getattr(summary, name)

    parts = []
    if field("rate_hz"):
        parts.append(f"{field('rate_hz'):.0f}{_pm(field('rate_half'), 0)}/s")
        parts.append(f"{int(field('count')):,}".replace(",", " "))
    if field("n"):
        unit = str(field("diameter_unit"))
        parts.append(f"d {field('diameter'):.1f}{_pm(field('diameter_half'), 1)}{unit}")
        parts.append(f"cv {field('cv_percent'):.1f}{_pm(field('cv_half'), 1)}%")
    parts.append(str(field("state")))
    return "  ".join(parts)


def _pm(half: Any, digits: int) -> str:
    return f"±{half:.{digits}f}" if isinstance(half, (int, float)) and math.isfinite(half) else ""


class BurstQC:
    """The measurements, burst by burst, and their summary over the window."""

    def __init__(self, detection: dict[str, Any] | None = None, *, microns_per_pixel: float = 0.0,
                 window_s: float = WINDOW_S, cv_limit: float = CV_LIMIT_PERCENT) -> None:
        from admet.engines.opencv.detection import DEFAULTS

        self.detection = {**DEFAULTS, **(detection or {})}
        self.microns_per_pixel = float(microns_per_pixel or 0.0)
        self.window_s = window_s
        self.cv_limit = cv_limit
        self._frames: deque[np.ndarray] = deque(maxlen=BACKGROUND_FRAMES)
        self._background: np.ndarray | None = None
        self._bursts_since_background = 0
        self.samples: deque[BurstSample] = deque()
        self.count = 0.0
        self._last_counted: float | None = None

    def reset_count(self) -> None:
        self.count = 0.0
        self._last_counted = None

    def reset(self) -> None:
        """A new recording starts from nothing: no background, no samples, no count."""
        self._frames.clear()
        self._background = None
        self._bursts_since_background = 0
        self.samples.clear()
        self.reset_count()

    def analyse(self, frames: list[np.ndarray], fps: float, at: float | None = None) -> BurstSample:
        """One burst of consecutive frames: its speed, spacing, rate and droplet sizes."""
        from admet.engines.opencv.detection import detect, percentile_background
        from admet.engines.opencv.pipeline import spacings
        from admet.engines.opencv.tracking import train_shift

        at = time.monotonic() if at is None else at
        self._frames.extend(frames)
        if self._background is None or self._bursts_since_background >= BACKGROUND_EVERY:
            self._background = percentile_background(list(self._frames))
            self._bursts_since_background = 0
        self._bursts_since_background += 1
        found = [detect(frame, self._background, self.detection)["properties"] for frame in frames]
        shifts = []
        for previous, current in zip(found, found[1:]):
            shift = train_shift([(p["centroid_x"], p["centroid_y"]) for p in previous],
                                [(p["centroid_x"], p["centroid_y"]) for p in current])
            if shift is not None and shift[0] > 0:
                shifts.append(shift[0])
        gaps = [gap for properties in found for gap in spacings(properties)]
        speed = float(median(shifts)) if shifts else 0.0
        spacing = float(median(gaps)) if gaps else 0.0
        rate = speed * fps / spacing if speed and spacing and fps else 0.0
        sample = BurstSample(at=at, fps=fps, speed_px_frame=speed, spacing_px=spacing, rate_hz=rate,
                             diameters_px=tuple(p["equivalent_diameter"] for properties in found for p in properties))
        self.samples.append(sample)
        while self.samples and at - self.samples[0].at > self.window_s:
            self.samples.popleft()
        if rate:
            if self._last_counted is not None:
                self.count += rate * max(0.0, at - self._last_counted)
            self._last_counted = at
        return sample

    def summary(self, at: float | None = None) -> QCSummary | None:
        at = time.monotonic() if at is None else at
        samples = [s for s in self.samples if at - s.at <= self.window_s]
        if not samples:
            return None
        rates = [s.rate_hz for s in samples if s.rate_hz]
        rate, rate_half = interval(rates)
        scale = self.microns_per_pixel or 1.0
        diameters = [d * scale for s in samples for d in s.diameters_px]
        diameter, diameter_half = interval(diameters)
        cv, cv_half = cv_interval(diameters)
        speeds = [s.speed_px_frame for s in samples if s.speed_px_frame]
        gaps = [s.spacing_px for s in samples if s.spacing_px]
        return QCSummary(
            at=at, rate_hz=rate, rate_half=rate_half, count=self.count,
            diameter=diameter, diameter_half=diameter_half, diameter_unit="um" if self.microns_per_pixel else "px",
            cv_percent=cv, cv_half=cv_half, n=len(diameters), bursts=len(rates),
            speed_px_frame=float(median(speeds)) if speeds else 0.0, spacing_px=float(median(gaps)) if gaps else 0.0,
            state=qc_state(cv, cv_half, len(diameters), len(rates), limit=self.cv_limit),
        )


# The columns a QC row carries, in the run's timeline CSV after the fluidics columns.
QC_COLUMNS = ("timestamp", "elapsed_s", "recording_id", "step", "part", "rate_hz", "rate_half", "count", "diameter",
              "diameter_unit", "diameter_half", "cv_percent", "cv_half", "n", "bursts", "speed_px_frame", "spacing_px",
              "state")


def _below_normal_priority() -> None:
    """On Windows, let this thread yield to the others when the machine is busy; elsewhere nothing."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32                      # type: ignore[attr-defined]
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)      # THREAD_PRIORITY_BELOW_NORMAL
    except (AttributeError, OSError):
        pass


def _num(value: float) -> str:
    return f"{value:.4f}" if math.isfinite(value) else ""


class QCWorker:
    """Takes a burst when it is free, measures it in its own thread, and tells the summary.

    A burst offered while the last one is still being measured is dropped: the camera never waits.
    Bursts come only while a recording runs, stamped with the time their frames were taken, so the
    QC, the video and the fluidics cover the same seconds on one clock. The rows go to the run's
    timeline through whoever listens to on_summary; the worker keeps no file.
    """

    def __init__(self, qc: BurstQC, on_summary: Callable[[QCSummary], None] | None = None) -> None:
        self.qc = qc
        self.on_summary = on_summary
        self._pending: tuple[list[np.ndarray], float, float | None] | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self.dropped = 0
        self.summary: QCSummary | None = None
        self._thread = threading.Thread(target=self._run, name="LiveQC", daemon=True)
        self._thread.start()

    def offer(self, frames: list[np.ndarray], fps: float, at: float | None = None) -> bool:
        """A burst, and the monotonic time its last frame was taken."""
        with self._lock:
            if self._pending is not None or self._busy:
                self.dropped += 1
                return False
            self._pending = (frames, fps, at)
        self._wake.set()
        return True

    def start_recording(self) -> None:
        """A recording starts: the measurements start over."""
        with self._lock:
            self._pending = None
            self.qc.reset()
            self.summary = None

    def stop_recording(self) -> None:
        """The recording stopped: nothing left to show."""
        with self._lock:
            self._pending = None
            self.summary = None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(2.0)
        self.stop_recording()

    def _run(self) -> None:
        _below_normal_priority()                 # the camera, the writer and the window come first
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                job, self._pending = self._pending, None
                self._busy = job is not None
            if job is None:
                continue
            try:
                frames, fps, at = job
                self.qc.analyse(frames, fps, at)
                summary = self.qc.summary(at)
            except Exception:
                log.exception("a burst could not be measured")
                summary = None
            finally:
                with self._lock:
                    self._busy = False
            if summary is None:
                continue
            self.summary = summary
            if self.on_summary is not None:
                try:
                    self.on_summary(summary)
                except Exception:
                    pass
