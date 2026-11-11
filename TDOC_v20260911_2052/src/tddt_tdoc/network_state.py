from __future__ import annotations
import json
from pathlib import Path
from typing import Any,Mapping
from .utils import jsonable,sha256_obj,utc_now

class NetworkStateMirror:
    """TDOC-owned versioned mirror of Layer-3 network state; it does not recompute Layer-3 physics."""
    KEYS=("std","weights","couplings","topology","center_of_consistency","mismatch","consistency_margin","consistency_region","stability","emergence","hidden_loop","guidance","uncertainty_td","constraints","context")
    def __init__(self,path:Path): self.path=path; path.parent.mkdir(parents=True,exist_ok=True); self.current={}
    def update(self,snapshot:Mapping[str,Any])->dict[str,Any]:
        d={k:jsonable(snapshot.get(k)) for k in self.KEYS}; d.update({"policy_version":snapshot.get("policy_version"),"reference_time_s":snapshot.get("reference_time_s"),"snapshot_provenance":jsonable(snapshot.get("provenance") or {}),"mirrored_at":utc_now()}); d["network_state_hash"]=sha256_obj(d)
        self.current=d; self.path.write_text(json.dumps(d,indent=2,ensure_ascii=False),encoding="utf-8"); return d
    def snapshot(self): return jsonable(self.current)
