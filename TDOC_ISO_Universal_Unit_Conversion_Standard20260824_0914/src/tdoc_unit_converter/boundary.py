"""Strict, opt-in Layer3..7 boundary conversion: unit != semantics.

A numeric transform is NOT evidence of flight readiness, current-state authority,
controller identification, or native execution certification. This library has
no network, simulator or actuator side effects. Existing UnitConverter/adapters
remain backward-compatible; call this API at security-sensitive boundaries.
"""
from __future__ import annotations
import hashlib
import json
import math
from collections.abc import Mapping
from .converter import UnitConverter
from .registry import resolve
from .adapters import ned_to_enu, enu_to_ned, frd_to_flu, flu_to_frd, T_NED_ENU, T_FRD_FLU
from .errors import (ConversionError, ReferenceRequiredError, FrameMismatchError,
                     DataSemanticError, TimeReferenceError)

SCHEMA = "TDDT-ISO-CrossLayerEnvelope-v1"
FRAMES = {"NED", "ENU", "FRD", "FLU", "SCALAR"}
ROLES = {"current_state", "prior", "reference", "truth", "controller_observation", "simulation_observation", "diagnostic"}
# Exact semantics (not units) govern compatibility and physical authority.
SIGNALS = {
    "navigation_velocity": ("speed", {"NED", "ENU"}, 3),
    "navigation_position_delta": ("length", {"NED", "ENU"}, 3),
    "wind_to_velocity": ("speed", {"NED", "ENU"}, 3),
    "body_velocity": ("speed", {"FRD", "FLU"}, 3),
    "body_acceleration": ("acceleration", {"FRD", "FLU"}, 3),
    "body_angular_velocity": ("angular_speed", {"FRD", "FLU"}, 3),
    "pilot_throttle_reference": ("dimensionless", {"SCALAR"}, 1),
    "post_mixer_motor_command": ("dimensionless", {"SCALAR"}, 1),
    "jsbsim_motor_position_norm": ("dimensionless", {"SCALAR"}, 1),
    "controller_pwm": ("duration", {"SCALAR"}, 1),
    "motor_speed_measured": ("frequency", {"SCALAR"}, 1),
    "motor_speed_estimated": ("frequency", {"SCALAR"}, 1),
    "attitude_yaw": ("angle", {"SCALAR"}, 1),
    "diagnostic_residual": ("dimensionless", {"SCALAR"}, 1),
}


def _finite(v, name):
    if isinstance(v, bool): raise ConversionError(f"{name}:boolean_is_not_measurement",code="BOUNDARY_INVALID")
    try: x=float(v)
    except (ValueError,TypeError,OverflowError) as exc:
        raise ConversionError(f"{name}:non_numeric",code="BOUNDARY_INVALID") from exc
    if not math.isfinite(x): raise ConversionError(f"{name}:non_finite",code="BOUNDARY_INVALID")
    return x


def _quantity(v, n):
    if n == 1:
        if isinstance(v,(list,tuple)): raise ConversionError("scalar_value_required",code="BOUNDARY_INVALID")
        return _finite(v,'value')
    if not isinstance(v,(list,tuple)) or len(v)!=n: raise ConversionError("vector_length_mismatch",code="BOUNDARY_INVALID")
    return tuple(_finite(x,f'value[{i}]') for i,x in enumerate(v))


def _covariance(P, n):
    if not isinstance(P,(list,tuple)) or len(P)!=n or any(not isinstance(row,(list,tuple)) or len(row)!=n for row in P):
        raise ConversionError("covariance_shape_mismatch",code="UNCERTAINTY_INVALID")
    M=tuple(tuple(_finite(x,f'covariance[{i},{j}]') for j,x in enumerate(row)) for i,row in enumerate(P))
    for i in range(n):
        if M[i][i] < 0: raise ConversionError("negative_covariance",code="UNCERTAINTY_INVALID")
        for j in range(n):
            if not math.isclose(M[i][j],M[j][i],rel_tol=1e-10,abs_tol=1e-12):
                raise ConversionError("asymmetric_covariance",code="UNCERTAINTY_INVALID")
    # PSD: all principal minors >=0; n<=3 so no numerical library required.
    for i in range(n):
        for j in range(i+1,n):
            det=M[i][i]*M[j][j]-M[i][j]*M[j][i]
            if det < -1e-10*max(1,abs(M[i][i]*M[j][j])):
                raise ConversionError("non_psd_covariance",code="UNCERTAINTY_INVALID")
    if n==3:
        a,b,c=M[0]; d,e,f=M[1]; g,h,k=M[2]
        det=a*(e*k-f*h)-b*(d*k-f*g)+c*(d*h-e*g)
        scale=max(1.0,abs(a*e*k),abs(b*d*h),abs(c*f*g),abs(a*f*h),abs(b*d*k),abs(c*e*g))
        if det < -1e-10*scale: raise ConversionError("non_psd_covariance",code="UNCERTAINTY_INVALID")
    return M


