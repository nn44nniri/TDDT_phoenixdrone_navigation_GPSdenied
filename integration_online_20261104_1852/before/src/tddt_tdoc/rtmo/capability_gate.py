from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import csv,gzip,json,math,time
from .phase_manager import PHASE_ORDER
from .capability_registry import load_live_capability_registry

@dataclass(frozen=True)
class CapabilityManifest:
    phases: dict[str,dict[str,Any]]
    source: str
    @property
    def ready_11phase(self)->bool:
        return all(bool((self.phases.get(p) or {}).get("capability")) for p in PHASE_ORDER)
    @property
    def blocked_phases(self)->list[str]:
        return [p for p in PHASE_ORDER if not bool((self.phases.get(p) or {}).get("capability"))]

    def runtime_release(self, phase:str, *, next_phase:str|None=None,
                        pre_release_evidence:dict[str,Any]|None=None,
                        next_pre_release_evidence:dict[str,Any]|None=None,
                        current_runtime_evidence:dict[str,Any]|None=None)->dict[str,Any]:
        """Return phase-local capability plus *current-state* RuntimeRelease.

        P0-3 contract:
        - durable/offline capability is historical evidence only;
        - PRE_RELEASE_VERIFY may qualify phase capability, but is non-actuating;
        - actuation/transition authority requires a separate transaction-scoped
          CurrentRuntimeRelease assembled from fresh Online state, compatible
          current L4 verification, current L5 FINAL, clock freshness and Layer6
          readiness.

        Consequently, neither an Offline/Durable proof nor a PRE_RELEASE_VERIFY
        proof can by itself make ``actuation_release`` true.
        """
        current=dict(self.phases.get(str(phase)) or {})
        durable_current_ok=bool(current.get("capability"))
        nxt=dict(self.phases.get(str(next_phase)) or {}) if next_phase else {}
        durable_next_ok=(bool(nxt.get("capability")) if next_phase else True)

        def _bootstrap_ok(ev:dict[str,Any]|None, expected_phase:str|None)->bool:
            if not expected_phase or not isinstance(ev,dict):
                return False
            return bool(
                ev.get("schema")=="TDOC-RTMO-pre-release-verify-v1"
                and str(ev.get("phase") or "")==str(expected_phase)
                and ev.get("qualified") is True
                and ev.get("non_actuating") is True
                and ev.get("layer2_current_state") is True
                and ev.get("layer3_atomic_state") is True
                and ev.get("l4_full_fidelity_verified") is True
                and ev.get("l5_final") is True
                and ev.get("control_effect_applied") is False
                and bool(ev.get("transaction_id"))
            )

        def _runtime_evidence(ev:dict[str,Any]|None, expected_phase:str)->tuple[bool,list[str]]:
            required = {
                "fresh_operational_state":"fresh_operational_state_missing",
                "current_l4_artifact_verified":"current_l4_verified_artifact_missing",
                "current_l4_artifact_compatible":"current_l4_artifact_incompatible",
                "current_l5_final":"current_l5_final_missing",
                "clock_freshness_ok":"clock_freshness_not_ok",
                "layer6_ready":"layer6_not_ready",
            }
            if not isinstance(ev,dict):
                return False,["current_runtime_evidence_missing"]
            reasons=[]
            if ev.get("schema")!="TDOC-RTMO-current-runtime-release-evidence-v1":
                reasons.append("current_runtime_evidence_schema_invalid")
            if str(ev.get("phase") or "")!=str(expected_phase):
                reasons.append("current_runtime_evidence_phase_mismatch")
            if str(ev.get("learning_mode") or "")!="ONLINE_JSBSIM":
                reasons.append("current_runtime_evidence_not_online")
            if not bool(ev.get("transaction_id")):
                reasons.append("current_runtime_evidence_transaction_missing")
            for key,reason in required.items():
                if ev.get(key) is not True:
                    reasons.append(reason)
            return (not reasons),reasons

        current_bootstrap_ok=_bootstrap_ok(pre_release_evidence,str(phase))
        next_bootstrap_ok=_bootstrap_ok(next_pre_release_evidence,str(next_phase) if next_phase else None)

        # Historical/phase capability remains an admission/knowledge property.
        # It is intentionally separate from current execution authority.
        current_capability_ok=bool(durable_current_ok or current_bootstrap_ok)
        next_ok=(bool(durable_next_ok or next_bootstrap_ok) if next_phase else True)

        current_runtime_ok,runtime_reasons=_runtime_evidence(current_runtime_evidence,str(phase))
        actuation_release=bool(current_capability_ok and current_runtime_ok)
        transition_release=bool(actuation_release and next_ok)

        blockers=[]
        if not current_capability_ok:
            blockers.append(f"current_phase_capability_blocked:{phase}")
        if current_capability_ok and not current_runtime_ok:
            blockers.extend(runtime_reasons or ["current_runtime_release_missing"])
        if next_phase and not next_ok:
            blockers.append(f"next_phase_capability_blocked:{next_phase}")

        if not current_capability_ok:
            status="WAIT_CAPABILITY"
        elif not current_runtime_ok:
            status="WAIT_RUNTIME_RELEASE"
        else:
            status="RELEASED"

        return {
            "schema":"TDOC-RTMO-runtime-release-v2",
            "phase":str(phase),
            # Backward-compatible capability fields: these describe phase
            # qualification, not current Online actuation authority.
            "current_phase_capability":current_capability_ok,
            "current_phase_durable_capability":durable_current_ok,
            "current_phase_pre_release_qualified":current_bootstrap_ok,
            "current_phase_capability_source":(
                "LIVE_JOINT_CONTROL_PROOF" if durable_current_ok
                else ("PRE_RELEASE_VERIFY_CURRENT_STATE" if current_bootstrap_ok else "NONE")
            ),
            "next_phase":str(next_phase) if next_phase else None,
            "next_phase_capability":next_ok,
            "next_phase_durable_capability":durable_next_ok if next_phase else True,
            "next_phase_pre_release_qualified":next_bootstrap_ok if next_phase else False,
            "current_runtime_release":current_runtime_ok,
            "current_runtime_release_evidence":dict(current_runtime_evidence or {}),
            "current_runtime_release_reasons":runtime_reasons,
            "actuation_release":actuation_release,
            "transition_release":transition_release,
            "status":status,
            "blockers":blockers,
            "far_phase_capabilities_are_admission_blockers":False,
            "durable_capability_is_actuation_authority":False,
            "pre_release_verify_is_actuation_authority":False,
            "current_runtime_release_requires_online_current_state":True,
            "fresh_l4_l5_evidence_enforced_in_cycle":True,
            "current_state_envelope_enforced_in_cycle":True,
            "pre_release_verify_non_actuating":True,
            "pre_release_verify_is_durable_joint_proof":False,
            "synthetic_final_forbidden":True,
        }

    def to_dict(self)->dict[str,Any]:
        return {"schema":"TDOC-RTMO-phase-capability-v7","ready_11phase":self.ready_11phase,"blocked_phases":self.blocked_phases,"phases":self.phases,"source":self.source,
                "mission_certification":{"schema":"TDOC-RTMO-mission-certification-v1",
                    "fully_certified":self.ready_11phase,"blocked_phases":self.blocked_phases,
                    "admission_authority":False,"runtime_release_policy":"CURRENT_PHASE_AND_TRANSITION_LOCAL"},
                "synthetic_final_forbidden":True}

