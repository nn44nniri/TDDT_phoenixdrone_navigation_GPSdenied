from __future__ import annotations
import hashlib, json, math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import numpy as np

def l2_map(values: Mapping[str, float]) -> float:
    if not values:
        return 0.0
    return float(math.sqrt(sum(float(v) ** 2 for v in values.values())))

def diff_maps(a: Mapping[str, float], b: Mapping[str, float]) -> dict[str, float]:
    keys = sorted(set(a) & set(b))
    return {k: float(a[k]) - float(b[k]) for k in keys}

def clip(x: float, lo: float, hi: float) -> float:
    return min(max(float(x), float(lo)), float(hi))

def project_vector(x: Sequence[float], bounds: Sequence[Sequence[float]]) -> np.ndarray:
    arr = np.asarray(x, dtype=float).copy()
    if len(bounds) != arr.size:
        raise ValueError("bounds length must equal vector size")
    for i, (lo, hi) in enumerate(bounds):
        arr[i] = clip(arr[i], float(lo), float(hi))
    return arr

def stable_softmax(logp: Mapping[str, float]) -> dict[str, float]:
    if not logp:
        return {}
    m = max(logp.values())
    ex = {k: math.exp(v - m) for k, v in logp.items()}
    s = sum(ex.values()) or 1.0
    return {k: v / s for k, v in ex.items()}

def gaussian_logpdf(x: float, mean: float, variance: float) -> float:
    v = max(float(variance), 1e-12)
    return -0.5 * (math.log(2.0 * math.pi * v) + ((float(x) - float(mean)) ** 2) / v)

def sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()

def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def dataclass_hash(obj: Any) -> str:
    """Stable SHA-256 for a dataclass/dict candidate payload."""
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(obj):
            obj = asdict(obj)
    except Exception:
        pass
    return sha256_json(obj)