def _require_text(d,k):
    v=d.get(k)
    if not isinstance(v,str) or not v.strip(): raise DataSemanticError(f"{k}:required_nonempty_string")
    return v.strip()


def _new_frame(frame, target, value, cov):
    if frame==target: return value,cov
    funcs={('NED','ENU'):ned_to_enu,('ENU','NED'):enu_to_ned,
           ('FRD','FLU'):frd_to_flu,('FLU','FRD'):flu_to_frd}
    fn=funcs.get((frame,target))
    if fn is None: raise FrameMismatchError(f"cannot_transform_frame:{frame}->{target}")
    T=T_NED_ENU if frame in ('NED','ENU') else T_FRD_FLU
    value=fn(value)
    cov=tuple(tuple(sum(T[i][a]*cov[a][b]*T[j][b] for a in range(3) for b in range(3)) for j in range(3)) for i in range(3)) if cov is not None else None
    return value,cov


def _declared_clock(obj, *, reference_time_s, max_age_s, max_future_skew_s, expected_epoch_id):
    epoch=_require_text(obj,'epoch_id')
    if expected_epoch_id is not None and epoch!=expected_epoch_id:
        raise TimeReferenceError("epoch_id_mismatch")
    clock=_require_text(obj,'clock_domain')
    if clock != 'PLANT_ELAPSED_S':
        raise TimeReferenceError("only PLANT_ELAPSED_S supported; UTC/GPS/FMI conversions require explicit adapter")
    ref=_finite(reference_time_s,'reference_time_s')
    src=_finite(obj.get('source_time_s'),'source_time_s')
    maxage=_finite(max_age_s,'max_age_s'); skew=_finite(max_future_skew_s,'max_future_skew_s')
    if maxage < 0 or skew < 0: raise TimeReferenceError('invalid_time_bound')
    if src > ref+skew: raise TimeReferenceError('future_timestamp')
    if ref-src > maxage: raise TimeReferenceError('stale_timestamp')
    return epoch, src, ref


def convert_boundary_envelope(obj: Mapping, *, target_unit: str, target_frame: str,
                              expected_semantic: str, expected_role: str,
                              reference_time_s: float, expected_epoch_id: str | None = None,
                              expected_producer: str | None = None,
                              expected_transaction_id: str | None = None,
                              expected_snapshot_hash: str | None = None,
                              max_age_s: float = 1.0, max_future_skew_s: float = .05) -> dict:
    """Validate and canonicalize a declared signal; never relabel quantity/role/domain.

    Source authority / hashes are recorded as producer claims, not authenticated by
    conversion. Value + covariance and units/frames are transformed together.
    Every normalization is one explicit boundary hop; an already normalized
    value can be checked without re-applying a scale factor.
    """
    if not isinstance(obj,Mapping) or obj.get('schema')!=SCHEMA:
        raise DataSemanticError('boundary_schema_mismatch')
    semantic=_require_text(obj,'semantic'); role=_require_text(obj,'data_role')
    if semantic not in SIGNALS: raise DataSemanticError('unregistered_signal_semantic')
    if role not in ROLES: raise DataSemanticError('unknown_data_role')
    if expected_semantic!=semantic or expected_role!=role:
        raise DataSemanticError('signal_semantic_or_role_mismatch; cannot_relabel_pilot_to_motor_or_truth_to_navigation')
    if role=='truth' and semantic.startswith('navigation_'):
        raise DataSemanticError('truth_cannot_be_navigation_current_state')
    source=_require_text(obj,'producer'); transaction=_require_text(obj,'transaction_id')
    snap=_require_text(obj,'snapshot_hash')
    if expected_producer is not None and source!=expected_producer: raise DataSemanticError('producer_mismatch')
    if expected_transaction_id is not None and transaction!=expected_transaction_id: raise DataSemanticError('transaction_mismatch')
    if expected_snapshot_hash is not None and snap!=expected_snapshot_hash: raise DataSemanticError('snapshot_mismatch')
    if len(snap)!=64 or any(k not in '0123456789abcdef' for k in snap):
        raise DataSemanticError('snapshot_hash_must_be_sha256')
    epoch, ts, ref=_declared_clock(obj,reference_time_s=reference_time_s,max_age_s=max_age_s,
                                max_future_skew_s=max_future_skew_s,expected_epoch_id=expected_epoch_id)
    q,allowed,n=SIGNALS[semantic]
    src_frame=_require_text(obj,'frame'); dst_frame=str(target_frame)
    if src_frame not in allowed or dst_frame not in allowed or src_frame not in FRAMES:
        raise FrameMismatchError('semantic_frame_mismatch')
    unit=_require_text(obj,'unit')
    source_def=resolve(unit); target_def=resolve(target_unit)
    if source_def is None or target_def is None or source_def.quantity!=q or target_def.quantity!=q:
        raise DataSemanticError('unit_quantity_incompatible_with_signal_semantic')
    val=_quantity(obj.get('value'),n)
    cov=_covariance(obj.get('covariance'),n)
    hist=obj.get('normalization_history',[])
    if not isinstance(hist,(tuple,list)) or any(not isinstance(h,str) for h in hist):
        raise DataSemanticError('normalization_history_invalid')
    # A repeated canonicalization of exactly the same units is identity only;
    # attempting to apply an earlier unit transform again is disallowed.
    this_step=f'{unit}->{target_unit}'
    if this_step in hist and unit != target_unit:
        raise DataSemanticError('repeated_unit_normalization')
    factor=source_def.scale/target_def.scale
    # affine scalar temperatures excluded by SIGNALS; unit conversion is linear
    if source_def.affine or target_def.affine: raise DataSemanticError('affine_unit_not_supported_for_this_semantic')
    val=val*factor if n==1 else tuple(v*factor for v in val)
    cov=tuple(tuple(x*factor*factor for x in row) for row in cov)
    frame_history=obj.get('frame_history',[])
    if not isinstance(frame_history,(list,tuple)) or any(not isinstance(h,str) for h in frame_history):
        raise DataSemanticError('frame_history_invalid')
    if src_frame!=dst_frame and f'{src_frame}->{dst_frame}' in frame_history:
        raise DataSemanticError('repeated_frame_transformation')
    if n==3: val,cov=_new_frame(src_frame,dst_frame,val,cov)
    elif src_frame!=dst_frame: raise FrameMismatchError('scalar_frame_mismatch')
    hist_frames=list(frame_history)
    if src_frame!=dst_frame: hist_frames.append(f'{src_frame}->{dst_frame}')
    hist_out=list(hist)
    if unit!=target_unit:hist_out.append(this_step)
    result={'schema':SCHEMA,'semantic':semantic,'data_role':role,'producer':source,
            'transaction_id':transaction,'snapshot_hash':snap,'epoch_id':epoch,
            'clock_domain':'PLANT_ELAPSED_S','source_time_s':ts,'reference_time_s':ref,
            'value':val,'unit':target_def.symbol,'frame':dst_frame,'covariance':cov,
            'normalization_history':hist_out,'frame_history':hist_frames,
            'evidence_scope':'DECLARED_SOURCE_NOT_AUTHENTICATED',
            'native_controller_certified':False,'dynamic_validation_certified':False,'flight_authorized':False}
    canonical=json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False)
    result['transform_sha256']=hashlib.sha256(canonical.encode()).hexdigest()
    return result