def _offline_rows(report_root:Path):
    ph=report_root/'offline'/'mission'/'PHASE_HISTORY.csv'
    cand=report_root/'offline'/'mission'/'LAYER4_CANDIDATES.csv'
    details=report_root/'offline'/'mission'/'DECISION_POINT_DETAILS.json.gz'
    phase_by_cycle={}
    if ph.is_file():
        with ph.open(newline='',encoding='utf-8') as f:
            for r in csv.DictReader(f):
                try: phase_by_cycle[int(r['cycle'])]=str(r['phase'])
                except Exception: pass
    l4={p:{"verified":False,"feasible":False,"verified_cycles":[],"verified_reference_times":[]} for p in PHASE_ORDER}
    if cand.is_file():
        with cand.open(newline='',encoding='utf-8') as f:
            for r in csv.DictReader(f):
                try: cyc=int(r['cycle'])
                except Exception: continue
                p=phase_by_cycle.get(cyc)
                if p not in l4: continue
                feasible=str(r.get('feasible','')).lower()=='true'; verified=str(r.get('verified','')).lower()=='true'
                l4[p]['feasible'] |= feasible; l4[p]['verified'] |= verified
                if verified and cyc not in l4[p]['verified_cycles']:
                    l4[p]['verified_cycles'].append(cyc)
                    try: l4[p]['verified_reference_times'].append(float(r.get('reference_time_s')))
                    except Exception: pass
    l5={p:{"admissible":False,"final_cycles":[]} for p in PHASE_ORDER}
    if details.is_file():
        try:
            with gzip.open(details,'rt',encoding='utf-8') as f: arr=json.load(f)
            if isinstance(arr,dict): arr=arr.get('records') or arr.get('cycles') or []
            for r in arr or []:
                p=str(((r.get('mission') or {}).get('phase')) or '')
                if p not in l5: continue
                dec=r.get('layer5_decision') or {}; gates=dec.get('gates') or {}; decision=dec.get('decision') or {}; approval=decision.get('approval') or {}
                final=bool(gates.get('FINAL') is True or approval.get('final_gate') is True)
                l5[p]['admissible'] |= final
                if final: l5[p]['final_cycles'].append(r.get('cycle'))
        except Exception: pass
    return l4,l5

