from __future__ import annotations
from typing import Any
import numpy as np
from .config import Layer5Config
from .models import Layer5Candidate, Recommendation
from .utils import clip, finite_float

class ObjectiveNormalizer:
    """Build and min-max normalize the formal Layer-5 objective vector (L5-40/L5-41)."""
    KEYS=("mission","time","energy","risk","uncertainty","consistency","margin","control_effort","sensitivity")
    def __init__(self,config:Layer5Config): self.config=config
    def enrich(self,c:Layer5Candidate,sensitivity:float|None=None)->dict[str,float]:
        obj={k:finite_float(v) for k,v in (c.objective_vector or {}).items()}
        net=c.network_trajectory or []
        jc=max([finite_float(n.get("J_C"),0.0) for n in net if isinstance(n,dict)] or [finite_float(obj.get("consistency"),0.0)])
        mu=min([finite_float(n.get("mu"),0.0) for n in net if isinstance(n,dict)] or [0.0])
        target=float(self.config.get("objectives.margin_target",0.0))
        u=finite_float((c.adm.metrics if c.adm else {}).get("mean_cov_trace"),finite_float(obj.get("uncertainty"),0.0))
        du=0.0
        try:
            arr=np.asarray(c.solver.get("delta_u") or [],dtype=float)
            if arr.size: du=float(np.sum(arr*arr))
        except Exception: pass
        out={
            "mission":finite_float(obj.get("mission"),0.0),
            "time":finite_float(obj.get("time"),0.0),
            "energy":finite_float(obj.get("energy"),0.0),
            "risk":finite_float(obj.get("risk"),c.violation_probability),
            "uncertainty":u,
            "consistency":max(jc,finite_float(obj.get("consistency"),jc)),
            "margin":max(0.0,target-mu) if target>0 else finite_float(obj.get("margin"),max(0.0,-mu)),
            "control_effort":du,
            "sensitivity":finite_float(sensitivity,finite_float(obj.get("sensitivity"),0.0)),
        }
        c.objective_vector.update(out)
        return out
    def normalize(self,candidates:list[Layer5Candidate],sensitivities:dict[str,dict[str,float]]|None=None)->dict[str,Any]:
        sens=sensitivities or {}; eps=float(self.config.get("objectives.normalization_epsilon",1e-9))
        raw={c.candidate_id:self.enrich(c,(sens.get(c.candidate_id) or {}).get("q_S")) for c in candidates}
        bounds={}
        for k in self.KEYS:
            vals=[raw[c.candidate_id][k] for c in candidates] or [0.0]
            bounds[k]={"best":min(vals),"worst":max(vals)}
        for c in candidates:
            n={}
            for k in self.KEYS:
                b=bounds[k]; den=b["worst"]-b["best"]
                n[k]=0.0 if abs(den)<=eps else clip((raw[c.candidate_id][k]-b["best"])/(den+eps))
            c.normalized_objectives=n
        return {"keys":list(self.KEYS),"bounds":bounds,"method":"minmax_per_invocation","epsilon":eps}

class SensitivityEngine:
    """Bounded top-K objective-weight sensitivity (formalism L5-55/L5-56)."""
    def __init__(self,config:Layer5Config): self.config=config
    def compute(self,candidates:list[Layer5Candidate],weights:dict[str,float],top_k:int|None=None)->dict[str,dict[str,float]]:
        keys=list(self.config.get("objectives.sensitivity_keys",ObjectiveNormalizer.KEYS)); eps=max(float(self.config.get("objectives.sensitivity_perturbation",.05)),1e-9)
        kmax=min(len(candidates),int(self.config.get("edge.sensitivity_top_k",3) if top_k is None else top_k)); out={}
        for c in candidates[:kmax]:
            obj=c.normalized_objectives or c.objective_vector; base=sum(float(weights.get(k,1.0))*finite_float(obj.get(k),0.0) for k in keys); worst=0.0
            for k in keys:
                delta=eps*max(abs(float(weights.get(k,1.0))),1.0)
                for sign in (-1.0,1.0):
                    wp=dict(weights); wp[k]=max(0.0,float(wp.get(k,1.0))+sign*delta)
                    val=sum(float(wp.get(j,1.0))*finite_float(obj.get(j),0.0) for j in keys)
                    worst=max(worst,abs(val-base)/(abs(delta)+1e-12))
            out[c.candidate_id]={"S":worst,"q_S":clip(worst/(1.0+worst))}
        return out

