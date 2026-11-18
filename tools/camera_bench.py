"""Does the camera's every frame reach the disk on this machine?

Runs the same path control uses, the acquisition thread with the video writer, for a few seconds
at the recording settings, then compares the camera's own count with what arrived and what was
written. With --contend a thread hogs the interpreter in bursts the way the GUI does while it
paints, which is what made the camera skip frames before pylon got enough buffers.

    uv run python tools/camera_bench.py
    uv run python tools/camera_bench.py --contend
    uv run python tools/camera_bench.py --contend --buffers 50     # the old setting, to see the loss
    uv run python tools/camera_bench.py --qc                       # with the live QC measuring the recording
    uv run python tools/camera_bench.py --qc --synthetic 4000      # no camera: a synthetic train at 4000 fps

Two probe threads report how late they wake: one every 0.1 s like the protocol's tick, one every
30 ms with 3 ms of work like the window. Lateness that grows with --qc is the interpreter being
held by the QC.

The raw frames go to --dir, the recording drive, and are deleted at the end; nothing is
encoded, so one run never competes with the next for the disk.
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path


from admet.engines.acquisition.camera.acquisition import CameraAcquisitionThread
from admet.engines.acquisition.camera.camera import Camera
from admet.engines.acquisition.camera.video import VideoWorker


class SyntheticCamera:
    """A camera for a machine without one: frames of a droplet train at the given rate, paced by the clock."""

    def __init__(self, fps: float, width: int, height: int) -> None:
        import cv2
        import numpy as np

        self.fps, self.period = fps, 1.0 / fps
        self.frames = []
        rng = np.random.default_rng(1)
        for index in range(64):                         # a cycle of frames; the train moves 48 px a frame
            frame = np.full((height, width), 157, dtype=np.float32)
            frame[:4, :] = 100
            frame[-4:, :] = 100
            offset = (index * 48.0) % 128.0
            for x in np.arange(offset - 128.0, width + 128.0, 128.0):
                cv2.circle(frame, (int(round(x)), height // 2), 22, 105, 3)
            self.frames.append(np.clip(frame + rng.normal(0, 2.0, frame.shape), 0, 255).astype(np.uint8))
        self.index = 0
        self.next_at = time.perf_counter()

    def start_grabbing(self, *, latest_only: bool = True, buffers: int | None = None) -> None:
        self.next_at = time.perf_counter()

    def stop_grabbing(self) -> None:
        pass

    def grab_frame(self, timeout_ms: int = 5):
        wait = self.next_at - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        self.next_at += self.period
        self.index += 1
        return self.frames[self.index % len(self.frames)].copy()


class Probe(threading.Thread):
    """A thread that wants to wake every period and measures how late it is: the pipeline's tick,
    or the window's event loop, as the live QC and the rest compete for the interpreter."""

    def __init__(self, name: str, period_s: float, busy_ms: float = 0.0) -> None:
        super().__init__(name=name, daemon=True)
        self.period_s, self.busy_ms = period_s, busy_ms
        self.lateness: list[float] = []
        self._stop = threading.Event()

    def run(self) -> None:
        due = time.perf_counter() + self.period_s
        while not self._stop.is_set():
            self._stop.wait(max(0.0, due - time.perf_counter()))
            now = time.perf_counter()
            self.lateness.append(max(0.0, now - due))
            until = now + self.busy_ms / 1000
            total = 0
            while time.perf_counter() < until:
                total += sum(range(500))
            due = max(due + self.period_s, time.perf_counter())

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> str:
        import numpy as np

        late = np.array(self.lateness[1:] or [0.0]) * 1000
        return (f"{self.name}: late by {np.median(late):.1f} ms median, {np.percentile(late, 99):.0f} ms p99, "
                f"{late.max():.0f} ms max")