def _boundary_evidence(report_root:Path)->dict[str,dict[str,Any]]:
    """Read passive-boundary evidence without promoting it to control evidence.

    on_ground/touchdown are mission boundary states, not L4/L5 controlled phases.
    Their readiness is therefore proven by boundary-state evidence while the runtime
    Ground-Start and Terminal gates remain authoritative during Online execution.
    """
    root=Path(report_root)
    mission=root/'offline'/'mission'
    metrics={}
    mp=mission/'VTOL_PHASE_METRICS.json'
    if mp.is_file():
        try:
            obj=json.loads(mp.read_text(encoding='utf-8'))
            if isinstance(obj,dict): metrics=obj
        except Exception:
            metrics={}
    first_phase=None; last_phase=None
    ph=mission/'PHASE_HISTORY.csv'
    if ph.is_file():
        try:
            with ph.open(newline='',encoding='utf-8') as f:
                rows=list(csv.DictReader(f))
            if rows:
                first_phase=str(rows[0].get('phase') or '')
                last_phase=str(rows[-1].get('phase') or '')
        except Exception:
            pass
    starts=bool(metrics.get('starts_on_ground')) and first_phase=='on_ground'
    touchdown_seen=bool(metrics.get('touchdown_seen')) and last_phase=='touchdown'
    try: final_agl=float(metrics.get('final_agl_m'))
    except Exception: final_agl=float('inf')
    try: final_goal=float(metrics.get('final_goal_distance_m'))
    except Exception: final_goal=float('inf')
    touchdown_terminal=touchdown_seen and final_agl<=1.0 and final_goal<=25.0
    common={'physics_policy':'TDOC-passive-ground-contact-v1',
            'source':'offline_mission_passive_boundary_metrics',
            'wind_advection_suppressed':True,
            'ground_velocity_zero':True}
    return {
        'on_ground':{
            'boundary_state_supported':starts,
            'boundary_evidence':{**common,'boundary_phase':'on_ground','starts_on_ground':bool(metrics.get('starts_on_ground')),'first_phase':first_phase,'terminal_location_required':False},
        },
        'touchdown':{
            'boundary_state_supported':touchdown_terminal,
            'boundary_evidence':{**common,'boundary_phase':'touchdown','touchdown_seen':bool(metrics.get('touchdown_seen')),'last_phase':last_phase,'final_agl_m':final_agl,'final_goal_distance_m':final_goal,'terminal_location_required':True},
        },
    }


