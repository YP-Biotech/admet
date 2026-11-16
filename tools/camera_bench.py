"""Does the camera's every frame reach the disk on this machine?

Runs the same path control uses, the acquisition thread with the video writer, for a few seconds
at the recording settings, then compares the camera's own count with what arrived and what was
written. With --contend a thread hogs the interpreter in bursts the way the GUI does while it
paints, which is what made the camera skip frames before pylon got enough buffers.

    uv run python tools/camera_bench.py
    uv run python tools/camera_bench.py --contend
    uv run python tools/camera_bench.py --contend --buffers 50     # the old setting, to see the loss

Leaves bench.avi in --dir; delete it afterwards.
"""
from __future__ import annotations

import argparse
import threading
import time


from admet.engines.acquisition.camera.acquisition import CameraAcquisitionThread
from admet.engines.acquisition.camera.camera import Camera
from admet.engines.acquisition.camera.video import VideoWorker


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
    parser.add_argument("--dir", default=".", help="where bench.avi goes; use the recording drive")
    args = parser.parse_args()

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
    thread.start()
    time.sleep(0.5)
    writer = VideoWorker(args.dir, "bench", args.width, args.height, 24.0)
    stop = threading.Event()
    hogger = threading.Thread(target=hog, args=(stop, 8.0, 25.0), daemon=True)
    if args.contend:
        hogger.start()
    if not thread.start_recording(writer, max_time=args.seconds):
        print("the writer could not start")
        return 1
    buffers = device.MaxNumBuffer.Value
    started = time.perf_counter()
    while thread.recording and time.perf_counter() - started < args.seconds + 2:
        time.sleep(0.1)
    elapsed = time.perf_counter() - started
    stop.set()

    grabber = getattr(device, "StreamGrabber", None)
    statistics = {}
    for name in ("Statistic_Total_Buffer_Count", "Statistic_Failed_Buffer_Count",
                 "Statistic_Buffer_Underrun_Count", "Statistic_Missed_Frame_Count"):
        try:
            statistics[name] = getattr(grabber, name).Value
        except Exception:
            pass
    thread.stop()
    camera.close()

    captured, written, arrived_fps = writer.frame_count, writer.written, writer.measured_fps
    print(f"pylon buffers {buffers}{' with the interpreter hogged' if args.contend else ''}")
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