class LexicographicDecisionEngine:
    """True staged lexicographic selection; no Pareto generation occurs in Layer 5."""
    def __init__(self,config:Layer5Config): self.config=config
    def _levels(self):
        return self.config.get("objectives.lexicographic_levels",[
            ["risk","consistency","margin"], ["mission"], ["time","energy"], ["control_effort","sensitivity"]])
    def _level_score(self,c:Layer5Candidate,level:list[str],weights:dict[str,float])->float:
        obj=c.normalized_objectives or c.objective_vector
        return float(sum(float(weights.get(k,1.0))*finite_float(obj.get(k),1e12) for k in level))
    def _vec(self,c:Layer5Candidate,weights:dict[str,float]): return tuple(self._level_score(c,list(level),weights) for level in self._levels())
    def _scalar(self,c:Layer5Candidate,weights:dict[str,float])->float: return float(sum(self._vec(c,weights)))
    def _tie_score(self,c:Layer5Candidate,sens:dict[str,dict[str,float]])->float:
        tw=self.config.get("objectives.tie_break_weights",{}) or {}; net=c.network_trajectory or []
        qmu=clip(min([finite_float(n.get("mu"),0.0) for n in net] or [0.0])); qff=1.0 if c.verified else 0.0
        qr=clip(c.violation_probability/max(float(self.config.get("admissibility.alpha_risk",.1)),1e-9)); u=finite_float((c.adm.metrics if c.adm else {}).get("mean_cov_trace"),0.0); umax=max(float(self.config.get("admissibility.uncertainty_trace_mean_max",1e6)),1e-9); qu=clip(u/umax)
        try: qdu=clip(float(np.linalg.norm(np.asarray(c.solver.get("delta_u") or [],float)))/(max(len(c.solver.get("delta_u") or []),1)*0.4))
        except Exception: qdu=0.0
        qs=finite_float((sens.get(c.candidate_id) or {}).get("q_S"),0.0)
        return (float(tw.get("margin",1))*qmu+float(tw.get("full_fidelity",1))*qff-float(tw.get("risk",1))*qr-float(tw.get("uncertainty",.5))*qu-float(tw.get("control_effort",.25))*qdu-float(tw.get("sensitivity",.5))*qs)
    def rank(self,candidates:list[Layer5Candidate],weights:dict[str,float]|None=None,sensitivities:dict[str,dict[str,float]]|None=None)->tuple[list[Layer5Candidate],Recommendation]:
        weights=weights or dict(self.config.get("objectives.guidance_weights",{}) or {}); sens=sensitivities or {}; levels=[list(x) for x in self._levels()]
        eps_cfg=self.config.get("objectives.lexicographic_epsilon",{}) or {}; survivors=list(candidates); stages=[]
        for idx,level in enumerate(levels):
            if len(survivors)<=1: break
            vals={c.candidate_id:self._level_score(c,level,weights) for c in survivors}; best=min(vals.values())
            eps=float(eps_cfg.get(str(idx+1),eps_cfg.get(idx+1,self.config.get("objectives.epsilon",1e-6))))
            kept=[c for c in survivors if vals[c.candidate_id]<=best+eps]
            stages.append({"level":idx+1,"objectives":level,"best":best,"epsilon":eps,"input":[c.candidate_id for c in survivors],"kept":[c.candidate_id for c in kept]})
            survivors=kept
        ranked=sorted(candidates,key=lambda c:(self._vec(c,weights),-self._tie_score(c,sens),c.candidate_id))
        if survivors:
            chosen=max(survivors,key=lambda c:(self._tie_score(c,sens),-self._scalar(c,weights),c.candidate_id))
            ranked=[chosen]+[c for c in ranked if c.candidate_id!=chosen.candidate_id]
        scores={c.candidate_id:{"lexicographic_vector":list(self._vec(c,weights)),"objectives":c.objective_vector,"normalized":c.normalized_objectives,"sensitivity":sens.get(c.candidate_id),"tie_score":self._tie_score(c,sens)} for c in ranked}
        rec=Recommendation([c.candidate_id for c in ranked],scores,{"rule":"staged lexicographic selection from Layer-4 candidates; no new Pareto point","lexicographic_stages":stages,"decision_weights":weights},{"candidate_count":len(ranked),"full_fidelity_required":True,"pareto_generation":False})
        return ranked,rec

