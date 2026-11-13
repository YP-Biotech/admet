"""Droplets followed from frame to frame, and counted as they pass a gate.

Each track follows its own velocity, in any direction. A new track starts from the
train's common shift: the 2-D displacement most droplets agree on between the last
frame and this one, never backwards along the flow, since a train moving most of
its spacing each frame looks just like one creeping backwards.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class Tracker:
    def __init__(self, *, max_shift: float = 250.0, bin_px: float = 4.0, tolerance_px: float = 15.0,
                 max_miss: int = 2, gate_x: float | None = None):
        self.tracks: dict[int, dict[str, Any]] = {}
        self.finished: list[dict[str, Any]] = []
        self.next_id = 1
        self.max_shift, self.bin_px, self.tolerance, self.max_miss = max_shift, bin_px, tolerance_px, max_miss
        self.shift: tuple[float, float] = (0.0, 0.0)
        self.steps: list[float] = []
        self.gate_x = gate_x
        self.crossings: list[float] = []     # when each droplet passed the gate, in frames, to a fraction

    def update(self, detections: list[dict[str, Any]], frame_index: int) -> None:
        """Match this frame's detections to the tracks; each gets a track_id and droplet_id."""
        live = [t for t in self.tracks.values() if frame_index - t["frames"][-1] == 1]
        shift = self._train_shift([(t["cx"][-1], t["cy"][-1]) for t in live],
                                  [(d["centroid_x"], d["centroid_y"]) for d in detections])
        if shift is not None:
            self.shift = shift
        used: set[int] = set()
        for tid, track in sorted(self.tracks.items(), key=lambda kv: kv[1]["frames"][-1]):
            gap = frame_index - track["frames"][-1]
            vx, vy = track["velocity"] if track["velocity"] is not None else self.shift
            px, py = track["cx"][-1] + vx * gap, track["cy"][-1] + vy * gap
            best, best_error = None, max(self.tolerance, 0.3 * float(np.hypot(vx, vy))) * gap
            for i, d in enumerate(detections):
                if i in used:
                    continue
                error = float(np.hypot(d["centroid_x"] - px, d["centroid_y"] - py))
                if error < best_error:
                    best, best_error = i, error
            if best is None:
                track["missed"] += 1
                if track["missed"] > self.max_miss:
                    self.finished.append(self.tracks.pop(tid))
                continue
            used.add(best)
            self._extend(track, detections[best], frame_index, gap)
        for i, d in enumerate(detections):
            if i in used:
                continue
            track = {"id": self.next_id, "frames": [], "cx": [], "cy": [], "properties": [], "velocity": None,
                     "missed": 0}
            self.tracks[self.next_id] = track
            self.next_id += 1
            self._extend(track, d, frame_index, 0)

    def _extend(self, track: dict, detection: dict, frame_index: int, gap: int) -> None:
        cx, cy = detection["centroid_x"], detection["centroid_y"]
        if gap:
            track["velocity"] = ((cx - track["cx"][-1]) / gap, (cy - track["cy"][-1]) / gap)
            self.steps.append(float(np.hypot(*track["velocity"])))
            if self.gate_x is not None and (track["cx"][-1] < self.gate_x) != (cx < self.gate_x):
                # The crossing time to a fraction of a frame, from the step across the gate.
                self.crossings.append(frame_index - (cx - self.gate_x) / (cx - track["cx"][-1]) * gap)
        track["frames"].append(frame_index)
        track["cx"].append(cx)
        track["cy"].append(cy)
        track["properties"].append(detection)
        track["missed"] = 0
        detection["track_id"] = track["id"]
        detection["droplet_id"] = track["id"]

    def _train_shift(self, previous: list[tuple], current: list[tuple]) -> tuple[float, float] | None:
        votes: dict[tuple, list] = {}
        for px, py in previous:
            for cx, cy in current:
                dx, dy = cx - px, cy - py
                if -self.bin_px <= dx <= self.max_shift and abs(dy) <= self.max_shift:
                    votes.setdefault((int(dx // self.bin_px), int(dy // self.bin_px)), []).append((dx, dy))
        if not votes:
            return None
        best = max(len(v) for v in votes.values())
        bins = sorted((b for b, v in votes.items() if len(v) == best), key=lambda b: b[0] ** 2 + b[1] ** 2)
        chosen = votes[bins[0]]
        return float(np.median([d[0] for d in chosen])), float(np.median([d[1] for d in chosen]))

    def finish(self) -> list[dict[str, Any]]:
        """Every track, the ones still open included; the tracker is done."""
        self.finished.extend(self.tracks.values())
        self.tracks = {}
        return self.finished

    def speed(self) -> float:
        """The train's speed, px per frame: the median step over every track."""
        return float(np.median(self.steps)) if self.steps else 0.0

    def generation(self, fps: float) -> dict[str, Any]:
        """How often droplets pass the gate: the period in frames and the rate per second."""
        gaps = np.diff(self.crossings)
        if not len(gaps):
            return {"crossings": len(self.crossings), "period_frames": None, "period_sd_frames": None, "rate_hz": None}
        period = float(np.mean(gaps))
        return {"crossings": len(self.crossings), "period_frames": period, "period_sd_frames": float(np.std(gaps)),
                "rate_hz": fps / period if fps and period else None}
