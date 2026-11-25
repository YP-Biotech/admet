"""Where a recording loses frames, measured on the rig's own camera, one stage added at a time.

The camera numbers every frame it makes, so a recording can be counted at each hand-over: frames
the camera made, frames that reached Python, frames the writer took, frames on the disk. Each
stage records for a few seconds and adds one consumer: grab alone, then the disk, then the
preview and the live QC as the control desktop runs them. The stage where the counts part says
where the frames go, and the time pylon takes to hand over one frame says whether Python keeps up.
"""

from __future__ import annotations

import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .acquisition import CameraAcquisitionThread
from .qc import BurstQC, QCWorker
from .video import VideoWorker

STAGES = ("grab", "disk", "preview+qc")
PREVIEW_HZ = 30.0                 # how often the desktop paints the preview


@dataclass
class StageResult:
    stage: str
    seconds: float
    camera_fps: float             # from the camera's frame numbers over its own clock, else frames over the PC's
    made: int                     # frames the camera numbered while recording; the ones handed over when it numbers none
    reached_python: int           # frames pylon handed over
    camera_skipped: int           # numbered by the camera, never handed over
    taken: int                    # frames the writer accepted
    written: int                  # frames on the disk
    writer_dropped: int           # accepted, then dropped because the disk fell behind
    grab_us_median: float         # time one call for a frame takes, the wait for it included
    grab_us_p99: float
    numbered: bool = True         # the camera's frame numbers and timestamps could be read

    @property
    def budget_us(self) -> float:
        return 1e6 / self.camera_fps if self.camera_fps else 0.0

    @property
    def lost(self) -> int:
        return max(0, self.made - self.written)


class TimedCamera:
    """The camera, with every frame handed over counted, numbered and timed."""

    def __init__(self, camera: Any) -> None:
        self.camera = camera
        self.reset()

    def reset(self) -> None:
        self.durations_ns: list[int] = []
        self.first: tuple[int, int] | None = None       # (ticks, number)
        self.last: tuple[int, int] | None = None
        self.skipped = 0
        self.frames = 0
        self.counting = True              # off just before the recording stops: a frame after it is not one of its

    def start_grabbing(self, *, latest_only: bool = True) -> None:
        self.camera.start_grabbing(latest_only=latest_only)

    def stop_grabbing(self) -> None:
        self.camera.stop_grabbing()

    def grab(self, timeout_ms: int = 5) -> tuple[np.ndarray, int, int] | None:
        started = time.perf_counter_ns()
        grabbed = self.camera.grab(timeout_ms)
        if grabbed is None or not self.counting:
            return grabbed
        self.durations_ns.append(time.perf_counter_ns() - started)
        _frame, ticks, number = grabbed
        self.frames += 1
        if self.last is not None and number > self.last[1] + 1:
            self.skipped += number - self.last[1] - 1
        if self.first is None:
            self.first = (ticks, number)
        self.last = (ticks, number)
        return grabbed


class NullWriter:
    """Takes every frame and keeps none: the grab loop alone."""

    def __init__(self) -> None:
        self.frame_count = 0
        self.written = 0

    def start(self) -> bool:
        return True

    def write(self, frame: np.ndarray) -> bool:
        self.frame_count += 1
        self.written += 1
        return True

    def stop(self) -> str:
        return ""


class PreviewPainter:
    """What the desktop does with the preview: takes the newest frame, copies it, says it is done."""

    def __init__(self, acquisition: CameraAcquisitionThread) -> None:
        self.acquisition = acquisition
        self.pending: np.ndarray | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._paint, name="BenchPreview", daemon=True)

    def offer(self, frame: np.ndarray) -> None:
        self.pending = frame.copy()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(1.0)

    def _paint(self) -> None:
        while not self._stop.wait(1.0 / PREVIEW_HZ):
            if self.pending is not None:
                self.pending = None
                self.acquisition.frame_processed()


