"""Video for the protocol steps that ask for it, and only while they run.

A step with record set gets its own file from its start to its end. Nothing is
recorded at a confirmation gate. A pause closes the file; resuming the step opens
a new part. Every way a protocol stops (stop, skip, error, timeout, E-STOP) closes
it, and no step records more than its limit, at most RECORD_CAP_S in total over
its parts: the camera stops itself there, and the protocol's own ticks check it too.
If the camera cannot start a step's video, the protocol is paused: the step asked
for video, so it does not run on without it unless the operator resumes.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .pipeline import RECORD_CAP_S
from .recording import _safe_recording_label

log = logging.getLogger(__name__)

# A part shorter than this is not worth opening once a step has nearly used its limit.
_MIN_PART_S = 0.5


class StepVideo:
    def __init__(
        self,
        steps: list[Any],
        camera: Any,
        *,
        video_dir: Path,
        label: str,
        frame_size: tuple[int, int],
        fps: float,
        writer_factory: Callable[[Path, str, int, int, float], Any],
        on_failure: Callable[[str], None] = lambda message: None,
    ) -> None:
        self._on_failure = on_failure
        self._steps = steps
        self._camera = camera
        self._video_dir = Path(video_dir)
        self._label = _safe_recording_label(label)
        self._frame_size = frame_size
        self._fps = fps
        self._writer_factory = writer_factory
        self._lock = threading.RLock()
        self._open: dict[str, Any] | None = None
        self._recorded_s: dict[int, float] = {}
        self._parts: dict[int, int] = {}
        self._finished: set[int] = set()
        self.videos: list[dict[str, Any]] = []

    def limit(self, index: int) -> float:
        step = self._steps[index]
        return min(step.record_max_s or RECORD_CAP_S, RECORD_CAP_S)

    def on_event(self, event: Any) -> None:
        """Open or close video as the protocol reports what it is doing."""
        with self._lock:
            index = event.current_step
            outcome, state = str(event.outcome), str(event.state)
            if not event.step_name or not 0 <= index < len(self._steps):
                self.close("protocol ended" if outcome != "running" else "no step")
                return
            if outcome != "running":
                self.close(outcome)
                self._finished.add(index)
                return
            if state == "paused":
                self.close("paused")
                return
            if event.confirmation_message:
                self.close("waiting at a gate")
                return
            if self._open is not None:
                if self._open["index"] != index:
                    self.close("next step")
                elif time.monotonic() - self._open["started"] >= self._open["limit"]:
                    self.close("limit")
                    return
            if (self._open is None and state == "running" and self._steps[index].record
                    and index not in self._finished):
                self._start(index)

    def _start(self, index: int) -> None:
        remaining = self.limit(index) - self._recorded_s.get(index, 0.0)
        if remaining < _MIN_PART_S:
            return
        part = self._parts.get(index, 0) + 1
        self._parts[index] = part
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{self._label}_step{index + 1:02d}" + (f"_part{part}" if part > 1 else "") + f"_{stamp}"
        width, height = self._frame_size
        writer = self._writer_factory(self._video_dir, name, width, height, self._fps)
        self._camera.set_recording_complete_callback(self._camera_stopped)
        if not self._camera.start_recording(writer, max_time=remaining):
            message = f"could not start video for step {index + 1} ({self._steps[index].name})"
            log.error(message)
            self.videos.append({"step": index + 1, "step_name": self._steps[index].name, "part": part,
                                "video_path": "", "started_at": datetime.now().isoformat(timespec="seconds"),
                                "duration_s": 0.0, "stopped_by": "camera failed to start"})
            self._on_failure(message)
            return
        self._open = {"index": index, "part": part, "name": name, "started": time.monotonic(),
                      "limit": remaining, "started_at": datetime.now().isoformat(timespec="seconds")}

    def _camera_stopped(self) -> None:
        """The camera reached the time limit and closed the file itself."""
        with self._lock:
            if self._open is not None:
                self._finish("limit", frames=getattr(self._camera, "last_recording_frames", None))

    def close(self, reason: str) -> None:
        with self._lock:
            if self._open is None:
                return
            frames = self._camera.stop_recording() if self._camera.recording else None
            self._finish(reason, frames=frames)

    def _finish(self, reason: str, *, frames: int | None) -> None:
        segment, self._open = self._open, None
        duration = time.monotonic() - segment["started"]
        index = segment["index"]
        self._recorded_s[index] = self._recorded_s.get(index, 0.0) + duration
        width, height = self._frame_size
        self.videos.append({
            "step": index + 1, "step_name": self._steps[index].name, "part": segment["part"],
            "recording_id": segment["name"], "video_path": str(self._video_dir / f"{segment['name']}.avi"),
            "started_at": segment["started_at"], "stopped_at": datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(duration, 3), "frames_recorded": frames, "stopped_by": reason,
            "width": width, "height": height, "converted_fps": self._fps,
            # The rate the camera actually delivered, as whole-run recordings report it.
            "acquisition_fps": round(frames / duration, 3) if frames and duration > 0 else 0.0,
        })
