from __future__ import annotations
import time, copy
from typing import Any
import numpy as np
from .config import Layer5Config
from .models import Layer5Candidate, GateRecord
from .utils import finite_float

class Layer4ContractAdapter:
    def __init__(self, config: Layer5Config): self.config=config
    @staticmethod
    def _full_fidelity_authority(vr:dict[str,Any])->bool:
        """Accept current FF execution or a fail-closed persisted FF certificate.

        A certificate is evidence of an earlier executed FF verdict, not a claim that
        FF ran in this cycle.  Its lane is intentionally narrow and excludes negative,
        unpersisted, hand-edited, or transition-chain-incomplete records.
        """
        if (vr.get('verification_scope')=='NATIVE_EXACT_REFERENCE_REVIEW' or
                vr.get('verification_origin')=='NATIVE_REFERENCE_CURRENT_CERTIFICATE'):
            # Even an injected executed=True cannot widen a review certificate.
            return False
        if vr.get("verified",vr.get("candidate_verified",False)) is not True:
            return False
        if vr.get("executed") is True:
            # Preserve the established current-FF contract (including explicit
            # test fixtures); the new predicates below apply only to reuse.
            return True
        certified=bool(
            vr.get("verification_origin")=="FF_CERT_REUSE"
            and vr.get("attempt_kind")=="CERTIFICATE_REUSE"
            and vr.get("current_cycle_ff_executed") is False
            and vr.get("certificate_persisted") is True
            and vr.get("ff_origin") is True
            and str(vr.get("certificate_id") or "")
            and vr.get("outcome_status")=="ATTEMPTED_AND_VERIFIED"
            and vr.get("attempted") is False
        )
        if not certified:
            return False
        if str(vr.get("phase") or "") in {"transition_forward","transition_back"}:
            transition=vr.get("transition_ff_evidence") or {}
            return bool(
                transition.get("schema")=="TDOC-Transition-FF-evidence-v1"
                and transition.get("required") is True
                and transition.get("certificate_reuse") is True
                and transition.get("audit_complete") is True
                and transition.get("certificate_id")==vr.get("certificate_id")
            )
        return True

    def candidates(self, data:dict[str,Any])->list[Layer5Candidate]:
        out=[]; top_verified={str(c.get("candidate_id")) for c in data.get("verified_candidates",[]) if isinstance(c,dict)}
        pareto_ids={str(c.get("candidate_id")) for c in (data.get("pareto_front") or []) if isinstance(c,dict) and c.get("candidate_id")}
        for c in data.get("candidates",[]) or []:
            if not isinstance(c,dict): continue
            cid=str(c.get("candidate_id",""))
            if pareto_ids and cid not in pareto_ids: continue
            ver=bool(c.get("candidate_verified",c.get("verified",False))) and cid in top_verified
            vr=c.get("verification") or {}
            # Layer4 can provide either a same-cycle FF verdict or a durable positive
            # certificate.  Both are explicit authorities; a bare verified boolean is not.
            ver=bool(ver and self._full_fidelity_authority(vr))
            is_fixture=bool((c.get("provenance") or {}).get("test_fixture") or (data.get("provenance") or {}).get("test_fixture"))
            if is_fixture and not bool(self.config.get("testing.allow_fixture_verification",False)): ver=False
            out.append(Layer5Candidate(candidate_id=cid,raw=c,snapshot_version=str(c.get("snapshot_version",data.get("snapshot_version",""))),
                snapshot_hash=str(c.get("snapshot_hash",data.get("snapshot_hash",""))),actions=[[float(v) for v in a] for a in c.get("actions",[])],
                objective_vector={k:finite_float(v) for k,v in (c.get("objective_vector") or {}).items()},
                normalized_objectives={k:finite_float(v) for k,v in (c.get("normalized_objectives") or {}).items()},
                network_trajectory=list(c.get("network_trajectory") or []),uncertainty=list(c.get("uncertainty") or c.get("predicted_uncertainty") or []),
                feasible=bool(c.get("feasible",False)),violation_probability=finite_float(c.get("violation_probability",c.get("chance_constraint_risk",1.0)),1.0),
                verified=ver,verification=copy.deepcopy(vr),provenance=copy.deepcopy(c.get("provenance") or {})))
        return out

