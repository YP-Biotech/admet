"""Camera frames to a video file.

Frames are appended to one raw file as they arrive, then ffmpeg turns that file
into the video once every queued frame is written. Beside the video, a small
<name>.video.json keeps what only an expert needs: frames captured, written and
dropped, and how the encoding went. If an encoding did not finish, the raw file
and its .video.json stay, and encode_leftovers() finishes it later.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

import numpy as np

from .constants import QUEUE_GET_TIMEOUT, WRITER_QUEUE_SIZE

log = logging.getLogger(__name__)

# Raw files being encoded now, so reopening a project never starts a second encoding of one.
_ENCODING: set[str] = set()
_ENCODING_LOCK = Lock()

# (raw file, video, width, height, fps) -> (encoded, why not)
Encoder = Callable[[Path, Path, int, int, float], tuple[bool, str]]
# (video path, encoded, why not)
Finished = Callable[[str, bool, str], None]


class VideoWorker:
    def __init__(
        self,
        output_dir: str | Path,
        prefix: str,
        width: int,
        height: int,
        fps: float,
        *,
        encoder: Encoder | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.prefix = prefix
        self.width = width
        self.height = height
        self.fps = fps
        self.encoder = encoder
        self.on_finished: Finished | None = None

        self.raw_path = self.output_dir / f"{prefix}.raw"
        self.info_path = self.output_dir / f"{prefix}.video.json"
        self.video_path = self.output_dir / f"{prefix}.avi"
        self.queue: Queue = Queue(maxsize=WRITER_QUEUE_SIZE)
        self.thread: Thread | None = None
        self._stop_event = Event()
        self._file = None
        self.frame_count = 0          # frames handed over by the camera
        self.written = 0              # frames actually in the raw file

    def start(self) -> bool:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._file = self.raw_path.open("wb")
            _write_info(self.info_path, self._info("recording"))
            self._stop_event.clear()
            self.frame_count = self.written = 0
            self.thread = Thread(target=self._writer_thread, daemon=True)
            self.thread.start()
            return True
        except Exception as exc:
            log.error("Failed to start video writer: %s", exc)
            return False

    def write(self, frame: np.ndarray) -> bool:
        if self._stop_event.is_set():
            return False
        if self.queue.full():
            try:
                self.queue.get_nowait()   # the disk is behind: the oldest waiting frame goes
            except Empty:
                return False
        try:
            self.queue.put_nowait(frame)
            self.frame_count += 1
            return True
        except Full:
            return False

    def stop(self) -> str:
        """Write every queued frame, then start the encoding; the video's path, or "" if nothing was written."""
        self._stop_event.set()
        if self.thread:
            self.thread.join()        # no time limit: a frame left unwritten would be lost
        if self._file:
            self._file.close()
            self._file = None
        dropped = self.frame_count - self.written
        if dropped:
            log.warning("%s: %d of %d frames were dropped, the disk could not keep up",
                        self.prefix, dropped, self.frame_count)
        if not self.written:
            self.raw_path.unlink(missing_ok=True)
            self.info_path.unlink(missing_ok=True)
            return ""
        _write_info(self.info_path, self._info("encoding"))
        Thread(target=self._encode, daemon=True).start()
        return str(self.video_path)

    def _writer_thread(self) -> None:
        while not self._stop_event.is_set() or not self.queue.empty():
            try:
                frame = self.queue.get(timeout=QUEUE_GET_TIMEOUT)
            except Empty:
                continue
            try:
                if frame.dtype == np.uint16:
                    frame = (frame >> 8).astype(np.uint8)
                self._file.write(frame.tobytes())
                self.written += 1
            except Exception as exc:
                log.error("%s: a frame could not be written: %s", self.prefix, exc)

    def _encode(self) -> None:
        encoded, reason = encode_recording(self.info_path, self.encoder)
        if self.on_finished is not None:
            try:
                self.on_finished(str(self.video_path), encoded, reason)
            except Exception:
                log.exception("video finished callback failed")

    def _info(self, status: str) -> dict:
        return {
            "video": self.video_path.name, "raw": self.raw_path.name,
            "width": self.width, "height": self.height, "fps": self.fps,
            "frames_captured": self.frame_count, "frames_written": self.written,
            "frames_dropped": self.frame_count - self.written, "status": status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }


