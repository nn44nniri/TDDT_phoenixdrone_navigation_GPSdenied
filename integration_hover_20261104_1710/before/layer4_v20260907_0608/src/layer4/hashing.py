from __future__ import annotations
import hashlib, json
from typing import Any
import numpy as np


def _default(x: Any):
    if isinstance(x,np.ndarray): return x.tolist()
    if isinstance(x,np.generic): return x.item()
    if hasattr(x,"to_dict"): return x.to_dict()
    raise TypeError(type(x).__name__)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False,default=_default).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def chain_hash(*parts: Any) -> str:
    h=hashlib.sha256()
    for p in parts:
        b=p.encode() if isinstance(p,str) else canonical_bytes(p)
        h.update(len(b).to_bytes(8,"big")); h.update(b)
    return h.hexdigest()
