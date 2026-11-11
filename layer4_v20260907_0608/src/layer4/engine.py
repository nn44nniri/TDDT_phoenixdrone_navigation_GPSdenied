from __future__ import annotations
import copy
import time
import numpy as np
from .snapshot import Layer3SnapshotAdapter
from .knowledge import KnowledgeContext
from .topology import DFATopologyManager
from .scenario import ScenarioManager
from .explicit import ExplicitParetoPolicy
from .trust import BetaTrustPredictor
from .rls import FFRLSCouplingPredictor
from .state import StateCodec
from .qlpv import QLPVModel
from .tube import PeriodicTubeMPC
from .optimizer import Layer4MultiObjectiveOptimizer
from .uq import SquareRootCubatureUQ
from .consistency import WLSConsistency
from .feasibility import FeasibilityEngine
from .objectives import ObjectiveEngine
from .pareto import ParetoEngine
from .robust import RobustScenarioAggregator
from .rom import PODDEIMSurrogate
from .verification import FullFidelityVerifier
from .registry import CandidateRegistry
from .models import Candidate,Layer4Result
from .hashing import sha256_obj
from .budget import evaluate_budget, InvocationMemoryProbe
from .multifidelity import CertifiedMultiFidelityManager
from .native_controller_contract import readiness, needs_native_controller, model_ff_allowed, model_prediction_scope
from .utils import probability01


