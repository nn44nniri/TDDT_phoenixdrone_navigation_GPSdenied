from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import numpy as np

from .config import Layer3Config
from .models import SharedTransDomainState
from .utils import safe_float


@dataclass
class Observation:
    twin_id: str
    key: str
    dimensions: list[str]
    z: np.ndarray
    covariance: np.ndarray
    P: np.ndarray | None = None


class ConstraintRegistry:
    """Registry of CoC constraints Psi_m.

    YAML/config constraints may be linear (rows/coefficient maps) or built-ins.
    Programmatic callers may register arbitrary residual functions for nonlinear
    constraints; optional Jacobians are used when provided.
    """
    def __init__(self):
        self._custom: dict[str, tuple[Callable[..., np.ndarray], Callable[..., np.ndarray] | None]] = {}

    def register(self, name: str, residual: Callable[..., Any], jacobian: Callable[..., Any] | None = None) -> None:
        self._custom[str(name)] = (lambda c,std: np.atleast_1d(np.asarray(residual(c,std),dtype=float)),
                                   None if jacobian is None else lambda c,std: np.atleast_2d(np.asarray(jacobian(c,std),dtype=float)))

    @staticmethod
    def _row(coefficients: Mapping[str,Any], dims: list[str], rhs: float=0.0) -> tuple[np.ndarray,float]:
        idx={d:i for i,d in enumerate(dims)}; a=np.zeros(len(dims),dtype=float)
        for d,v in coefficients.items():
            if d in idx: a[idx[d]]=float(v)
        return a,float(rhs)

    def compile(self, specs: list[Mapping[str,Any]], dims: list[str], std: SharedTransDomainState) -> tuple[list[dict[str,Any]],list[dict[str,Any]]]:
        linear=[]; nonlinear=[]
        for spec0 in specs:
            spec=dict(spec0); name=str(spec.get("name") or spec.get("type") or "constraint")
            ctype=str(spec.get("type","linear")); lam=float(spec.get("lambda",1.0)); hard=bool(spec.get("hard",False))
            edges=list(spec.get("edges") or [])
            rows=[]
            if ctype=="builtin" and name=="velocity_triangle_ned":
                triples=(("Vg_N","Va_N","W_N"),("Vg_E","Va_E","W_E"),("Vg_D","Va_D","W_D"))
                for vg,va,w in triples:
                    if all(d in dims for d in (vg,va,w)):
                        rows.append(self._row({vg:1.,va:-1.,w:-1.},dims,0.0))
            elif ctype in {"linear","linear_rows"}:
                raw_rows=spec.get("rows") or [{"coefficients":spec.get("coefficients") or {},"rhs":spec.get("rhs",0.0)}]
                for row in raw_rows:
                    rows.append(self._row(dict(row.get("coefficients") or {}),dims,float(row.get("rhs",0.0))))
            elif name in self._custom:
                fn,jac=self._custom[name]
                nonlinear.append({"name":name,"lambda":lam,"hard":hard,"residual":fn,"jacobian":jac,"edges":edges})
                continue
            else:
                continue
            if rows:
                A=np.vstack([r[0] for r in rows]); b=np.asarray([r[1] for r in rows],dtype=float)
                linear.append({"name":name,"lambda":lam,"hard":hard,"A":A,"b":b,"edges":edges})
        return linear,nonlinear


