"""R7.01 structural boundary for an isolated native predictor, not a predictor.

Validation grants no model qualification, certificate, FINAL or flight authority.
No imports from Layer6, process launch, clock mutation or mission-state writes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any, Mapping, Protocol

REVISION = "phoenix-native-provider-r7.01-v1"
REQUEST_SCHEMA = "TDDT-native-provider-request-v1"
PREDICTION_SCHEMA = "TDDT-native-provider-prediction-v1"
CONTRACT_SCHEMA = "TDDT-native-provider-contract-v1"
DOMAIN = "LAYER6_PILOT_NORMALIZED"
MODES = ("QSTABILIZE", "QHOVER", "QLOITER", "FBWA")
DISCRETE = ("ARM", "DISARM", "THROTTLE_ZERO", "NEUTRAL_ATTITUDE")
AXES = ("aileron", "elevator", "throttle", "rudder")
HASH_FIELDS = ("snapshot_hash", "candidate_hash", "model_id", "binding_hash",
               "handoff_hash", "forcing_sha256")
BACKEND_HASH_FIELDS = ("arduplane_sha256", "jsbsim_sha256", "aircraft_sha256",
                       "defaults_sha256", "gui_config_sha256", "execution_path_sha256",
                       "parameter_snapshot_sha256")
REQUIRED_PARAMETERS = tuple("SERVO%d_%s" % (i, suffix) for i in range(1,5)
                            for suffix in ("FUNCTION", "MIN", "TRIM", "MAX", "REVERSED")) + (
    "RC_OVERRIDE_TIME", "FS_GCS_ENABL", "AHRS_EKF_TYPE", "Q_ENABLE", "Q_TAILSIT_ENABLE",
    "RCMAP_ROLL", "RCMAP_PITCH", "RCMAP_THROTTLE", "RCMAP_YAW")
# Fixed physical vectors are deliberately distinct from unobservable internal state.
VECTOR_SPECS = {
    "navigation": ("NED_AND_BODY_FRD", ["pn", "pe", "pd", "vn", "ve", "vd",
                    "qw", "qx", "qy", "qz", "p", "q", "r"],
                   ["m"]*3+["m/s"]*3+["1"]*4+["rad/s"]*3),
    "attitude_body": ("BODY_FRD_TO_NED", ["qw", "qx", "qy", "qz", "p", "q", "r"],
                      ["1"]*4+["rad/s"]*3),
    "previous_pilot": (DOMAIN, list(AXES), ["1"]*4),
    "motor_speed": ("ROTOR_SHAFT", ["left", "right"], ["rad/s"]*2),
    "manual_control": ("MAVLINK_MANUAL_CONTROL", ["x", "y", "z", "r"], ["1"]*4),
    "post_mixer_pwm": ("ACTUATOR_CHANNELS", ["servo1", "servo2", "servo3", "servo4"], ["us"]*4),
    "plant_state": ("NED_AND_BODY_FRD", ["pn", "pe", "pd", "vn", "ve", "vd",
                     "qw", "qx", "qy", "qz", "p", "q", "r"],
                    ["m"]*3+["m/s"]*3+["1"]*4+["rad/s"]*3),
}
INTERNAL_FRAMES = {"controller": "ARDUPILOT_INTERNAL", "mixer": "ARDUPILOT_INTERNAL",
                   "spool": "ARDUPILOT_INTERNAL", "actuator_dynamics": "ACTUATOR_INTERNAL",
                   "rc_override": "ARDUPILOT_RC_INTERNAL"}
INITIAL_FIELDS = ("navigation", "attitude_body", "previous_pilot", "controller", "mixer",
                  "spool", "actuator_dynamics", "rc_override", "motor_speed")
OUTPUT_FIELDS = ("manual_control", "post_mixer_pwm", "plant_state", "controller",
                 "spool", "motor_speed")
SOURCE_KINDS = ("MESA_L3", "FCU_TELEMETRY", "NATIVE_INTERNAL", "NATIVE_REPLAY",
                "MEASURED", "PWM_DERIVED", "UNAVAILABLE")


class NativeControllerProvider(Protocol):
    """Future R7.02–R7.06 implementation; never call the active mission executor."""

    def predict(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def contract() -> dict:
    """Serializable normative descriptor, shipped in the Layer4 wheel as code."""
    return deepcopy({
        "schema": CONTRACT_SCHEMA, "revision": REVISION,
        "request_schema": REQUEST_SCHEMA, "prediction_schema": PREDICTION_SCHEMA,
        "api": "NativeControllerProvider.predict(request) -> prediction",
        "request_fields": ["schema","request_id","context","backend","pilot_plan","timing","initial_state","isolation"],
        "context_fields": [*HASH_FIELDS,"snapshot_id","snapshot_version","candidate_id","phase","native_session_id","telemetry_session_id"],
        "backend_fields": [*BACKEND_HASH_FIELDS,"parameters","gui_gains","initial_mode","armed"],
        "timing_fields": ["plant_clock","clock_mapping_sha256","plant_start_bounds_s","prediction_dt_s","commit_segment_s","close_guard_s",
                          "execution_rate_hz","gui_rate_hz","gui_ttl_s","admission_deadline_unix_s","monotonic_domain_id"],
        "state_field_keys": ["status","value","components","units","frame","source","uncertainty"],
        "source_field_keys": ["kind","artifact_sha256","source_time_s","clock_domain"],
        "prediction_fields": ["schema","request_sha256","status","reason","isolation","samples","uncertainty"],
        "sample_fields": ["offset_s","mode","channels"],
        "implemented_provider": False, "qualification_granted": False,
        "flight_authorized": False, "offline_role": "prior_only_never_current_state",
        "domain": DOMAIN, "pilot_order": AXES,
        "pilot_ranges": [[-1, 1], [-1, 1], [0, 1], [-1, 1]],
        "requested_modes": MODES, "initial_modes": ("MANUAL", *MODES), "discrete": DISCRETE,
        "mapping": {
            "gui": "roll=a; pitch=e; throttle=t; yaw=r; virtual input bypasses gamepad expo/inversion",
            "manual_control": "x=round(1000*clamp(e*max_pitch)); y=round(1000*clamp(a*max_roll)); z=round(1000*t); r=round(1000*clamp(r*max_yaw)); buttons=0",
            "rounding": "Python round; clamp signed axes to [-1,1]",
            "FCU": "Plane.manual_override: roll=packet.y; pitch=packet.x with reversed=true; throttle=packet.z; rudder=packet.r",
            "post_mixer": "SERVO1/2 elevon, SERVO3/4 motor; exact FCU functions, trims, min/max and reversal from parameter snapshot",
            "forbidden_equivalence": "pilot != PWM != mechanical angle != spool != RPM; PWM alone proves none of the latter",
            "mode_discrete": "executor dispatches changed mode, then discrete actions, then continuous tick; action receipt is not mode/ARM completion",
        },
        "identity": {"context_hash_fields": HASH_FIELDS, "backend_hash_fields": BACKEND_HASH_FIELDS,
                     "minimum_required_parameters": REQUIRED_PARAMETERS,
                     "parameters": "complete finite FCU parameter map; digest(parameters) == parameter_snapshot_sha256",
                     "comparison": "exact hashes rechecked by consumer; structural validation is not provenance verification"},
        "state": {"initial_fields": INITIAL_FIELDS, "output_fields": OUTPUT_FIELDS,
                  "vectors": VECTOR_SPECS, "internal_frames": INTERNAL_FRAMES,
                  "controller_components": "integrators, filters, mode/transition timers and estimator/controller state; name and unit each observed component",
                  "spool_components": "desired/current spool, ramp/timers/arming state; do not infer from disarmed heartbeat or PWM",
                  "unknown": "value=null; source=UNAVAILABLE; explicit unbounded uncertainty and reason; never zero-fill",
                  "known": "observed/estimated value vector with provenance and bounded interval or explicit unbounded uncertainty",
                  "navigation": "MESA/L3 estimate only; FCU/JSBSim truth is evaluation-only and cannot replace navigation"},
        "timing": {
            "plant_clock": "FCU_BOOT seconds; plant_start_bounds_s is an interval, never an invented exact send time",
            "mapping": "clock_mapping_sha256 binds explicit native/FCU clock mapping; do not assume JSBSim clock equals FCU_BOOT",
            "grid": "N commands; prediction_dt_s each; horizon=N*dt; samples offsets 0,dt,...,N*dt",
            "commit": "0 < close_guard_s < commit_segment_s <= prediction_dt_s; commit is only first move",
            "deadline": "UNIX_WALL admission deadline distinct from FCU_BOOT exposure and HOST_MONOTONIC TTL",
            "cadence": "execution_rate_hz and gui_rate_hz explicit; gui_ttl_s=max(.15,3/execution_rate_hz); 30Hz is not 10Hz control",
            "post_commit": "latch stop/cancel prevents replay; FCU override timeout, failsafe and mode still govern plant; no physical HOLD implied",
        },
        "isolation": "ISOLATED_REPLAY_ONLY; branch differs from mission session; restore/reference hash required; active mission never advanced",
        "response": {"statuses": ["PREDICTED", "UNAVAILABLE"],
                     "binding": "request_sha256 covers complete immutable request; plan/state/parameters/env cannot be substituted",
                     "evidence": "trace SHA256 + isolated branch session + initial reference hash; no ACK-as-prediction",
                     "uncertainty": "any unbounded initial state requires unbounded output uncertainty; bounded forecasts require independent R7.04/05 qualification",
                     "comparability": "same offsets, mode, units, frames and post-mixer channels; motor speed unavailable unless native/measured source exists",
                     "unavailable": "reason required; samples=[]; no synthetic nominal response"},
        "acceptance_boundary": "R7.01 validates structure only; replay R7.02; prediction R7.03; tolerance/validation R7.04; certificate R7.05; L4 integration R7.06; FINAL R7.07",
    })


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _hash(x):
    return isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) is not None


def _text(x):
    return isinstance(x, str) and bool(x.strip()) and len(x) <= 256


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _keys(obj, keys, name):
    _require(isinstance(obj, dict) and set(obj) == set(keys), name+":fields")


def _vector(values, n):
    return isinstance(values, list) and len(values) == n and all(_finite(x) for x in values)


def _state(name, field):
    _keys(field, ("status", "value", "components", "units", "frame", "source", "uncertainty"), name)
    status=field['status']; components=field['components']; units=field['units']
    _require(status in ('observed', 'estimated', 'unknown'), name+":status")
    _require(isinstance(components, list) and all(_text(x) for x in components)
             and len(set(components)) == len(components), name+":components")
    _require(isinstance(units, list) and len(units) == len(components) and all(_text(x) for x in units), name+":units")
    if name in VECTOR_SPECS:
        frame, expected, expected_units=VECTOR_SPECS[name]
        _require((field['frame'], components, units) == (frame, expected, expected_units), name+":frame_units_order")
    else:
        _require(field['frame'] == INTERNAL_FRAMES[name], name+":frame")
    source=field['source']
    _keys(source, ('kind', 'artifact_sha256', 'source_time_s', 'clock_domain'), name+":source")
    _require(source['kind'] in SOURCE_KINDS, name+":source_kind")
    uncertainty=field['uncertainty']
    _require(isinstance(uncertainty, dict) and uncertainty.get('kind') in ('bounded', 'unbounded'), name+":uncertainty")
    if uncertainty['kind'] == 'unbounded':
        _keys(uncertainty, ('kind', 'reason'), name+":unbounded")
        _require(_text(uncertainty['reason']), name+":uncertainty_reason")
    else:
        _keys(uncertainty, ('kind', 'lower', 'upper', 'evidence_sha256'), name+":bounded")
        lo, hi=uncertainty['lower'], uncertainty['upper']
        _require(bool(components) and _vector(lo,len(components)) and _vector(hi,len(components))
                 and all(a <= b for a,b in zip(lo,hi)) and _hash(uncertainty['evidence_sha256']), name+":bounds")
    if status == 'unknown':
        _require(field['value'] is None and source == {'kind':'UNAVAILABLE','artifact_sha256':None,
                 'source_time_s':None,'clock_domain':None} and uncertainty['kind'] == 'unbounded', name+":unknown_must_not_be_invented")
    else:
        _require(bool(components) and _vector(field['value'],len(components)), name+":value")
        _require(source['kind'] != 'UNAVAILABLE' and _hash(source['artifact_sha256'])
                 and _finite(source['source_time_s']) and source['source_time_s'] >= 0
                 and source['clock_domain'] == 'FCU_BOOT', name+":provenance")
        if uncertainty['kind'] == 'bounded':
            _require(all(a <= x <= b for a,x,b in zip(uncertainty['lower'],field['value'],uncertainty['upper'])), name+":value_outside_bounds")
        if name in ('navigation', 'attitude_body', 'plant_state'):
            q=field['value'][6:10] if name != 'attitude_body' else field['value'][:4]
            _require(abs(sum(x*x for x in q)-1) <= .002, name+":quaternion_not_unit")
        if name == 'navigation':
            _require(source['kind'] == 'MESA_L3', name+":truth_is_not_navigation")
        if name == 'motor_speed':
            _require(source['kind'] in ('NATIVE_INTERNAL','NATIVE_REPLAY','MEASURED')
                     and all(x >= 0 for x in field['value']), name+":PWM_is_not_RPM")
        if name in INTERNAL_FRAMES:
            _require(source['kind'] in ('NATIVE_INTERNAL','NATIVE_REPLAY','MEASURED'), name+":internal_state_not_observed_by_PWM_or_standard_telemetry")
        if name == 'manual_control':
            v=field['value']
            _require(all(int(x) == x for x in v) and all(-1000 <= v[i] <= 1000 for i in (0,1,3))
                     and 0 <= v[2] <= 1000, name+":wire_domain")
        if name == 'post_mixer_pwm':
            _require(all(0 < x < 65535 for x in field['value']), name+":pulse_domain")
        if name == 'previous_pilot':
            _require(all(lo <= x <= hi for x,(lo,hi) in zip(field['value'],((-1,1),(-1,1),(0,1),(-1,1)))), name+":pilot_domain")
    return uncertainty['kind'] == 'unbounded'


def unknown_state(name, reason):
    """Explicit absence, never a synthetic zero or a fictitious state estimate."""
    if name in VECTOR_SPECS:
        frame,components,units=deepcopy(VECTOR_SPECS[name])
    else:
        frame,components,units=INTERNAL_FRAMES[name],[],[]
    result={'status':'unknown','value':None,'components':components,'units':units,'frame':frame,
            'source':{'kind':'UNAVAILABLE','artifact_sha256':None,'source_time_s':None,'clock_domain':None},
            'uncertainty':{'kind':'unbounded','reason':reason}}
    _state(name,result)
    return result


def pilot_to_manual(u_cmd, gains):
    """Pure Layer6 GUI→MANUAL_CONTROL projection; never PWM/plant prediction."""
    _require(_vector(u_cmd,4) and all(lo <= x <= hi for x,(lo,hi) in
             zip(u_cmd,((-1,1),(-1,1),(0,1),(-1,1)))), 'pilot_domain')
    _keys(gains, ('max_pitch','max_roll','max_yaw'), 'gui_gains')
    _require(all(_finite(x) and x >= 0 for x in gains.values()), 'gui_gains:range')
    clamp=lambda x:max(-1,min(1,x))
    a,e,t,r=u_cmd
    return {'x':int(round(1000*clamp(e*gains['max_pitch']))),
            'y':int(round(1000*clamp(a*gains['max_roll']))), 'z':int(round(1000*t)),
            'r':int(round(1000*clamp(r*gains['max_yaw']))), 'buttons':0}


def _request(request):
    _keys(request, ('schema','request_id','context','backend','pilot_plan','timing','initial_state','isolation'), 'request')
    _require(request['schema'] == REQUEST_SCHEMA and _text(request['request_id']), 'request:schema_id')
    c=request['context']
    _keys(c, (*HASH_FIELDS,'snapshot_id','snapshot_version','candidate_id','phase','native_session_id','telemetry_session_id'), 'context')
    _require(all(_hash(c[k]) for k in HASH_FIELDS) and all(_text(c[k]) for k in
             ('snapshot_id','snapshot_version','candidate_id','phase','native_session_id','telemetry_session_id')), 'context:identity')
    backend=request['backend']
    _keys(backend, (*BACKEND_HASH_FIELDS,'parameters','gui_gains','initial_mode','armed'), 'backend')
    _require(all(_hash(backend[k]) for k in BACKEND_HASH_FIELDS), 'backend:identity')
    parameters=backend['parameters']
    _require(isinstance(parameters,dict) and bool(parameters) and all(_text(k) and _finite(v) for k,v in parameters.items()), 'backend:parameters')
    _require(digest(parameters) == backend['parameter_snapshot_sha256'], 'backend:parameter_hash')
    _require(all(k in parameters for k in REQUIRED_PARAMETERS), 'backend:incomplete_critical_parameters')
    for i, function in enumerate((77,78,73,74),1):
        lo,trim,hi=(parameters['SERVO%d_%s'%(i,k)] for k in ('MIN','TRIM','MAX'))
        _require(parameters['SERVO%d_FUNCTION'%i] == function and lo <= trim <= hi and lo < hi,
                 'backend:phoenix_mixer_parameter_contract')
    _require(backend['initial_mode'] in ('MANUAL',*MODES) and type(backend['armed']) is bool, 'backend:mode_armed')
    t=request['timing']
    _keys(t, ('plant_clock','clock_mapping_sha256','plant_start_bounds_s','prediction_dt_s','commit_segment_s','close_guard_s',
              'execution_rate_hz','gui_rate_hz','gui_ttl_s','admission_deadline_unix_s','monotonic_domain_id'), 'timing')
    _require(t['plant_clock'] == 'FCU_BOOT' and _text(t['monotonic_domain_id']), 'timing:clock')
    _require(_hash(t['clock_mapping_sha256']), 'timing:clock_mapping')
    bounds=t['plant_start_bounds_s']
    _require(_vector(bounds,2) and 0 <= bounds[0] <= bounds[1], 'timing:start_bounds')
    _require(all(_finite(t[k]) and t[k] > 0 for k in ('prediction_dt_s','commit_segment_s','close_guard_s',
             'execution_rate_hz','gui_rate_hz','gui_ttl_s','admission_deadline_unix_s')), 'timing:positive_finite')
    _require(0 < t['close_guard_s'] < t['commit_segment_s'] <= t['prediction_dt_s'], 'timing:commit')
    _require(math.isclose(t['gui_ttl_s'],max(.15,3/t['execution_rate_hz']),rel_tol=0,abs_tol=1e-12), 'timing:GUI_TTL')
    plan=request['pilot_plan']
    _require(isinstance(plan,list) and 0 < len(plan) <= 10000, 'pilot_plan:empty_or_unbounded')
    _require(math.isfinite(len(plan)*t['prediction_dt_s']), 'pilot_plan:forecast_horizon_overflow')
    for command in plan:
        _keys(command, ('domain','u_cmd','mode','discrete'), 'pilot_plan:command')
        _require(command['domain'] == DOMAIN, 'pilot_plan:domain')
        pilot_to_manual(command['u_cmd'],backend['gui_gains'])
        _require(command['mode'] is None or command['mode'] in MODES, 'pilot_plan:mode')
        _require(isinstance(command['discrete'],list) and all(x in DISCRETE for x in command['discrete']), 'pilot_plan:discrete')
        _require(not {'ARM','DISARM'}.issubset(command['discrete']), 'pilot_plan:contradictory_arm')
    state=request['initial_state'];_keys(state,INITIAL_FIELDS,'initial_state')
    unbounded=[name for name in INITIAL_FIELDS if _state(name,state[name])]
    isolation=request['isolation']
    _keys(isolation, ('policy','initial_reference_sha256'), 'isolation')
    _require(isolation['policy'] == 'ISOLATED_REPLAY_ONLY' and _hash(isolation['initial_reference_sha256']), 'isolation:policy_reference')
    return unbounded


def _result(valid, errors, unbounded=()):
    return {'contract_valid':valid, 'errors':errors, 'unbounded_initial_fields':list(unbounded),
            'provider_available':False, 'native_controller_certified':False,
            'flight_authorized':False, 'qualification_required':True, 'revision':REVISION}


def validate_request(request):
    try:
        unbounded=_request(request)
        return _result(True,[],unbounded)
    except (ValueError,TypeError,KeyError,OverflowError) as exc:
        return _result(False,[str(exc)])


def validate_prediction(request, prediction):
    """Shape/correlation only. A passing document is never a certificate."""
    try:
        unbounded=_request(request)
        _keys(prediction, ('schema','request_sha256','status','reason','isolation','samples','uncertainty'), 'prediction')
        _require(prediction['schema'] == PREDICTION_SCHEMA and prediction['request_sha256'] == digest(request), 'prediction:request_binding')
        _require(prediction['status'] in ('PREDICTED','UNAVAILABLE') and _text(prediction['reason']), 'prediction:status_reason')
        isolation=prediction['isolation']
        _keys(isolation, ('branch_session_id','initial_reference_sha256','active_mission_advanced','trace_sha256'), 'prediction:isolation')
        _require(isolation['active_mission_advanced'] is False
                 and isolation['initial_reference_sha256'] == request['isolation']['initial_reference_sha256'], 'prediction:not_isolated_or_unbound')
        if prediction['status'] == 'UNAVAILABLE' and isolation['branch_session_id'] is None:
            _require(isolation['trace_sha256'] is None, 'prediction:unavailable_fake_trace')
        else:
            _require(_text(isolation['branch_session_id']) and isolation['branch_session_id'] != request['context']['native_session_id']
                     and _hash(isolation['trace_sha256']), 'prediction:not_isolated_or_unbound')
        u=prediction['uncertainty'];_keys(u, ('kind','reason'), 'prediction:uncertainty')
        _require(u['kind'] in ('bounded','unbounded') and _text(u['reason']), 'prediction:uncertainty')
        _require(not unbounded or u['kind'] == 'unbounded', 'prediction:unknown_state_hidden')
        rows=prediction['samples'];_require(isinstance(rows,list), 'prediction:samples')
        if prediction['status'] == 'UNAVAILABLE':
            _require(rows == [] and u['kind'] == 'unbounded', 'prediction:unavailable_must_not_fabricate_samples')
        else:
            _require(len(rows) == len(request['pilot_plan'])+1, 'prediction:grid_length')
            for i,row in enumerate(rows):
                _keys(row, ('offset_s','mode','channels'), 'prediction:sample')
                _require(_finite(row['offset_s']) and math.isclose(row['offset_s'],i*request['timing']['prediction_dt_s'],rel_tol=0,abs_tol=1e-9), 'prediction:offset')
                _require(row['mode'] in ('MANUAL',*MODES), 'prediction:mode')
                _keys(row['channels'],OUTPUT_FIELDS,'prediction:channels')
                _require(all(row['channels'][k]['status'] != 'unknown' for k in
                         ('manual_control','post_mixer_pwm','plant_state')), 'prediction:missing_comparable_output')
                channel_unbounded=[name for name in OUTPUT_FIELDS if _state(name,row['channels'][name])]
                _require(not channel_unbounded or u['kind'] == 'unbounded', 'prediction:channel_uncertainty_hidden')
                lower,upper=request['timing']['plant_start_bounds_s']
                for field in row['channels'].values():
                    if field['status'] != 'unknown':
                        stamp=field['source']['source_time_s']
                        _require(lower+row['offset_s']-1e-9 <= stamp <= upper+row['offset_s']+1e-9,
                                 'prediction:channel_time_alignment')
        return _result(True,[],unbounded)
    except (ValueError,TypeError,KeyError,OverflowError) as exc:
        return _result(False,[str(exc)])
