"""R7.04 independent raw-trace validation in the declared ground reference scope.

Finite residual budgets apply even when prediction uncertainty is unbounded.
A consistent experiment is not a bounded-error model or provider certificate.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from .native_provider_contract import OUTPUT_FIELDS, digest, validate_prediction, _state

REVISION = 'phoenix-independent-native-validation-r7.04-v1'
CORE = ('LOCAL_POSITION_NED', 'ATTITUDE_QUATERNION', 'SERVO_OUTPUT_RAW', 'RC_CHANNELS')
WIND = ('SIM_WIND_T', 'SIM_WIND_TURB', 'SIM_WIND_TC', 'SIM_WIND_DIR_Z', 'SIM_WIND_DIR', 'SIM_WIND_SPD')


def validation_policy():
    return {'schema': 'TDDT-native-prediction-validation-policy-v1', 'revision': REVISION,
            'scope': 'DISARMED_ZERO_THROTTLE_COLD_REFERENCE; NO_FLIGHT_QUALIFICATION',
            'limits': {'position_norm_m': .02, 'velocity_norm_m_s': .02,
                       'quaternion_angle_rad': .02, 'body_rate_norm_rad_s': .02,
                       'PWM_max_abs_us': 100., 'manual_control_max_abs': 0.},
            'initial_state_uses_same_limits': True, 'maximum_source_interval_s': .101,
            'wind_parameter_match_abs': .002, 'other_parameters_match_abs': 0.,
            'comparison': 'pointwise; original source intervals; no interpolation, best-fit shift or tolerance expansion',
            'quaternion_metric': '2*acos(abs(normalized_dot)); input norm must already pass 0.002',
            'unknown_state_policy': 'retain unbounded; never claim bounded coverage or mission checkpoint',
            'engineering_budget_basis': 'ground diagnostic: 2cm/2cm_s/0.02rad/0.02rad_s; PWM 10 percent of 1000us span',
            'minimum_independent_sessions': 2, 'bounded_model_qualification': False,
            'provider_certificate_issued': False, 'flight_authorized': False}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _finite(value):
    return isinstance(value, (int,float)) and not isinstance(value,bool) and math.isfinite(value)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quaternion_distance(a, b):
    _require(len(a) == len(b) == 4 and all(_finite(v) for v in [*a,*b]), 'invalid quaternion')
    na, nb = math.sqrt(sum(v*v for v in a)), math.sqrt(sum(v*v for v in b))
    _require(abs(na-1) <= .002 and abs(nb-1) <= .002, 'invalid quaternion norm')
    return 2*math.acos(min(1., abs(sum(x*y for x,y in zip(a,b))/(na*nb))))


def residuals(a, b):
    def norm(x,y): return math.sqrt(sum((i-j)**2 for i,j in zip(x,y)))
    av, bv = a['plant_state'], b['plant_state']
    _require(len(av) == len(bv) == 13 and all(_finite(v) for v in [*av,*bv]), 'invalid plant vector')
    return {'position_norm_m': norm(av[:3],bv[:3]), 'velocity_norm_m_s': norm(av[3:6],bv[3:6]),
            'quaternion_angle_rad': quaternion_distance(av[6:10],bv[6:10]),
            'body_rate_norm_rad_s': norm(av[10:],bv[10:]),
            'PWM_max_abs_us': max(abs(x-y) for x,y in zip(a['pwm'],b['pwm'])),
            'manual_control_max_abs': max(abs(x-y) for x,y in zip(a['manual_control'],b['manual_control']))}


def decode_row(row):
    from pymavlink.dialects.v20 import ardupilotmega as mav
    try:
        message = mav.MAVLink(None).decode(bytearray.fromhex(row['raw_MAVLink_hex']))
    except mav.MAVError as exc:
        raise ValueError('invalid raw MAVLink frame/CRC') from exc
    _require(message.get_srcSystem() == row['source_system'] and message.get_srcComponent() == row['source_component'],
             'raw MAVLink source mismatch')
    payload = message.to_dict()
    _require(payload == row['message'], 'raw MAVLink payload differs from published JSON')
    return payload


def _stamp(name, row):
    p = row['message']
    value = p['time_usec'] if name == 'SERVO_OUTPUT_RAW' else p['time_boot_ms']
    _require(_finite(value) and value >= 0, 'invalid source timestamp')
    return value/(1e6 if name == 'SERVO_OUTPUT_RAW' else 1000.)


class RawTrace:
    def __init__(self, path):
        self.sha256 = sha(path)
        self.rows = {k: [] for k in CORE}
        self.members = {}
        self.heartbeats = 0
        last = {}
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            name = row['message'].get('mavpackettype')
            if name not in (*CORE,'HEARTBEAT'):
                continue
            payload = decode_row(row)
            if name == 'HEARTBEAT':
                _require(not payload['base_mode'] & 128, 'ARMED observation outside validation scope')
                self.heartbeats += 1
                continue
            stamp = _stamp(name,row)
            _require(name not in last or stamp >= last[name], 'reversed native trace clock')
            last[name] = stamp
            self.rows[name].append(row)
            self.members.setdefault(row['raw_MAVLink_hex'], []).append(row)
        _require(self.heartbeats > 0 and all(self.rows.values()), 'incomplete independent native trace')

    def witness(self, witness):
        _require(set(witness) == set(CORE), 'incomplete physical sample witnesses')
        for row in witness.values():
            _require(row in self.members.get(row['raw_MAVLink_hex'], []), 'witness not present in raw trace')
        return decode_sample(witness)

    def point(self, lower, upper, command, parameters):
        chosen = {name: [r for r in rows if lower-1e-9 <= _stamp(name,r) <= upper+1e-9]
                  for name,rows in self.rows.items()}
        _require(all(chosen.values()), 'missing/stale independent sample')
        lp = {r['message']['time_boot_ms']: r for r in chosen['LOCAL_POSITION_NED']}
        aq = {r['message']['time_boot_ms']: r for r in chosen['ATTITUDE_QUATERNION']}
        common = sorted(lp.keys() & aq.keys())
        _require(bool(common), 'independent position/quaternion timestamps do not match')
        wanted = _wire(command, parameters['gui_gains'])
        rc = next((r for r in chosen['RC_CHANNELS'] if _manual(r['message'], parameters['parameters']) == wanted), None)
        _require(rc is not None, 'independent applied RC command does not match candidate')
        witness = {'LOCAL_POSITION_NED': lp[common[0]], 'ATTITUDE_QUATERNION': aq[common[0]],
                   'SERVO_OUTPUT_RAW': chosen['SERVO_OUTPUT_RAW'][0], 'RC_CHANNELS': rc}
        point = self.witness(witness)
        point['manual_control'] = _manual(rc['message'], parameters['parameters'])
        return point


def _wire(command, gains):
    a,e,t,r = command['u_cmd']
    def axis(v,g): return round(1000*min(1.,max(-1.,v*g)))
    return [axis(e,gains['max_pitch']),axis(a,gains['max_roll']),round(1000*t),axis(r,gains['max_yaw'])]


def _manual(p, parameters):
    def signed(axis):
        ch = int(parameters['RCMAP_'+axis])
        low, high = parameters['RC%d_MIN'%ch], parameters['RC%d_MAX'%ch]
        _require(high > low, 'invalid RC calibration')
        return (p['chan%d_raw'%ch]-low)/(high-low)
    return [-round(2000*signed('PITCH')-1000), round(2000*signed('ROLL')-1000),
            round(1000*signed('THROTTLE')), round(2000*signed('YAW')-1000)]


def decode_sample(witness):
    lp, aq, pwm, rc = (decode_row(witness[k]) for k in CORE)
    _require(lp['time_boot_ms'] == aq['time_boot_ms'], 'physical sample position/quaternion not atomic')
    vector = [lp[k] for k in ('x','y','z','vx','vy','vz')]
    vector += [aq[k] for k in ('q1','q2','q3','q4','rollspeed','pitchspeed','yawspeed')]
    pulses = [pwm['servo%d_raw'%i] for i in range(1,5)]
    _require(all(_finite(v) for v in vector+pulses), 'nonfinite physical observation')
    _require(all(0 < v < 65535 for v in pulses) and max(pulses[2:]) <= 1000, 'invalid/unsafe native PWM')
    quaternion_distance(vector[6:10],vector[6:10])
    return {'plant_state': vector, 'pwm': pulses, 'manual_control': None,
            'stamps': {'plant_state': lp['time_boot_ms']/1000.,
                       'post_mixer_pwm': pwm['time_usec']/1e6, 'manual_control': rc['time_boot_ms']/1000.}}


def _published_sample(row, witness, trace, backend, bounds):
    observed = trace.witness(witness)
    observed['manual_control'] = _manual(witness['RC_CHANNELS']['message'],backend['parameters'])
    expected = {'plant_state': observed['plant_state'], 'post_mixer_pwm': observed['pwm'],
                'manual_control': observed['manual_control']}
    for name, field in row['channels'].items():
        _state(name, field)
        if name in expected:
            _require(field['status'] != 'unknown' and field['value'] == expected[name], 'prediction channel differs from its raw witness: '+name)
            _require(field['source']['artifact_sha256'] == trace.sha256, 'prediction artifact hash mismatch')
            _require(abs(field['source']['source_time_s']-observed['stamps'][name]) <= 1e-9, 'prediction source time relabelled')
            _require(bounds[0]+row['offset_s']-1e-9 <= observed['stamps'][name] <= bounds[1]+row['offset_s']+1e-9,
                     'prediction timestamp outside declared source interval')
    return observed


def validate_native_pair(request, prediction, evidence, observation, *, prediction_trace, observation_trace, policy):
    result = {'schema': 'TDDT-independent-native-validation-v1', 'revision': REVISION,
              'status': 'REJECTED', 'reason': None, 'residuals': [], 'policy_sha256': digest(validation_policy()),
              'bounded_error_model_qualified': False, 'provider_certificate_issued': False, 'flight_authorized': False,
              'uncertainty': {'kind': 'unbounded', 'bounded_coverage_claimed': False}}
    try:
        _require(policy == validation_policy(), 'unregistered/modified validation tolerance policy')
        _require(validate_prediction(request,prediction)['contract_valid'] and prediction['status'] == 'PREDICTED',
                 'invalid or unavailable prediction')
        seal = json.loads((Path(prediction_trace).parent/'SEALED_REFERENCE.json').read_text())
        _require(digest(seal) == request['isolation']['initial_reference_sha256'] == request['context']['snapshot_hash'],
                 'prediction initial reference seal mismatch')
        _require(seal['initial_state'] == request['initial_state'] and seal['backend'] == request['backend'],
                 'prediction state/backend differs from sealed reference')
        _require(request['context']['candidate_hash'] == digest({'pilot_plan':request['pilot_plan'],
                 'prediction_dt_s':request['timing']['prediction_dt_s'], 'commit_segment_s':request['timing']['commit_segment_s']}),
                 'candidate plan/timing hash mismatch')
        _require(evidence['request_sha256'] == digest(request) and evidence['prediction_sha256'] == digest(prediction),
                 'prediction evidence binding mismatch')
        _require(not evidence['qualified'] and not evidence['flight_authorized'], 'premature qualification claim')
        _require(evidence['policy'] == 'COUNTERFACTUAL_ZOH_PLAN; COMMIT_FIRST_MOVE_ONLY; NO_EXECUTION_AUTHORITY',
                 'wrong horizon/commit policy')
        _require(observation['schema'] == 'TDDT-independent-native-observation-v1' and observation['provider_or_forecast_called'] is False,
                 'observation is not an independent execution')
        pred_id, obs_id = prediction['isolation']['branch_session_id'], observation['branch_session_id']
        _require(pred_id == seal['branch_session_id'], 'prediction session differs from sealed reference')
        _require(pred_id != obs_id and pred_id != request['context']['native_session_id'] and obs_id != request['context']['native_session_id'],
                 'same/live native session used as independent reference')
        ref = observation['reference']
        independent_seal = json.loads((Path(observation_trace).parent/'SEALED_REFERENCE.json').read_text())
        _require(ref == independent_seal, 'independent initial reference seal mismatch')
        _require(ref['branch_session_id'] == obs_id and observation['mode'] == request['backend']['initial_mode'], 'independent mode/session mismatch')
        _require(observation['pilot_plan'] == request['pilot_plan'] and observation['candidate_hash'] == request['context']['candidate_hash'],
                 'independent candidate/input plan mismatch')
        _require(observation['prediction_dt_s'] == request['timing']['prediction_dt_s'] and observation['commit_segment_s'] == request['timing']['commit_segment_s'],
                 'independent horizon/commit timing mismatch')
        for k in ('model_id','binding_hash','handoff_hash','forcing_sha256','native_session_id'):
            _require(ref['context'][k] == request['context'][k], 'independent context mismatch: '+k)
        for k in ('arduplane_sha256','jsbsim_sha256','aircraft_sha256','defaults_sha256','gui_config_sha256','execution_path_sha256','gui_gains','initial_mode','armed'):
            _require(ref['backend'][k] == request['backend'][k], 'independent backend mismatch: '+k)
        pa, pb = request['backend']['parameters'], ref['backend']['parameters']
        _require(all(_finite(v) for v in pb.values()), 'invalid independent parameter map')
        _require(digest(pb) == ref['backend']['parameter_snapshot_sha256'] and set(pa) == set(pb), 'independent parameter snapshot mismatch')
        for k in pa:
            _require(abs(pa[k]-pb[k]) <= (policy['wind_parameter_match_abs'] if k in WIND else 0.), 'independent parameter mismatch: '+k)
        pbound, obound = request['timing']['plant_start_bounds_s'], ref['plant_start_bounds_s']
        _require(all(0 <= b[1]-b[0] <= policy['maximum_source_interval_s'] for b in (pbound,obound)), 'source uncertainty interval too wide')
        pred, obs = RawTrace(prediction_trace), RawTrace(observation_trace)
        _require(pred.sha256 == prediction['isolation']['trace_sha256'] == evidence['trace_sha256'], 'prediction raw trace hash mismatch')
        _require(obs.sha256 == observation['trace_sha256'] and pred.sha256 != obs.sha256, 'observation trace missing/duplicated')
        origin = evidence['coordinate_origin']['message']
        other = ref['coordinate_origin']['message']
        _require(all(origin[k] == other[k] for k in ('latitude','longitude','altitude')), 'independent NED origin mismatch')
        rows = list(prediction['samples'])+[evidence['first_move']]
        _require(evidence['first_move']['offset_s'] == request['timing']['commit_segment_s'], 'wrong first move endpoint')
        _require(evidence['forecast_horizon_s'] == len(request['pilot_plan'])*request['timing']['prediction_dt_s'], 'wrong forecast horizon')
        witnesses = evidence['source_time_witnesses']
        for row in rows:
            offset = row['offset_s']
            _require(row['mode'] == request['backend']['initial_mode'] and set(row['channels']) == set(OUTPUT_FIELDS),
                     'prediction sample mode/channels mismatch')
            candidates = [w for w in witnesses if abs(_stamp('LOCAL_POSITION_NED',w['LOCAL_POSITION_NED'])-
                          row['channels']['plant_state']['source']['source_time_s']) <= 1e-9]
            _require(bool(candidates), 'prediction sample has no physical source witness')
            point = _published_sample(row,candidates[0],pred,request['backend'],pbound)
            if offset == 0:
                command = {'u_cmd': [0.,0.,0.,0.]}
            else:
                index = min(len(request['pilot_plan'])-1, max(0, math.ceil(offset/request['timing']['prediction_dt_s']-1e-9)-1))
                command = request['pilot_plan'][index]
            other_point = obs.point(obound[0]+offset,obound[1]+offset,command,ref['backend'])
            differences = residuals(point,other_point)
            violations = [k for k,v in differences.items() if v > policy['limits'][k]]
            result['residuals'].append({'offset_s':offset,'metrics':differences,'violations':violations,
                'prediction_source_times':point['stamps'],'independent_source_times':other_point['stamps']})
        _require(not any(r['violations'] for r in result['residuals']), 'finite residual budget exceeded')
        result.update(status='CONSISTENT_IN_EXPERIMENTAL_SCOPE',reason='independent native raw traces within predeclared budgets',
                      prediction_trace_sha256=pred.sha256,independent_trace_sha256=obs.sha256,
                      prediction_branch_session_id=pred_id,independent_branch_session_id=obs_id)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,IndexError) as exc:
        result['reason'] = str(exc)
    return result
