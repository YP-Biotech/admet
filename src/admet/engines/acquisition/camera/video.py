"""Camera frames to a video file.

Frames are appended to one raw file as they arrive, then ffmpeg turns that file
into the video once every queued frame is written, and writes the recording's
record into the video itself (see tags.py): what it is, the camera's settings,
frames captured, written and dropped. While the raw file waits, a small
<name>.video.json holds the same record; it goes once the video is whole. If an
encoding did not finish, the raw file and its .video.json stay, and
encode_leftovers() finishes it later.

Frames are gathered into blocks as they arrive, the camera's thread only keeping each one (no
copy, nothing that lets go of the interpreter mid-frame); a block, once full, goes to the writer
thread, which joins it in one copy and puts it on the disk in one write, without holding the
interpreter while the disk works, so the preview and the live QC slow it little. A disk slower than the camera leaves full blocks waiting
in memory, up to a limit set from the free memory; frames that find no room are dropped and
counted, never a reason to stop: a recording with gaps is still the recording.
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
from collections import deque
from threading import Condition, Event, Lock, Thread

import numpy as np

from .acquisition import as_8bit
from .constants import BLOCK_BYTES, BLOCK_SECONDS, MEMORY_LIMIT_MAX, MEMORY_LIMIT_SHARE, QUEUE_GET_TIMEOUT
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
        memory_limit: int | None = None,
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
        # A protocol's step videos are encoded after the run, one after another, so no encode
        # competes with the next step's recording for the disk; a lone recording encodes at stop.
        self.encode_at_stop = True
        self.started_at = ""
        self.stopped_at = ""
        self._first_frame_at: float | None = None      # monotonic, for the measured frame rate
        self._last_frame_at: float | None = None

        self.raw_path = self.output_dir / f"{prefix}.raw"
        self.info_path = self.output_dir / f"{prefix}.video.json"
        self.video_path = self.output_dir / f"{prefix}.avi"
        self.thread: Thread | None = None
        self._stop_event = Event()
        self._file = None
        self.frame_count = 0          # frames handed over by the camera
        self.written = 0              # frames actually in the raw file
        # Frames wait in blocks: the one being gathered, and the full ones for the disk.
        self.memory_limit = memory_limit      # bytes waiting frames may take; None: from the free memory
        self._block: list[np.ndarray] = []
        self._block_frames = 0                # frames a block holds, set by the first frame's size
        self._full: deque[list[np.ndarray]] = deque()
        self._held = 0                        # bytes of every frame kept and not yet on the disk
        self.peak_held = 0
        self._ready = Condition()

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
            self._block, self._block_frames, self._held, self.peak_held = [], 0, 0, 0
            self._full.clear()
            if self.memory_limit is None:
                self.memory_limit = default_memory_limit()
            self.thread = Thread(target=self._writer_thread, daemon=True)
            self.thread.start()
            return True
        except Exception as exc:
            log.error("Failed to start video writer: %s", exc)
            return False

    def write(self, frame: np.ndarray) -> bool:
        """Take a frame: kept in the block being gathered, or dropped and counted when the memory for
        waiting frames is full. True while the recording runs, kept or not: it was captured."""
        if self._stop_event.is_set():
            return False
        self.frame_count += 1
        self._last_frame_at = time.monotonic()
        if self._first_frame_at is None:
            self._first_frame_at = self._last_frame_at
        frame = as_8bit(frame)
        if self._held + frame.nbytes > (self.memory_limit or 0) and self._held:
            return True                                       # no room: dropped, frame_count - written says so
        if not self._block_frames:
            self._block_frames = block_frames(frame.shape, self.fps)
        self._block.append(frame)
        self._held += frame.nbytes
        if self._held > self.peak_held:
            self.peak_held = self._held
        if len(self._block) >= self._block_frames:
            self._hand_over()
        return True

    def _hand_over(self) -> None:
        """The block being gathered goes to the writer thread, as full as it is."""
        block, self._block = self._block, []
        if not block:
            return
        with self._ready:
            self._full.append(block)
            self._ready.notify()

    def stop(self) -> str:
        """Stop taking frames and return at once; the writer thread drains what is queued, closes the
        file and starts the encoding on its own. The video's path, or "" if no frame was ever taken.

        The caller is the protocol's thread at a step's end: it must not wait for a disk."""
        self._stop_event.set()
        self._hand_over()
        self.stopped_at = datetime.now().isoformat(timespec="seconds")
        return str(self.video_path) if self.frame_count else ""

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until every queued frame is on disk and the file is closed; False on timeout."""
        if self.thread is not None:
            self.thread.join(timeout)
            return not self.thread.is_alive()
        return True

    def _finish_file(self) -> None:
        """The writer thread's last act: the file closed, the record written, the encode started."""
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
            return
        if not self.encode_at_stop:
            _write_info(self.info_path, self._info("recorded"))   # the raw file waits; encode() comes later
            return
        _write_info(self.info_path, self._info("encoding"))
        Thread(target=self._encode, daemon=True).start()

    def _writer_thread(self) -> None:
        _DRAINING.add(self)
        try:
            self._drain()
            self._finish_file()
        finally:
            _DRAINING.discard(self)

    def _drain(self) -> None:
        """Full blocks to the disk, oldest first, one write each, until the recording stopped and none waits."""
        while True:
            with self._ready:
                while not self._full and not self._stop_event.is_set():
                    self._ready.wait(QUEUE_GET_TIMEOUT)
                block = self._full.popleft() if self._full else None
            if block is None:                                 # stopped, nothing waiting
                if self._block:                               # a frame came in as it stopped: its block goes too
                    self._hand_over()
                    continue
                break
            size = sum(frame.nbytes for frame in block)
            try:
                same = all(frame.shape == block[0].shape for frame in block)
                data = np.stack(block) if same else b"".join(frame.tobytes() for frame in block)
                self._file.write(data.data if same else data)   # one write; the disk works without the interpreter
                self.written += len(block)
            except Exception as exc:
                log.error("%s: %d frames could not be written: %s", self.prefix, len(block), exc)
            finally:
                self._held -= size

    def encode(self) -> tuple[bool, str]:
        """Encode the raw file now, in this thread, and tell on_finished; nothing to do if no frames were kept."""
        self.wait()                                   # every frame on disk first
        if not self.info_path.is_file():
            return False, "nothing recorded"
        encoded, reason = encode_recording(self.info_path, self.encoder)
        if self.on_finished is not None:
            try:
                self.on_finished(str(self.video_path), encoded, reason)
            except Exception:
                log.exception("video finished callback failed")
        return encoded, reason

    def _encode(self) -> None:
        self.encode()

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
            "memory_peak_mb": round(self.peak_held / 2**20, 1),
            "memory_limit_mb": round((self.memory_limit or 0) / 2**20, 1),
            "started_at": self.started_at, "stopped_at": self.stopped_at,
            "acquisition_fps": self.measured_fps,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "record": dict(self.record),
        }


