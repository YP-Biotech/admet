"""Saved protocol definitions: data/protocols/<name>.json."""

import json
from pathlib import Path
import re

from admet.workflows.json_protocol import load, normalize
from admet.core.session import write_atomic


def write_json(path, payload):
    write_atomic(path, json.dumps(payload, indent=2, allow_nan=False) + "\n")


class ProtocolStore:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
            raise ValueError("invalid protocol name")
        path = self.root / "protocols" / f"{name}.json"
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("protocol path leaves the data folder")
        return path

    def save(self, document, *, replace=False):
        document = normalize(document)
        path = self._path(document["name"])
        if path.exists() and not replace:
            raise FileExistsError(f"{document['name']} already exists; use replace=true or a new name")
        write_json(path, document)
        return {"path": str(path), "protocol": document}

    def read(self, name):
        path = self._path(name)
        return {"path": str(path), "protocol": load(path)}

    def list(self):
        result = []
        for path in sorted((self.root / "protocols").glob("*.json")):
            try:
                document = load(self._path(path.stem))
                result.append({"name": document["name"], "path": str(path),
                               "steps": len(document["steps"])})
            except (OSError, ValueError) as exc:
                result.append({"name": path.stem, "path": str(path), "error": str(exc)})
        return {"protocols": result}
