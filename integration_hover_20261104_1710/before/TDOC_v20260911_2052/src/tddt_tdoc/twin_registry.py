from __future__ import annotations
import json
from pathlib import Path
from typing import Any,Mapping
from .utils import sha256_obj,utc_now,jsonable

class TwinRegistry:
    """Runtime Twin registry implementing TDOC section 3 from observed Layer-2 packets."""
    def __init__(self,path:Path): self.path=path; path.parent.mkdir(parents=True,exist_ok=True); self.records={}; self._load()
    def _load(self):
        if self.path.exists():
            try:self.records=json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:self.records={}
    def observe(self,packets:Mapping[str,Mapping[str,Any]])->dict[str,Any]:
        details={}; all_ok=True
        for key,p0 in packets.items():
            p=dict(p0); tid=str(p.get("twin_id") or key); prov=dict(p.get("provenance") or {}); state=dict(p.get("state") or {})
            q=p.get("quality") or {}; v=p.get("validity") or {}; r=p.get("reliability") or {}
            rec={
              "id":tid,"type":prov.get("model") or prov.get("formalism") or tid,"layer":int(p.get("layer",2)),
              "input_schema":"external_contract","output_schema":sorted(state),"uncertainty_schema":sorted((p.get("uncertainty") or {}).keys()) if isinstance(p.get("uncertainty"),Mapping) else [],
              "clock":{"timestamp":p.get("timestamp")},"version":prov.get("model_version") or prov.get("tep_version") or prov.get("version") or "declared-by-external-package",
              "health":{"quality":q.get("score"),"validity":v.get("score"),"reliability":r.get("posterior",r.get("prior"))},"provenance":prov,
              "last_seen":p.get("timestamp"),"observed_at":utc_now(),"packet_hash":sha256_obj(p)
            }
            ok=bool(tid and p.get("timestamp") is not None and isinstance(p.get("provenance"),Mapping) and isinstance(p.get("state"),Mapping))
            rec["accepted"]=ok; self.records[tid]=jsonable(rec); details[tid]={"ok":ok,"record":rec}; all_ok &= ok
        self.path.write_text(json.dumps(self.records,indent=2,ensure_ascii=False),encoding="utf-8")
        return {"ok":all_ok,"details":details,"count":len(self.records)}
    def snapshot(self): return jsonable(self.records)