def encode_recording(info_path: Path, encoder: Encoder | None = None) -> tuple[bool, str]:
    """Turn a recording's raw file into its video; the raw file goes once the video is whole."""
    info = json.loads(Path(info_path).read_text(encoding="utf-8"))
    directory = Path(info_path).parent
    raw, video = directory / info["raw"], directory / info["video"]
    with _ENCODING_LOCK:
        if str(raw) in _ENCODING:
            return False, "already being encoded"
        _ENCODING.add(str(raw))
    try:
        return _encode_raw(info_path, info, raw, video, encoder)
    finally:
        with _ENCODING_LOCK:
            _ENCODING.discard(str(raw))


def _encode_raw(info_path: Path, info: dict, raw: Path, video: Path, encoder: Encoder | None) -> tuple[bool, str]:
    frame_bytes = int(info["width"]) * int(info["height"])
    if not raw.is_file() or frame_bytes <= 0:
        return _finish(info_path, info, False, "the raw frames are missing")
    whole = raw.stat().st_size // frame_bytes
    if whole * frame_bytes != raw.stat().st_size:
        with raw.open("r+b") as handle:   # a recording cut off mid-frame: keep the whole frames
            handle.truncate(whole * frame_bytes)
    if not whole:
        return _finish(info_path, info, False, "no whole frame in the raw file")
    info["frames_written"] = whole
    encoded, reason = (encoder or _ffmpeg)(raw, video, int(info["width"]), int(info["height"]), float(info["fps"]))
    if encoded:
        raw.unlink(missing_ok=True)
        log.info("Encoded %s from %d frames", video.name, whole)
    else:
        log.error("Encoding %s failed: %s; its raw frames are kept at %s", video.name, reason, raw)
    return _finish(info_path, info, encoded, reason)


def encode_leftovers(camera_dir: Path, on_finished: Finished | None = None,
                     encoder: Encoder | None = None) -> list[str]:
    """Encode every recording whose raw frames are still waiting, one after another; their videos."""
    done = []
    for info_path in sorted(Path(camera_dir).glob("*.video.json")):
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if info.get("status") == "done" or not (info_path.parent / str(info.get("raw", ""))).is_file():
            continue
        encoded, reason = encode_recording(info_path, encoder)
        if reason == "already being encoded":
            continue
        video = str(info_path.parent / info["video"])
        if on_finished is not None:
            on_finished(video, encoded, reason)
        if encoded:
            done.append(video)
    return done


def _ffmpeg(raw: Path, video: Path, width: int, height: int, fps: float) -> tuple[bool, str]:
    partial = video.with_name(video.name + ".part")   # the video appears only once it is whole
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pixel_format", "gray",
           "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", str(raw),
           "-c:v", "rawvideo", "-pix_fmt", "gray", "-f", "avi", str(partial)]
    try:
        done = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        return False, f"ffmpeg could not start: {exc}"
    if done.returncode != 0:
        partial.unlink(missing_ok=True)
        lines = (done.stderr or "").strip().splitlines()
        return False, lines[-1] if lines else f"ffmpeg exited with {done.returncode}"
    partial.replace(video)
    return True, ""


def _finish(info_path: Path, info: dict, encoded: bool, reason: str) -> tuple[bool, str]:
    info.update({"status": "done" if encoded else "failed", "reason": reason,
                 "updated_at": datetime.now().isoformat(timespec="seconds")})
    _write_info(info_path, info)
    return encoded, reason


def _write_info(path: Path, info: dict) -> None:
    path.write_text(json.dumps(info, indent=2), encoding="utf-8")
