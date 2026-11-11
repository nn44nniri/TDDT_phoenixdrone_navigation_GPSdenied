from __future__ import annotations
import itertools, json
from collections import OrderedDict
from pathlib import Path
from typing import Any
import numpy as np
from .models import Scenario
from .utils import normalize_probabilities


class ScenarioManager:
    """Bounded factorized what-if generator + probability-preserving medoid reduction (E4-14..E4-17)."""
    def __init__(self,config,topology_manager,knowledge,explicit_policy=None):
        self.config=config; self.topology_manager=topology_manager; self.knowledge=knowledge; self.explicit_policy=explicit_policy
        p=config.package_root/config.get("scenario.templates_file","resources/scenario_templates.json")
        self.templates=json.loads(Path(p).read_text(encoding="utf-8"))
        self._reduction_cache=OrderedDict()
        self._reduction_cache_max=max(1,int(self.config.get("scenario.reduction_cache_entries",8)))

    def generate(self,snapshot)->list[Scenario]:
        H=self.config.horizon; modes=self.topology_manager.modes(snapshot)
        prior=self.knowledge.snapshot_prior(snapshot)
        actions=list(self.templates.get("actions",[]) or [{"name":"hold","vector":[0.0]*int(self.config.get("qlpv.action_dim",5)),"probability":1.0}])
        if self.explicit_policy is not None:
            actions.extend(self.explicit_policy.proposals(snapshot))
        disturbances=self.templates.get("disturbances",[]) or [{"name":"nominal","vector":[0.0]*int(self.config.get("qlpv.disturbance_dim",6)),"probability":1.0}]
        degradations=self.templates.get("degradations",[]) or [{"name":"none","twins":{},"probability":1.0}]
        coupling_modes=self.templates.get("coupling_variations",[]) or [{"name":"nominal_coupling","delta":{},"probability":1.0}]
        objective_profiles=self.templates.get("objective_profiles",[]) or [{"name":"default_objectives","weights":{},"probability":1.0}]
        raw=[]; idx=0
        product=itertools.product(modes,actions,disturbances,degradations,coupling_modes,objective_profiles)
        raw_max=int(self.config.get("edge.raw_scenarios_max",2048))
        for mode,a,d,q,cm,op in product:
            if idx>=raw_max: break
            idx+=1
            av=np.asarray(a["vector"],dtype=float); dv=np.asarray(d["vector"],dtype=float)
            action=np.tile(av,(H,1)).tolist(); dist=np.tile(dv,(H,1)).tolist()
            prob=(float(mode.probability)*float(a.get("probability",1))*float(d.get("probability",1))*
                  float(q.get("probability",1))*float(cm.get("probability",1))*float(op.get("probability",1)))
            if prior.get("crp_in_distribution") is False and (q.get("severity",0)>0 or d.get("conservative",False)):
                prob*=float(self.config.get("scenario.ood_probability_boost",1.5))
            constraints={}
            constraints.update(snapshot.constraints or {})
            constraints.update(q.get("constraints") or {})
            constraints.update(cm.get("constraints") or {})
            # Layer-3 guidance may add controlled, non-committed what-if constraints/references.
            gd=(snapshot.guidance or {}).get("constraint_delta") or {}
            if gd: constraints["layer3_guidance_delta"]=gd
            params={
                "action_name":a.get("name"),"disturbance_name":d.get("name"),"degradation_name":q.get("name"),
                "coupling_mode":cm.get("name"),"coupling_delta":dict(cm.get("delta") or {}),
                "objective_profile":op.get("name","balanced"),
                "model_parameters":dict(a.get("parameters") or {}) | dict(d.get("parameters") or {}),
                "knowledge_prior":prior,
            }
            # SARG can enter only through the controlled Layer-3 guidance path as target/constraint context.
            if prior.get("reference_admissible"):
                params["sarg_target"]=(prior.get("sarg_reference") or {}).get("reference") or prior.get("sarg_reference")
            change_step=int(self.config.get("scenario.topology_change_step",1))
            topology_sequence=[]
            for ll in range(H):
                topology_sequence.append(snapshot.topology if (mode.mode_id=="G_nominal" or ll<change_step) else mode.topology)
            raw.append(Scenario(
                scenario_id=f"S{idx:05d}",probability=prob,topology_mode=mode.mode_id,topology=mode.topology,
                action_sequence=action,disturbance_sequence=dist,topology_sequence=topology_sequence,parameters=params,constraints=constraints,
                objectives=dict(op.get("weights") or {}),degradation=dict(q.get("twins") or {}),
                provenance={"generator":"factorized-bounded","mode_event":mode.event,"coupling_mode":cm.get("name"),
                            "objective_profile":op.get("name"),"not_actuator_command":True,"network_commit":False},
            ))
        probs=normalize_probabilities([s.probability for s in raw])
        for s,p in zip(raw,probs): s.probability=float(p)
        return raw

    def uncertainty_pool(self, scenarios:list[Scenario], n:int|None=None)->list[Scenario]:
        """Build a reduced uncertainty-only pool for robust/chance optimization.

        Action and objective-profile alternatives are decisions, not random uncertainty.
        We therefore deduplicate raw scenarios by disturbance/degradation/coupling/topology
        and reduce that set separately before evaluating a shared candidate action sequence.
        """
        uniq={}
        for s in scenarios:
            p=s.parameters or {}
            key=(s.topology_mode,p.get("disturbance_name"),p.get("degradation_name"),p.get("coupling_mode"))
            if key not in uniq:
                uniq[key]=s
            else:
                uniq[key].probability+=float(s.probability)
        pool=list(uniq.values())
        total=sum(max(float(x.probability),0.0) for x in pool) or 1.0
        for x in pool: x.probability=max(float(x.probability),0.0)/total
        return self.reduce(pool,n=n)

    def _feature(self,s:Scenario)->np.ndarray:
        a=np.mean(np.asarray(s.action_sequence,dtype=float),axis=0); d=np.mean(np.asarray(s.disturbance_sequence,dtype=float),axis=0)
        deg=np.asarray(sorted(float(v) if not isinstance(v,dict) else float(v.get("severity",0.0)) for v in s.degradation.values()),dtype=float)
        topo=np.asarray([len(s.topology.get("active_edges",[]))],dtype=float)
        cd=np.asarray(sorted(float(v) for v in (s.parameters.get("coupling_delta") or {}).values()),dtype=float)
        # Objective settings are part of E4-14 scenario semantics and must survive reduction.
        ok=("mission","time","energy","risk","uncertainty","consistency","margin")
        ow=np.asarray([float((s.objectives or {}).get(k,0.0)) for k in ok],dtype=float)
        return np.concatenate([a,d,deg,topo,cd,ow])

    @staticmethod
    def _assignment_cost(X,p,selected):
        dist=np.stack([np.linalg.norm(X-X[j],axis=1) for j in selected],axis=1)
        nearest=np.argmin(dist,axis=1)
        return float(np.sum(p*dist[np.arange(len(X)),nearest])),nearest

    def reduce(self,scenarios:list[Scenario], n:int|None=None)->list[Scenario]:
        n=min(n or self.config.reduced_scenarios,len(scenarios))
        if len(scenarios)<=n:return scenarios
        # v2 codebook/cache realization: exact semantic scenario set -> retained IDs/masses.
        # Cache changes computation only; Scenario objects and output contract stay unchanged.
        key=(n,tuple((s.scenario_id,round(float(s.probability),12),s.topology_mode,
                         (s.parameters or {}).get("disturbance_name"),(s.parameters or {}).get("degradation_name"),
                         (s.parameters or {}).get("coupling_mode"),(s.parameters or {}).get("objective_profile")) for s in scenarios))
        cached=self._reduction_cache.get(key)
        if cached is not None:
            self._reduction_cache.move_to_end(key)
            byid={s.scenario_id:s for s in scenarios}; out=[]
            for sid,mass in cached:
                if sid in byid:
                    byid[sid].probability=float(mass); byid[sid].provenance["scenario_reduction"]={"method":"v2_cached_codebook","retained":n,"source_count":len(scenarios),"probability_mass_reassigned":True}; out.append(byid[sid])
            if len(out)==n: return out
        X=[self._feature(s) for s in scenarios]
        m=max(x.size for x in X); X=np.stack([np.pad(x,(0,m-x.size)) for x in X])
        # Normalize feature dimensions to avoid unit/dimension dominance.
        scale=np.std(X,axis=0); scale=np.where(scale>1e-12,scale,1.0); X=X/scale
        p=normalize_probabilities([s.probability for s in scenarios])
        # Probability-seeded farthest medoids followed by bounded PAM-like local swaps.
        selected=[int(np.argmax(p))]; dist=np.linalg.norm(X-X[selected[0]],axis=1)
        while len(selected)<n:
            score=dist*(0.25+p); score[selected]=-1
            j=int(np.argmax(score)); selected.append(j); dist=np.minimum(dist,np.linalg.norm(X-X[j],axis=1))
        best_cost,_=self._assignment_cost(X,p,selected)
        max_swaps=int(self.config.get("scenario.reduction_swap_iterations",2))
        candidates=np.argsort(-(p+1e-12))[:min(len(scenarios),int(self.config.get("scenario.reduction_swap_pool",64)))]
        for _ in range(max_swaps):
            improved=False
            for pos in range(len(selected)):
                for cand in candidates:
                    cand=int(cand)
                    if cand in selected: continue
                    trial=selected.copy(); trial[pos]=cand
                    cost,_=self._assignment_cost(X,p,trial)
                    if cost < best_cost-1e-12:
                        selected=trial; best_cost=cost; improved=True
            if not improved: break
        _,nearest=self._assignment_cost(X,p,selected)
        reps=[scenarios[i] for i in selected]; masses=np.zeros(n)
        for i,j in enumerate(nearest): masses[int(j)]+=p[i]
        for r,mass in zip(reps,masses):
            r.probability=float(mass)
            r.provenance["scenario_reduction"]={"method":"bounded_probability_weighted_kmedoids","retained":n,
                                                  "source_count":len(scenarios),"objective":best_cost,
                                                  "probability_mass_reassigned":True}
        self._reduction_cache[key]=[(r.scenario_id,float(r.probability)) for r in reps]
        self._reduction_cache.move_to_end(key)
        while len(self._reduction_cache)>self._reduction_cache_max:
            self._reduction_cache.popitem(last=False)
        return reps