def hog(stop: threading.Event, busy_ms: float, every_ms: float) -> None:
    """Python work in bursts, holding the interpreter: busy_ms of it every every_ms."""
    while not stop.is_set():
        until = time.perf_counter() + busy_ms / 1000
        total = 0
        while time.perf_counter() < until:
            total += sum(range(2000))
        time.sleep(every_ms / 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=60)
    parser.add_argument("--exposure", type=float, default=59.0, help="µs")
    parser.add_argument("--buffers", type=int, default=None, help="pylon buffers; default: as control sets them")
    parser.add_argument("--contend", action="store_true", help="a thread hogs the interpreter like the GUI")
    parser.add_argument("--hog-ms", type=float, default=20.0, help="how long each hogging burst holds it")
    parser.add_argument("--every-ms", type=float, default=60.0, help="how often a burst comes")
    parser.add_argument("--dir", default=".", help="where bench.avi goes; use the recording drive")
    parser.add_argument("--qc", action="store_true", help="the live QC measures bursts of the recording, as control does")
    parser.add_argument("--synthetic", type=float, default=0.0, metavar="FPS",
                        help="no camera: a synthetic droplet train at this many frames a second")
    args = parser.parse_args()

    if args.synthetic:
        camera = SyntheticCamera(args.synthetic, args.width, args.height)
        device = None
        expected_fps = args.synthetic
        print(f"synthetic camera: {args.width}x{args.height} at {expected_fps:.0f} fps")
    else:
        camera = Camera()
        if not camera.open():
            print("no camera")
            return 1
        device = camera.device
        for name, value in (("Width", args.width), ("Height", args.height), ("ExposureTime", args.exposure),
                            ("SensorReadoutMode", "Fast"), ("DeviceLinkThroughputLimitMode", "Off"),
                            ("AcquisitionFrameRateEnable", False)):
            camera.set_parameter(name, value)
        expected_fps = camera.get_resulting_framerate()
        print(f"camera {device.GetDeviceInfo().GetModelName()}: {args.width}x{args.height}, "
              f"{args.exposure:.0f} us, resulting {expected_fps:.0f} fps")

    if args.buffers:
        original = Camera.start_grabbing
        Camera.start_grabbing = lambda self, *, latest_only=True, buffers=None: original(
            self, latest_only=latest_only, buffers=args.buffers)

    thread = CameraAcquisitionThread(camera, preview_callback=lambda frame: thread.frame_processed())
    thread.frame_rate_hint = expected_fps
    thread.start()
    time.sleep(0.5)

    def discard(raw: Path, video: Path, width: int, height: int, fps: float) -> tuple[bool, str]:
        raw.unlink(missing_ok=True)
        return True, ""

    writer = VideoWorker(args.dir, "bench", args.width, args.height, 24.0, encoder=discard)
    stop = threading.Event()
    hogger = threading.Thread(target=hog, args=(stop, args.hog_ms, args.every_ms), daemon=True)
    if args.contend:
        hogger.start()
    qc_worker = None
    if args.qc:
        from admet.engines.acquisition.camera.qc import BurstQC, QCWorker

        summaries = []
        qc_worker = QCWorker(BurstQC(microns_per_pixel=2.27), on_summary=summaries.append)
        qc_worker.start_recording()
        thread.qc_sink = qc_worker.offer
    probes = [Probe("pipeline tick (0.1 s)", 0.1), Probe("window loop (30 ms, 3 ms of work)", 0.03, busy_ms=3.0)]
    for probe in probes:
        probe.start()
    if not thread.start_recording(writer, max_time=args.seconds):
        print("the writer could not start")
        return 1
    buffers = device.MaxNumBuffer.Value if device is not None else 0
    started = time.perf_counter()
    while thread.recording and time.perf_counter() - started < args.seconds + 2:
        time.sleep(0.1)
    elapsed = time.perf_counter() - started
    stop.set()
    for probe in probes:
        probe.stop()

    grabber = getattr(device, "StreamGrabber", None)
    statistics = {}
    for name in ("Statistic_Total_Buffer_Count", "Statistic_Failed_Buffer_Count",
                 "Statistic_Buffer_Underrun_Count", "Statistic_Missed_Frame_Count"):
        try:
            statistics[name] = getattr(grabber, name).Value
        except Exception:
            pass
    thread.stop()
    if device is not None:
        camera.close()
    (Path(args.dir) / "bench.video.json").unlink(missing_ok=True)
    if qc_worker is not None:
        summary = qc_worker.summary
        qc_worker.stop()
        print(f"live QC: {len(summaries)} bursts measured, {qc_worker.dropped} dropped while busy"
              + (f"; last: {summary.rate_hz:.0f}/s cv {summary.cv_percent:.1f}% {summary.state}" if summary else ""))
    for probe in probes:
        print(probe.report())

    writer.wait()
    captured, written, arrived_fps = writer.frame_count, writer.written, writer.measured_fps
    print(f"pylon buffers {buffers}"
          + (f", the interpreter hogged {args.hog_ms:.0f} ms every {args.every_ms:.0f} ms" if args.contend else ""))
    print(f"frames: {captured} reached Python in {elapsed:.1f} s, arriving at {arrived_fps:.0f}/s against the "
          f"camera's {expected_fps:.0f}/s; written {written}, dropped by the writer {captured - written}")
    for name, value in statistics.items():
        print(f"  pylon {name.replace('Statistic_', '')} = {value}")
    missed = (int(statistics.get("Statistic_Missed_Frame_Count") or 0)
              + int(statistics.get("Statistic_Failed_Buffer_Count") or 0))
    share = arrived_fps / expected_fps if expected_fps else 0.0
    if not missed and share >= 0.98 and captured == written:
        print("verdict: every frame reached the disk")
        return 0
    print(f"verdict: {100 * (1 - share):.0f}% of the camera's frames never reached Python, "
          f"{captured - written} lost at the writer")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