class Layer4Engine:
    """E4-96 orchestration. Predict/evaluate/optimize/verify only; never actuates or commits W/K/G."""
    @staticmethod
    def _semantic_parameter_payload(config):
        """Return safety/model parameters without deployment-local storage paths.

        TDOC redirects registry JSONL files into a fresh transient workspace for
        every process.  Those paths have no effect on prediction or verification and
        must not invalidate otherwise reusable FF evidence.
        """
        payload=copy.deepcopy(config.data)
        registry=payload.get("registry")
        if isinstance(registry,dict):
            registry.pop("path",None); registry.pop("provenance_path",None)
        verification=payload.get("verification")
        if isinstance(verification,dict):
            # This selects qualification-vs-reuse scheduling; it changes neither
            # the model nor any verification threshold.  Certificates produced by
            # a no-reuse qualification must remain usable by the reuse runtime.
            verification.pop("certificate_reuse_enabled",None)
        scope=model_prediction_scope(config)
        if scope:
            payload["model_prediction_scope"]=scope
        return payload

    def __init__(self,config,*,native_reference_service=None):
        self.config=config
        self.adapter=Layer3SnapshotAdapter(config); self.knowledge=KnowledgeContext(config); self.topology=DFATopologyManager(config); self.explicit=ExplicitParetoPolicy(config); self.scenarios=ScenarioManager(config,self.topology,self.knowledge,self.explicit)
        self.codec=StateCodec(config.package_root/config.get("qlpv.state_schema_file")); self.trust=BetaTrustPredictor(config); self.rls=FFRLSCouplingPredictor(config); self.model=QLPVModel(config,self.codec); self.tube=PeriodicTubeMPC(config,self.model); self.optimizer=Layer4MultiObjectiveOptimizer(config); self.uq=SquareRootCubatureUQ(config,self.codec,self.model); self.coc=WLSConsistency(config,self.codec); self.feas=FeasibilityEngine(config,self.codec); self.objectives=ObjectiveEngine(config,self.codec); self.pareto=ParetoEngine(config); self.robust=RobustScenarioAggregator(config); self.rom=PODDEIMSurrogate(config); self.verifier=FullFidelityVerifier(config,self.codec); self.mf=CertifiedMultiFidelityManager(config); self.registry=CandidateRegistry(config)
        self.model_hash=sha256_obj({"metadata":self.model.meta,"A":self.model.A,"Ba":self.model.Ba,"Bd":self.model.Bd,"b":self.model.b})
        self.param_hash=sha256_obj(self._semantic_parameter_payload(config)); self.solver_hash=sha256_obj({"solver":"bounded_deterministic_spsa_augmented_lagrangian+periodic_RPI+robust_minmax+chance","iterations":config.get("optimization.iterations")})
        self.runtime_action_envelope=None
        # Python orchestration owns this dependency. Snapshot JSON flags cannot
        # install a provider, a certificate issuer, or a permissive callback.
        self.verifier.native_reference_service=native_reference_service
        self.native_execution_service=None

    def verify_native_reference(self,request):
        return self.verifier.verify_native_reference(request)

    def set_runtime_action_envelope(self, envelope):
        """Bind one TDOC-supplied, non-actuating Layer5 planning envelope."""
        self.runtime_action_envelope=copy.deepcopy(envelope) if isinstance(envelope,dict) else None

    def _network_sequences(self,snapshot,scenario):
        # v2 E4-23a/E4-28a: update Beta/RLS once per invocation/scenario;
        # horizon propagation is bounded hold. This removes repeated estimator updates
        # without changing W/K representation or Layer-4 I/O.
        H=self.config.horizon; ts=self.trust.initial(snapshot); ks=self.rls.initial(snapshot)
        g0=(scenario.topology_sequence[0] if scenario.topology_sequence else scenario.topology)
        ts,w,ev=self.trust.step(ts,snapshot,scenario.degradation,step_index=0,dt_s=self.config.dt,return_details=True)
        ks,k,kd=self.rls.step(ks,snapshot,w,g0,scenario=scenario,step_index=0,return_details=True)
        W=[]; K=[]; evidence=[]; kdetails=[]
        for l in range(H):
            g=(scenario.topology_sequence[min(l,len(scenario.topology_sequence)-1)] if scenario.topology_sequence else scenario.topology)
            # Topology still gates active couplings; estimates themselves are not re-fit in horizon.
            active=set(g.get("active_edges",[])); kl={edge:(val if edge in active else 0.0) for edge,val in k.items()}
            W.append(dict(w)); K.append(kl); evidence.append(dict(ev)); kdetails.append(dict(kd))
        return W,K,evidence,kdetails

    def _make_case(self,snapshot,scenario,x0,C0):
        H=self.config.horizon; W,K,trust_details,k_details=self._network_sequences(snapshot,scenario); D=np.asarray(scenario.disturbance_sequence,dtype=float); topo=scenario.topology_sequence if scenario.topology_sequence else scenario.topology
        tube=self.tube.propagate_tube(W,K,topo,H); terminal=self.tube.terminal_certificate(W,K,topo)
        # In the packaged affine qLPV model covariance does not depend on additive U.
        # Precompute it once for bounded optimizer evaluations; final candidate UQ is rerun exactly.
        _,Cs,Sroots=self.uq.rollout(x0,C0,np.zeros((H,self.model.action_dim)),D,W,K,topo,return_roots=True)
        return {"scenario":scenario,"W":W,"K":K,"D":D,"topology":topo,"tube":tube,"terminal":terminal,"Cs":Cs,"Sroots":Sroots,"trust_details":trust_details,"k_details":k_details}

    def _network(self,X,Cs,case):
        sc=case["scenario"]; W=case["W"]; K=case["K"]; tube=case["tube"]; net=[]
        for l in range(len(X)):
            wi=W[min(l,len(W)-1)] if W else {}; ki=K[min(l,len(K)-1)] if K else {}
            gl=(sc.topology_sequence[min(max(l-1,0),len(sc.topology_sequence)-1)] if sc.topology_sequence else sc.topology)
            ce=self.coc.evaluate(X[l],Cs[l],wi,gl)
            net.append({"step":l,"W":wi,"K":ki,"G":gl,"G_mode":sc.topology_mode,"C_star":ce["dimensions"],"J_C":ce["mismatch"],"mu":ce["margin"],"residual":ce["residual"],"edge_residuals":ce.get("edge_residuals",{}),"tube_radius":tube[min(l,len(tube)-1)].tolist(),
                        "trust_evidence":case["trust_details"][min(l,len(case["trust_details"])-1)] if case["trust_details"] else {},"coupling_estimation":case["k_details"][min(l,len(case["k_details"])-1)] if case["k_details"] else {}})
        return net

    def _case_eval(self,U,case,snapshot,weights, *, exact_uq=False, x0=None, C0=None):
        if exact_uq:
            X,Cs,Sroots=self.uq.rollout(x0,C0,U,case["D"],case["W"],case["K"],case["topology"],return_roots=True)
        else:
            X,_=self.model.rollout(x0,U,case["D"],case["W"],case["K"],case["topology"]); Cs=case["Cs"]; Sroots=case["Sroots"]
        net=self._network(X,Cs,case)
        # expose square-root covariance on final/evaluation network records
        for l,n in enumerate(net): n["sqrt_covariance"]=np.asarray(Sroots[min(l,len(Sroots)-1)]).tolist()
        fr=self.feas.evaluate_trajectory(X,net,snapshot,case["scenario"].degradation,actions=U,scenario=case["scenario"],tube=case["tube"],terminal=case["terminal"])
        obj=self.objectives.compute(X,Cs,net,U,snapshot,case["scenario"]); scalar,sw=self.objectives.scalarize(obj,weights)
        return {"X":X,"Cs":Cs,"Sroots":Sroots,"network":net,"feasibility":fr,"objectives":obj,"scalar":scalar,"weights":sw}

    def _physical_action_bounds(self,snapshot,x0,lo,hi,input_meta):
        """Add phase-local bounds required by intrinsic physical state semantics.

        For forward transition, while the current scalar airspeed is below the
        existing terminal transition target, a negative ``delta_airspeed_m_s``
        hypothesis is incompatible with the monotonic acceleration corridor.  This
        narrows the hypothetical Layer-4 action set only; it is not an actuator
        command and does not weaken Full-Fidelity verification.
        """
        lo=np.asarray(lo,dtype=float).copy(); hi=np.asarray(hi,dtype=float).copy()
        meta=dict(input_meta or {})
        ctx=snapshot.context or {}
        phase=str(ctx.get("flight_mode") or ctx.get("stage") or ctx.get("stage_id") or "cruise").lower()
        order=list(self.model.meta.get("action_order") or [])
        try: aidx=order.index("delta_airspeed_m_s")
        except ValueError: aidx=None
        sidx=self.codec.index.get("airspeed_m_s")
        current=float(x0[sidx]) if sidx is not None and sidx < len(x0) and np.isfinite(x0[sidx]) else None
        lim=self.feas.vtol.limits(phase)
        terminal=float(lim.get("terminal_min_airspeed_m_s",lim.get("min_airspeed_m_s",0.0)) or 0.0)
        applied=False
        if phase=="transition_forward" and aidx is not None and current is not None and current < terminal-1e-12:
            lo[:,aidx]=np.maximum(lo[:,aidx],0.0)
            applied=True
        thrust_constraint={"applied":False}
        vertical_model=getattr(self.model,"vertical_collective_model",None)
        if phase in ("vertical_takeoff","hover") and isinstance(vertical_model,dict):
            try:
                tidx=list(self.model.meta.get("action_order") or []).index("delta_thrust_norm")
                equilibrium=float(vertical_model["hover_equilibrium_motor_norm"])
                # The qLPV action is a local delta about hover equilibrium.  Restrict
                # it to the physical normalized-motor domain before optimization;
                # commissioning and WingMPET apply their narrower gates downstream.
                lo[:,tidx]=np.maximum(lo[:,tidx],-equilibrium)
                hi[:,tidx]=np.minimum(hi[:,tidx],1.0-equilibrium)
                thrust_constraint={"applied":True,"action":"delta_thrust_norm",
                    "phase":phase,
                    "hover_equilibrium_motor_norm":equilibrium,
                    "resulting_motor_domain":[0.0,1.0],
                    "source":vertical_model.get("source"),"not_actuator_command":True,
                    "full_fidelity_still_required":True}
            except (ValueError,TypeError,KeyError):
                thrust_constraint={"applied":False,"reason":"vertical_collective_model_invalid"}
        runtime_constraint={"applied":False,"reason":"not_required_for_phase"}
        if phase in ("vertical_takeoff","hover"):
            contract=getattr(self,"runtime_action_envelope",None)
            if not (isinstance(contract,dict)
                    and contract.get("schema")=="Layer5-to-Layer4-commissioned-action-envelope-v1"
                    and contract.get("ok") is True and contract.get("applied") is True
                    and str(contract.get("phase") or "")==phase
                    and contract.get("action")=="delta_thrust_norm"
                    and contract.get("not_actuator_command") is True
                    and contract.get("actuation_authority") is False
                    and contract.get("layer4_full_fidelity_required") is True
                    and contract.get("layer5_final_required") is True
                    and contract.get("layer6_executor_gate_required") is True
                    and contract.get("ardupilot_mixer_authoritative") is True
                    and contract.get("direct_jsbsim_actuation") is False):
                # ONLINE TDOC explicitly supplies this contract.  If it is absent
                # in standalone/offline use, preserve the source-domain behavior;
                # if supplied but invalid, make the intersection empty fail-closed.
                if isinstance(contract,dict):
                    lo[0,0]=1.0; hi[0,0]=0.0
                    runtime_constraint={"applied":False,"reason":"invalid_layer5_action_envelope","contract":contract}
            else:
                try:
                    tidx=list(self.model.meta.get("action_order") or []).index("delta_thrust_norm")
                    bounds=list(contract["per_step_delta_bounds"])
                    if len(bounds)!=lo.shape[0]:
                        raise ValueError("layer5_action_envelope_horizon_mismatch")
                    for step,item in enumerate(bounds):
                        lower=float(item["lower"]); upper=float(item["upper"])
                        if not np.isfinite(lower) or not np.isfinite(upper) or lower>upper:
                            raise ValueError("layer5_action_envelope_bound_invalid")
                        lo[step,tidx]=max(lo[step,tidx],lower)
                        hi[step,tidx]=min(hi[step,tidx],upper)
                    runtime_constraint={"applied":True,"source":"Layer5 commissioned runtime certificate",
                        "contract_sha256":contract.get("contract_sha256"),"phase":phase,
                        "per_step_delta_bounds":bounds,"not_actuator_command":True,
                        "full_fidelity_still_required":True}
                except (KeyError,TypeError,ValueError,IndexError):
                    lo[0,0]=1.0; hi[0,0]=0.0
                    runtime_constraint={"applied":False,"reason":"malformed_layer5_action_envelope","contract":contract}
        empty=bool(np.any(lo>hi))
        meta["empty_intersection"]=bool(meta.get("empty_intersection",False) or empty)
        meta["physical_airspeed_action_constraint"]={
            "phase":phase,"action":"delta_airspeed_m_s","applied":applied,
            "current_airspeed_m_s":current,"terminal_target_m_s":terminal,
            "rule":"delta_airspeed_m_s>=0 while transition_forward entry is below terminal target",
            "not_actuator_command":True,
        }
        meta["physical_hover_collective_action_constraint"]=thrust_constraint
        meta["layer5_commissioned_action_constraint"]=runtime_constraint
        return lo,hi,meta

    def _vertical_reference_seed(self,snapshot,x0,case,lo,hi):
        """Construct a model-derived hover seed without deciding or actuating.

        The generic SPSA seed set can miss a narrow, alternating collective
        sequence when the vehicle enters hover with appreciable vertical speed.
        Use the source-derived qLPV collective Jacobian to invert only the next
        predicted AGL sample, then roll the same model forward and repeat.  The
        result remains a hypothetical Layer-4 candidate: all robust/chance,
        tube, Pareto and Full-Fidelity gates still run unchanged downstream.
        """
        ctx=snapshot.context or {}
        phase=str(ctx.get("flight_mode") or ctx.get("stage") or ctx.get("stage_id") or "cruise").lower()
        vm=getattr(self.model,"vertical_collective_model",None)
        if phase!="hover" or not isinstance(vm,dict):
            return None,{"available":False,"reason":"not_source_modelled_hover","phase":phase}
        try:
            current_agl=float(ctx["altitude_agl_m"])
            target_agl=float(ctx["phase_target_agl_m"])
            tidx=int(vm["action_index"])
            pd=int(self.codec.index["mesa_position_ned_m_2"])
            gain=float(-0.5*self.config.dt*self.config.dt*float(vm["down_accel_sensitivity_m_s2_per_motor_norm"]))
        except (KeyError,TypeError,ValueError,IndexError):
            return None,{"available":False,"reason":"vertical_reference_contract_incomplete","phase":phase}
        if not all(np.isfinite(v) for v in (current_agl,target_agl,gain)) or abs(gain)<=np.finfo(float).eps:
            return None,{"available":False,"reason":"vertical_reference_model_singular","phase":phase}

        ground_msl=-float(x0[pd])-current_agl
        x=np.asarray(x0,dtype=float).copy()
        U=np.zeros((self.config.horizon,self.model.action_dim),dtype=float)
        predicted_agl=[]
        for l in range(self.config.horizon):
            zero=np.zeros(self.model.action_dim,dtype=float)
            topology=self.model._topology_at(case["topology"],l)
            x_zero,_=self.model.step(x,zero,case["D"][l],case["W"][l],case["K"][l],topology)
            zero_agl=-float(x_zero[pd])-ground_msl
            U[l,tidx]=np.clip((target_agl-zero_agl)/gain,lo[l,tidx],hi[l,tidx])
            x,_=self.model.step(x,U[l],case["D"][l],case["W"][l],case["K"][l],topology)
            predicted_agl.append(-float(x[pd])-ground_msl)
        return U,{"available":True,"schema":"Layer4-model-derived-hover-reference-seed-v1",
            "phase":phase,"current_agl_m":current_agl,"target_agl_m":target_agl,
            "collective_to_agl_gain_m_per_motor_norm":gain,
            "predicted_agl_m":predicted_agl,"source":vm.get("source"),
            "not_actuator_command":True,"full_fidelity_still_required":True}

    def _robust_evaluator(self,snapshot,seed,uncertainty_cases,x0,C0):
        probs=np.asarray([max(float(c["scenario"].probability),0.0) for c in uncertainty_cases],dtype=float); probs=probs/(probs.sum() or 1.0)
        weights=seed.objectives or None; alpha=float(self.config.get("feasibility.alpha_risk",0.1))
        mix=float(self.config.get("optimization.robust_worst_case_weight",0.8)); mix=min(max(mix,0.0),1.0)
        pc=float(self.config.get("optimization.constraint_penalty",40.0)); pchance=float(self.config.get("optimization.chance_penalty",80.0)); pt=float(self.config.get("optimization.terminal_penalty",100.0))
        def evaluate(U):
            ev=[self._case_eval(U,c,snapshot,weights,exact_uq=False,x0=x0,C0=C0) for c in uncertainty_cases]
            costs=np.asarray([x["scalar"] for x in ev],dtype=float); v=np.asarray([x["feasibility"].get("constraint_violation_score",0.0) for x in ev],dtype=float)
            hard=np.asarray([0.0 if x["feasibility"]["feasible"] else 1.0 for x in ev],dtype=float)
            expected=float(np.dot(probs,costs)); worst=float(np.max(costs)); robust=float(mix*worst+(1.0-mix)*expected)
            pviol=probability01(np.dot(probs,hard)); chance_excess=max(0.0,pviol-alpha)
            violation=float(np.dot(probs,v)+np.max(v))
            terminal_fail=sum(0 if c["terminal"].get("certified") else 1 for c in uncertainty_cases)
            augmented=robust+pc*violation+pchance*chance_excess*chance_excess+pt*float(terminal_fail)
            worst_i=int(np.argmax(costs)) if len(costs) else -1
            return {"augmented_cost":float(augmented),"robust_cost":worst,"expected_cost":expected,"chance_probability":pviol,"chance_excess":chance_excess,"constraint_violation":violation,"terminal_failures":terminal_fail,
                    "case_costs":costs.tolist(),"case_feasible":[bool(x["feasibility"]["feasible"]) for x in ev],"case_violations":v.tolist(),
                    "case_scenario_ids":[c["scenario"].scenario_id for c in uncertainty_cases],
                    "worst_case_scenario_id":uncertainty_cases[worst_i]["scenario"].scenario_id if worst_i>=0 else None,
                    "worst_case_disturbance":uncertainty_cases[worst_i]["scenario"].parameters.get("disturbance_name") if worst_i>=0 else None,
                    "objective_weights":ev[0]["weights"] if ev else {}}
        return evaluate

    def run(self,data):
        from .native_operational import REQUEST_SCHEMA as NATIVE_EXECUTION_REQUEST, verify_operational
        if isinstance(data,dict) and data.get('schema')==NATIVE_EXECUTION_REQUEST:
            return verify_operational(self,data)
        from .native_provider_contract import REQUEST_SCHEMA
        if isinstance(data,dict) and (data.get('schema')==REQUEST_SCHEMA or
                all(k in data for k in ('pilot_plan','backend','isolation'))):
            return self.verify_native_reference(data)
        memory_probe=InvocationMemoryProbe(self.config.get("edge.memory_sample_interval_ms",10.0))
        started=time.perf_counter(); snapshot=self.adapter.adapt(data); self.model.set_context(snapshot.context); x0=self.codec.encode(snapshot); C0=self.uq.initial_covariance(snapshot)
        raw=self.scenarios.generate(snapshot); scenarios=self.scenarios.reduce(raw); nrob=min(int(self.config.get("optimization.robust_scenarios",4)),int(self.config.get("edge.reduced_scenarios_max",12)))
        uncertainty_scenarios=self.scenarios.uncertainty_pool(raw,n=nrob)
        uncertainty_cases=[self._make_case(snapshot,s,x0,C0) for s in uncertainty_scenarios]
        H=self.config.horizon; lo,hi,input_meta=self.tube.robust_intersection_bounds(uncertainty_cases,H)
        lo,hi,input_meta=self._physical_action_bounds(snapshot,x0,lo,hi,input_meta)
        project=lambda U:self.tube.project(U,lo,hi)
        candidates=[]; scenario_by_candidate={}
        seed_case_cache={id(c["scenario"]):c for c in uncertainty_cases}
        for si,sc in enumerate(scenarios):
            # Candidate's own representative case need not be in uncertainty pool.
            case=seed_case_cache.get(id(sc)) or self._make_case(snapshot,sc,x0,C0)
            U0=np.asarray(sc.action_sequence,dtype=float)
            # Retain old quadratic qLPV solution only as a physically plausible warm start.
            Uw,_,_,warm_meta=self.tube.optimize(x0,U0,case["D"],case["W"],case["K"],case["topology"],return_meta=True)
            evaluator=self._robust_evaluator(snapshot,sc,uncertainty_cases,x0,C0)
            # Do not assume the quadratic qLPV warm-start is admissible.  Compare
            # three bounded deterministic seeds with the *same* robust/chance evaluator
            # and start SPSA from the best feasibility-first seed.  This preserves all
            # hard constraints and prevents an infeasible warm-start from hiding a
            # readily available safe hold/zero move.
            seed_rows=[]
            vertical_seed,vertical_seed_meta=self._vertical_reference_seed(snapshot,x0,case,lo,hi)
            seeds=[("qlpv_warm",Uw),("scenario_seed",U0),("zero_hold",np.zeros_like(U0))]
            if vertical_seed is not None:
                seeds.append(("vertical_reference",vertical_seed))
            for seed_name,seed in seeds:
                Us=project(np.asarray(seed,dtype=float)); ev=evaluator(Us)
                if not any(np.allclose(Us,row[1],rtol=0.0,atol=1e-12) for row in seed_rows):
                    seed_rows.append((seed_name,Us,ev))
            seed_name,Useed,seed_eval=min(seed_rows,key=lambda row:self.optimizer._rank_eval(row[2]))
            U,opt_eval,opt_meta=self.optimizer.optimize(
                Useed,project,evaluator,initial_evaluation=seed_eval)
            opt_meta["seed_selection"]={"selected":seed_name,"seeds":[{"name":n,"chance_probability":float(ev.get("chance_probability",1.0)),"constraint_violation":float(ev.get("constraint_violation",1.0)),"augmented_cost":float(ev.get("augmented_cost",1e30))} for n,_,ev in seed_rows],"feasibility_first":True,"vertical_reference_seed":vertical_seed_meta}
            final=self._case_eval(U,case,snapshot,sc.objectives or None,exact_uq=True,x0=x0,C0=C0)
            fr=final["feasibility"]; obj=final["objectives"]; scalar,scalar_weights=self.objectives.scalarize(obj,sc.objectives or None)
            chance=probability01(opt_eval["chance_probability"]); opt_eval["chance_probability"]=chance; chance_safe=chance<=float(self.config.get("feasibility.alpha_risk",0.1))+1e-12
            terminal_safe=all(c["terminal"].get("certified",False) for c in uncertainty_cases)
            cand=Candidate(candidate_id=f"C{si+1:04d}",scenario_id=sc.scenario_id,snapshot_version=snapshot.version,snapshot_hash=snapshot.hash,probability=float(sc.probability),trajectory=final["X"].tolist(),uncertainty=np.asarray(final["Cs"]).tolist(),network_trajectory=final["network"],actions=U.tolist(),objective_vector=obj,
                           feasible=bool(fr["feasible"] and chance_safe and terminal_safe and not input_meta["empty_intersection"]),violation_probability=chance,scalar_cost=scalar,
                           robust_score=float(opt_eval["robust_cost"]),robust_feasible=bool(chance_safe and terminal_safe and opt_eval["constraint_violation"]<=1e-12 and not input_meta["empty_intersection"]),
                           provenance={"action_name":sc.parameters.get("action_name"),"disturbance_name":sc.parameters.get("disturbance_name"),"degradation_name":sc.parameters.get("degradation_name"),"coupling_mode":sc.parameters.get("coupling_mode"),"topology_mode":sc.topology_mode,"scenario_parameters":sc.parameters,"scenario_constraints":sc.constraints,"objective_weights":scalar_weights,"feasibility":fr,
                                       "tube":{"terminal":case["terminal"],"robust_input_intersection":{"lower":lo.tolist(),"upper":hi.tolist(),**input_meta},"warm_start":warm_meta},"optimization":{**opt_meta,"final_robust_evaluation":opt_eval},
                                       "optimizer_chance_probability":chance,"not_actuator_command":True,"network_commit":False})
            rom=self.rom.screen(final["X"]); cand.rom_valid=rom["valid"]; cand.rom_error=rom["error"]; cand.provenance["rom"]=rom
            candidates.append(cand); scenario_by_candidate[cand.candidate_id]=sc
        pviol=self.feas.chance_probability(candidates)
        fronts=self.pareto.rank(candidates); front=fronts[0] if fronts else []
        robust_summary=self.robust.evaluate(candidates)
        pool=[c for c in front if c.robust_feasible is not False and c.rom_valid is not False]
        if not pool: pool=[c for c in front if c.rom_valid is not False]
        if not pool:
            pool=[c for c in candidates if c.feasible]
            for c in pool:
                if c.rom_valid is False: c.provenance.setdefault("rom",{})["bypass_to_full_fidelity"]=True
        pool=sorted(pool,key=lambda c:(float(c.robust_score if c.robust_score is not None else c.scalar_cost if c.scalar_cost is not None else 1e30),c.candidate_id)); verify_pool=pool[:self.config.top_k_ff]
        verify_ids={c.candidate_id for c in verify_pool}; phase_name=self.mf.phase(snapshot)
        # P0-8: every candidate receives exactly one auditable FF outcome.  Empty
        # ``verification={}`` is forbidden because it cannot distinguish a candidate
        # that was ineligible, budget-pruned, rejected by FF, verified, or failed by
        # exception.  Selective FF remains selective; NOT_ATTEMPTED is an explicit
        # safety outcome, not a request to consume more FF budget.
        front_ids={c.candidate_id for c in front}
        for c in candidates:
            if c.candidate_id in verify_ids:
                continue
            if not c.feasible:
                reason="surrogate_infeasible_not_full_fidelity_attempted"
            elif c.rom_valid is False:
                reason="rom_rejected_not_full_fidelity_attempted"
            elif c.candidate_id not in front_ids:
                reason="not_pareto_selected_for_full_fidelity"
            else:
                reason="top_k_full_fidelity_budget_not_selected"
            c.verification=self.verifier.outcome(
                "NOT_ATTEMPTED",reason,reason_codes=[reason],phase=phase_name,
                verification_origin="UNVERIFIED",selection={"selected":False,"top_k_ff":self.config.top_k_ff},
            )
            c.verified=False
        for c in verify_pool:
            sc=scenario_by_candidate.get(c.candidate_id)
            native=readiness(snapshot,c,self.config)
            if native["native_pilot_execution_requested"] and not model_ff_allowed(snapshot,self.config):
                c.verification=self.verifier.outcome(
                    "NOT_ATTEMPTED",native["reason"],reason_codes=[native["reason"]],
                    verification_origin="UNVERIFIED",phase=phase_name,
                    verification_scope="NATIVE_PILOT_CONTROLLER_REQUIRED_NOT_AVAILABLE",
                    native_control_binding=native,native_controller_certified=False,flight_authorized=False,
                    selection={"selected":True,"top_k_ff":self.config.top_k_ff},
                )
                c.verified=False
                continue
            cert=self.mf.find(c,snapshot,sc,self.model_hash,self.param_hash)
            if cert is not None:
                vr=self.verifier.outcome(
                    "ATTEMPTED_AND_VERIFIED","ff_certificate_reuse",reason_codes=["ff_certificate_reuse"],
                    verified=True,attempted=False,executed=False,verification_origin="FF_CERT_REUSE",
                    certificate_id=cert.get("certificate_id"),certificate_persisted=True,ff_origin=True,phase=phase_name,
                    attempt_kind="CERTIFICATE_REUSE",current_cycle_ff_executed=False,
                    selection={"selected":True,"top_k_ff":self.config.top_k_ff},
                    verification_scope="MODEL_ONLY_LAYER3_FMU_REPLAY",
                    native_controller_certified=False,flight_authorized=False,
                )
            else:
                vr=self.verifier.verify(c,snapshot,sc)
                vr["phase"]=phase_name
                vr.setdefault("selection",{"selected":True,"top_k_ff":self.config.top_k_ff})
                if vr.get("verified"):
                    cert=self.mf.build(c,snapshot,sc,self.model_hash,self.param_hash,vr)
                    if cert is not None:
                        vr["certificate_id"]=cert.get("certificate_id")
                        vr["certificate_persisted"]=bool(cert.get("bank_persisted",True))
                        vr["certificate_issuance_contract"]=cert.get("issuance_contract")
                    else:
                        # A successful current FF replay without a durable certificate
                        # is not an auditable verified candidate. Fail closed.
                        vr=self.verifier.outcome(
                            "ATTEMPTED_AND_REJECTED","ff_certificate_issuance_failed",
                            reason_codes=list(vr.get("reason_codes") or vr.get("verification_reasons") or [])+["ff_certificate_issuance_failed"],
                            verified=False,attempted=True,executed=bool(vr.get("executed")),verification_origin="UNVERIFIED",phase=phase_name,
                            prior_verification=vr,certificate_persisted=False,selection={"selected":True,"top_k_ff":self.config.top_k_ff},
                        )
            c.verification=vr; c.verified=bool(vr.get("verified",False))
        # Do not let budget-pruned/model-only candidates hide a native execution
        # requirement behind vacuous conformance or an archived FF certificate.
        if needs_native_controller(snapshot) and not model_ff_allowed(snapshot,self.config):
            for c in candidates:
                if c.verification.get("native_control_binding") is None:
                    native=readiness(snapshot,c,self.config)
                    previous=c.verification
                    c.verification=self.verifier.outcome(
                        "NOT_ATTEMPTED",native["reason"],reason_codes=[native["reason"]],
                        verification_origin="UNVERIFIED",phase=phase_name,
                        verification_scope="NATIVE_PILOT_CONTROLLER_REQUIRED_NOT_AVAILABLE",
                        native_control_binding=native,native_controller_certified=False,
                        flight_authorized=False,prior_selection_reason=previous.get("reason"),
                    )
                c.verified=False
        scope=model_prediction_scope(self.config)
        if scope:
            for c in candidates:
                c.provenance.update(scope)
                c.verification.update(scope)
        self.registry.register_many(candidates,self.model_hash,self.param_hash,self.solver_hash,snapshot_time=snapshot.reference_time_s)
        elapsed=time.perf_counter()-started; memory_sample=memory_probe.stop(); budget=evaluate_budget(self.config,elapsed,memory_sample)
        budget.update({"raw_scenarios":len(raw),"reduced_scenarios":len(scenarios),"robust_uncertainty_scenarios":len(uncertainty_cases),"horizon":self.config.horizon,"top_k_ff":self.config.top_k_ff,"chance_violation_probability":pviol})
        c_atomic=all(c.snapshot_hash==snapshot.hash and c.snapshot_version==snapshot.version for c in candidates)
        c_coupled=all(all("K" in n and "G" in n and "edge_residuals" in n for n in c.network_trajectory) for c in candidates)
        c_uq=all(len(c.uncertainty)==self.config.horizon+1 and all(np.asarray(n.get("sqrt_covariance")).shape==(self.codec.dim,self.codec.dim) for n in c.network_trajectory) for c in candidates)
        c_network=all(all(k in n for k in ("W","K","G","C_star","J_C","mu")) for c in candidates for n in c.network_trajectory)
        c_chance=all(0<=c.violation_probability<=1 for c in candidates) and all("optimizer_chance_probability" in c.provenance for c in candidates)
        c_rom=all((c.rom_valid in (True,False,None)) and (c.rom_valid is not False or bool((c.provenance.get("rom") or {}).get("bypass_to_full_fidelity")) or not c.verified) for c in candidates)
        c_ff=all((not c.verified) or (c.verification.get("candidate_verified") and c.verification.get("verification_origin") in ("FF_CURRENT","FF_CERT_REUSE")) for c in candidates)
        ff_states={"NOT_ATTEMPTED","ATTEMPTED_AND_REJECTED","ATTEMPTED_AND_VERIFIED","EXCEPTION"}
        c_ff_audit=all(
            isinstance(c.verification,dict)
            and c.verification.get("schema")=="Layer4-FF-verification-outcome-v1"
            and c.verification.get("outcome_status") in ff_states
            and bool(c.verification.get("reason_codes"))
            and (not c.verified or (c.verification.get("outcome_status")=="ATTEMPTED_AND_VERIFIED" and bool(c.verification.get("certificate_id"))))
            for c in candidates
        )
        c_phase=all((not c.verified) or bool(c.verification.get("phase")) for c in candidates)
        c_cert=all((not c.verified) or c.verification.get("verification_origin") in ("FF_CURRENT","FF_CERT_REUSE") for c in candidates)
        c_opt=all(bool((c.provenance.get("optimization") or {}).get("objective_in_loop")) and bool((c.provenance.get("optimization") or {}).get("chance_constraint_in_loop")) and bool((c.provenance.get("optimization") or {}).get("robust_minmax_in_loop")) for c in candidates)
        c_terminal=all(bool(((c.provenance.get("tube") or {}).get("terminal") or {}).get("certified")) for c in candidates)
        conformance={"C_atomic":c_atomic,"C_bounded":bool(budget["all_within_budget"]),"C_coupled":c_coupled,"C_UQ":c_uq,"C_network":c_network,"C_chance":c_chance,"C_Pareto":bool(front) or not any(c.feasible for c in candidates),"C_ROM_valid":c_rom,"C_FF":c_ff,"C_FF_origin":c_ff,"C_FF_audit_complete":c_ff_audit,"C_cert_envelope":c_cert,"C_phase_capability":c_phase,"C_trace":False,"C_no_commit":True,"C_robust":all((c.provenance.get("optimization") or {}).get("robust_minmax_in_loop") for c in candidates),"C_tube":all(bool((c.provenance.get("feasibility") or {}).get("robust_state_tightening")) for c in candidates),"C_optimizer":c_opt,"C_terminal":c_terminal}
        conformance["C_trace"]=all(bool(c.signature) for c in candidates)
        conformance["C_native_pilot_controller_prediction"]=False
        conformance["C_native_pilot_execution_authorized"]=False
        conformance["C_FF_verified_count"]=sum(1 for c in candidates if c.verified)
        conformance["C_FF_positive_evidence_available"]=bool(conformance["C_FF_verified_count"])
        conformance["C_native_pilot_controller_required"]=needs_native_controller(snapshot)
        # Materialize each large candidate once and reuse the immutable mapping in
        # the three result projections.  JSON serialization still emits the exact
        # same public schema.
        candidate_dicts=[c.to_dict() for c in candidates]
        candidate_by_id={c["candidate_id"]:c for c in candidate_dicts}
        result=Layer4Result(snapshot_version=snapshot.version,snapshot_hash=snapshot.hash,pareto_front=[candidate_by_id[c.candidate_id] for c in front],verified_candidates=[candidate_by_id[c.candidate_id] for c in candidates if c.verified],candidates=candidate_dicts,budget=budget,conformance=conformance,robust_summary=robust_summary,
                            provenance={"layer":"4","formalism":"E4-1..E4-97","layer4_details":"TDDT_Dynamic_NTwin_Layer4_details_FA_v2","model_hash":self.model_hash,"parameter_hash":self.param_hash,"solver_hash":self.solver_hash,"knowledge":self.knowledge.snapshot_prior(snapshot),"explicit_pareto":self.explicit.status(),"optimization":"seven-objective robust chance-constrained bounded solver","formalism_v2":{"certified_multifidelity":True,"phase_capability_fail_closed":True,"uq_affine_default":bool(self.config.get("uq.affine_default",True)),"ff_capability_phases":self.mf.capability({self.mf.phase(snapshot):[snapshot.reference_time_s]}),"ff_certificate_reuse_stats":self.mf.reuse_stats()},"actuator_command":False,"network_commit":False,
                                        "native_pilot_controller_prediction_certified":False,"flight_authorized":False,
                                        "verification_scope":"MODEL_ONLY_UNLESS_EXPLICIT_NATIVE_REPLAY_AVAILABLE"})
        self.registry.event({"event":"layer4_run","snapshot_hash":snapshot.hash,"snapshot_time":snapshot.reference_time_s,"elapsed_s":elapsed,"candidate_count":len(candidates),"verified_count":len(result.verified_candidates),"conformance":conformance,"robust_summary":robust_summary})
        return result
