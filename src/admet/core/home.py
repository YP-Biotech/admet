"""Where admet keeps its things on this machine: one home, three rooms.

    ~/.admet/            ADMET_HOME overrides it
      control/rig.json   the rig's settings: calibration, profiles, liquids, optics, acquisition mode
      data/              what the rig records; analyze watches it from the start
      analyze/           what analyze makes: its sources, sets, results and cache

A machine that runs control and analyze needs nothing moved; another machine's data folder is
copied into this one's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENV_HOME = "ADMET_HOME"
RIG_KEYS = ("acquisition_mode", "calibration", "profiles", "liquids", "optics", "camera", "qt_checkup", "export")


class AdmetHome:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    @classmethod
    def resolve(cls, root: str | Path | None = None) -> AdmetHome:
        """The home asked for, else ADMET_HOME, else ~/.admet."""
        return cls(root or os.environ.get(ENV_HOME) or Path.home() / ".admet")

    @property
    def control_dir(self) -> Path:
        return self.root / "control"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def analyze_dir(self) -> Path:
        return self.root / "analyze"

    @property
    def rig_path(self) -> Path:
        return self.control_dir / "rig.json"

    def ensure(self) -> AdmetHome:
        for directory in (self.control_dir, self.data_dir, self.analyze_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def rig(self) -> dict[str, Any]:
        """The rig's settings as kept; empty until something is kept."""
        try:
            loaded = json.loads(self.rig_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def update_rig(self, **changes: Any) -> dict[str, Any]:
        """Keep these settings, whole file rewritten atomically; a value of None removes its key."""
        unknown = sorted(set(changes) - set(RIG_KEYS))
        if unknown:
            raise KeyError(f"not rig settings: {', '.join(unknown)}")
        settings = self.rig()
        for key, value in changes.items():
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value
        self.control_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(settings, indent=2, sort_keys=True, ensure_ascii=False)
        temporary = self.rig_path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.rig_path)
        return settings

    def __repr__(self) -> str:
        return f"AdmetHome({str(self.root)!r})"
