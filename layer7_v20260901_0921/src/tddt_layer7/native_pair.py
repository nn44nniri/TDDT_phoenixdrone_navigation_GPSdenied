"""Stage-6 native prediction/execution pairing: evidence is NOT a flight certificate.

An asserted provenance flag is never independent source authentication. Positive
comparison is diagnostic only until TDOC validates native producers/certificates.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any
import math

SCHEMA = 'TDDT-L7-NativePair-v1'
NATIVE_MODES={'QHOVER','QSTABILIZE','QLOITER','QLAND','QRTL','FBWA','FBWB'}
BIND_KEYS=('transaction_id','candidate_id','snapshot_hash','command_id','flight_mode')
STATES_FORBIDDEN=('gps_truth','gnss_truth','ground_truth','sim_truth')


def is_native_request(inp: Any) -> bool:
    es=getattr(inp.execution,'provenance',{}) or {}
    ps=getattr(inp.prediction,'provenance',{}) or {}
    ns=getattr(inp.network,'metadata',{}) or {}
    for meta in (es,ps,ns):
        if meta.get('native_pilot_execution_requested') is True or 'native_controller_evidence' in meta:
            return True
        if str(meta.get('command_domain') or meta.get('control_domain') or '').lower() in ('pilot','pilot_reference','native_pilot','layer6_pilot'):
            return True
        if str(meta.get('flight_mode') or '').upper() in NATIVE_MODES:
            return True
        if str(meta.get('verification_scope') or '').upper().startswith('NATIVE_'):
            return True
        if 'online_episode_id' in meta or 'execution_transaction_id' in meta:
            return True
    for name, sem in {**inp.execution.output_semantics, **inp.prediction.output_semantics}.items():
        if str(name).lower() in ('pilot_control_effort','motor_pwm_as_rpm') or str(sem.get('quantity','')).lower()=='pilot_control_effort':
            return True
    return False


def _scalar_number(value: Any) -> float | None:
    if isinstance(value,bool): return None
    try:
        v=float(value)
    except (ValueError,TypeError,OverflowError): return None
    return v if math.isfinite(v) else None


def _semantic(meta: Any) -> tuple[str,str,str] | None:
    if not isinstance(meta,dict): return None
    result=(str(meta.get('quantity','')).strip(),str(meta.get('unit','')).strip(),str(meta.get('frame','')).strip())
    return result if all(result) else None


@dataclass(frozen=True)
class NativePairResult:
    required: bool
    comparable: bool
    reasons: list[str]
    state_residual: dict[str,float]
    normalized_output_residual: float | None
    native_controller_certified: bool = False
    flight_authorized: bool = False
    scope: str = 'DIAGNOSTIC_PAIR_ONLY_UNCERTIFIED'

    def to_dict(self): return {'schema':SCHEMA,**asdict(self)}


def check_pair(inp: Any, plant_tolerance_s: float=.05, *, model_diagnostic: bool=False) -> NativePairResult:
    if not is_native_request(inp):
        return NativePairResult(False,False,['native_scope_not_requested'],{},None,scope='LEGACY_MODEL_OR_DIAGNOSTIC_ONLY')
    e,p=inp.execution,inp.prediction
    ep=e.provenance or {}; pp=p.provenance or {}; reasons=[]
    for key in BIND_KEYS:
        a,b=ep.get(key),pp.get(key)
        if not isinstance(a,str) or not a.strip() or not isinstance(b,str) or not b.strip():
            reasons.append('missing_binding:'+key)
        elif a!=b: reasons.append('binding_mismatch:'+key)
    if ep.get('transaction_id')!=inp.transaction_id or not inp.transaction_id:
        reasons.append('outer_transaction_mismatch')
    if ep.get('snapshot_hash')!=getattr(inp.network,'content_hash','') or not getattr(inp.network,'content_hash',''):
        reasons.append('network_snapshot_hash_mismatch')
    if ep.get('command_id')!=e.command_id or not e.command_id:
        reasons.append('execution_command_id_mismatch')
    if not isinstance(e.actuator_feedback_id,str) or not e.actuator_feedback_id.strip():
        reasons.append('missing_actual_actuator_feedback_id')
    kinds=('native_controller_state','trans_domain_model_state') if model_diagnostic else ('native_controller_state',)
    if ep.get('observation_kind')!='physical_state' or pp.get('prediction_kind') not in kinds:
        reasons.append('state_pair_source_kind_mismatch')
    et,pt=(_scalar_number(ep.get('plant_time_s')),_scalar_number(pp.get('plant_time_s')))
    if et is None or pt is None or abs(et-pt)>plant_tolerance_s:
        reasons.append('plant_time_mismatch_or_missing')
    target=_scalar_number(pp.get('target_plant_timestamp'))
    bounds=pp.get('prediction_target_plant_time_bounds_s')
    if model_diagnostic and isinstance(bounds,dict):
        lo,hi=(_scalar_number(bounds.get(k)) for k in ('lower_s','upper_s'))
        duration=ep.get('execution_duration_bounds_s') or {}
        dl,du=(_scalar_number(duration.get(k)) for k in ('lower_s','upper_s'))
        if (lo is None or hi is None or not 0<=lo<=hi or et is None
            or max(lo-et,et-hi,0.)>plant_tolerance_s
            or dl is None or du is None or not 0<dl<=du
            or duration!=pp.get('execution_duration_bounds_s')
            or target is not None):
            reasons.append('bounded_target_time_mismatch_or_missing')
    elif target is None or et is None or abs(target-et)>plant_tolerance_s:
        reasons.append('target_plant_time_mismatch_or_missing')
    # Executed pilot command, post-mixer motor command, and motor RPM are
    # distinct physical quantities. State semantics are mandatory here.
    x=e.x_obs; xhat=p.x_hat_td
    if not x or not xhat or set(x)!=set(xhat):
        reasons.append('missing_or_partial_state_pair')
    esm=ep.get('state_semantics') or {}; psm=pp.get('state_semantics') or {}
    if not isinstance(esm,dict):
        reasons.append('invalid_execution_state_semantic_container');esm={}
    if not isinstance(psm,dict):
        reasons.append('invalid_prediction_state_semantic_container');psm={}
    residual={}
    for key in sorted(set(x)&set(xhat)):
        a,b=_semantic(esm.get(key)),_semantic(psm.get(key))
        if a is None or b is None: reasons.append('missing_state_semantics:'+key);continue
        if a!=b: reasons.append('state_semantic_mismatch:'+key);continue
        if any(k in key.lower() for k in STATES_FORBIDDEN):
            reasons.append('validation_truth_not_navigation_state:'+key);continue
        if ('rpm' in key.lower() or 'omega_l' in key.lower() or 'omega_r' in key.lower()):
            if ep.get('motor_speed_source') not in ('independent_native_property','independent_sensor'):
                reasons.append('motor_speed_not_independently_observed:'+key);continue
        av,bv=_scalar_number(x[key]),_scalar_number(xhat[key])
        if av is None or bv is None: reasons.append('nonfinite_state:'+key);continue
        d=av-bv
        if a[0].lower() in ('angle','heading','yaw','roll','pitch') and a[1]=='rad':
            d=(d+math.pi)%(2*math.pi)-math.pi
        residual[key]=d
    if set(e.y_real)!=set(p.y_hat_td) or not e.y_real:
        reasons.append('missing_or_partial_output_pair')
    y2=0.0
    for key in sorted(set(e.y_real)&set(p.y_hat_td)):
        a=_semantic(e.output_semantics.get(key));b=_semantic(p.output_semantics.get(key))
        if a is None or b is None or a!=b:
            reasons.append('output_semantic_mismatch:'+key);continue
        if a[0].lower() in ('pilot_control_effort','pilot_throttle','motor_pwm_as_rpm'):
            reasons.append('pilot_effort_not_physical_state:'+key);continue
        av,bv=_scalar_number(e.y_real[key]),_scalar_number(p.y_hat_td[key])
        sig=_scalar_number(p.sigma_pred.get(key))
        if av is None or bv is None or sig is None or sig<=0:
            reasons.append('invalid_output_or_covariance:'+key);continue
        y2+=(av-bv)**2/sig
    if reasons:
        return NativePairResult(True,False,sorted(set(reasons)),{},None)
    return NativePairResult(True,True,[],residual,math.sqrt(y2),
        scope='MODEL_PREDICTION_NATIVE_EXECUTION_DIAGNOSTIC' if model_diagnostic else 'DIAGNOSTIC_PAIR_ONLY_UNCERTIFIED')