def online_on_ground_boundary_evidence(episode_preparation:dict[str,Any]|None)->dict[str,Any]:
    """Derive fail-closed ``on_ground`` evidence from the current PT_LIB preparation.

    Offline mission history is deliberately ignored.  The evidence is accepted only
    when the public PT_LIB preparation reports a frozen/stationary bridge state, zero
    relative altitude within tolerance, and successful physical-readiness checks.
    """
    ep=dict(episode_preparation or {})
    prep=dict(ep.get('preparation') or ep)
    if prep.get('transport')=='Layer6Client_GUI_MAVLink':
        from ..native_control_binding import ground_contact
        st=dict(prep.get('current_gui_state') or {})
        def bounded(key,limit):
            try:
                value=float(st[key]); return math.isfinite(value) and abs(value)<=limit
            except (KeyError,TypeError,ValueError): return False
        ages=st.get('telemetry_age_s') or {}
        fresh=True
        for name in ('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT'):
            try:
                age=float(ages[name]); fresh=fresh and math.isfinite(age) and 0<=age<=2.0
            except (KeyError,TypeError,ValueError): fresh=False
        checks={
            'episode_preparation_ok':bool(ep.get('ok',prep.get('ok',False))),
            'native_connection_ready':prep.get('ready') is True and st.get('mavlink_connected') is True,
            'fresh_telemetry':fresh,
            'on_ground':ground_contact(st),
            'groundspeed_stationary':bounded('groundspeed_m_s',0.8),
            'relative_altitude_grounded':bounded('relative_altitude_m',1.0),
            'reported_velocity_stationary':all(bounded(k,1.0) for k in ('velocity_n_m_s','velocity_e_m_s','velocity_d_m_s')),
        }
        return {'supported':all(checks.values()),'source':'online_native_layer6_current_operational_state',
                'authority':'current GUI/MAVLink telemetry; no historical boundary evidence',
                'checks':checks,'simulation_only':True,'not_field_evidence':True,
                'reason':'current_ground_contact_state_valid' if all(checks.values()) else 'current_ground_contact_state_not_valid'}
    seq=list(prep.get('sequence') or [])
    current=None
    current_step=None
    for step in seq:
        if not isinstance(step,dict):
            continue
        result=step.get('result')
        if isinstance(result,dict) and isinstance(result.get('physical_readiness'),dict) and isinstance(result.get('position'),dict):
            current=dict(result); current_step=str(step.get('step') or '')
            if current_step=='precheck':
                break
    if current is None:
        return {'supported':False,'source':'online_ptlib_current_operational_state','reason':'ptlib_current_ground_state_evidence_missing'}
    physical=dict(current.get('physical_readiness') or {})
    checks=dict(physical.get('checks') or {})
    pos=dict(current.get('position') or {})
    hold=dict(checks.get('bridge_preflight_hold') or {})
    gs=dict(checks.get('groundspeed_m_s') or {})
    air=dict(checks.get('current_bridge_airspeed_m_s') or {})
    alt=dict(checks.get('initial_alt_error_m') or {})
    rel_raw=pos.get('relative_alt')
    try: rel_alt_m=float(rel_raw)/1000.0
    except Exception: rel_alt_m=None
    velocity_cm_s=[]
    velocity_ok=True
    for k in ('vx','vy','vz'):
        try:
            v=float(pos.get(k)); velocity_cm_s.append(v); velocity_ok=velocity_ok and abs(v)<=100.0
        except Exception:
            velocity_ok=False
    checks_out={
        'episode_preparation_ok':bool(ep.get('ok',prep.get('ok',False))),
        'ptlib_preparation_ready':bool(prep.get('ready',False)),
        'physical_readiness_ready':bool(physical.get('ready',False)),
        'bridge_preflight_hold':bool(hold.get('ok') is True and hold.get('holding') is True and hold.get('released') is False and hold.get('release_authorized') is False),
        'groundspeed_stationary':bool(gs.get('ok') is True),
        'bridge_airspeed_stationary':bool(air.get('ok') is True),
        'initial_altitude_consistent':bool(alt.get('ok') is True),
        'relative_altitude_grounded':bool(rel_alt_m is not None and abs(rel_alt_m)<=1.0),
        'reported_velocity_stationary':bool(velocity_ok),
    }
    supported=all(checks_out.values())
    return {
        'supported':supported,
        'source':'online_ptlib_current_operational_state',
        'authority':'current Online PT_LIB structured preparation; Offline boundary evidence is audit-only',
        'ptlib_evidence_step':current_step,
        'relative_alt_m':rel_alt_m,
        'position_velocity_cm_s':velocity_cm_s if len(velocity_cm_s)==3 else None,
        'checks':checks_out,
        'reason':'current_ground_contact_state_valid' if supported else 'current_ground_contact_state_not_valid',
    }


