"""Files written whole: the old one survives any failure part-way."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: str | Path, text: str) -> None:
    """Replace a file whole: the old one survives any failure part-way."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
