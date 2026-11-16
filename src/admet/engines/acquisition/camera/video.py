"""Camera frames to a video file.

Frames are appended to one raw file as they arrive, then ffmpeg turns that file
into the video once every queued frame is written, and writes the recording's
record into the video itself (see tags.py): what it is, the camera's settings,
frames captured, written and dropped. While the raw file waits, a small
<name>.video.json holds the same record; it goes once the video is whole. If an
encoding did not finish, the raw file and its .video.json stay, and
encode_leftovers() finishes it later.

Frames the disk cannot keep up with are dropped and counted, never a reason to
stop: a recording with gaps is still the recording.
"""

from __future__ import annotations

import json
import logging
import time
import subprocess
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

import numpy as np

from .acquisition import as_8bit
from .constants import QUEUE_GET_TIMEOUT, WRITER_BATCH, WRITER_QUEUE_SIZE
from .tags import ffmpeg_metadata_args

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
        record: dict | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.prefix = prefix
        self.width = width
        self.height = height
        self.fps = fps
        self.encoder = encoder
        self.on_finished: Finished | None = None
        # What the recording is, beyond its frames: written into the video at encode.
        self.record: dict = dict(record or {})
        self.started_at = ""
        self.stopped_at = ""
        self._first_frame_at: float | None = None      # monotonic, for the measured frame rate
        self._last_frame_at: float | None = None

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
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.stopped_at = ""
            self._first_frame_at = self._last_frame_at = None
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
            self._last_frame_at = time.monotonic()
            if self._first_frame_at is None:
                self._first_frame_at = self._last_frame_at
            return True
        except Full:
            return False

    def stop(self) -> str:
        """Write every queued frame, then start the encoding; the video's path, or "" if nothing was written."""
        self._stop_event.set()
        self.stopped_at = datetime.now().isoformat(timespec="seconds")
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
                frames = [self.queue.get(timeout=QUEUE_GET_TIMEOUT)]
            except Empty:
                continue
            while len(frames) < WRITER_BATCH:          # whatever else is waiting goes in the same write
                try:
                    frames.append(self.queue.get_nowait())
                except Empty:
                    break
            try:
                self._file.write(b"".join(as_8bit(frame).tobytes() for frame in frames))
                self.written += len(frames)
            except Exception as exc:
                log.error("%s: %d frames could not be written: %s", self.prefix, len(frames), exc)

    def _encode(self) -> None:
        encoded, reason = encode_recording(self.info_path, self.encoder)
        if self.on_finished is not None:
            try:
                self.on_finished(str(self.video_path), encoded, reason)
            except Exception:
                log.exception("video finished callback failed")

    @property
    def measured_fps(self) -> float:
        """Frames a second as they arrived, first frame to last; 0 before two frames."""
        if self._first_frame_at is None or self._last_frame_at is None or self.frame_count < 2:
            return 0.0
        span = self._last_frame_at - self._first_frame_at
        return round((self.frame_count - 1) / span, 3) if span > 0 else 0.0

    def _info(self, status: str) -> dict:
        return {
            "video": self.video_path.name, "raw": self.raw_path.name,
            "width": self.width, "height": self.height, "fps": self.fps,
            "frames_captured": self.frame_count, "frames_written": self.written,
            "frames_dropped": self.frame_count - self.written, "status": status,
            "started_at": self.started_at, "stopped_at": self.stopped_at,
            "acquisition_fps": self.measured_fps,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "record": dict(self.record),
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
    encode = encoder or partial(_ffmpeg, record=tag_record(info))
    encoded, reason = encode(raw, video, int(info["width"]), int(info["height"]), float(info["fps"]))
    if encoded:
        raw.unlink(missing_ok=True)
        log.info("Encoded %s from %d frames", video.name, whole)
    else:
        log.error("Encoding %s failed: %s; its raw frames are kept at %s", video.name, reason, raw)
    return _finish(info_path, info, encoded, reason)


def tag_record(info: dict) -> dict:
    """The record the video carries: what the writer counted, and what it was told it was recording."""
    captured = int(info.get("frames_captured") or 0)
    written = int(info.get("frames_written") or 0)
    return {
        **{key: value for key, value in (info.get("record") or {}).items()},
        "recording_id": (info.get("record") or {}).get("recording_id") or Path(str(info.get("video", ""))).stem,
        "width": int(info.get("width") or 0), "height": int(info.get("height") or 0),
        "file_fps": float(info.get("fps") or 0.0),
        "frames_captured": captured, "frames_written": written,
        "frames_dropped": max(0, captured - written) if captured else int(info.get("frames_dropped") or 0),
        "complete": not captured or written >= captured,
        "started_at": info.get("started_at") or (info.get("record") or {}).get("started_at", ""),
        "stopped_at": info.get("stopped_at", ""),
        "acquisition_fps": float(info.get("acquisition_fps") or 0.0),
        "encoded_at": datetime.now().isoformat(timespec="seconds"),
    }


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


def _ffmpeg(raw: Path, video: Path, width: int, height: int, fps: float,
            record: dict | None = None) -> tuple[bool, str]:
    part = video.with_name(video.name + ".part")   # the video appears only once it is whole
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pixel_format", "gray",
           "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", str(raw),
           "-c:v", "rawvideo", "-pix_fmt", "gray", "-f", "avi",
           *(ffmpeg_metadata_args(record) if record else []), str(part)]
    try:
        done = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        return False, f"ffmpeg could not start: {exc}"
    if done.returncode != 0:
        part.unlink(missing_ok=True)
        lines = (done.stderr or "").strip().splitlines()
        return False, lines[-1] if lines else f"ffmpeg exited with {done.returncode}"
    part.replace(video)
    return True, ""


def _finish(info_path: Path, info: dict, encoded: bool, reason: str) -> tuple[bool, str]:
    if encoded:
        Path(info_path).unlink(missing_ok=True)     # the video now carries the record itself
        return True, ""
    info.update({"status": "failed", "reason": reason,
                 "updated_at": datetime.now().isoformat(timespec="seconds")})
    _write_info(info_path, info)
    return encoded, reason


def _write_info(path: Path, info: dict) -> None:
    path.write_text(json.dumps(info, indent=2), encoding="utf-8")
