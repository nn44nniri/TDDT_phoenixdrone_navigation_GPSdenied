from __future__ import annotations

import json
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from .utils import jsonable


class JSONLStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Any) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(jsonable(record), ensure_ascii=False, sort_keys=True) + "\n")


class SnapshotStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, fast_index: int, snapshot: Any) -> Path:
        path = self.directory / f"snapshot_{fast_index:08d}.json"
        data = jsonable(snapshot)
        if not isinstance(data, dict): raise ValueError("snapshot_must_be_mapping")
        data = dict(data)
        provenance = dict(data.get("provenance") or {})
        provenance.pop("snapshot_payload_sha256", None)
        data["provenance"] = provenance
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        provenance["snapshot_payload_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        fd, tmp = tempfile.mkstemp(prefix=".snapshot_staging_", suffix=".json", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            dirfd = os.open(self.directory, os.O_RDONLY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return path