def run_stage(camera: Any, stage: str, seconds: float, out_dir: Path, *, fps_hint: float = 0.0,
              microns_per_pixel: float = 0.0, width: int = 0, height: int = 0) -> StageResult:
    """Record for this long with this stage's consumers, and count every hand-over."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(STAGES)}")
    timed = TimedCamera(camera)
    acquisition = CameraAcquisitionThread(timed)
    acquisition.frame_rate_hint = fps_hint
    painter = qc = None
    if stage == "preview+qc":
        painter = PreviewPainter(acquisition)
        acquisition.preview_callback = painter.offer
        qc = QCWorker(BurstQC(microns_per_pixel=microns_per_pixel))
        acquisition.qc_sink = qc.offer
        painter.start()
    else:
        acquisition.set_preview_enabled(False)
    writer: Any = NullWriter() if stage == "grab" else VideoWorker(
        out_dir, f"bench_{stage.replace('+', '_')}", width, height, fps_hint, encoder=lambda *args: (True, ""))
    if isinstance(writer, VideoWorker):
        writer.encode_at_stop = False
    acquisition.start()
    try:
        time.sleep(0.3)                                    # live first, as the desktop is before a recording
        if not acquisition.start_recording(writer):
            raise RuntimeError("the writer could not start")
        timed.reset()
        started = time.monotonic()
        time.sleep(seconds)
        timed.counting = False
        acquisition.stop_recording()
        elapsed = time.monotonic() - started
        if isinstance(writer, VideoWorker):
            writer.wait(60)
    finally:
        acquisition.stop()
        if painter is not None:
            painter.stop()
        if qc is not None:
            qc.stop()
    if isinstance(writer, VideoWorker):
        for path in (writer.raw_path, writer.info_path):   # the bench's own files, never a recording's
            path.unlink(missing_ok=True)
    return _result(stage, elapsed, timed, writer, acquisition.clock.tick_hz)


def _result(stage: str, elapsed: float, timed: TimedCamera, writer: Any, tick_hz: float) -> StageResult:
    """The stage's counts. The camera's frame numbers and clock are used only when they make sense:
    numbers that span fewer frames than arrived, or a clock that disagrees with the PC's by more than
    half, are not the camera's count; then the frames handed over and the PC's clock are all there is."""
    pc_fps = timed.frames / elapsed if elapsed > 0 else 0.0
    made, camera_fps, numbered = timed.frames, pc_fps, False
    if timed.first is not None and timed.last is not None:
        numbers = timed.last[1] - timed.first[1] + 1
        span_s = (timed.last[0] - timed.first[0]) / tick_hz if tick_hz else 0.0
        clocked = (numbers - 1) / span_s if span_s > 0 else 0.0
        if numbers >= timed.frames > 1 and pc_fps and 0.5 <= clocked / pc_fps <= 2.0 * numbers / timed.frames:
            made, camera_fps, numbered = numbers, clocked, True
    durations = np.array(timed.durations_ns, dtype=float) / 1000.0
    return StageResult(
        stage=stage, seconds=round(elapsed, 2), camera_fps=round(camera_fps, 1), made=made,
        reached_python=timed.frames, taken=int(writer.frame_count),
        written=int(writer.written), writer_dropped=int(writer.frame_count - writer.written),
        grab_us_median=round(float(np.median(durations)), 1) if len(durations) else 0.0,
        grab_us_p99=round(float(np.percentile(durations, 99)), 1) if len(durations) else 0.0,
        camera_skipped=timed.skipped if numbered else 0, numbered=numbered,
    )


def bench(settings: dict[str, Any], *, seconds: float = 10.0, out_dir: str | Path | None = None,
          stages: tuple[str, ...] = STAGES, controller: Any | None = None) -> list[StageResult]:
    """Connect the camera with these camera settings and run each stage in turn."""
    from admet.engines.acquisition.settings import CAMERA_SETTINGS

    from .controller import CameraController

    settings = CAMERA_SETTINGS.validate(settings)
    controller = controller or CameraController()
    connected = controller.connect(settings)
    if not connected.get("camera_connect_ok"):
        raise RuntimeError(str(connected.get("camera_connect_message") or "the camera did not connect"))
    if connected.get("camera_settings_ok") is False:
        raise RuntimeError(f"the camera refused the settings: {connected.get('camera_settings_message')}")
    camera = controller.camera
    fps_hint = float(camera.get_resulting_framerate() or 0.0)
    width, height = controller.recording_frame_size(settings)
    results = []
    if out_dir is not None:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="admet-camera-bench-", dir=out_dir) as folder:
            for stage in stages:
                results.append(run_stage(camera, stage, seconds, Path(folder), fps_hint=fps_hint,
                                         microns_per_pixel=float(settings.get("camera_microns_per_pixel") or 0.0),
                                         width=width, height=height))
    finally:
        controller.disconnect()
    return results


@dataclass
class SystemCheck:
    """What the PC can give a recording: memory to hold frames the disk has not taken yet, and the
    disk's sustained write speed where the recordings go."""
    ram_total_gb: float
    ram_free_gb: float | None     # None where the system does not say
    disk_mb_s: float              # sustained: written and forced to the disk, not into its cache
    disk_gb: float                # how much was written to measure it
    needed_mb_s: float            # a frame's bytes times the camera's rate
    folder: str

    @property
    def ram_seconds(self) -> float | None:
        """How many seconds of recording the free memory holds."""
        if self.ram_free_gb is None or not self.needed_mb_s:
            return None
        return self.ram_free_gb * 1024 / self.needed_mb_s

    @property
    def behind_mb_s(self) -> float:
        """How far the disk falls behind the camera; 0 when it keeps up."""
        return max(0.0, self.needed_mb_s - self.disk_mb_s) if self.disk_mb_s else 0.0