def _certificate_bank_guard(bank_path:Path|None, l4:dict[str,dict[str,Any]],model_dependencies_provider=None)->dict[str,dict[str,Any]]:
    """Audit the live FF certificate bank without fabricating candidate evidence.

    v4 certificates are freshness-checked in mission-reference time against the
    verified Offline reference-times for the same phase and must carry explicit
    state/action envelopes and integrity hashes. Legacy certificates retain their
    wall-clock semantics but cannot satisfy the new envelope-aware readiness guard.
    The per-cycle Layer4 manager remains authoritative for actual current-state/action
    containment before reuse.
    """
    out={p:{"certificate_contract_ready":False,"reusable_certificate_count":0,"bank_entries_for_phase":0,
            "reasons":[],"schemas":[]} for p in PHASE_ORDER}
    if bank_path is None:
        for p in PHASE_ORDER: out[p]["reasons"]=["certificate_bank_path_unavailable"]
        return out
    path=Path(bank_path)
    if not path.is_file():
        for p in PHASE_ORDER: out[p]["reasons"]=["certificate_bank_missing"]
        return out
    rows=[]
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                r=json.loads(line)
                if isinstance(r,dict): rows.append(r)
            except Exception: pass
    except Exception:
        for p in PHASE_ORDER: out[p]["reasons"]=["certificate_bank_unreadable"]
        return out
    now=time.time()
    current_model_dependencies=None
    if model_dependencies_provider is not None:
        try:current_model_dependencies=model_dependencies_provider()
        except Exception:current_model_dependencies={}
    for p in PHASE_ORDER:
        phase_rows=[r for r in rows if r.get('origin')=='FF_CURRENT' and r.get('verified') is True and str(r.get('phase') or '')==p]
        rec=out[p]; rec['bank_entries_for_phase']=len(phase_rows); rec['schemas']=sorted({str(r.get('schema') or 'legacy') for r in phase_rows})
        refs=[float(x) for x in (l4.get(p,{}).get('verified_reference_times') or []) if isinstance(x,(int,float)) and math.isfinite(float(x))]
        reasons=set(); usable=0
        for r in phase_rows:
            if model_dependencies_provider is not None:
                try:
                    from ..model_compatibility import certificate_compatibility
                    compatible=certificate_compatibility(r,current_model_dependencies)['ok']
                except Exception:compatible=False
                if not compatible:
                    reasons.add('certificate_model_dependencies_missing_or_changed');continue
            schema=str(r.get('schema') or '')
            if schema!='Layer4-FF-certificate-v4':
                # Old point-certificates remain readable by Layer4 for exact legacy
                # reuse but are not sufficient for the v4 RTMO envelope-aware guard.
                reasons.add('legacy_certificate_missing_v4_envelope_contract'); continue
            required_hashes=('model_hash','parameter_hash','context_signature','scenario_semantics_hash','ff_evidence_hash')
            if any(not str(r.get(k) or '') for k in required_hashes): reasons.add('certificate_integrity_hash_incomplete'); continue
            if p in {'vertical_takeoff','hover','vertical_landing'}:
                if r.get('bank_persisted') is not True:
                    reasons.add('certificate_bank_persistence_unconfirmed'); continue
                if str(r.get('issuance_contract') or '')!='Layer4-FF-vertical-certificate-issuance-v1':
                    reasons.add('vertical_certificate_physical_issuance_contract_missing'); continue
                pe=dict(r.get('physical_issuance_evidence') or {})
                if not (
                    pe.get('verification_origin')=='FF_CURRENT'
                    and pe.get('physical_actions_consumed')
                    and pe.get('physical_runtime_safe')
                    and pe.get('physical_mission_safe')
                    and pe.get('verdict_authoritative')
                    and pe.get('verdict_non_bypassable')
                    and pe.get('all_rows_vtol_feasible')
                    and bool(pe.get('verdict_sources'))
                    and all(
                        str(src).startswith('WingMPET/op27')
                        or str(src).startswith('WingMPET/op35-ground-supported')
                        for src in (pe.get('verdict_sources') or [])
                    )
                    and str(r.get('physical_verdict_hash') or '')
                ):
                    reasons.add('vertical_certificate_physical_evidence_incomplete'); continue
            current_models=set(str(x) for x in (l4.get(p,{}).get('model_hashes') or []) if x)
            current_params=set(str(x) for x in (l4.get(p,{}).get('parameter_hashes') or []) if x)
            current_climate=set(str(x) for x in (l4.get(p,{}).get('effective_climate_fingerprints') or []) if x)
            if current_models and str(r.get('model_hash') or '') not in current_models: reasons.add('certificate_model_hash_not_current'); continue
            if current_params and str(r.get('parameter_hash') or '') not in current_params: reasons.add('certificate_parameter_hash_not_current'); continue
            if current_climate and str(r.get('climate_hash') or '') not in current_climate: reasons.add('certificate_climate_fingerprint_not_current'); continue
            if not isinstance(r.get('state_envelope'),dict) or not isinstance(r.get('action_envelope'),dict): reasons.add('certificate_envelope_missing'); continue
            if str(r.get('freshness_clock') or '')!='mission_reference': reasons.add('certificate_freshness_clock_not_mission_reference'); continue
            try:
                cr=float(r.get('reference_time_s')); fr=float(r.get('freshness_s'))
                if not (math.isfinite(cr) and math.isfinite(fr) and fr>=0): raise ValueError
            except Exception:
                reasons.add('certificate_freshness_metadata_invalid'); continue
            # Before Online cycle 1 we can evaluate freshness against the verified
            # Offline phase-time support. Actual current Online reference time and
            # state/action envelopes are checked again by Layer4 find()/RTMO artifact gate.
            if refs and not any(abs(rr-cr)<=fr for rr in refs): reasons.add('certificate_not_fresh_for_phase_reference_window'); continue
            usable+=1
        rec['reusable_certificate_count']=usable
        rec['certificate_contract_ready']=usable>0
        rec['reasons']=[] if usable else sorted(reasons or {'no_verified_ff_certificate_for_phase'})
        rec['freshness_basis']='mission_reference_for_v4; legacy_wall_clock_not_promoted'
        rec['envelope_check']='v4 envelope metadata required here; current state/action containment rechecked at Layer4 reuse time'
    return out