class ConfidenceEngine:
    def __init__(self,config:Layer5Config):
        self.config=config
        self._lex=LexicographicDecisionEngine(config)
    def _decision_separation(self,ranked:list[Layer5Candidate],weights:dict[str,float]|None=None)->tuple[float,dict[str,Any]]:
        """L5-42/L5-59 separation on the actual lexicographic decision operator.

        Safety-critical objectives are explicitly *not* collapsed into an unweighted
        scalar sum.  Compare the top two candidates at the first priority level that
        discriminates them beyond that level's configured epsilon; then normalize the
        gap on that level.  This keeps q_delta aligned with the operator that actually
        selected c1/c2, and avoids confidence loss caused by cancellation across lower
        priority objectives.
        """
        if not ranked:
            return 0.0,{"method":"lexicographic_first_discriminating_level","reason":"no_candidates"}
        if len(ranked)<2:
            return 1.0,{"method":"lexicographic_first_discriminating_level","reason":"single_candidate","level":None}
        # Use the exact weight map supplied to the selector.  Reloading static
        # guidance weights here can change which level crosses its epsilon and
        # make confidence describe a different decision operator than ranking.
        weights=dict(weights if weights is not None else (self.config.get("objectives.guidance_weights",{}) or {}))
        a=ranked[0]; b=ranked[1]
        va=self._lex._vec(a,weights); vb=self._lex._vec(b,weights)
        eps_cfg=self.config.get("objectives.lexicographic_epsilon",{}) or {}
        levels=[list(x) for x in self._lex._levels()]
        def first_move(c:Layer5Candidate):
            sequences=(
                (c.solver or {}).get("corrected_actions"),
                c.logical_actions,
            )
            for seq in sequences:
                try:
                    arr=np.asarray(seq,dtype=float)
                    if arr.ndim==2 and arr.shape[0]>0 and arr.shape[1]>0 and np.all(np.isfinite(arr[0])):
                        return arr[0]
                except Exception:
                    continue
            return None
        ua,ub=first_move(a),first_move(b)
        action_equivalence=0.0; action_meta={"available":False}
        if (a.verified and b.verified and ua is not None and ub is not None
                and ua.shape==ub.shape and ua.size):
            # Confidence is attached to the committed first MPC move, not to an
            # arbitrary identity among scenario-expanded candidates.  Measure
            # component-wise directional overlap in the normalized pilot domain:
            # equal zero components overlap fully, opposite directions do not,
            # and same-direction magnitudes use their scale-free min/max ratio.
            # No numeric equivalence threshold or actuator authority is invented.
            overlap=[]
            for x,y in zip(ua,ub):
                ax,ay=abs(float(x)),abs(float(y))
                if ax==0.0 and ay==0.0:
                    overlap.append(1.0)
                elif float(x)*float(y)<=0.0:
                    overlap.append(0.0)
                else:
                    overlap.append(min(ax,ay)/max(ax,ay))
            action_equivalence=clip(min(overlap))
            action_meta={"available":True,"method":"minimum_component_directional_overlap",
                         "component_overlap":overlap,"first_move_a":ua.tolist(),"first_move_b":ub.tolist(),
                         "verified_pair_required":True,"only_first_mpc_move":True,
                         "manual_threshold_used":False}
        for idx,(x,y) in enumerate(zip(va,vb),start=1):
            eps=float(eps_cfg.get(str(idx),eps_cfg.get(idx,self.config.get("objectives.epsilon",1e-6))))
            gap=abs(float(y)-float(x))
            if gap>eps:
                sep=clip(gap/(abs(float(y))+abs(float(x))+1e-9))
                return max(sep,action_equivalence),{"method":"lexicographic_first_discriminating_level_plus_first_move_effect_equivalence","level":idx,"objectives":levels[idx-1],"best_level_score":float(x),"runner_up_level_score":float(y),"gap":gap,"epsilon":eps,"objective_separation":sep,"first_move_effect_equivalence":action_equivalence,"first_move_effect":action_meta}
        # The controller executes only the first MPC move.  Scenario expansion can
        # legitimately produce multiple verified candidates with identical decision
        # objectives *and* the same corrected pilot-domain first move.  They are one
        # actionable decision class, not competing control choices.  Treat that case
        # like a single candidate, but only when equivalence is proven in the final
        # corrected/logical command space.  A tied pair with different commands stays
        # fail-closed with zero separation.
        if ua is not None and ub is not None and ua.shape==ub.shape:
            max_abs_delta=float(np.max(np.abs(ua-ub))) if ua.size else 0.0
            if max_abs_delta==0.0:
                return 1.0,{"method":"lexicographic_first_discriminating_level","reason":"decision_equivalent_first_move","levels":len(levels),"action_space":"corrected_or_logical_pilot_domain","max_abs_first_move_delta":max_abs_delta,"manual_threshold_used":False,"only_first_mpc_move":True}
        if action_equivalence>0.0:
            return action_equivalence,{"method":"first_move_effect_equivalence","reason":"decision_effect_equivalent_first_move","levels":len(levels),"first_move_effect_equivalence":action_equivalence,"first_move_effect":action_meta}
        return 0.0,{"method":"lexicographic_first_discriminating_level","reason":"lexicographic_tie","levels":len(levels),"first_move_effect":action_meta}
    def compute(self,ranked:list[Layer5Candidate], selected:Layer5Candidate, sensitivity_penalty:float=0.0,
                weights:dict[str,float]|None=None)->tuple[float,dict[str,float]]:
        w=self.config.get("confidence.weights",{}) or {}
        separation,separation_meta=self._decision_separation(ranked,weights); mu=min([finite_float(n.get("mu"),0.0) for n in selected.network_trajectory] or [0.0]); q_mu=clip(mu)
        q_r=clip(selected.violation_probability/max(float(self.config.get("admissibility.alpha_risk",0.1)),1e-9)); q_ff=1.0 if selected.verified else 0.0
        age=(selected.ci.metrics if selected.ci else {}).get("snapshot_age_s"); maxage=max(float(self.config.get("contract.max_decision_age_s",5.0)),1e-9)
        q_f=1.0 if age is None else clip(1.0-finite_float(age)/maxage)
        u=finite_float((selected.adm.metrics if selected.adm else {}).get("mean_cov_trace"),0.0); umax=max(float(self.config.get("admissibility.uncertainty_trace_mean_max",1e6)),1e-9); q_u=clip(u/umax); sens=clip(sensitivity_penalty)
        wd=float(w.get("separation",.2)); wm=float(w.get("margin",.2)); wff=float(w.get("full_fidelity",.2)); wf=float(w.get("freshness",.15))
        wr=float(w.get("risk",.1)); wu=float(w.get("uncertainty",.1)); ws=float(w.get("sensitivity",.05))
        w_positive=wd+wm+wff+wf
        # Formalism_layer5_v4 L5-58N: normalize the original positive-minus-penalty
        # aggregation by the total positive evidence weight.  This preserves all
        # relative weights and penalty semantics while mapping an ideal evidence
        # vector to rho=1.  Invalid/non-positive normalization is fail-closed.
        if w_positive <= 0.0 or any(x < 0.0 for x in (wd,wm,wff,wf,wr,wu,ws)):
            return 0.0,{"q_delta":separation,"q_delta_meta":separation_meta,"q_mu":q_mu,"q_R":q_r,"q_U":q_u,"q_FF":q_ff,"q_F":q_f,"q_S":sens,"W_positive":w_positive,"normalization_valid":0.0}
        raw=(wd*separation+wm*q_mu+wff*q_ff+wf*q_f-wr*q_r-wu*q_u-ws*sens)
        rho=raw/w_positive
        return clip(rho),{"q_delta":separation,"q_delta_meta":separation_meta,"q_mu":q_mu,"q_R":q_r,"q_U":q_u,"q_FF":q_ff,"q_F":q_f,"q_S":sens,"W_positive":w_positive,"rho_raw":raw,"normalization_valid":1.0}
    def _scalar(self,c:Layer5Candidate)->float:
        obj=c.normalized_objectives or c.objective_vector; return float(sum(finite_float(v) for v in obj.values()))

