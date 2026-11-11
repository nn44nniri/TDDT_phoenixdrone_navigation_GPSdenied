from __future__ import annotations
import json
from typing import Any


class KnowledgeContext:
    """Read-only CRP/SARG contract/provenance adapter; never re-implements Layer-3 CRP/SARG selection."""
    def __init__(self, config): self.config=config

    def _root(self,key,derived=None):
        p=self.config.env_path(key)
        if p: return p
        if derived:
            base=self.config.env_path(derived[0]); return (base/derived[1]) if base else None
        return None

    def validate(self)->dict[str,Any]:
        sarg=self._root("TDDT_SARG_ROOT",("TDDT_SARG_PACKAGE_ROOT","generated/SARG")); crp=self._root("TDDT_CRP_ROOT",("TDDT_BOULDER_ROOT","outputs/region_v1_map/CRP"))
        reqs={"sarg":["sarg_records.csv","sarg_lookup_index.csv","metadata.json"],"crp":["crp_cluster_catalog.csv","crp_runtime_statistics.json","metadata.json"]}; out={}
        for name,root in [("sarg",sarg),("crp",crp)]:
            miss=reqs[name] if root is None else [x for x in reqs[name] if not (root/x).exists()]
            meta={}
            if root and (root/"metadata.json").exists():
                try:
                    raw=json.loads((root/"metadata.json").read_text(encoding="utf-8")); meta={k:raw.get(k) for k in ("schema","record_count","cluster_count","version","dataset_version") if k in raw}
                except Exception as e: meta={"read_error":repr(e)}
            if name=="crp" and root and (root/"crp_runtime_statistics.json").exists():
                try:
                    rs=json.loads((root/"crp_runtime_statistics.json").read_text(encoding="utf-8")); meta["runtime_schema"]=rs.get("schema"); meta["cluster_count"]=rs.get("cluster_count",meta.get("cluster_count"))
                except Exception: pass
            out[name]={"root":str(root) if root else None,"ok":not miss,"missing":miss,"contract_metadata":meta,"usage":"validation/provenance only; Layer-3 guidance remains authoritative"}
        out["ok"]=all(v["ok"] for k,v in out.items() if isinstance(v,dict))
        return out

    def snapshot_prior(self,snapshot)->dict[str,Any]:
        g=snapshot.guidance or {}; crp=g.get("crp_match") or {}; sarg=g.get("sarg_reference") or {}
        tep=g.get("tep_reference") or g.get("tep_match") or (snapshot.context or {}).get("tep_reference") or {}
        return {
            "tep_reference":tep,"tep_source":"Layer2/Layer3 packet evidence only; Layer4 performs no TEP reselection",
            "crp_id":crp.get("crp_id", snapshot.context.get("crp_assignment_id")),"crp_code":crp.get("crp_code"),"crp_quality":crp.get("quality_score"),
            "crp_in_distribution":crp.get("in_distribution"),"sarg_reference":sarg,"sarg_deviation":g.get("sarg_deviation") or {},
            "reference_admissible":g.get("reference_admissible"),"risk_signal":g.get("risk_signal",0.0),"optimizer_delta":g.get("optimizer_delta") or {},
            "constraint_delta":g.get("constraint_delta") or {},"transient_coupling_delta":g.get("transient_coupling_delta") or {},
            "source":"TEP via Layer2/Layer3 evidence; Layer3 guidance first for CRP/SARG; direct SARG/CRP access restricted to contract validation/provenance",
        }