def _live_registry_rows(registry_path:Path|None):
    obj=load_live_capability_registry(registry_path)
    if not obj:
        return None,None,None
    phases=dict(obj.get("phases") or {})
    l4={}; l5={}; boundary={}
    for p in PHASE_ORDER:
        r=dict(phases.get(p) or {})
        l4[p]={
            "verified":bool(r.get("l4_verified")),
            "feasible":bool(r.get("l4_feasible_seen")),
            "verified_cycles":list(r.get("l4_verified_cycles") or []),
            "verified_reference_times":list(r.get("l4_verified_reference_times") or []),
            "snapshot_versions":list(r.get("l4_snapshot_versions") or []),
            "snapshot_hashes":list(r.get("l4_snapshot_hashes") or []),
            "model_hashes":list(r.get("l4_model_hashes") or []),
            "parameter_hashes":list(r.get("l4_parameter_hashes") or []),
            "effective_climate_epochs":list(r.get("effective_climate_epochs") or []),
            "effective_climate_fingerprints":list(r.get("effective_climate_fingerprints") or []),
        }
        l5[p]={"admissible":bool(r.get("l5_final")),"final_cycles":list(r.get("l5_final_cycles") or []),"decision_ids":list(r.get("l5_decision_ids") or []),
               "joint_control_proof_cycles":list(r.get("joint_control_proof_cycles") or [])}
        boundary[p]={"boundary_state_supported":bool(r.get("boundary_state_supported")),"boundary_evidence":r.get("boundary_evidence") or {}}
    return l4,l5,{"registry":obj,"boundary":boundary}


