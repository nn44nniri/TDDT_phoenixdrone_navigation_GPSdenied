"""Layer-3 Stage-3 evidence binding, NOT an authorization or controller predictor.

Explicit opt-in through context['layer3_state_authority_v1']. Legacy paths still work,
but absence of trustworthy controller evidence is reported as unobserved.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

SCHEMA = "TDDT-L3-StateAuthority-v1"
ROLES = {
    "navigation": ("MESA_NAV", "navigation_current_state"),
    "execution": ("JSBSIM_PLANT", "simulation_observation"),
    "controller": ("ARDUPILOT_FCU", "controller_observation"),
}
NAV_PROTECTED = {"position_ned_m", "ground_velocity_ned_m_s", "heading_rad"}
TRUTH_TOKENS = ("gps", "gnss", "ground_truth", "validation_truth", "sim_truth")

class StateAuthorityError(ValueError):
    pass


def _required_finite(v: Any, field: str) -> float:
    if isinstance(v, bool):
        raise StateAuthorityError(f"{field}:boolean_not_numeric")
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise StateAuthorityError(f"{field}:not_numeric") from e
    if not math.isfinite(f):
        raise StateAuthorityError(f"{field}:non_finite")
    return f


def _strict_hash(value: Any, name: str) -> str:
    s = str(value or "")
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise StateAuthorityError(f"{name}:expected_lowercase_sha256")
    return s


def _check_values(value: Any, field: str) -> None:
    if isinstance(value, bool) or value is None:
        raise StateAuthorityError(f"{field}:invalid_value")
    if isinstance(value, (int, float)):
        _required_finite(value, field)
    elif isinstance(value, (list, tuple)) and value and all(isinstance(v, (int, float)) and not isinstance(v,bool) for v in value):
        for i, x in enumerate(value): _required_finite(x, f"{field}[{i}]")
    elif isinstance(value, str) and value:
        return
    else:
        raise StateAuthorityError(f"{field}:invalid_value_shape")


def bind_state_authority(contract: Mapping[str, Any], *, reference_time_s: float,
                         max_age_s: float = 1.0, max_future_skew_s: float = .05) -> dict[str, Any]:
    """Reject mixed clocks, unlabelled frames, GPS truth masquerading as navigation,
    missing controller state. Retain controller unknown as explicit noncertification.
    Provenance claims remain claims, never cryptographic verification of a producer.
    """
    if not isinstance(contract, Mapping) or contract.get('schema') != SCHEMA:
        raise StateAuthorityError('schema_missing_or_mismatch')
    ref = _required_finite(reference_time_s, 'reference_time_s')
    if not isinstance(contract.get('snapshot_id'), str) or not contract['snapshot_id'].strip():
        raise StateAuthorityError('snapshot_id_missing')
    epoch = contract.get('epoch_id')
    if not isinstance(epoch, str) or not epoch.strip():
        raise StateAuthorityError('epoch_id_missing')
    states = contract.get('states')
    if not isinstance(states, Mapping) or 'navigation' not in states:
        raise StateAuthorityError('navigation_evidence_required')
    if set(states) - set(ROLES):
        raise StateAuthorityError('unknown_state_domain')
    max_age = _required_finite(max_age_s, 'max_age_s')
    max_skew = _required_finite(max_future_skew_s, 'max_future_skew_s')
    if max_age < 0 or max_skew < 0: raise StateAuthorityError('negative_time_bound')
    result = {'schema': SCHEMA, 'snapshot_id': contract['snapshot_id'], 'epoch_id': epoch,
              'reference_time_s': ref, 'navigation_authority': 'MESA_NAV', 'domains': {},
              'controller_prediction_eligible': False, 'native_controller_certified': False,
              'flight_authorized': False, 'role': 'diagnostic_state_binding_only'}
    for domain in ('navigation', 'execution', 'controller'):
        data = states.get(domain)
        if data is None:
            result['domains'][domain] = {'status': 'UNOBSERVED', 'authoritative': False, 'values': {}}
            continue
        if not isinstance(data, Mapping): raise StateAuthorityError(f'{domain}:invalid_record')
        producer, role = ROLES[domain]
        if data.get('producer') != producer or data.get('role') != role:
            raise StateAuthorityError(f'{domain}:source_authority_mismatch')
        if data.get('epoch_id') != epoch: raise StateAuthorityError(f'{domain}:mixed_epoch')
        if data.get('snapshot_id') != contract['snapshot_id']:
            raise StateAuthorityError(f'{domain}:mixed_snapshot_id')
        sha = _strict_hash(data.get('source_sha256'), f'{domain}.source_sha256')
        ts = _required_finite(data.get('source_time_s'), f'{domain}.source_time_s')
        age = ref - ts
        if age < -max_skew: raise StateAuthorityError(f'{domain}:future_timestamp')
        if age > max_age: raise StateAuthorityError(f'{domain}:stale_timestamp')
        signals = data.get('signals')
        if not isinstance(signals, Mapping) or not signals:
            raise StateAuthorityError(f'{domain}:signals_missing')
        output = {}
        for key, sig in signals.items():
            if not isinstance(key,str) or not key or not isinstance(sig, Mapping):
                raise StateAuthorityError(f'{domain}:signal_invalid')
            if domain == 'navigation' and any(t in key.lower() for t in TRUTH_TOKENS):
                raise StateAuthorityError('gps_truth_cannot_override_navigation_authority')
            if domain == 'controller' and ('rpm' in key.lower() or 'omega' in key.lower()):
                # Native Phoenix model has no independent motor RPM sensor.
                raise StateAuthorityError('controller_omega_unverified')
            unit, frame, semantic = (sig.get(k) for k in ('unit','frame','semantic'))
            if not all(isinstance(x,str) and x.strip() for x in (unit,frame,semantic)):
                raise StateAuthorityError(f'{domain}.{key}:unit_frame_semantic_required')
            if sig.get('semantic') != key: raise StateAuthorityError(f'{domain}.{key}:semantic_mismatch')
            if domain == 'navigation' and frame not in ('NED','SCALAR','WGS84'):
                raise StateAuthorityError(f'{domain}.{key}:frame_mismatch')
            if domain == 'execution' and frame not in ('NED','FRD','SCALAR'):
                raise StateAuthorityError(f'{domain}.{key}:frame_mismatch')
            if domain == 'controller' and frame not in ('SCALAR','FRD'):
                raise StateAuthorityError(f'{domain}.{key}:frame_mismatch')
            if domain == 'navigation' and key in ('ground_velocity_ned_m_s','position_ned_m') and (frame,unit)!=('NED','m/s' if 'velocity' in key else 'm'):
                raise StateAuthorityError(f'{domain}.{key}:unit_frame_mismatch')
            if key.endswith('_norm') and unit != '1':
                raise StateAuthorityError(f'{domain}.{key}:unit_mismatch')
            _check_values(sig.get('value'), f'{domain}.{key}.value')
            sigma = _required_finite(sig.get('uncertainty'),f'{domain}.{key}.uncertainty')
            if sigma < 0: raise StateAuthorityError(f'{domain}.{key}:negative_uncertainty')
            if sig.get('data_role') != role:
                raise StateAuthorityError(f'{domain}.{key}:role_mismatch')
            output[key] = {'value':sig['value'], 'unit':unit,'frame':frame,'semantic':key,
                           'uncertainty':sigma,'data_role':role}
        result['domains'][domain]={'status':'SOURCE_DECLARED_UNVERIFIED','authoritative':False, 'declared_navigation_authority':domain=='navigation',
                                  'producer':producer,'source_sha256':sha,'source_time_s':ts,
                                  'age_s':max(0.0,age),'values':output}
    # A supplied, source-labelled state is evidence of values, not of controller
    # identification, spool estimator fidelity, FF certificates, or authorization.
    canonical=json.dumps({k:v for k,v in result.items() if k!='binding_sha256'},sort_keys=True,
                         ensure_ascii=False,allow_nan=False,separators=(',',':'))
    result['binding_sha256']=hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return result
