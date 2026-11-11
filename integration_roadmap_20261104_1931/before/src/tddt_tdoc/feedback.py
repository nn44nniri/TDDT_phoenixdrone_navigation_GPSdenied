from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping
from .utils import sha256_obj, utc_now
from .storage import GzipChunkStore

class FeedbackRouter:
    """TDOC-owned versioned feedback router with bounded gzip route history."""
    TARGETS=(("feedback_layer2","Layer2"),("feedback_layer31","Layer3.1"),("feedback_layer32","Layer3.2"),("feedback_layer33","Layer3.3"))
    def __init__(self, root:Path|None=None):
        self.root=Path(root or "runtime/feedback/layer7"); self.root.mkdir(parents=True,exist_ok=True)
        self._stores={target:GzipChunkStore(self.root/target.lower().replace('.','_'),max_records=64,max_uncompressed_bytes=524288) for _,target in self.TARGETS}
    def route(self,error:Mapping[str,Any])->dict[str,Any]:
        kinds=set(error.get("types") or [])
        if error.get("domain_error"): kinds.add("domain")
        if error.get("alignment_error"): kinds.add("alignment")
        if error.get("coupling_error"): kinds.add("coupling")
        if error.get("sync_error"): kinds.add("sync")
        routes=[]
        if "domain" in kinds: routes.append("Layer2")
        if "alignment" in kinds: routes.append("Layer3.1")
        if "coupling" in kinds: routes.append("Layer3.2")
        if "sync" in kinds: routes.append("Layer3.3")
        if error.get("decision_error"): routes.append("Layer5")
        return {"routes":routes,"routed":bool(routes),"source":"TDOC FeedbackRouter","update_owner":"Layer7"}
    @staticmethod
    def _run_scope(run_context:Mapping[str,Any]|None)->dict[str,Any]:
        if not isinstance(run_context,Mapping):
            return {}
        return {key:run_context.get(key) for key in
                ("run_id","episode_id","mission_version","cycle_id","phase_id")
                if run_context.get(key) is not None}

    def route_layer7(self, output:Mapping[str,Any], *, transaction_id:str, base_version:str,
                     run_context:Mapping[str,Any]|None=None)->dict[str,Any]:
        run_scope=self._run_scope(run_context)
        routed={}
        for key,target in self.TARGETS:
            packet=dict(output.get(key) or {})
            if not packet: continue
            rec={"schema":"TDOC-L7-versioned-route-v2-gzip-chunked","transaction_id":transaction_id,"source":"Layer7","destination":target,
                 "base_version":str(base_version),"candidate_version":packet.get("nu_new"),"packet":packet,"routed_at":utc_now()}
            if run_scope:
                rec["run_context"]=dict(run_scope)
            rec["route_hash"]=sha256_obj(rec)
            loc=self._stores[target].append(rec)
            routed[target]={"status":"ROUTED","path":str(self._stores[target].latest_path),"route_hash":rec["route_hash"],"candidate_version":packet.get("nu_new"),"storage":loc}
        return {"routed":bool(routed),"destinations":routed,"owner":"TDOC/RuntimeScheduler"}
    def pending_context(self, *, run_context:Mapping[str,Any]|None=None)->dict[str,Any]:
        requested_scope=self._run_scope(run_context)
        scoped=run_context is not None
        out={}
        for _,target in self.TARGETS:
            rec=self._stores[target].latest()
            if not isinstance(rec,dict):
                continue
            if scoped:
                stored_scope=self._run_scope(rec.get("run_context"))
                # Online feedback is transient evidence.  Legacy/unscoped records and
                # evidence from another run or episode must never seed a new flight.
                if not requested_scope.get("run_id") or not requested_scope.get("episode_id"):
                    continue
                if stored_scope.get("run_id") != requested_scope.get("run_id"):
                    continue
                if stored_scope.get("episode_id") != requested_scope.get("episode_id"):
                    continue
            out[target]=rec
        return out