def compare_boundary_envelopes(prediction: Mapping, observation: Mapping, *, time_tolerance_s: float = 0.0):
    """Diagnostic predicted/realized residual for same semantic/epoch/transaction.

    Roles are intentionally different: a model prior predicts, a simulation or
    controller observation realizes; matching roles is NOT the criterion.
    No single residual can certify control authority or prove native behavior.
    """
    fields=('schema','semantic','transaction_id','snapshot_hash','epoch_id','clock_domain','unit','frame')
    for f in fields:
        if prediction.get(f)!=observation.get(f):
            raise DataSemanticError(f'unpaired_prediction_observation:{f}')
    if prediction.get('schema')!=SCHEMA or prediction.get('semantic') not in SIGNALS:
        raise DataSemanticError('unsupported_boundary_schema_or_semantic')
    if prediction.get('data_role') not in ('prior','reference'):
        raise DataSemanticError('prediction_must_be_declared_prior_or_reference')
    if observation.get('data_role') not in ('controller_observation','simulation_observation','current_state'):
        raise DataSemanticError('observation_must_be_declared_observation')
    if prediction.get('clock_domain')!='PLANT_ELAPSED_S':raise TimeReferenceError('unsupported_pair_clock')
    tol=_finite(time_tolerance_s,'time_tolerance_s')
    if tol<0:raise TimeReferenceError('negative_time_tolerance')
    a_time=_finite(prediction.get('source_time_s'),'pred_time')
    b_time=_finite(observation.get('source_time_s'),'obs_time')
    if abs(a_time-b_time)>tol:
        raise TimeReferenceError('prediction_observation_time_mismatch')
    v1=prediction.get('value');v2=observation.get('value')
    if isinstance(v1,(tuple,list)) and isinstance(v2,(tuple,list)) and len(v1)==len(v2):
        residual=tuple(_finite(b,'obs')-_finite(a,'pred') for a,b in zip(v1,v2))
    elif not isinstance(v1,(tuple,list)) and not isinstance(v2,(tuple,list)):
        residual=_finite(v2,'obs')-_finite(v1,'pred')
    else:raise DataSemanticError('pair_dimension_mismatch')
    return {'status':'PAIR_COMPARABLE_DIAGNOSTIC_ONLY','residual':residual,'semantic':prediction['semantic'],
            'unit':prediction['unit'],'frame':prediction['frame'], 'pair_delta_time_s':b_time-a_time,
            'dynamic_validation_certified':False,'native_controller_certified':False,'flight_authorized':False}