def memory_gb() -> tuple[float, float | None]:
    """The PC's memory, total and free, in GB; free is None where the system does not say."""
    import os
    import sys

    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("free", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                        ("page_free", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                        ("virtual_free", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]

        status = Status()
        status.length = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))     # type: ignore[attr-defined]
        return status.total / 2**30, status.free / 2**30
    page = os.sysconf("SC_PAGE_SIZE")
    total = os.sysconf("SC_PHYS_PAGES") * page / 2**30
    try:
        free = os.sysconf("SC_AVPHYS_PAGES") * page / 2**30
    except (ValueError, OSError):
        free = None
    return total, free


def disk_speed(folder: str | Path, gigabytes: float = 4.0, *, chunk_mb: int = 64) -> float:
    """MB/s written to a file in FOLDER and forced to the disk, GIGABYTES of it; the file is removed."""
    import os

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"admet-disk-speed-{os.getpid()}.bin"
    block = os.urandom(chunk_mb << 20)                 # not zeros: a compressing disk would flatter itself
    chunks = max(1, int(round(gigabytes * 1024 / chunk_mb)))
    started = time.perf_counter()
    try:
        with open(path, "wb") as handle:
            for _ in range(chunks):
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        elapsed = time.perf_counter() - started
    finally:
        path.unlink(missing_ok=True)
    return chunks * chunk_mb / elapsed if elapsed > 0 else 0.0


def system_check(folder: str | Path, frame_bytes: int, fps: float, *, disk_gb: float = 4.0) -> SystemCheck:
    total, free = memory_gb()
    speed = disk_speed(folder, disk_gb) if disk_gb > 0 else 0.0
    return SystemCheck(ram_total_gb=round(total, 1), ram_free_gb=round(free, 1) if free is not None else None,
                       disk_mb_s=round(speed, 1), disk_gb=disk_gb, needed_mb_s=round(frame_bytes * fps / 2**20, 1),
                       folder=str(Path(folder).resolve()))


def system_lines(check: SystemCheck) -> list[str]:
    """The memory and the disk against what the camera makes."""
    free = f", {check.ram_free_gb:.1f} GB free" if check.ram_free_gb is not None else ""
    holds = f": holds {check.ram_seconds:.0f} s of recording" if check.ram_seconds is not None else ""
    lines = [f"camera makes {check.needed_mb_s:.0f} MB/s",
             f"RAM {check.ram_total_gb:.1f} GB{free}{holds}"]
    if not check.disk_mb_s:
        lines.append("disk not measured")
        return lines
    lines.append(f"disk {check.disk_mb_s:.0f} MB/s sustained ({check.disk_gb:g} GB written in {check.folder})")
    if check.behind_mb_s:
        lines.append(f"the disk falls {check.behind_mb_s:.0f} MB/s behind the camera: a 60 s recording leaves "
                     f"{check.behind_mb_s * 60 / 1024:.1f} GB to hold in memory")
    else:
        lines.append("the disk keeps up with the camera")
    return lines


def report_lines(results: list[StageResult]) -> list[str]:
    """One line a stage, then where the frames went."""
    lines = [f"{'stage':11s} {'camera fps':>10s} {'made':>8s} {'to python':>9s} {'cam skip':>8s} "
             f"{'on disk':>8s} {'disk drop':>9s} {'lost':>6s} {'grab µs med/p99':>16s} {'budget µs':>9s}"]
    for r in results:
        lost = f"{100 * r.lost / r.made:5.1f}%" if r.made else "    –"
        lines.append(f"{r.stage:11s} {r.camera_fps:10.0f} {r.made:8d} {r.reached_python:9d} {r.camera_skipped:8d} "
                     f"{r.written:8d} {r.writer_dropped:9d} {lost:>6s} "
                     f"{r.grab_us_median:7.1f}/{r.grab_us_p99:<8.1f} {r.budget_us:9.1f}")
    for r in results:
        if r.camera_skipped and r.made:
            lines.append(f"{r.stage}: {r.camera_skipped} frames never reached Python: the grab loop is too slow "
                         f"(grab takes {r.grab_us_median:.0f} µs of a {r.budget_us:.0f} µs budget, plus the loop)")
        if r.writer_dropped:
            lines.append(f"{r.stage}: {r.writer_dropped} frames dropped by the writer: the disk fell behind")
    if any(not r.numbered for r in results):
        lines.append("the camera gave no usable frame numbers or timestamps: frames it made but never handed over "
                     "cannot be counted, and the rate is the frames that arrived over the PC's clock")
    lines.append("grab µs includes the wait for the next frame: near the budget means Python keeps up, well above "
                 "it means it falls behind")
    if all(not r.lost for r in results):
        lines.append("no frame lost at any stage" + ("" if all(r.numbered for r in results)
                                                     else " that the PC can see"))
    return lines


def as_dicts(results: list[StageResult]) -> list[dict[str, Any]]:
    return [{**asdict(r), "lost": r.lost, "budget_us": round(r.budget_us, 1)} for r in results]