def build_capability_manifest(report_root:Path, *, layer6_forward_only:bool=True, execution_overrides:dict[str,bool]|None=None,
                              certificate_bank_path:Path|None=None, require_certificate_guard:bool=False,
                              capability_registry_path:Path|None=None, require_live_registry:bool=False,
                              model_dependencies_provider=None)->CapabilityManifest:
    """Build RTMO readiness from live registries; reports are audit-only.

    With ``require_live_registry`` (the production default), Layer4 verified and
    Layer5 FINAL evidence MUST come from the TDOC runtime capability registry that is
    written directly from transaction results.  CSV/JSON reports are never promoted
    to release authority.  The live Layer4 certificate bank independently enforces
    current phase/model/epoch/freshness/envelope contract readiness.
    """
    live_l4,live_l5,live_meta=_live_registry_rows(capability_registry_path)
    if require_live_registry:
        if live_l4 is None:
            l4={p:{"verified":False,"feasible":False,"verified_cycles":[],"verified_reference_times":[]} for p in PHASE_ORDER}
            l5={p:{"admissible":False,"final_cycles":[],"joint_control_proof_cycles":[]} for p in PHASE_ORDER}
            boundary={p:{"boundary_state_supported":False,"boundary_evidence":{}} for p in PHASE_ORDER}
            registry_ready=False
        else:
            l4,l5=live_l4,live_l5
            boundary=dict((live_meta or {}).get("boundary") or {})
            registry_ready=True
    else:
        l4,l5=_offline_rows(Path(report_root))
        boundary=_boundary_evidence(Path(report_root))
        registry_ready=False
    cert_guard=_certificate_bank_guard(certificate_bank_path,l4,model_dependencies_provider) if require_certificate_guard else {p:{"certificate_contract_ready":None,"reusable_certificate_count":None,"reasons":[],"guard_required":False} for p in PHASE_ORDER}
    ff={"transition_forward","climb","cruise","turn","approach"}
    overrides=execution_overrides or {}
    phases={}
    passive={"on_ground","touchdown"}
    for p in PHASE_ORDER:
        l6=bool(overrides[p]) if p in overrides else (p in ff if layer6_forward_only else True)
        blockers=[]
        is_passive=p in passive
        binfo=boundary.get(p) or {}
        if require_live_registry and not registry_ready:
            blockers.append('live_capability_registry_unavailable')
        if is_passive:
            if not bool(binfo.get('boundary_state_supported')):
                blockers.append('passive_boundary_evidence_not_satisfied')
        else:
            if not l4[p]['verified']: blockers.append('l4_not_full_fidelity_verified_for_phase')
            if not l5[p]['admissible']: blockers.append('l5_not_final_admissible_for_phase')
            joint_cycles=list(l5[p].get('joint_control_proof_cycles') or [])
            if require_live_registry and not joint_cycles:
                blockers.append('no_same_cycle_joint_control_proof_for_phase')
            if require_certificate_guard and not bool((cert_guard.get(p) or {}).get('certificate_contract_ready')):
                blockers.append('layer4_ff_certificate_not_reusable_for_phase')
        if not l6: blockers.append('layer6_execution_profile_not_validated_for_phase')
        phases[p]={
            "semantic_role":"passive_boundary" if is_passive else "controlled_phase",
            "requires_l4_l5_control_evidence":not is_passive,
            "l4_verified":bool(l4[p]['verified']),"l4_feasible_seen":bool(l4[p]['feasible']),
            "l5_admissible":bool(l5[p]['admissible']),"execution_supported":l6,
            "boundary_state_supported":bool(binfo.get('boundary_state_supported')) if is_passive else None,
            "boundary_evidence":binfo.get('boundary_evidence') if is_passive else None,
            "verified_cycles":l4[p]['verified_cycles'],"verified_reference_times":l4[p].get('verified_reference_times',[]),"l5_final_cycles":l5[p]['final_cycles'],
            "joint_control_proof_cycles":list(l5[p].get('joint_control_proof_cycles') or []),
            "same_cycle_joint_control_proof":bool(l5[p].get('joint_control_proof_cycles')) if not is_passive else None,
            "live_registry_evidence":{
                "required":bool(require_live_registry),"available":bool(registry_ready),
                "snapshot_versions":list(l4[p].get('snapshot_versions') or []),"snapshot_hashes":list(l4[p].get('snapshot_hashes') or []),
                "model_hashes":list(l4[p].get('model_hashes') or []),"parameter_hashes":list(l4[p].get('parameter_hashes') or []),
                "effective_climate_epochs":list(l4[p].get('effective_climate_epochs') or []),"effective_climate_fingerprints":list(l4[p].get('effective_climate_fingerprints') or []),
                "l5_decision_ids":list(l5[p].get('decision_ids') or []),
            },
            "certificate_guard":cert_guard.get(p),
            "capability":not blockers,"blockers":blockers,
            "evidence_type":"LIVE_RUNTIME_BOUNDARY+live_execution_profile" if is_passive else "LIVE_RUNTIME_JOINT_CONTROL_PROOF+live_certificate_registry+live_execution_profile",
        }
    source='TDOC live capability registry + live Layer4 certificate bank + current execution profile' if require_live_registry else 'legacy report audit fallback + current execution profile'
    return CapabilityManifest(phases=phases,source=source)
