"""Droplet measurement over a video: detect each frame, follow droplets, count them at a gate."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import detection
from .io import VideoReader
from .logger import get_logger
from .tracking import Tracker

logger = get_logger("Pipeline")


class DropletPipeline:
    """background -> per frame: match, difference, threshold, bodies -> tracks -> gate."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.detection = {**detection.DEFAULTS, **self.config.get("detection", {})}
        self.video_reader: VideoReader | None = None
        self.background: np.ndarray | None = None
        self.threshold_value: float | None = None
        self.noise_sigma: float | None = None
        self.frame_count = 0
        self.progress_callback: Callable[[int, str], None] | None = None
        self.frame_callback: Callable[[dict], None] | None = None
        self.tracker: Tracker | None = None

        # What the run keeps for its rows and its viewer.
        self.stored_frame_data: dict[int, dict] = {}
        self.all_detections: list[dict] = []
        self.finished_tracks: list[dict] = []
        self.corrected_tracks: list[dict] = []
        self.trajectory_meta: dict = {}
        self.cache_dir: Path | None = None
        self.contour_cache_buffer: list = []
        self.contour_chunk_index = 0
        self.contour_cache_chunk_size = 5000

    def set_video(self, video_path: str) -> None:
        video_config = self.config.get("video", {})
        self.video_reader = VideoReader(source=video_path, scale_factor=video_config.get("scale_factor", 1.0),
                                        roi=video_config.get("roi"))

    def set_progress_callback(self, callback: Callable[[int, str], None]) -> None:
        self.progress_callback = callback

    def set_frame_callback(self, callback: Callable[[dict], None]) -> None:
        self.frame_callback = callback

    def run(self) -> dict:
        logger.info("Starting pipeline")
        started = time.time()
        if not self.video_reader:
            return {"error": "No video source"}
        if not self.video_reader.open():
            return {"error": "Failed to open video"}
        total_frames = self.video_reader.get_properties()["total_frames"]
        start_frame, end_frame = self._frame_range(total_frames)

        self._report_progress(0, "Building the background...")
        if not self._compute_background():
            return {"error": "Background extraction failed"}
        self._report_progress(10, "Processing frames...")
        self._prepare_cache_dir()
        self._process_all_frames(start_frame, end_frame)
        self.video_reader.release()
        elapsed = time.time() - started
        self._report_progress(100, "Complete")
        logger.info(f"Pipeline completed in {elapsed:.2f}s")
        return self._get_results(elapsed)

    # -- frames --------------------------------------------------------------------------

    def _frame_range(self, total_frames: int) -> tuple[int, int]:
        video_config = self.config.get("video", {})
        start_frame = max(int(video_config.get("start_frame") or 0), 0)
        if total_frames > 0:
            start_frame = min(start_frame, total_frames)
        configured_end = video_config.get("end_frame")
        end_frame = total_frames if configured_end is None else max(int(configured_end), start_frame)
        if total_frames > 0:
            end_frame = min(end_frame, total_frames)
        return start_frame, end_frame

    def _report_progress(self, percent: int, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(percent, message)
        logger.info(f"{percent}% - {message}")

    def _compute_background(self) -> bool:
        """The empty channel: the bright side of each pixel over frames sampled across the range."""
        try:
            background_config = self.config.get("background", {})
            n_frames = int(background_config.get("sample_frames", 120))
            start_frame, end_frame = self._frame_range(self.video_reader.total_frames)
            frames = self.video_reader.read_sampled_frames(n_frames, seed=11, start_frame=start_frame,
                                                           end_frame=end_frame)
            if not frames:
                logger.error("No frames to build a background from")
                return False
            self.background = detection.percentile_background(frames, float(background_config.get("percentile", 85)))
            self.video_reader.seek_frame(0)
            return True
        except Exception as exc:
            logger.error(f"Background failed: {exc}")
            return False

    def _process_all_frames(self, start_frame: int, end_frame: int) -> None:
        total_frames = max(end_frame - start_frame, 0)
        processing_config = self.config.get("processing", {})
        store_preview_frames = processing_config.get("store_preview_frames", True)
        max_preview_frames = max(1, int(processing_config.get("max_preview_frames", 500)))
        preview_stride = max(1, int(np.ceil(total_frames / max_preview_frames)))
        next_progress = 15
        processing_start = time.time()
        processed = 0
        tracking = self.config.get("tracking", {})
        roi = self.config.get("video", {}).get("roi")
        width = int(roi[2]) if roi else self.video_reader.get_properties()["width"]    # the frames are cropped
        gate = int(round(width * float(self.detection.get("gate_pct", 50.0)) / 100))
        self.tracker = Tracker(max_miss=int(tracking.get("max_miss", 2)), gate_x=gate if 0 < gate < width else None)

        self.video_reader.seek_frame(start_frame)
        for frame_idx, frame in self.video_reader.get_frame_generator():
            absolute = start_frame + frame_idx
            if absolute >= end_frame:
                break
            result = self._process_frame(frame, absolute)
            self.tracker.update(result["properties"], absolute)
            self._cache_detection_assets(result)
            if store_preview_frames and (processed % preview_stride == 0 or processed == total_frames - 1):
                self.stored_frame_data[absolute] = result
                if self.frame_callback:
                    self.frame_callback(result)
            self.all_detections.append({"frame": absolute, "count": len(result["contours"]),
                                        "properties": result["properties"]})
            processed += 1
            self.frame_count = processed
            if total_frames > 0:
                progress = 10 + int(85 * processed / total_frames)
                while progress >= next_progress and next_progress <= 95:
                    eta = self._format_eta(processing_start, processed, total_frames)
                    self._report_progress(next_progress, f"Frame {absolute + 1}/{end_frame}, ETA {eta}")
                    next_progress += 5

        self._flush_contour_cache()
        self.finished_tracks = self.tracker.finish()
        self._apply_trajectory_counter()

    @staticmethod
    def _format_eta(start_time: float, processed_frames: int, total_frames: int) -> str:
        elapsed = max(time.time() - start_time, 0.001)
        rate = processed_frames / elapsed if processed_frames > 0 else 0
        if rate <= 0:
            return "calculating"
        minutes, seconds = divmod(int(round(max((total_frames - processed_frames) / rate, 0))), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"

    def _process_frame(self, frame: np.ndarray, frame_idx: int) -> dict:
        found = detection.detect(frame, self.background, self.detection)
        self.threshold_value = found["threshold"]
        self.noise_sigma = found["sigma"]
        return {
            "frame_number": frame_idx,
            "original": frame,
            "background": self.background,
            "processed": found["images"]["darker"],
            "binary": found["images"]["mask"],
            "bodies": found["images"]["bodies"],
            "contours": found["contours"],
            "rejected": found["rejected"],
            "satellites": found["satellites"],
            "properties": found["properties"],
            "window": found["window"],
        }

    # -- contour cache -----------------------------------------------------------------------

    def _prepare_cache_dir(self) -> None:
        processing_config = self.config.get("processing", {})
        cache_dir = processing_config.get("cache_dir")
        self.contour_cache_buffer = []
        self.contour_chunk_index = 0
        if not cache_dir:
            self.cache_dir = None
            return
        self.cache_dir = Path(cache_dir)
        (self.cache_dir / "contours").mkdir(parents=True, exist_ok=True)
        self.contour_cache_chunk_size = max(1, int(processing_config.get("contour_cache_chunk_size", 5000)))

    def _cache_detection_assets(self, result: dict) -> None:
        if self.cache_dir is None:
            return
        for props in result.get("properties", []):
            contour = props.pop("contour_points", None)
            if contour is not None:
                self.contour_cache_buffer.append((props, contour))
                if len(self.contour_cache_buffer) >= self.contour_cache_chunk_size:
                    self._flush_contour_cache()

    def _flush_contour_cache(self) -> None:
        if not self.contour_cache_buffer or self.cache_dir is None:
            return
        chunk_path = self.cache_dir / "contours" / f"chunk_{self.contour_chunk_index:05d}.npz"
        points, offsets, lengths, offset = [], [], [], 0
        for _, contour in self.contour_cache_buffer:
            contour = np.asarray(contour, dtype=np.int16).reshape(-1, 2)
            points.append(contour)
            offsets.append(offset)
            lengths.append(len(contour))
            offset += len(contour)
        packed = np.vstack(points).astype(np.int16) if points else np.empty((0, 2), dtype=np.int16)
        np.savez_compressed(chunk_path, points=packed, offsets=np.asarray(offsets, dtype=np.int64),
                            lengths=np.asarray(lengths, dtype=np.int32))
        for idx, (props, _) in enumerate(self.contour_cache_buffer):
            props["contour_path"] = str(chunk_path)
            props["contour_offset"] = int(offsets[idx])
            props["contour_length"] = int(lengths[idx])
        self.contour_cache_buffer = []
        self.contour_chunk_index += 1

    # -- trajectories --------------------------------------------------------------------------

    def _apply_trajectory_counter(self) -> None:
        """Place every detection along the flow axis; a track that never progresses is stuck, not a droplet."""
        tracking_config = self.config.get("tracking", {})
        if not tracking_config.get("trajectory_counter", True):
            return
        detections = self._trajectory_detections()
        if len(detections) < 2:
            return
        points = np.asarray([[d["prop"]["centroid_x"], d["prop"]["centroid_y"]] for d in detections], dtype=np.float32)
        areas = np.asarray([d["prop"].get("area", 0.0) for d in detections], dtype=np.float32)
        center = np.mean(points, axis=0)
        centered = points - center
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
        axis = vh[0]
        raw_s = centered @ axis
        if float(np.ptp(raw_s)) <= 1e-6:
            return
        low_limit, high_limit = np.percentile(raw_s, 20), np.percentile(raw_s, 80)
        low_area = float(np.mean(areas[raw_s <= low_limit])) if np.any(raw_s <= low_limit) else 0.0
        high_area = float(np.mean(areas[raw_s >= high_limit])) if np.any(raw_s >= high_limit) else 0.0
        direction = -1.0 if low_area < high_area else 1.0
        s_values = raw_s * direction
        for det, s_value in zip(detections, s_values):
            det["s"] = float(s_value)
            det["prop"]["trajectory_s"] = float(s_value)
        s_min, s_max = float(np.min(s_values)), float(np.max(s_values))
        checkpoint_count = max(1, int(tracking_config.get("trajectory_checkpoints", 6)))
        checkpoints = np.linspace(s_min, s_max, checkpoint_count + 2)[1:-1]
        spacing = float((s_max - s_min) / (checkpoint_count + 1))
        stuck_min_frames = int(tracking_config.get("stuck_min_frames", 8))

        events, next_event_id = [], 1
        for _, source_detections in self._group_detections_by_source(detections).items():
            source_detections.sort(key=lambda item: (item["frame"], item["index"]))
            event = {"detections": source_detections, "checkpoints": set()}
            last_s = None
            for det in source_detections:
                if last_s is not None and det["s"] >= last_s:
                    event["checkpoints"].update(i for i, c in enumerate(checkpoints) if last_s < c <= det["s"])
                last_s = det["s"]
            frames = [det["frame"] for det in source_detections]
            progress = max(det["s"] for det in source_detections) - min(det["s"] for det in source_detections)
            stuck = len(set(frames)) >= stuck_min_frames and progress < max(spacing, 1.0)
            for det in source_detections:
                prop = det["prop"]
                prop["stuck"] = stuck
                prop["source_track_id"] = prop.get("track_id")
                prop["trajectory_event_id"] = None if stuck else next_event_id
                prop["droplet_id"] = None if stuck else next_event_id
                if not stuck:
                    prop["track_id"] = next_event_id
            event.update(stuck=stuck, droplet_id=None if stuck else next_event_id, frames=frames,
                         properties=[det["prop"] for det in source_detections],
                         checkpoint_count=len(event["checkpoints"]))
            if not stuck:
                next_event_id += 1
            events.append(event)
        self.corrected_tracks = events
        self.trajectory_meta = {
            "enabled": True, "axis": (axis * direction).astype(float).tolist(), "center": center.astype(float).tolist(),
            "direction": "toward_lower_area_end", "checkpoints": checkpoints.astype(float).tolist(),
            "low_end_area": low_area, "high_end_area": high_area,
            "stuck_events": len([event for event in events if event.get("stuck")]),
        }

    def _trajectory_detections(self) -> list:
        return [{"frame": frame.get("frame", 0), "index": index, "prop": prop,
                 "source_id": prop.get("track_id", prop.get("droplet_id", index))}
                for frame in self.all_detections for index, prop in enumerate(frame.get("properties", []))]

    @staticmethod
    def _group_detections_by_source(detections: list) -> dict:
        grouped: dict = {}
        for det in detections:
            grouped.setdefault(det["source_id"], []).append(det)
        return grouped

    # -- results ----------------------------------------------------------------------------------

    def _fps(self) -> float:
        return float(self.config.get("analysis", {}).get("fps") or (self.video_reader.fps if self.video_reader else 0) or 1)

    def _track_summaries(self) -> list:
        min_count = int(self.config.get("tracking", {}).get("min_count", 3))
        fps, scale = self._fps(), float(self.config.get("analysis", {}).get("microns_per_pixel", 1.0))
        if self.corrected_tracks:
            sources = [(e["droplet_id"], e["frames"], e["properties"], e.get("checkpoint_count", 0))
                       for e in self.corrected_tracks if not e.get("stuck") and e.get("droplet_id") is not None]
        else:
            sources = [(t["id"], t["frames"], t["properties"], 0) for t in self.finished_tracks]
        summaries = []
        for droplet_id, frames, props, checkpoint_count in sources:
            if len(frames) < min_count:
                continue
            first, last = props[0], props[-1]
            dx, dy = last["centroid_x"] - first["centroid_x"], last["centroid_y"] - first["centroid_y"]
            duration = (frames[-1] - frames[0]) / fps if frames[-1] > frames[0] else 0.0
            speed_um_s = float(np.hypot(dx, dy)) * scale / duration if duration > 0 else 0.0
            areas = [p["area"] for p in props]
            summaries.append({
                "track_id": droplet_id, "droplet_id": droplet_id,
                "source_track_ids": sorted({p.get("source_track_id") for p in props
                                            if p.get("source_track_id") is not None}),
                "first_frame": frames[0], "last_frame": frames[-1], "frame_count": len(frames),
                "checkpoint_count": checkpoint_count, "duration_s": duration,
                # The median: one frame with the droplet half out of the window must not shift the size.
                "mean_area_px": float(np.median(areas)), "mean_area_um2": float(np.median(areas) * scale * scale),
                "mean_perimeter_um": float(np.median([p["perimeter"] for p in props]) * scale),
                "mean_diameter_um": float(np.median([p["equivalent_diameter"] for p in props]) * scale),
                "length_px": float(np.median([p["bbox_width"] for p in props])),
                "height_px": float(np.median([p["bbox_height"] for p in props])),
                "speed_um_s": speed_um_s, "speed_mm_s": speed_um_s / 1000.0,
                "orientation_deg": float(np.degrees(np.arctan2(dy, dx))),
            })
        return summaries

    @staticmethod
    def _confined_volume_nl(length_um: float, width_um: float, depth_um: float) -> float:
        """Volume (nl) of a droplet confined in a width x depth channel: a capsule, or an ellipsoid when shorter."""
        cross_area = (np.pi / 4.0) * width_um * depth_um
        if length_um >= width_um:
            vol_um3 = cross_area * (length_um - width_um / 3.0)
        else:
            vol_um3 = (np.pi / 6.0) * length_um * width_um * depth_um
        return vol_um3 / 1e6

    def _compute_true_statistics(self, tracks: list) -> tuple:
        """Volume per droplet from the channel's known width and depth, and the rate from mass conservation."""
        system = self.config.get("system", {})
        q_aq = float(system.get("aqueous_flow_ul_min", 0.0) or 0.0)
        chan_w = float(system.get("channel_width_um", 0.0) or 0.0)
        chan_d = float(system.get("channel_depth_um", 0.0) or 0.0)
        if q_aq <= 0 or chan_w <= 0 or chan_d <= 0 or not tracks:
            return {}, {}
        accepted = {t["droplet_id"] for t in tracks if t.get("droplet_id") is not None}
        per_droplet: dict = {}
        centroids = []
        for det in self.all_detections:
            for prop in det["properties"]:
                did = prop.get("droplet_id")
                if did is None or did not in accepted:
                    continue
                centroids.append((prop.get("centroid_x", 0.0), prop.get("centroid_y", 0.0)))
                entry = per_droplet.setdefault(int(did), {"w": [], "h": []})
                entry["w"].append(prop.get("bbox_width", 0.0))
                entry["h"].append(prop.get("bbox_height", 0.0))
        if len(centroids) < 2 or not per_droplet:
            return {}, {}
        pts = np.asarray(centroids, dtype=np.float64)
        _, _, vh = np.linalg.svd(pts - pts.mean(axis=0), full_matrices=False)
        cross_is_x = abs(vh[0][0]) < abs(vh[0][1])
        geom_px = {did: ((np.median(e["h"]), np.median(e["w"])) if cross_is_x else (np.median(e["w"]), np.median(e["h"])))
                   for did, e in per_droplet.items()}
        median_cross_px = float(np.median([cross for _, cross in geom_px.values()]))
        if median_cross_px <= 0:
            return {}, {}
        scale = chan_w / median_cross_px
        geometry, volumes = {}, []
        for did, (flow_px, cross_px) in geom_px.items():
            vol_nl = self._confined_volume_nl(flow_px * scale, cross_px * scale, chan_d)
            geometry[did] = {"volume_nl": vol_nl, "length_um": flow_px * scale, "width_um": cross_px * scale}
            volumes.append(vol_nl)
        median_volume_nl = float(np.median(volumes))
        f_true = q_aq * 1000.0 / 60.0 / median_volume_nl if median_volume_nl > 0 else 0.0
        t_analyzed = self.frame_count / self._fps()
        return ({"droplet_volume_nl": median_volume_nl, "true_frequency_hz": f_true,
                 "true_count": f_true * t_analyzed, "scale_um_px": scale}, geometry)

    def _get_results(self, elapsed: float) -> dict:
        tracks = self._track_summaries()
        fps = self._fps()
        scale = float(self.config.get("analysis", {}).get("microns_per_pixel", 1.0))
        total_detections = sum(d["count"] for d in self.all_detections)
        diameters = [t["mean_diameter_um"] for t in tracks]
        mean_diameter_um = float(np.mean(diameters)) if diameters else 0.0
        std_diameter_um = float(np.std(diameters)) if diameters else 0.0
        generation = self.tracker.generation(fps) if self.tracker else {}
        if generation.get("rate_hz"):
            frequency = float(generation["rate_hz"])
        elif tracks:
            first = min(t["first_frame"] for t in tracks)
            last = max(t["first_frame"] for t in tracks)
            frequency = (len(tracks) - 1) * fps / (last - first) if last > first else 0.0
        else:
            frequency = 0.0
        speed_px = self.tracker.speed() if self.tracker else 0.0
        true_stats, droplet_geometry = self._compute_true_statistics(tracks)
        return {
            "total_droplets": len(tracks),
            "total_detections": total_detections,
            "frames_processed": self.frame_count,
            "avg_droplets_per_frame": total_detections / self.frame_count if self.frame_count else 0.0,
            "mean_diameter_um": mean_diameter_um,
            "std_diameter_um": std_diameter_um,
            "cv_diameter": std_diameter_um / mean_diameter_um if mean_diameter_um > 0 else 0.0,
            "mean_speed_mm_s": speed_px * scale * fps / 1000.0,
            "speed_px_frame": speed_px,
            "frequency_hz": frequency,
            "generation": generation,
            "processing_time": elapsed,
            "fps_achieved": self.frame_count / elapsed if elapsed > 0 else 0.0,
            "frame_data": self.all_detections,
            "stored_frames": self.stored_frame_data,
            "analysis": self.config.get("analysis", {}),
            "detection": dict(self.detection),
            "threshold": self.threshold_value,
            "noise_sigma": self.noise_sigma,
            "tracks": tracks,
            "trajectory": self.trajectory_meta,
            "true_stats": true_stats,
            "droplet_geometry": droplet_geometry,
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
        }
