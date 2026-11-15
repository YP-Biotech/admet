"""The recording's own record, carried inside its AVI.

An AVI is a RIFF file with an INFO list of text tags. ffmpeg writes a comment into it
(the ICMT tag) and any RIFF reader gets it back, so the whole record of a recording --
which project and step it came from, the camera's settings, how many frames were kept
-- travels with the file. A copied or orphaned video still says what it is; the
project manifest is only an index of these records.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

RECORD_KEY = "admet_recording"          # the comment holds {"admet_recording": {...}}
RECORD_VERSION = 1

# Sensor pixel pitch of the cameras in use, µm; the model name as the camera reports it.
PIXEL_SIZE_UM = {
    "acA640-750um": 4.8,
    "acA640-750uc": 4.8,
    "acA800-510um": 4.8,
    "acA1300-200um": 4.8,
    "acA1920-155um": 5.86,
    "acA2040-120um": 5.5,
}


def pixel_size_um(model: str | None) -> float | None:
    """The camera's pixel pitch from its model name, when the camera is known."""
    if not model:
        return None
    for name, size in PIXEL_SIZE_UM.items():
        if name.lower() in str(model).lower():
            return size
    return None


def ffmpeg_metadata_args(record: dict[str, Any]) -> list[str]:
    """ffmpeg arguments that put the record into the AVI's INFO list."""
    comment = json.dumps({RECORD_KEY: {"version": RECORD_VERSION, **record}}, sort_keys=True, default=str)
    args = ["-metadata", f"comment={comment}"]
    if record.get("recording_id"):
        args += ["-metadata", f"title={record['recording_id']}"]
    return args


def read_recording_tag(path: str | Path) -> dict[str, Any] | None:
    """The record an AVI carries, or None when the file has none (or is not an AVI)."""
    comment = riff_info(path).get("ICMT", "")
    if RECORD_KEY not in comment:
        return None
    try:
        record = json.loads(comment).get(RECORD_KEY)
    except (ValueError, AttributeError):
        return None
    return record if isinstance(record, dict) else None


def riff_info(path: str | Path, *, head_bytes: int = 1 << 20) -> dict[str, str]:
    """The text tags of a RIFF file's INFO list, by their four-letter id.

    The list sits in the header before the frames, so the first megabyte holds it.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(head_bytes)
    except OSError:
        return {}
    if head[:4] != b"RIFF":
        return {}
    tags: dict[str, str] = {}
    position = head.find(b"LIST")
    while 0 <= position <= len(head) - 12:
        size = struct.unpack("<I", head[position + 4:position + 8])[0]
        kind = head[position + 8:position + 12]
        if kind == b"INFO":
            cursor, end = position + 12, min(position + 8 + size, len(head))
            while cursor + 8 <= end:
                tag = head[cursor:cursor + 4].decode("ascii", "replace")
                length = struct.unpack("<I", head[cursor + 4:cursor + 8])[0]
                value = head[cursor + 8:cursor + 8 + length]
                tags[tag] = value.rstrip(b"\0").decode("utf-8", "replace")
                cursor += 8 + length + (length & 1)
            break
        if kind == b"movi":
            break
        position = head.find(b"LIST", position + 8 + size)
    return tags