class OperationalModeManager:
    def __init__(self,config:Layer5Config): self.config=config
    def resolve(self,confidence:float,risk:float,authorized:bool,human_approval:bool,requested:str|None=None,*,learning_mode:str|None=None,simulation_authorized:bool=False)->dict[str,Any]:
        req=(requested or self.config.get("operational.requested_mode","AUTO")).upper()
        lm=str(learning_mode or "").upper()
        risk_human=float(self.config.get("operational.human_required_risk",self.config.get("confidence.auto_risk_max",.05)))
        rho_human=float(self.config.get("operational.human_required_below",self.config.get("confidence.auto_threshold",.8)))
        operational_human_required=bool(req=="SUPERVISED" or risk>risk_human or confidence<rho_human)
        # OFFLINE_SE commits only to the simulated Layer-2 plant. It is deliberately
        # distinct from operational AUTO/SUPERVISED authority: no Layer-6 package,
        # no PT_LIB, no direct PWM, and no claim of operational confidence. The hard
        # candidate/VP/PSF/RTA/governance gates still execute unchanged.
        if lm=="OFFLINE_SE":
            sim_ok=bool(simulation_authorized and authorized and self.config.get("operational.offline_simulation_authorized",True))
            conf_floor=float(self.config.get("confidence.hold_threshold",.15))
            risk_ceiling=float(self.config.get("admissibility.alpha_risk",.1))
            if req=="HOLD": mode="HOLD"; commit=False
            elif req=="ADVISORY": mode="ADVISORY"; commit=False
            elif sim_ok and confidence>=conf_floor and risk<=risk_ceiling:
                mode="OFFLINE_SIMULATION"; commit=True
            elif confidence>=conf_floor:
                mode="ADVISORY"; commit=False
            else:
                mode="HOLD"; commit=False
            return {"mode":mode,"commit_authorized":commit,"requested":req,"human_approval":False,"human_required":False,
                    "operational_human_required":operational_human_required,"system_authorized":authorized,
                    "simulation_authorized":sim_ok,"simulation_only":True,"operational_authority":False,
                    "confidence_semantics":"offline_simulation_reference_not_operational_confidence"}
        # Online/SITL can be configured as AUTONOMOUS_ONLY.  This does not bypass
        # admissibility, full-fidelity verification, PSF, RTA or Governance.  It only
        # removes the human-authority branch for a mission whose declared controller
        # authority is machine-only.  A separate SITL risk ceiling is allowed because
        # simulation evidence is not operational/field evidence.  If AUTO eligibility
        # is not met, fail closed to HOLD instead of silently resolving SUPERVISED.
        authority_profile=str(self.config.get("operational.authority_profile","SHARED")).upper()
        autonomous_only=bool(authority_profile=="AUTONOMOUS_ONLY" and lm=="ONLINE_JSBSIM")
        auto_risk_max=float(self.config.get("confidence.auto_risk_max",.05))
        if autonomous_only:
            auto_risk_max=float(self.config.get("operational.online_simulation_auto_risk_max",
                                                self.config.get("admissibility.alpha_risk",auto_risk_max)))
            human_required=False
        else:
            human_required=operational_human_required
        auto_eligible=bool(confidence>=float(self.config.get("confidence.auto_threshold",.8)) and risk<=auto_risk_max and authorized)
        if req=="HOLD": mode="HOLD"; mode_reason="requested_hold"
        elif req=="ADVISORY": mode="ADVISORY"; mode_reason="requested_advisory"
        elif auto_eligible: mode="AUTO"; mode_reason="auto_eligible"
        elif autonomous_only: mode="HOLD"; mode_reason="no_auto_eligible_candidate"
        elif confidence>=float(self.config.get("confidence.supervised_threshold",.55)): mode="SUPERVISED"; mode_reason="supervised_confidence_band"
        elif confidence>=float(self.config.get("confidence.hold_threshold",.15)): mode="ADVISORY"; mode_reason="advisory_confidence_band"
        else: mode="HOLD"; mode_reason="confidence_below_hold_threshold"
        commit=bool(mode=="AUTO" or (mode=="SUPERVISED" and human_approval));
        if mode in ("ADVISORY","HOLD"): commit=False
        return {"mode":mode,"commit_authorized":commit,"requested":req,"human_approval":human_approval if not autonomous_only else False,
                "human_required":human_required,"system_authorized":authorized,"authority_profile":authority_profile,
                "autonomous_only":autonomous_only,"auto_risk_max_effective":auto_risk_max,"auto_eligible":auto_eligible,
                "mode_reason":mode_reason,"simulation_only":False,"operational_authority":bool(commit)}
