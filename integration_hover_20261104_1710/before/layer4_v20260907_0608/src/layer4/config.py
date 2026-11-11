from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
from typing import Any
import yaml

@dataclass
class Layer4Config:
    path: Path
    data: dict[str,Any]
    package_root: Path

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Layer4Config":
        p=Path(path).resolve(); d=yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(path=p,data=d,package_root=p.parent.parent.resolve())

    def get(self, dotted: str, default: Any=None) -> Any:
        cur: Any=self.data
        for token in dotted.split('.'):
            if isinstance(cur,dict) and token in cur: cur=cur[token]
            else: return default
        return cur

    def env_path(self, key: str, *, allow_missing: bool=True) -> Path | None:
        raw=os.environ.get(key) or self.get(f"paths.{key}")
        if raw in (None,""): return None
        p=Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if not p.is_absolute(): p=(self.package_root/p).resolve()
        if not allow_missing and not p.exists(): raise FileNotFoundError(f"{key}: {p}")
        return p

    @property
    def horizon(self)->int: return min(int(self.get('edge.horizon',6)),int(self.get('edge.horizon_max',12)))
    @property
    def dt(self)->float: return float(self.get('edge.dt_s',1.0))
    @property
    def reduced_scenarios(self)->int: return min(int(self.get('edge.reduced_scenarios',6)),int(self.get('edge.reduced_scenarios_max',12)))
    @property
    def max_topology_modes(self)->int: return int(self.get('edge.max_topology_modes',4))
    @property
    def max_uq_dim(self)->int: return int(self.get('edge.max_uq_dim',9))
    @property
    def top_k_ff(self)->int: return min(int(self.get('verification.top_k',2)),int(self.get('edge.top_k_ff_max',3)))
