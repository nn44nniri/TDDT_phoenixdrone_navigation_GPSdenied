from __future__ import annotations
import json, math, os, time
from collections import Counter
from pathlib import Path
import numpy as np
from .hashing import sha256_obj
from .native_controller_contract import needs_native_controller, model_ff_allowed, model_prediction_scope, MODEL_PREDICTION_SCOPE


class CertifiedMultiFidelityManager:
    """Fail-closed FF-certificate manager for Formalism_layer4_v2 §15.

    v4 certificates separate wall-clock audit time from mission-reference freshness.
    Reuse is phase/model/parameter/environment/scenario-semantics gated and requires
    the requested initial state/action sequence to remain inside the *certified*
    physical envelopes. Runtime identities are provenance only.
    """
    def __init__(self, config):
        self.config=config
        self.path=config.package_root/config.get("verification.certificate_bank_path","runtime/verification/ff_certificates.jsonl")
        self._cache=None
        self._positive_index=None
        self._positive_exact_index=None
        self._negative_index=None
        self._legacy_positive=None
        self._reuse_attempts=0
        self._reuse_hits=0
        self._reuse_misses=Counter()
        # Integrated TDOC installs a live dependency provider. Standalone model-
        # only operation retains its existing model/parameter contract.
        self.model_dependencies_provider=None

    def _model_dependencies_match(self,evidence):
        if self.model_dependencies_provider is None:return True
        try:
            current=self.model_dependencies_provider()
            ok=bool(isinstance(current,dict) and current.get('model_id')
                    and (evidence or {}).get('model_dependencies')==current)
        except Exception:
            ok=False
        if not ok:self._miss('model_dependencies_missing_or_changed')
        return ok

    @staticmethod
    def phase(snapshot):
        c=snapshot.context or {}
        return str(c.get("flight_phase") or c.get("stage") or c.get("stage_id") or c.get("flight_mode") or "unknown").lower()

    def _context_hashes(self,snapshot):
        c=snapshot.context or {}; p=snapshot.provenance or {}
        return {
            "climate_hash": str(c.get("effective_climate_fingerprint") or c.get("climate_hash") or p.get("climate_hash") or ""),
            "map_hash": str(c.get("map_hash") or c.get("terrain_hash") or p.get("map_hash") or ""),
        }

    @staticmethod
    def _scenario_semantics(scenario):
        if scenario is None:
            return {}
        p=dict(getattr(scenario,"parameters",{}) or {})
        return {
            "topology_mode":str(getattr(scenario,"topology_mode","") or ""),
            "topology":getattr(scenario,"topology",{}) or {},
            "topology_sequence":getattr(scenario,"topology_sequence",[]) or [],
            "disturbance_name":p.get("disturbance_name"),
            "disturbance_sequence":getattr(scenario,"disturbance_sequence",[]) or [],
            "degradation_name":p.get("degradation_name"),
            "degradation":getattr(scenario,"degradation",{}) or {},
            "coupling_mode":p.get("coupling_mode"),
            "coupling_delta":p.get("coupling_delta") or {},
            "model_parameters":p.get("model_parameters") or {},
            "constraints":getattr(scenario,"constraints",{}) or {},
        }

    def _scenario_semantics_hash(self,scenario):
        return sha256_obj(self._scenario_semantics(scenario))

    def _load(self):
        if self._cache is not None: return self._cache
        rows=[]
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    r=json.loads(line); rows.append(r)
                except Exception:
                    pass
        self._cache=rows
        # Certificate lookup is on the Layer-4 critical path.  Index immutable
        # identity fields once instead of rescanning the complete durable bank for
        # every top-k candidate.  The lists retain append order so newest evidence
        # is still preferred.
        self._positive_index={}; self._positive_exact_index={}; self._negative_index={}; self._legacy_positive=[]
        for row in rows:
            key=(str(row.get("phase") or ""),str(row.get("model_hash") or ""),
                 str(row.get("parameter_hash") or ""),str(row.get("context_signature") or ""))
            if row.get("origin")=="FF_CURRENT" and row.get("verified") is True:
                if row.get("scenario_semantics_hash"):
                    self._positive_index.setdefault(key,[]).append(row)
                    digest=self._positive_exact_sha256_from_certificate(row)
                    if digest: self._positive_exact_index.setdefault(digest,[]).append(row)
                else:
                    self._legacy_positive.append(row)
            elif (row.get("schema")=="Layer4-FF-rejection-certificate-v2"
                  and row.get("exact_identity_schema") in {
                      "Layer4-semantic-exact-request-v2",
                      "Layer4-semantic-exact-request-v3",
                  }
                  and row.get("origin")=="FF_CURRENT_REJECTED"):
                self._negative_index.setdefault(str(row.get("exact_request_sha256") or ""),[]).append(row)
        return rows

    @staticmethod
    def _positive_exact_sha256_from_certificate(cert):
        try:
            return sha256_obj({
                "identity_schema":"Layer4-positive-semantic-exact-v1",
                "phase":str(cert.get("phase") or ""),
                "reference_time_s":float(cert.get("reference_time_s")),
                "model_hash":str(cert.get("model_hash") or ""),
                "parameter_hash":str(cert.get("parameter_hash") or ""),
                "climate_hash":str(cert.get("climate_hash") or ""),
                "map_hash":str(cert.get("map_hash") or ""),
                "scenario_semantics_hash":str(cert.get("scenario_semantics_hash") or ""),
                "x0":cert.get("x0") or [],"actions":cert.get("actions") or [],
            })
        except Exception:
            return None

    def _positive_exact_sha256(self,candidate,snapshot,scenario,model_hash,param_hash):
        context=self._context_hashes(snapshot)
        return sha256_obj({
            "identity_schema":"Layer4-positive-semantic-exact-v1",
            "phase":self.phase(snapshot),"reference_time_s":float(snapshot.reference_time_s),
            "model_hash":str(model_hash),"parameter_hash":str(param_hash),
            "climate_hash":context["climate_hash"],"map_hash":context["map_hash"],
            "scenario_semantics_hash":self._scenario_semantics_hash(scenario),
            "x0":candidate.trajectory[0] if candidate.trajectory else [],"actions":candidate.actions,
        })

    def _exact_request_sha256(self,candidate,snapshot,scenario,model_hash,param_hash):
        """Run-independent identity for negative evidence, with no tolerance envelope."""
        context=self._context_hashes(snapshot)
        snapshot_context=snapshot.context or {}
        physical_initial_state=(
            snapshot_context.get("online_execution_state")
            or snapshot_context.get("online_physical_state")
            or snapshot_context.get("offline_physical_state")
            or snapshot_context.get("offline_plant_state")
            or {}
        )
        return sha256_obj({
            "identity_schema":"Layer4-semantic-exact-request-v3",
            # A negative physical replay is valid only for the initial physical
            # condition that was actually replayed.  Candidate state alone is not
            # sufficient: RTMO intentionally carries JSBSim truth on a separate
            # execution-only channel that is absent from the optimization vector.
            "physical_initialization_contract":"TDOC-L4FF-initial-state-agl-map-frame-v4",
            "physical_initial_state":physical_initial_state,
            "phase":CertifiedMultiFidelityManager.phase(snapshot),
            "reference_time_s":float(snapshot.reference_time_s),
            "model_hash":model_hash,"parameter_hash":param_hash,
            "climate_hash":context["climate_hash"],"map_hash":context["map_hash"],
            "candidate_contract":{
                "scenario_id":candidate.scenario_id,"snapshot_version":candidate.snapshot_version,
                "probability":candidate.probability,"trajectory":candidate.trajectory,
                "uncertainty":candidate.uncertainty,"network_trajectory":candidate.network_trajectory,
                "actions":candidate.actions,"objective_vector":candidate.objective_vector,
                "normalized_objectives":candidate.normalized_objectives,"feasible":candidate.feasible,
                "violation_probability":candidate.violation_probability,"rom_valid":candidate.rom_valid,
                "rom_error":candidate.rom_error,"robust_score":candidate.robust_score,
                "robust_feasible":candidate.robust_feasible,
                "worst_case_scenario_id":candidate.worst_case_scenario_id,
                "worst_case_disturbance":candidate.worst_case_disturbance,
            },
            "scenario_semantics":CertifiedMultiFidelityManager._scenario_semantics(scenario),
        })

    def _signature(self,snapshot,model_hash,param_hash,scenario_semantics_hash):
        h=self._context_hashes(snapshot)
        return sha256_obj({
            "phase":self.phase(snapshot),"model_hash":model_hash,"parameter_hash":param_hash,
            "climate_hash":h["climate_hash"],"map_hash":h["map_hash"],
            "scenario_semantics_hash":scenario_semantics_hash,
        })

    def _legacy_signature(self,candidate,snapshot,scenario,model_hash,param_hash):
        h=self._context_hashes(snapshot)
        return sha256_obj({
            "phase":self.phase(snapshot),"model_hash":model_hash,"parameter_hash":param_hash,
            "climate_hash":h["climate_hash"],"map_hash":h["map_hash"],
            "scenario_id":candidate.scenario_id,
            "scenario_parameters":getattr(scenario,"parameters",{}) if scenario is not None else {},
        })

    def _miss(self,reason):
        self._reuse_misses[str(reason)]+=1

    @staticmethod
    def _finite_array(value):
        try:
            a=np.asarray(value,dtype=float)
        except Exception:
            return None
        return a if a.size and np.all(np.isfinite(a)) else None

    def _state_envelope(self,candidate):
        x=self._finite_array(candidate.trajectory[0] if candidate.trajectory else [])
        if x is None: return None
        floor=max(float(self.config.get("verification.certificate_state_tolerance",0.05)),0.0)
        sigma_mult=max(float(self.config.get("verification.certificate_state_sigma_multiplier",3.0)),0.0)
        slack=np.full(x.shape,floor,dtype=float)
        try:
            cov=np.asarray((candidate.uncertainty or [])[0],dtype=float)
            if cov.ndim==2 and cov.shape[0]==x.size and cov.shape[1]==x.size:
                sig=np.sqrt(np.maximum(np.diag(cov),0.0)).reshape(x.shape)
                slack=np.maximum(slack,sigma_mult*sig)
        except Exception:
            pass
        return {"lower":(x-slack).tolist(),"upper":(x+slack).tolist(),"slack":slack.tolist(),
                "basis":"x0 +/- max(abs_floor, sigma_multiplier*sqrt(diag(Sigma0)))","sigma_multiplier":sigma_mult}

    def _action_envelope(self,candidate):
        a=self._finite_array(candidate.actions)
        if a is None: return None
        floor=max(float(self.config.get("verification.certificate_action_tolerance",0.01)),0.0)
        frac=max(float(self.config.get("verification.certificate_action_range_fraction",0.05)),0.0)
        try:
            lo=np.asarray(self.config.get("qlpv.action_lower"),dtype=float)
            hi=np.asarray(self.config.get("qlpv.action_upper"),dtype=float)
            if a.ndim==2 and lo.shape==(a.shape[1],) and hi.shape==(a.shape[1],):
                per=np.maximum(floor,frac*np.maximum(hi-lo,0.0))
                lower=np.maximum(a-per,lo); upper=np.minimum(a+per,hi)
                return {"lower":lower.tolist(),"upper":upper.tolist(),"slack_per_channel":per.tolist(),
                        "basis":"action +/- max(abs_floor, range_fraction*(upper-lower)), clipped to hard bounds",
                        "range_fraction":frac}
        except Exception:
            pass
        return {"lower":(a-floor).tolist(),"upper":(a+floor).tolist(),"slack_per_channel":floor,
                "basis":"action +/- abs_floor"}

    @staticmethod
    def _inside_envelope(value,envelope):
        try:
            a=np.asarray(value,dtype=float); lo=np.asarray((envelope or {}).get("lower"),dtype=float); hi=np.asarray((envelope or {}).get("upper"),dtype=float)
            return bool(a.shape==lo.shape==hi.shape and np.all(np.isfinite(a)) and np.all(a>=lo-1e-12) and np.all(a<=hi+1e-12))
        except Exception:
            return False

    @staticmethod
    def _fresh(cert,snapshot):
        freshness=float(cert.get("freshness_s",0.0) or 0.0)
        if freshness < 0: return False
        if cert.get("freshness_clock")=="mission_reference" and cert.get("reference_time_s") is not None:
            try:
                age=abs(float(snapshot.reference_time_s)-float(cert.get("reference_time_s")))
                return math.isfinite(age) and age<=freshness
            except Exception:
                return False
        # Legacy certificates keep their original wall-clock semantics and are never
        # silently upgraded to mission-time freshness.
        try:
            return time.time()-float(cert.get("created_unix_s",0.0))<=freshness
        except Exception:
            return False

    def find(self,candidate,snapshot,scenario,model_hash,param_hash):
        if not model_ff_allowed(snapshot,self.config):
            self._miss("native_controller_certificate_not_supported"); return None
        if not bool(self.config.get("verification.certificate_reuse_enabled",True)):
            self._miss("reuse_disabled"); return None
        self._reuse_attempts+=1
        phase=self.phase(snapshot); semantics_hash=self._scenario_semantics_hash(scenario)
        sig=self._signature(snapshot,model_hash,param_hash,semantics_hash)
        x=candidate.trajectory[0] if candidate.trajectory else []
        a=candidate.actions
        saw={"origin":False,"phase":False,"model":False,"context":False,"fresh":False,"envelope":False,"state":False,"action":False}
        self._load()
        exact_digest=self._positive_exact_sha256(candidate,snapshot,scenario,model_hash,param_hash)
        candidates=list((self._positive_exact_index or {}).get(exact_digest,[]))
        # Legacy signatures retain their historical exact-point compatibility.  A
        # v4 envelope is no longer verification authority: only an exact semantic
        # replay can enter the positive lane.
        if not candidates:
            candidates=self._legacy_positive or []
            keys=list((self._positive_index or {}).keys())
            saw["origin"]=bool(keys or candidates)
            saw["phase"]=any(k[0]==phase for k in keys)
            saw["model"]=any(k[:3]==(phase,str(model_hash),str(param_hash)) for k in keys)
        for cert in reversed(candidates):
            if not self._scope_matches(cert):continue
            if not self._model_dependencies_match(cert):continue
            if self._retired_fixture_evidence(cert.get('physical_issuance_evidence') or {}):
                self._miss('retired_fixture_certificate'); continue
            if cert.get("origin")!="FF_CURRENT" or not cert.get("verified",False): continue
            saw["origin"]=True
            if cert.get("phase")!=phase: continue
            saw["phase"]=True
            if cert.get("model_hash")!=model_hash or cert.get("parameter_hash")!=param_hash: continue
            saw["model"]=True
            cert_semantics_hash=cert.get("scenario_semantics_hash")
            if cert_semantics_hash:
                if cert_semantics_hash!=semantics_hash or cert.get("context_signature")!=sig: continue
            else:
                if cert.get("context_signature")!=self._legacy_signature(candidate,snapshot,scenario,model_hash,param_hash): continue
            saw["context"]=True
            if not self._fresh(cert,snapshot): continue
            saw["fresh"]=True
            if cert.get("schema")=="Layer4-FF-certificate-v4":
                state_env=cert.get("state_envelope"); action_env=cert.get("action_envelope")
                if not state_env or not action_env: continue
                saw["envelope"]=True
                if not self._inside_envelope(x,state_env): continue
                saw["state"]=True
                if not self._inside_envelope(a,action_env): continue
                saw["action"]=True
            else:
                # v3/legacy exact point gate retained only for backward compatibility.
                try:
                    xc=np.asarray(cert.get("x0",[]),float); ac=np.asarray(cert.get("actions",[]),float)
                    xx=np.asarray(x,float); aa=np.asarray(a,float)
                    x_tol=float(cert.get("x_tolerance",0.0)); a_tol=float(cert.get("action_tolerance",0.0))
                    if xx.shape!=xc.shape or aa.shape!=ac.shape: continue
                    saw["envelope"]=True
                    if np.max(np.abs(xx-xc),initial=0.0)>x_tol: continue
                    saw["state"]=True
                    if np.max(np.abs(aa-ac),initial=0.0)>a_tol: continue
                    saw["action"]=True
                except Exception:
                    continue
            self._reuse_hits+=1
            if not self._model_dependencies_match(cert):return None
            return cert
        for key in ("origin","phase","model","context","fresh","envelope","state","action"):
            if not saw[key]: self._miss(key+"_mismatch"); break
        return None

    def _scope_matches(self,certificate):
        scope=model_prediction_scope(self.config)
        diagnostic=(certificate or {}).get('verification_scope')==MODEL_PREDICTION_SCOPE
        ok=bool(scope)==diagnostic
        if diagnostic:
            ok=bool(ok and (certificate or {}).get('model_prediction_only') is True
                    and (certificate or {}).get('native_controller_certified') is False
                    and (certificate or {}).get('flight_authorized') is False)
        if not ok:self._miss('model_prediction_scope_mismatch')
        return ok

    def find_rejection(self,candidate,snapshot,scenario,model_hash,param_hash):
        """Reuse only an exact, durable FF rejection; this can never verify a candidate."""
        if not bool(self.config.get("verification.certificate_reuse_enabled",True)):
            return None
        self._load()
        digest=self._exact_request_sha256(candidate,snapshot,scenario,model_hash,param_hash)
        for cert in reversed(list((self._negative_index or {}).get(digest,[]))):
            if not self._model_dependencies_match(cert):continue
            if (cert.get("bank_persisted") is True and cert.get("verified") is False
                    and cert.get("outcome_status")=="ATTEMPTED_AND_REJECTED"
                    and self._fresh(cert,snapshot)):
                return cert
        return None

    @staticmethod
    def _physical_certificate_evidence(verification):
        verification=dict(verification or {})
        verdict=dict(verification.get("wingmpet_physical_verdict") or {})
        transition=dict(verification.get("transition_ff_evidence") or {})
        bridge=dict(verification.get("bridge") or {})
        verdict_sources=sorted({str(r.get("source") or "") for r in (verdict.get("rows") or []) if isinstance(r,dict)})
        fixture_rows=[r for r in (verdict.get('rows') or []) if isinstance(r,dict) and (
            r.get('retired_launch_fixture_present') or 'ideal-SITL-launch-fixture' in str(r.get('source') or '')
            or r.get('launch_fixture_evidence_valid') or r.get('launch_fixture_contract_hash'))]
        return {
            "verification_origin":str(verification.get("verification_origin") or ""),
            "physical_actions_consumed":bool(verification.get("physical_actions_consumed")),
            "physical_runtime_safe":bool(verification.get("physical_runtime_safe")),
            "physical_mission_safe":bool(verification.get("physical_mission_safe")),
            "verdict_authoritative":bool(verdict.get("authoritative")),
            "verdict_non_bypassable":bool(verdict.get("non_bypassable")),
            "all_rows_vtol_feasible":bool(verdict.get("all_rows_vtol_feasible")),
            "verdict_sources":verdict_sources,
            "retired_launch_fixture_present":bool(fixture_rows),
            # P1-3 transition evidence chain. These fields are evidence only and
            # cannot turn a rejected replay into a verified certificate.
            "bridge_ready":bool(transition.get("bridge_ready",bridge.get("ok"))),
            "bridge_reason":transition.get("bridge_reason",bridge.get("reason")),
            "bridge_owner":transition.get("bridge_owner",bridge.get("owner")),
            "bridge_binding_source":transition.get("bridge_binding_source",bridge.get("binding_source")),
            "bridge_sha256":transition.get("bridge_sha256"),
            "physical_action_count":int(transition.get("physical_action_count") or len(bridge.get("physical_actions") or [])),
            "physical_actions_sha256":transition.get("physical_actions_sha256"),
            "workspace_count":int(transition.get("workspace_count") or 0),
            "workspace_identity_sha256":transition.get("workspace_identity_sha256"),
            "transition_audit_complete":bool(transition.get("audit_complete")),
        }

    @staticmethod
    def _retired_fixture_evidence(evidence):
        e=evidence or {}
        return bool(e.get('retired_launch_fixture_present') or e.get('launch_fixture_evidence_valid')
                    or e.get('launch_fixture_contract_hashes')
                    or any('ideal-SITL-launch-fixture' in str(src) for src in (e.get('verdict_sources') or [])))

    @staticmethod
    def _physical_certificate_evidence_ok(evidence, *, require_transition_chain=False):
        e=evidence or {}
        base=bool(
            e.get("verification_origin")=="FF_CURRENT"
            and not CertifiedMultiFidelityManager._retired_fixture_evidence(e)
            and e.get("physical_actions_consumed")
            and e.get("physical_runtime_safe")
            and e.get("physical_mission_safe")
            and e.get("verdict_authoritative")
            and e.get("verdict_non_bypassable")
            and e.get("all_rows_vtol_feasible")
            and bool(e.get("verdict_sources"))
            and all(
                str(src).startswith("WingMPET/op27")
                or str(src).startswith("WingMPET/op35-ground-supported")
                for src in (e.get("verdict_sources") or [])
            )
        )
        if not require_transition_chain:
            return base
        return bool(
            base
            and e.get("bridge_ready")
            and int(e.get("physical_action_count") or 0)>0
            and bool(e.get("physical_actions_sha256"))
            and int(e.get("workspace_count") or 0)>0
            and bool(e.get("workspace_identity_sha256"))
            and e.get("transition_audit_complete")
        )

    def _persist_and_confirm(self,cert):
        """Append one certificate durably and confirm exact-ID readback.

        A verified candidate is not allowed to *claim* a persisted reusable certificate
        until the JSONL bank can read the exact certificate back.  This is deliberately
        synchronous because certificate issuance is outside the 10-Hz hot path.
        """
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            encoded=json.dumps(cert,sort_keys=True,allow_nan=False)+"\n"
            with self.path.open("a+",encoding="utf-8") as f:
                offset=f.tell()
                f.write(encoded)
                f.flush(); os.fsync(f.fileno())
            cid=str(cert.get("certificate_id") or "")
            if not cid: return False
            # Exact readback of the just-appended record is sufficient after fsync;
            # rereading a multi-megabyte historical bank made issuance O(bank size).
            with self.path.open("r",encoding="utf-8") as f:
                f.seek(offset); row=json.loads(f.readline())
            found=bool(
                str(row.get("certificate_id") or "")==cid
                and row.get("bank_persisted") is True
                and row.get("schema") in {"Layer4-FF-certificate-v4","Layer4-FF-rejection-certificate-v2"}
            )
            if found and self._cache is not None:
                self._cache.append(row)
                key=(str(row.get("phase") or ""),str(row.get("model_hash") or ""),
                     str(row.get("parameter_hash") or ""),str(row.get("context_signature") or ""))
                if row.get("schema")=="Layer4-FF-certificate-v4":
                    self._positive_index.setdefault(key,[]).append(row)
                    digest=self._positive_exact_sha256_from_certificate(row)
                    if digest: self._positive_exact_index.setdefault(digest,[]).append(row)
                else:
                    self._negative_index.setdefault(str(row.get("exact_request_sha256") or ""),[]).append(row)
            return found
        except Exception:
            return False

    def build(self,candidate,snapshot,scenario,model_hash,param_hash,verification,require_physical_verdict=False):
        # An FMU-only FF replay cannot certify an ArduPilot pilot command, even
        # when an old bank row or caller-supplied verdict claims verified=True.
        if not model_ff_allowed(snapshot,self.config): return None
        if not self._model_dependencies_match(verification):return None
        if not (verification.get("executed") and verification.get("candidate_verified") and verification.get("verified")):
            return None
        phase=self.phase(snapshot)
        physical_evidence=self._physical_certificate_evidence(verification) if require_physical_verdict else {}
        require_transition_chain=phase in {"transition_forward","transition_back"}
        if require_physical_verdict and not self._physical_certificate_evidence_ok(physical_evidence,require_transition_chain=require_transition_chain):
            return None
        h=self._context_hashes(snapshot); semantics=self._scenario_semantics(scenario); semantics_hash=sha256_obj(semantics)
        state_env=self._state_envelope(candidate); action_env=self._action_envelope(candidate)
        if state_env is None or action_env is None:
            return None
        now=time.time()
        cert={
            "schema":"Layer4-FF-certificate-v4",
            "certificate_id":sha256_obj({"candidate":candidate.candidate_id,"snapshot":candidate.snapshot_hash,"t":now}),
            "origin":"FF_CURRENT","verified":True,"created_unix_s":now,
            "reference_time_s":float(snapshot.reference_time_s),"freshness_clock":"mission_reference",
            "freshness_s":float(self.config.get("verification.certificate_freshness_s",300.0)),
            "phase":phase,"model_hash":model_hash,"parameter_hash":param_hash,
            "climate_hash":h["climate_hash"],"map_hash":h["map_hash"],
            "context_signature":self._signature(snapshot,model_hash,param_hash,semantics_hash),
            "scenario_semantics_hash":semantics_hash,"scenario_semantics":semantics,
            "scenario_id_provenance":candidate.scenario_id,
            "state_envelope":state_env,"action_envelope":action_env,
            # Legacy mirrors retained for traceability only; v4 find() uses envelopes.
            "x0":candidate.trajectory[0] if candidate.trajectory else [],"actions":candidate.actions,
            "x_tolerance":float(self.config.get("verification.certificate_state_tolerance",0.05)),
            "action_tolerance":float(self.config.get("verification.certificate_action_tolerance",0.01)),
            "snapshot_hash":candidate.snapshot_hash,"ff_evidence_hash":sha256_obj(verification),
            "bank_persisted":True,
            "issuance_contract":"Layer4-FF-certificate-issuance-v1",
            "verification_scope":"MODEL_ONLY_LAYER3_FMU_REPLAY",
            "native_controller_certified":False,"flight_authorized":False,
            "issuance_requires_physical_verdict":bool(require_physical_verdict),
            "timing_contract":dict(verification.get("timing_contract") or {}),
            "knowledge_policy_sha256":verification.get("knowledge_policy_sha256"),
        }
        cert.update(model_prediction_scope(self.config))
        if require_physical_verdict:
            cert["physical_issuance_evidence"]=physical_evidence
            cert["physical_verdict_hash"]=sha256_obj(verification.get("wingmpet_physical_verdict") or {})
            cert["transition_evidence_hash"]=sha256_obj(verification.get("transition_ff_evidence") or {}) if phase in {"transition_forward","transition_back"} else None
            cert["issuance_contract"]=(
                "Layer4-FF-transition-certificate-issuance-v1"
                if phase in {"transition_forward","transition_back"}
                else "Layer4-FF-vertical-certificate-issuance-v1"
            )
        if self.model_dependencies_provider is not None:
            cert['model_dependencies']=json.loads(json.dumps(verification['model_dependencies']))
            if not self._model_dependencies_match(cert):return None
        if not self._persist_and_confirm(cert):
            return None
        return cert

    def build_rejection(self,candidate,snapshot,scenario,model_hash,param_hash,verification):
        """Persist exact negative FF evidence without granting any safety authority.

        Only a completed, non-exception FF rejection is eligible.  Unlike positive
        certificates this record has no neighborhood: one changed bit in state,
        action, scenario semantics, model, parameters or mission time is a miss.
        """
        if not self._model_dependencies_match(verification):return None
        if not (verification.get("executed") and verification.get("attempted")
                and not verification.get("verified")
                and verification.get("outcome_status")=="ATTEMPTED_AND_REJECTED"):
            return None
        now=time.time(); semantics=self._scenario_semantics(scenario); semantics_hash=sha256_obj(semantics)
        digest=self._exact_request_sha256(candidate,snapshot,scenario,model_hash,param_hash)
        cert={
            "schema":"Layer4-FF-rejection-certificate-v2",
            "exact_identity_schema":"Layer4-semantic-exact-request-v3",
            "certificate_id":sha256_obj({"negative":digest,"t":now}),
            "origin":"FF_CURRENT_REJECTED","verified":False,
            "outcome_status":"ATTEMPTED_AND_REJECTED",
            "created_unix_s":now,"reference_time_s":float(snapshot.reference_time_s),
            "freshness_clock":"mission_reference",
            "freshness_s":float(self.config.get("verification.certificate_freshness_s",300.0)),
            "phase":self.phase(snapshot),"model_hash":model_hash,"parameter_hash":param_hash,
            "context_signature":self._signature(snapshot,model_hash,param_hash,semantics_hash),
            "scenario_semantics_hash":semantics_hash,"exact_request_sha256":digest,
            "reason_codes":list(verification.get("reason_codes") or []),
            "ff_evidence_hash":sha256_obj(verification),"bank_persisted":True,
            "issuance_contract":"Layer4-FF-exact-rejection-certificate-v1",
            "authority":"reject_only_never_verified",
            "timing_contract":dict(verification.get("timing_contract") or {}),
            "knowledge_policy_sha256":verification.get("knowledge_policy_sha256"),
        }
        if self.model_dependencies_provider is not None:
            cert['model_dependencies']=json.loads(json.dumps(verification['model_dependencies']))
            if not self._model_dependencies_match(cert):return None
        return cert if self._persist_and_confirm(cert) else None

    def capability(self, reference_times_by_phase=None):
        """Return phases with at least one reusable certificate contract.

        For v4, freshness is evaluated in mission-reference time when a phase reference
        is supplied. Legacy rows keep wall-clock semantics.
        """
        refs=reference_times_by_phase or {}; out=set()
        for c in self._load():
            if not self._scope_matches(c):continue
            if not self._model_dependencies_match(c):continue
            if self._retired_fixture_evidence(c.get('physical_issuance_evidence') or {}):continue
            if c.get("origin")!="FF_CURRENT" or not c.get("verified"): continue
            p=str(c.get("phase","unknown")); ok=False
            if p in {"qstabilize", "qhover", "qloiter", "qland", "qrtl", "fbwa", "fbwb"}: continue
            if c.get("schema")=="Layer4-FF-certificate-v4" and c.get("freshness_clock")=="mission_reference":
                try:
                    cr=float(c.get("reference_time_s")); fr=float(c.get("freshness_s",0.0))
                    rvals=[float(x) for x in (refs.get(p) or [])]
                    ok=bool(c.get("state_envelope") and c.get("action_envelope") and c.get("context_signature") and c.get("scenario_semantics_hash") and c.get("ff_evidence_hash") and (not rvals or any(abs(r-cr)<=fr for r in rvals)))
                except Exception:
                    ok=False
            else:
                try: ok=time.time()-float(c.get("created_unix_s",0))<=float(c.get("freshness_s",0))
                except Exception: ok=False
            if ok: out.add(p)
        return sorted(out)

    def reuse_stats(self):
        return {"attempts":int(self._reuse_attempts),"hits":int(self._reuse_hits),"misses":dict(sorted(self._reuse_misses.items())),
                "bank_entries":len(self._load()),"positive_index_keys":len(self._positive_index or {}),
                "positive_exact_index_keys":len(self._positive_exact_index or {}),
                "negative_index_keys":len(self._negative_index or {}),"schema":"Layer4-FF-certificate-reuse-stats-v3"}