def block_frames(shape: tuple[int, ...], fps: float) -> int:
    """Frames a block gathers: about BLOCK_BYTES of them, and no more than BLOCK_SECONDS of recording,
    so a slow recording reaches the disk soon too."""
    frame_bytes = max(1, int(np.prod(shape)))
    by_size = max(1, BLOCK_BYTES // frame_bytes)
    by_time = max(1, int(fps * BLOCK_SECONDS)) if fps and fps > 0 else by_size
    return max(1, min(by_size, by_time))


def default_memory_limit() -> int:
    """Bytes waiting frames may take: a share of the free memory, capped; 1 GB where the system does not say."""
    from .memory import memory_gb

    try:
        _total, free = memory_gb()
    except Exception:
        free = None
    if not free:
        return 1 << 30
    return int(min(free * 2**30 * MEMORY_LIMIT_SHARE, MEMORY_LIMIT_MAX))


_DRAINING: set[VideoWorker] = set()        # writers still putting frames on disk after their stop()


def wait_for_writers(timeout: float = 60.0) -> int:
    """Wait for every writer still draining, so a shutdown never leaves frames in a queue; how many waited for."""
    deadline = time.monotonic() + timeout
    waited = 0
    for writer in list(_DRAINING):
        if writer.wait(max(0.0, deadline - time.monotonic())):
            waited += 1
    return waited


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
