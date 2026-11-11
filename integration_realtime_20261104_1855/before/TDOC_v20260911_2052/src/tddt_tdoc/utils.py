from __future__ import annotations
import dataclasses, hashlib, json, math, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def jsonable(x: Any) -> Any:
    if dataclasses.is_dataclass(x): return jsonable(dataclasses.asdict(x))
    if hasattr(x, "to_dict") and callable(x.to_dict): return jsonable(x.to_dict())
    if isinstance(x, Path): return str(x)
    if isinstance(x, Mapping): return {str(k): jsonable(v) for k,v in x.items()}
    if isinstance(x, (list,tuple,set)): return [jsonable(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x): return None
    return x

def canonical_json(x: Any) -> str:
    return json.dumps(jsonable(x), sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)

def sha256_obj(x: Any) -> str:
    return hashlib.sha256(canonical_json(x).encode("utf-8")).hexdigest()

def sha256_file(path: str|Path) -> str:
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""): h.update(b)
    return h.hexdigest()

def unwrap(value: Any) -> Any:
    return value.get("value") if isinstance(value, Mapping) and "value" in value else value

def unwrap_mapping(data: Mapping[str,Any]|None) -> dict[str,Any]:
    return {str(k):unwrap(v) for k,v in (data or {}).items()}

def deep_get(data: Mapping[str,Any], dotted: str, default: Any=None) -> Any:
    cur: Any=data
    for token in dotted.split("."):
        if not isinstance(cur, Mapping) or token not in cur: return default
        cur=cur[token]
    return cur

def expand_path(raw: str|Path, root: Path|None=None) -> Path:
    p=Path(os.path.expandvars(os.path.expanduser(str(raw))))
    if not p.is_absolute() and root is not None: p=(root/p)
    return p.resolve()
