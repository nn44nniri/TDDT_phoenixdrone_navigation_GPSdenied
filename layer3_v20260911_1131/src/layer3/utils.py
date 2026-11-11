from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np


def clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(value)))


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def mean(values: Iterable[float], default: float = 0.0) -> float:
    vals = [float(v) for v in values if finite(v)]
    return float(sum(vals) / len(vals)) if vals else default


def rms(values: Iterable[float], default: float = 0.0) -> float:
    vals = [float(v) for v in values if finite(v)]
    return math.sqrt(sum(v * v for v in vals) / len(vals)) if vals else default


def norm3(v: Iterable[float] | None) -> float:
    if v is None:
        return 0.0
    a = np.asarray(list(v), dtype=float)
    return float(np.linalg.norm(a))


def jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return jsonable(asdict(obj))
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    return obj


def write_json(path: str | Path, obj: Any, *, indent: int = 2) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(jsonable(obj), ensure_ascii=False, indent=indent, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def append_jsonl(path: str | Path, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(jsonable(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def covariance(samples: list[list[float]]) -> list[list[float]]:
    if not samples:
        return []
    a = np.asarray(samples, dtype=float)
    if a.ndim != 2:
        return []
    if a.shape[0] < 2:
        return np.zeros((a.shape[1], a.shape[1]), dtype=float).tolist()
    return np.cov(a, rowvar=False, ddof=1).tolist()


def recursive_numeric_mean(dicts: list[Mapping[str, Any]]) -> dict[str, Any]:
    keys: set[str] = set()
    for d in dicts:
        keys.update(map(str, d.keys()))
    out: dict[str, Any] = {}
    for key in sorted(keys):
        vals = [d.get(key) for d in dicts if key in d]
        nums = [float(v) for v in vals if finite(v)]
        if nums and len(nums) == len(vals):
            out[key] = float(sum(nums) / len(nums))
        elif vals and all(isinstance(v, Mapping) for v in vals):
            out[key] = recursive_numeric_mean([v for v in vals if isinstance(v, Mapping)])
        elif vals:
            out[key] = vals[-1]
    return out
