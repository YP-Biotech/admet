"""
Sequential batch analysis runner.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .pipeline import DropletPipeline
from .session import bundle_path_for_video, save_analysis


@dataclass
class BatchItem:
    video_path: str
    config: dict
    bundle_dir: str | Path = None
    video_id: str = None
    params: dict = None


class BatchRunner:
    def __init__(
        self,
        items: list[BatchItem],
        item_started: Callable[[str], None] = None,
        item_progress: Callable[[str, int, str], None] = None,
        item_finished: Callable[[str, str, dict], None] = None,
        item_failed: Callable[[str, str], None] = None,
    ):
        self.items = items
        self.item_started = item_started
        self.item_progress = item_progress
        self.item_finished = item_finished
        self.item_failed = item_failed

    def run(self) -> list[dict]:
        outcomes = []
        for item in self.items:
            video_id = item.video_id or Path(item.video_path).stem
            bundle_dir = Path(item.bundle_dir or bundle_path_for_video(item.video_path))
            try:
                if self.item_started:
                    self.item_started(video_id)
                config = self._config_for_item(item, bundle_dir)
                pipeline = DropletPipeline(config)
                pipeline.set_video(item.video_path)
                pipeline.set_progress_callback(
                    lambda pct, msg, vid=video_id: self._progress(vid, pct, msg)
                )
                results = pipeline.run()
                if "error" in results:
                    raise RuntimeError(results["error"])
                meta = save_analysis(
                    bundle_dir,
                    results,
                    config,
                    item.video_path,
                    pipeline.background,
                    video_id=video_id,
                )
                outcome = {
                    "video_id": video_id,
                    "video_path": item.video_path,
                    "bundle_path": str(bundle_dir),
                    "params": item.params or {},
                    "status": "finished",
                    "meta": meta,
                }
                outcomes.append(outcome)
                if self.item_finished:
                    self.item_finished(video_id, str(bundle_dir), outcome)
            except Exception as exc:
                message = str(exc)
                outcome = {
                    "video_id": video_id,
                    "video_path": item.video_path,
                    "bundle_path": str(bundle_dir),
                    "params": item.params or {},
                    "status": "failed",
                    "error": message,
                }
                outcomes.append(outcome)
                if self.item_failed:
                    self.item_failed(video_id, message)
        return outcomes

    def _progress(self, video_id: str, percent: int, message: str):
        if self.item_progress:
            self.item_progress(video_id, percent, message)

    def _config_for_item(self, item: BatchItem, bundle_dir: Path) -> dict:
        config = copy.deepcopy(item.config)
        processing = config.setdefault("processing", {})
        processing["cache_dir"] = str(bundle_dir)
        processing.setdefault("store_preview_frames", False)
        return config
