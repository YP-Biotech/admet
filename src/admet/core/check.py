"""What is wrong with a project that would stop it from saving, and how to mend it.

A manifest names its files and its items (samples, runs); an item that names a file the
project no longer has makes every save fail. A video in records/camera that carries its
own record but is not listed is a recording the project forgot. `check` says so; `mend`
takes the dangling names out and lists the forgotten videos, nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .session import session_path


@dataclass(frozen=True)
class CheckReport:
    manifest: Path
    dangling: dict[str, tuple[str, ...]] = field(default_factory=dict)   # item id -> file ids it names but has not
    unlisted: tuple[str, ...] = ()                                        # tagged videos the manifest lacks
    unreadable: str = ""

    @property
    def clean(self) -> bool:
        return not self.dangling and not self.unlisted and not self.unreadable

    def lines(self) -> list[str]:
        if self.unreadable:
            return [f"{self.manifest}: cannot be read: {self.unreadable}"]
        if self.clean:
            return [f"{self.manifest}: every item names only files the project has, and every recording is listed."]
        out = []
        for item_id, ids in self.dangling.items():
            out.append(f"item {item_id!r} names {len(ids)} file(s) the project no longer has:")
            out.extend(f"    {file_id}" for file_id in ids)
        if self.unlisted:
            out.append(f"{len(self.unlisted)} recording(s) in records/camera carry their record but are not listed:")
            out.extend(f"    {name}" for name in self.unlisted)
        return out


def check_project(project: str | Path) -> CheckReport:
    manifest = session_path(project) / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckReport(manifest, unreadable=str(exc))
    known = {file.get("id") for file in data.get("files", []) if isinstance(file, dict)}
    dangling = {}
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        missing = tuple(file_id for file_id in item.get("files", []) if file_id not in known)
        if missing:
            dangling[str(item.get("id"))] = missing
    return CheckReport(manifest, dangling=dangling, unlisted=_unlisted_recordings(manifest.parent, data))


def _unlisted_recordings(project: Path, data: dict) -> tuple[str, ...]:
    from admet.engines.acquisition.camera.tags import read_recording_tag

    listed = set()
    for file in data.get("files", []):
        path = Path(str(file.get("path", "")))
        listed.add((path if path.is_absolute() else project / path).resolve())
    return tuple(video.name for video in sorted((project / "records" / "camera").glob("*.avi"))
                 if video.resolve() not in listed and read_recording_tag(video))


def mend_project(project: str | Path) -> CheckReport:
    """Take the dangling file names out of the items and list the forgotten recordings.

    The manifest as it was is kept as .bak.
    """
    report = check_project(project)
    if report.clean or report.unreadable:
        return report
    manifest = report.manifest
    text = manifest.read_text(encoding="utf-8")
    manifest.with_suffix(".json.bak").write_text(text, encoding="utf-8")
    if report.dangling:
        data = json.loads(text)
        known = {file.get("id") for file in data.get("files", []) if isinstance(file, dict)}
        for item in data.get("items", []):
            if isinstance(item, dict):
                item["files"] = [file_id for file_id in item.get("files", []) if file_id in known]
        manifest.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    if report.unlisted:
        from .project import ProjectStore

        ProjectStore(manifest.parent).register_tagged_recordings()
    return report
