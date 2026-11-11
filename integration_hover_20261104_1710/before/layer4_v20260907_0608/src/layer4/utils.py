from __future__ import annotations
import json, math
from pathlib import Path
from typing import Any, Iterable
import numpy as np


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=json_default)+"\n", encoding="utf-8")


def json_default(x: Any) -> Any:
    if isinstance(x, np.ndarray): return x.tolist()
    if isinstance(x, np.generic): return x.item()
    if hasattr(x, "to_dict"): return x.to_dict()
    raise TypeError(type(x).__name__)


def finite_float(x: Any, default: float = 0.0) -> float:
    try:
        y=float(x)
        return y if math.isfinite(y) else default
    except Exception:
        return default


def get_path(obj: Any, path: str, default: Any=None) -> Any:
    cur=obj
    for token in path.split("."):
        if isinstance(cur, dict) and token in cur: cur=cur[token]
        else: return default
    return cur


def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    cur=obj; parts=path.split(".")
    for token in parts[:-1]: cur=cur.setdefault(token,{})
    cur[parts[-1]]=value


def as_vector(x: Any, length: int | None=None, default: float=0.0) -> np.ndarray:
    if x is None: a=np.array([],dtype=float)
    elif isinstance(x,(list,tuple,np.ndarray)): a=np.asarray(x,dtype=float).reshape(-1)
    else: a=np.asarray([x],dtype=float)
    a=np.where(np.isfinite(a),a,default)
    if length is not None:
        if a.size<length: a=np.pad(a,(0,length-a.size),constant_values=default)
        elif a.size>length: a=a[:length]
    return a


def normalize_probabilities(p: Iterable[float]) -> np.ndarray:
    a=np.maximum(np.asarray(list(p),dtype=float),0.0); s=float(a.sum())
    return a/s if s>0 else np.ones_like(a)/max(len(a),1)


def clip01(x: float) -> float: return min(1.0,max(0.0,float(x)))


def probability01(x: Any, default: float = 1.0) -> float:
    """Return a finite probability in the closed interval [0, 1].

    Probability is a contract value rather than an unconstrained score.  Non-finite
    producer output fails closed to ``default`` (1.0 by default); finite round-off
    excursions such as 1.0000000000000002 are clipped at the producer boundary.
    """
    return clip01(finite_float(x, default))
