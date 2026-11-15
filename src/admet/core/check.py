"""What is wrong with a project that would stop it from saving, and how to mend it.

A manifest names its files and its items (samples, runs); an item that names a file the
project no longer has makes every save fail. `check` says so; `mend` takes the dangling
names out and nothing else.
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
    unreadable: str = ""

    @property
    def clean(self) -> bool:
        return not self.dangling and not self.unreadable

    def lines(self) -> list[str]:
        if self.unreadable:
            return [f"{self.manifest}: cannot be read: {self.unreadable}"]
        if self.clean:
            return [f"{self.manifest}: every item names only files the project has."]
        out = []
        for item_id, ids in self.dangling.items():
            out.append(f"item {item_id!r} names {len(ids)} file(s) the project no longer has:")
            out.extend(f"    {file_id}" for file_id in ids)
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
    return CheckReport(manifest, dangling=dangling)


def mend_project(project: str | Path) -> CheckReport:
    """Take the dangling file names out of the items; the manifest as it was is kept as .bak."""
    report = check_project(project)
    if report.clean or report.unreadable:
        return report
    manifest = report.manifest
    text = manifest.read_text(encoding="utf-8")
    data = json.loads(text)
    known = {file.get("id") for file in data.get("files", []) if isinstance(file, dict)}
    for item in data.get("items", []):
        if isinstance(item, dict):
            item["files"] = [file_id for file_id in item.get("files", []) if file_id in known]
    manifest.with_suffix(".json.bak").write_text(text, encoding="utf-8")
    manifest.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return report