class IntegrityFreshnessGate:
    def __init__(self, config:Layer5Config): self.config=config
    def evaluate(self,c:Layer5Candidate, layer4:dict[str,Any], now_s:float|None=None)->GateRecord:
        reasons=[]; metrics={}
        if c.snapshot_version!=str(layer4.get("snapshot_version","")): reasons.append("snapshot_version_mismatch")
        if c.snapshot_hash!=str(layer4.get("snapshot_hash","")): reasons.append("snapshot_hash_mismatch")
        # Absolute wall-clock freshness is enforced when an explicit epoch timestamp is available. Layer3 reference_time_s may be mission elapsed.
        ts=(layer4.get("provenance") or {}).get("snapshot_epoch_s") or layer4.get("snapshot_epoch_s")
        if ts is not None:
            age=(now_s if now_s is not None else time.time())-finite_float(ts)
            metrics["snapshot_age_s"]=age
            if age<0 or age>float(self.config.get("contract.max_decision_age_s",5.0)): reasons.append("expired_snapshot")
        else:
            metrics["snapshot_age_s"]=None; metrics["freshness_basis"]="no_absolute_epoch; upstream atomic snapshot boundary only"
            if not bool(self.config.get("contract.allow_missing_absolute_snapshot_time",True)): reasons.append("missing_absolute_snapshot_time")
        return GateRecord(not reasons,reasons,metrics)

class AdmissibilitySupervisor:
    def __init__(self,config:Layer5Config): self.config=config
    def evaluate(self,c:Layer5Candidate,cvar_enabled:bool|None=None)->GateRecord:
        reasons=[]; metrics={}
        if self.config.get("admissibility.require_feasible",True) and not c.feasible: reasons.append("layer4_infeasible")
        if self.config.get("admissibility.require_full_fidelity_verified",True) and not c.verified: reasons.append("not_full_fidelity_verified")
        if c.violation_probability>float(self.config.get("admissibility.alpha_risk",0.1)): reasons.append("chance_risk")
        if bool(self.config.get("admissibility.cvar_enabled",False) if cvar_enabled is None else cvar_enabled):
            cv=None; vr=c.verification or {}
            for key in ("CVaR","cvar","cvar_risk"):
                if key in vr: cv=finite_float(vr.get(key)); break
            if cv is None:
                losses=c.raw.get("tail_losses") or vr.get("tail_losses")
                if isinstance(losses,list) and losses:
                    vals=sorted([finite_float(x) for x in losses],reverse=True)[:int(self.config.get("edge.cvar_samples_max",16))]
                    alpha=0.95; n=max(1,int(round((1-alpha)*len(vals)))); cv=float(np.mean(vals[:n]))
            metrics["CVaR"]=cv
            if cv is None: reasons.append("missing_cvar_evidence")
            elif cv>float(self.config.get("admissibility.cvar_max",1.0)): reasons.append("cvar_limit")
        jc=[finite_float(n.get("J_C"),float("inf")) for n in c.network_trajectory if isinstance(n,dict)]
        mu=[finite_float(n.get("mu"),-float("inf")) for n in c.network_trajectory if isinstance(n,dict)]
        jpeak=max(jc) if jc else float("inf"); mumin=min(mu) if mu else -float("inf")
        metrics.update({"J_C_peak":jpeak,"mu_min":mumin,"P_viol":c.violation_probability})
        if jpeak>float(self.config.get("admissibility.j_c_max",25.0)): reasons.append("consistency_limit")
        if mumin<float(self.config.get("admissibility.mu_min",0.0)): reasons.append("network_margin")
        traces=[]
        for cov in c.uncertainty:
            try:
                a=np.asarray(cov,dtype=float)
                if a.ndim==2: traces.append(float(np.trace(a)))
            except Exception: pass
        umean=float(np.mean(traces)) if traces else 0.0; metrics["mean_cov_trace"]=umean
        if umean>float(self.config.get("admissibility.uncertainty_trace_mean_max",1e6)): reasons.append("uncertainty_limit")
        return GateRecord(not reasons,reasons,metrics)