class ConsistencyManager:
    """Registry-driven uncertainty-aware N-Twin Center of Consistency.

    Observations use arbitrary P_i selection/projection matrices into a shared
    dimension vector. Linear and registered Psi_m constraints are supported.
    The legacy velocity triangle is just the default configured constraint.
    """
    def __init__(self, config: Layer3Config, registry: Any | None = None):
        self.config=config; self.registry=registry; self.constraints=ConstraintRegistry()

    @staticmethod
    def _fallback_sigma(rep: Any) -> float:
        return max(0.1,0.1+3.0*float(rep.normalized_uncertainty))

    def _observation_specs(self,twin_id:str,rep:Any)->list[dict[str,Any]]:
        specs=[]
        if self.registry is not None:
            specs.extend(self.registry.coc_observations_for(twin_id))
        if not specs:
            specs.extend(list((self.config.network.coc_observations or {}).get(twin_id) or []))
        extra=(rep.context or {}).get("coc_observations")
        if isinstance(extra,list): specs.extend(dict(x) for x in extra if isinstance(x,Mapping))
        return specs

    def _collect_observations(self,std:SharedTransDomainState)->tuple[list[Observation],list[str],dict[str,Any]]:
        # Two-pass construction lets each Twin provide either a simple selection
        # map (`dimensions`) or an explicit projection P_i into arbitrary shared
        # dimensions (`shared_dimensions` + `P`/`projection`).
        provisional=[]; dims=[]; detail={}
        for twin,rep in std.representations.items():
            if not rep.gate:
                detail[twin]={"observation_count":0,"gate":0,"missing":[]}
                continue
            twin_obs=[]; missing=[]
            for spec0 in self._observation_specs(twin,rep):
                spec=dict(spec0); key=str(spec.get("key") or "")
                value=rep.canonical_state.get(key)
                if not isinstance(value,(list,tuple)):
                    missing.append(key or '<unnamed>'); continue
                try:
                    z=np.asarray(value,dtype=float).reshape(-1)
                    if not np.all(np.isfinite(z)): raise ValueError('non-finite')
                except Exception:
                    missing.append(key or '<unnamed>'); continue
                shared=[str(x) for x in (spec.get("shared_dimensions") or spec.get("dimensions") or [])]
                local=[str(x) for x in (spec.get("dimensions") or [f"{key}[{i}]" for i in range(len(z))])]
                rawP=spec.get("P",spec.get("projection"))
                P_local=None
                if rawP is not None:
                    try:
                        P_local=np.asarray(rawP,dtype=float)
                    except Exception:
                        missing.append(f"{key}:invalid_P"); continue
                    if P_local.ndim!=2 or P_local.shape[0]!=len(z) or P_local.shape[1]!=len(shared):
                        missing.append(f"{key}:P_shape"); continue
                elif len(shared)!=len(z):
                    missing.append(f"{key}:dimension_shape"); continue
                for dname in shared:
                    if dname not in dims: dims.append(dname)
                fallback=self._fallback_sigma(rep); cov=None
                cov_key=spec.get("covariance_key")
                if cov_key and rep.canonical_state.get(cov_key) is not None:
                    try: cov=np.asarray(rep.canonical_state[cov_key],dtype=float)
                    except Exception: cov=None
                if cov is None or cov.shape!=(len(z),len(z)):
                    raw_cov=spec.get("covariance")
                    if raw_cov is not None:
                        try: cov=np.asarray(raw_cov,dtype=float)
                        except Exception: cov=None
                if cov is None or cov.shape!=(len(z),len(z)): cov=np.eye(len(z),dtype=float)*(fallback**2)
                twin_obs.append((key,local,shared,z,cov,P_local))
            provisional.append((twin,rep,twin_obs)); detail[twin]={"observation_count":len(twin_obs),"gate":1,"missing":missing}
        obs=[]; idx={d:i for i,d in enumerate(dims)}
        for twin,rep,twin_obs in provisional:
            for key,local,shared,z,cov,P_local in twin_obs:
                P=np.zeros((len(z),len(dims)),dtype=float)
                if P_local is None:
                    for r,dname in enumerate(shared): P[r,idx[dname]]=1.0
                else:
                    for j,dname in enumerate(shared): P[:,idx[dname]]=P_local[:,j]
                obs.append(Observation(twin,key,shared,z,cov,P))
        return obs,dims,detail

    @staticmethod
    def _numeric_rank(A:np.ndarray,tol:float=1e-9)->int:
        return int(np.linalg.matrix_rank(A,tol=tol)) if A.size else 0

    def evaluate(self,std:SharedTransDomainState,weights:Mapping[str,float],topology:Mapping[str,Any],couplings:Mapping[str,float])->dict[str,Any]:
        observations,dims,obs_detail=self._collect_observations(std)
        if not observations or not dims:
            return {"center":{},"center_vector":[],"center_dimensions":[],"status":"unassessed",
                    "assessed":False,"mismatch":None,"consistency_margin":None,
                    "consistency_region":{"inside":None,"reason":"no_CoC_observations"},
                    "edge_residuals":{},"edge_normalized_residuals":{},"uncertainty_td":{"status":"unavailable"},
                    "provenance":{"formula":"3.2-17a..3.2-20","note":"no registered observations"}}

        d=len(dims); H=np.eye(d)*float(self.config.network.consistency_regularizer); g=np.zeros(d)
        data_terms=[]
        for ob in observations:
            R=ob.covariance+np.eye(len(ob.z))*float(self.config.network.consistency_regularizer)
            Rinv=np.linalg.pinv(R); w=max(float(weights.get(ob.twin_id,0.0)),1e-9)
            H+=w*(ob.P.T@Rinv@ob.P); g+=w*(ob.P.T@Rinv@ob.z)
            data_terms.append((ob,w,Rinv))

        specs=list(self.config.network.coc_constraints or [])
        runtime_specs=(std.constraints or {}).get("coc_constraints") if isinstance(std.constraints,Mapping) else None
        if isinstance(runtime_specs,list): specs.extend(dict(x) for x in runtime_specs if isinstance(x,Mapping))
        linear,nonlinear=self.constraints.compile(specs,dims,std)
        compiled_names={c["name"] for c in linear}|{c["name"] for c in nonlinear}
        requested_names={str(dict(x).get("name") or dict(x).get("type") or "constraint") for x in specs if isinstance(x,Mapping)}
        uncompiled_constraints=sorted(requested_names-compiled_names)
        hard_A=[]; hard_b=[]
        for c in linear:
            if c["hard"]:
                hard_A.append(c["A"]); hard_b.append(c["b"])
            else:
                H+=float(c["lambda"])*(c["A"].T@c["A"]); g+=float(c["lambda"])*(c["A"].T@c["b"])

        if hard_A:
            A=np.vstack(hard_A); b=np.concatenate(hard_b)
            KKT=np.block([[H,A.T],[A,np.zeros((A.shape[0],A.shape[0]))]])
            rhs=np.concatenate([g,b])
            try: sol=np.linalg.solve(KKT,rhs)
            except np.linalg.LinAlgError: sol=np.linalg.pinv(KKT)@rhs
            cvec=sol[:d]; cov=np.linalg.pinv(KKT)[:d,:d]
            constraint_rank=self._numeric_rank(A)
        else:
            try: cvec=np.linalg.solve(H,g)
            except np.linalg.LinAlgError: cvec=np.linalg.pinv(H)@g
            cov=np.linalg.pinv(H); constraint_rank=0

        # One Gauss-Newton refinement for custom nonlinear soft constraints.
        for con in nonlinear:
            if con["hard"]: continue
            r=np.asarray(con["residual"](cvec,std),dtype=float).reshape(-1)
            if con["jacobian"] is not None: J=np.asarray(con["jacobian"](cvec,std),dtype=float)
            else:
                eps=1e-6; J=np.zeros((len(r),d))
                for j in range(d):
                    cp=cvec.copy(); cp[j]+=eps; J[:,j]=(np.asarray(con["residual"](cp,std)).reshape(-1)-r)/eps
            lam=float(con["lambda"]); Hn=H+lam*(J.T@J); gn=H@cvec-lam*(J.T@(r-J@cvec))
            cvec=np.linalg.pinv(Hn)@gn; cov=np.linalg.pinv(Hn)

        mismatch=0.0
        observation_residuals={}
        for ob,w,Rinv in data_terms:
            r=ob.z-ob.P@cvec; term=float(w*(r.T@Rinv@r)); mismatch+=term
            observation_residuals[f"{ob.twin_id}:{ob.key}"]={"norm":float(np.linalg.norm(r)),"term":term,"dimensions":ob.dimensions}
        constraint_residuals={}
        for con in linear:
            r=con["A"]@cvec-con["b"]; term=float(con["lambda"]*np.dot(r,r)); mismatch+=0.0 if con["hard"] else term
            constraint_residuals[con["name"]]={"residual":r.tolist(),"norm":float(np.linalg.norm(r)),"hard":con["hard"],"term":term,"edges":con["edges"]}
        for con in nonlinear:
            r=np.asarray(con["residual"](cvec,std),dtype=float).reshape(-1); term=float(con["lambda"]*np.dot(r,r)); mismatch+=term
            constraint_residuals[con["name"]]={"residual":r.tolist(),"norm":float(np.linalg.norm(r)),"hard":con["hard"],"term":term,"edges":con["edges"]}

        center={"dimensions":{k:float(v) for k,v in zip(dims,cvec)}}
        aliases=(("ground_velocity_ned_m_s",("Vg_N","Vg_E","Vg_D")),("air_velocity_ned_m_s",("Va_N","Va_E","Va_D")),("wind_ned_m_s",("W_N","W_E","W_D")))
        dimmap=center["dimensions"]
        for key,names in aliases:
            if all(n in dimmap for n in names): center[key]=[dimmap[n] for n in names]

        # Network raw velocity residual remains distinct from MESA internal anchors.
        raw_vel=None
        def find_obs(twin,key):
            for ob in observations:
                if ob.twin_id==twin and ob.key==key:return ob.z
            return None
        vg=find_obs("MESA_NAV","ground_velocity_ned_m_s"); va=find_obs("WingMPET","air_velocity_ned_m_s"); wind=find_obs("MicroMetTwin","wind_ned_m_s")
        if vg is not None and va is not None and wind is not None: raw_vel=vg-va-wind

        active=set(topology.get("active_edges") or []); edge_res={}; edge_rho={}; unassessed=[]
        # Explicit runtime edge residuals take precedence.
        explicit=dict((std.constraints or {}).get("edge_residuals") or {}) if isinstance(std.constraints,Mapping) else {}
        velocity_norm=float(np.linalg.norm(raw_vel)) if raw_vel is not None else None
        constraint_edge_map={}
        for name,rec in constraint_residuals.items():
            for e in rec.get("edges") or []: constraint_edge_map.setdefault(e,[]).append(float(rec["norm"]))
        for key in active:
            spec=next((e for e in self.config.network.edges if e.key==key),None)
            if key in explicit: r=abs(float(explicit[key]))
            elif key in constraint_edge_map: r=max(constraint_edge_map[key])
            elif key in {"MicroMetTwin->WingMPET","MicroMetTwin->MESA_NAV","MESA_NAV->WingMPET"} and velocity_norm is not None: r=velocity_norm
            elif key=="WingMPET->MESA_NAV":
                wing=std.representations.get("WingMPET"); raw=wing.raw_state if wing else {}; feasible=None
                for n in ("feasible","flight_ok","vtol_envelope_valid","formula_valid"):
                    if n in raw: feasible=safe_float(raw.get(n),1.0); break
                if feasible is None: unassessed.append(key); continue
                r=max(0.0,1.0-feasible)
            elif key=="MESA_NAV->MicroMetTwin" and "climate_query_error" in std.context: r=abs(float(std.context["climate_query_error"]))
            else: unassessed.append(key); continue
            tol=float(spec.tolerance if spec else 1.0); edge_res[key]=float(r); edge_rho[key]=float(r/max(tol,1e-9))

        # A CoC may be numerically solvable under partial observation, but a
        # missing expected observation or an uncompiled Psi_m must never be
        # reported as a fully assessed/inside consistency region.
        expected=set()
        if self.registry is not None:
            for tid,entry in self.registry.twins.items():
                if entry.get("required"):
                    for spec in self._observation_specs(tid,std.representations.get(tid)) if tid in std.representations else self.registry.coc_observations_for(tid):
                        if isinstance(spec,Mapping) and spec.get("key"): expected.add((tid,str(spec.get("key"))))
        else:
            for tid,arr in (self.config.network.coc_observations or {}).items():
                for spec in arr or []:
                    if isinstance(spec,Mapping) and spec.get("key"): expected.add((str(tid),str(spec.get("key"))))
        observed={(ob.twin_id,ob.key) for ob in observations}
        missing_expected=sorted([f"{t}:{k}" for t,k in expected-observed])
        partial=bool(missing_expected or uncompiled_constraints or unassessed)
        if partial:
            margin=None; inside=None
        elif edge_rho:
            margin=1.0-max(edge_rho.values()); inside=bool(margin>=0.0)
        else:
            margin=None; inside=None

        obs_matrix=np.vstack([ob.P for ob in observations]); info_rank=self._numeric_rank(obs_matrix)
        structural_rows=[ob.P for ob in observations]+[c["A"] for c in linear if c.get("hard")]
        structural_rank=self._numeric_rank(np.vstack(structural_rows)) if structural_rows else 0
        full_rank=self._numeric_rank(H)
        status="partial_solved" if partial or structural_rank<d else "solved"
        return {
            "center":center,"center_vector":cvec.tolist(),"center_dimensions":dims,"status":status,"assessed":True,
            "observation_rank":info_rank,"structural_rank":structural_rank,"system_rank":full_rank,"constraint_rank":constraint_rank,
            "observation_detail":obs_detail,"missing_expected_observations":missing_expected,"uncompiled_constraints":uncompiled_constraints,
            "observation_residuals":observation_residuals,"constraint_residuals":constraint_residuals,
            "network_residual_raw_ned_m_s":None if raw_vel is None else raw_vel.tolist(),
            "network_residual_raw_norm":None if raw_vel is None else float(np.linalg.norm(raw_vel)),
            "network_residual_reconciled_ned_m_s":([dimmap["Vg_N"]-dimmap["Va_N"]-dimmap["W_N"],dimmap["Vg_E"]-dimmap["Va_E"]-dimmap["W_E"],dimmap["Vg_D"]-dimmap["Va_D"]-dimmap["W_D"]] if all(k in dimmap for k in ("Vg_N","Vg_E","Vg_D","Va_N","Va_E","Va_D","W_N","W_E","W_D")) else None),
            "mismatch":float(mismatch),"edge_residuals":edge_res,"edge_normalized_residuals":edge_rho,
            "unassessed_active_edges":sorted(unassessed),"consistency_margin":margin,
            "consistency_region":{"inside":inside,"minimum_margin":margin,"unassessed_edges":sorted(unassessed),
                                  "missing_expected_observations":missing_expected,"uncompiled_constraints":uncompiled_constraints,
                                  "reason":("partial_observation_or_constraint" if partial else "fully_assessed")},
            "uncertainty_td":{"covariance":np.asarray(cov,dtype=float).tolist(),"std":np.sqrt(np.maximum(np.diag(cov),0.)).tolist(),"ordering":dims},
            "epistemic_weights":{k:float(v) for k,v in weights.items()},"couplings":{k:float(v) for k,v in couplings.items()},
            "provenance":{"formula":"3.2-16..3.2-24; P_i + Mahalanobis + ConstraintRegistry(Psi_m)","network_residual_distinct_from_mesa_internal_anchor":True},
        }
