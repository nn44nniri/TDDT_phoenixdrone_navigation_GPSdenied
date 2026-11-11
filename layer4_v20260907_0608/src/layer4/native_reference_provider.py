"""R7.03 conditional native forecasts on a sealed, isolated cold-boot branch.

This provider is deliberately not wired into Layer4 readiness (R7.06). Its
unbounded predictions are neither certificates nor executable Layer5 decisions.
"""
from copy import deepcopy
import math
import time

from .native_provider_contract import (OUTPUT_FIELDS, _state,
    PREDICTION_SCHEMA, REQUEST_SCHEMA, digest, pilot_to_manual, unknown_state,
    validate_prediction, validate_request)

REVISION = 'phoenix-native-reference-prediction-r7.03-v1'
POLICY = 'COUNTERFACTUAL_ZOH_PLAN; COMMIT_FIRST_MOVE_ONLY; NO_EXECUTION_AUTHORITY'


def candidate_digest(plan, dt, commit):
    return digest({'pilot_plan': plan, 'prediction_dt_s': dt, 'commit_segment_s': commit})


def make_reference_request(reference, *, candidate_id, pilot_plan, prediction_dt_s=.2,
                           commit_segment_s=.1, admission_seconds=30.):
    """Experimental reference request; never fabricate an actual L3 snapshot."""
    initial = deepcopy(reference['initial_state'])
    dt, commit = prediction_dt_s, commit_segment_s
    r = {'schema': REQUEST_SCHEMA, 'request_id': 'reference-'+candidate_id,
         'context': {**deepcopy(reference['context']),
                     'snapshot_id': 'isolated-reference-'+reference['branch_session_id'],
                     'snapshot_version': REVISION, 'snapshot_hash': digest(reference),
                     'candidate_id': candidate_id,
                     'candidate_hash': candidate_digest(pilot_plan, dt, commit),
                     'phase': 'isolated_on_ground_reference'},
         'backend': deepcopy(reference['backend']), 'pilot_plan': deepcopy(pilot_plan),
         'timing': {'plant_clock': 'FCU_BOOT',
                    'clock_mapping_sha256': reference['clock_mapping_sha256'],
                    'plant_start_bounds_s': list(reference['plant_start_bounds_s']),
                    'prediction_dt_s': dt, 'commit_segment_s': commit,
                    'close_guard_s': min(.02, commit/2), 'execution_rate_hz': 30.,
                    'gui_rate_hz': 30., 'gui_ttl_s': .15,
                    'admission_deadline_unix_s': time.time()+admission_seconds,
                    'monotonic_domain_id': reference['monotonic_domain_id']},
         'initial_state': initial,
         'isolation': {'policy': 'ISOLATED_REPLAY_ONLY', 'initial_reference_sha256': digest(reference)}}
    return r


def observed_channel(name, value, stamp, artifact):
    field = unknown_state(name, 'unobserved native internal state')
    field.update(status='observed', value=list(value),
                 source={'kind': 'NATIVE_REPLAY', 'artifact_sha256': artifact,
                         'source_time_s': stamp, 'clock_domain': 'FCU_BOOT'},
                 uncertainty={'kind': 'unbounded',
                              'reason': 'reference sample only; no R7.04 tolerance or R7.05 certificate'})
    return field


def output_row(sample, artifact):
    fields = {name: unknown_state(name, 'native internal state not observable; no PWM inference')
              for name in OUTPUT_FIELDS}
    fields['manual_control'] = observed_channel('manual_control', sample['manual_control'],
                                                sample['rc_time_s'], artifact)
    fields['post_mixer_pwm'] = observed_channel('post_mixer_pwm', sample['pwm'],
                                               sample['pwm_time_s'], artifact)
    fields['plant_state'] = observed_channel('plant_state', sample['plant_state'],
                                             sample['plant_time_s'], artifact)
    return {'offset_s': sample['offset_s'], 'mode': sample['mode'], 'channels': fields}


def unavailable(request, reason):
    return {'schema': PREDICTION_SCHEMA, 'request_sha256': digest(request),
            'status': 'UNAVAILABLE', 'reason': reason, 'samples': [],
            'isolation': {'branch_session_id': None,
                          'initial_reference_sha256': request['isolation']['initial_reference_sha256'],
                          'active_mission_advanced': False, 'trace_sha256': None},
            'uncertainty': {'kind': 'unbounded', 'reason': reason}}


class NativeReferenceProvider:
    """One admitted prediction per prepared runner. Invalid requests never thaw it."""
    def __init__(self, runner):
        self.runner = runner
        self.last_evidence = None
        self.consumed = False

    def predict(self, request):
        # Detach every nested field before checks or process interaction.
        r = deepcopy(request)
        self.last_evidence = None
        validation = validate_request(r)
        if not validation['contract_valid']:
            # Malformed requests have no safe digest/correlation shape to return.
            raise ValueError('invalid native provider request: '+','.join(validation['errors']))
        try:
            self._admit(r)
        except (ValueError, RuntimeError) as exc:
            return unavailable(r, str(exc))
        self.consumed = True
        try:
            trace = self.runner.forecast(r)
            artifact = trace['trace_sha256']
            rows = [output_row(sample, artifact) for sample in trace['horizon_samples']]
            prediction = {'schema': PREDICTION_SCHEMA, 'request_sha256': digest(r),
                          'status': 'PREDICTED', 'reason': REVISION+'; '+POLICY,
                          'isolation': {'branch_session_id': trace['branch_session_id'],
                                        'initial_reference_sha256': r['isolation']['initial_reference_sha256'],
                                        'active_mission_advanced': False, 'trace_sha256': artifact},
                          'samples': rows,
                          'uncertainty': {'kind': 'unbounded',
                                          'reason': 'cold reference only; unknown controller/mixer/spool/actuator state; unqualified'}}
            valid = validate_prediction(r, prediction)
            if not valid['contract_valid']:
                raise ValueError('native prediction output contract: '+','.join(valid['errors']))
            first = output_row(trace['first_move_sample'], artifact)
            self._check_first_move(r, first)
            self.last_evidence = {
                'schema': 'TDDT-native-forecast-evidence-v1', 'revision': REVISION,
                'request_sha256': digest(r), 'prediction_sha256': digest(prediction),
                'initial_reference_sha256': r['isolation']['initial_reference_sha256'],
                'candidate_hash': r['context']['candidate_hash'], 'policy': POLICY,
                'first_move': first, 'commit_segment_s': r['timing']['commit_segment_s'],
                'forecast_horizon_s': len(r['pilot_plan'])*r['timing']['prediction_dt_s'],
                'horizon_is_counterfactual_not_committed_execution': True,
                'source_time_witnesses': trace['source_time_witnesses'],
                'plan_application': trace['plan_application'], 'coordinate_origin': trace['coordinate_origin'],
                'trace_sha256': artifact, 'branch_session_id': trace['branch_session_id'],
                'active_mission_advanced': False, 'qualified': False, 'flight_authorized': False}
            return prediction
        except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
            self.runner.stop()
            return unavailable(r, 'native reference forecast unavailable: '+str(exc))

    def _admit(self, r):
        if self.consumed:
            raise ValueError('reference provider already consumed')
        ref = self.runner.reference
        if ref is None or r['isolation']['initial_reference_sha256'] != digest(ref):
            raise ValueError('sealed native reference mismatch; mission checkpoint unavailable')
        if r['context']['snapshot_hash'] != digest(ref) or r['initial_state'] != ref['initial_state']:
            raise ValueError('initial state is not the sealed isolated reference')
        if (r['context']['phase'] != 'isolated_on_ground_reference'
                or r['context']['snapshot_id'] != 'isolated-reference-'+ref['branch_session_id']
                or r['context']['snapshot_version'] != REVISION):
            raise ValueError('reference forecast cannot claim an active mission/L3 snapshot')
        if r['backend'] != ref['backend']:
            raise ValueError('native backend or complete parameter map mismatch')
        for k, v in ref['context'].items():
            if r['context'].get(k) != v:
                raise ValueError('native reference context mismatch: '+k)
        t = r['timing']
        if (t['clock_mapping_sha256'] != ref['clock_mapping_sha256']
                or t['plant_start_bounds_s'] != ref['plant_start_bounds_s']
                or t['monotonic_domain_id'] != ref['monotonic_domain_id']):
            raise ValueError('native reference clock mismatch')
        if time.time() >= t['admission_deadline_unix_s']:
            raise ValueError('native prediction admission deadline expired')
        if r['context']['candidate_hash'] != candidate_digest(r['pilot_plan'], t['prediction_dt_s'], t['commit_segment_s']):
            raise ValueError('candidate plan/timing hash mismatch')
        if not (.2 <= t['prediction_dt_s'] <= 1 and .1 <= t['commit_segment_s'] <= t['prediction_dt_s']):
            raise ValueError('reference forecast timing outside implemented scope')
        if len(r['pilot_plan'])*t['prediction_dt_s'] > 3 or len(r['pilot_plan']) > 10:
            raise ValueError('reference forecast horizon exceeds three source seconds')
        if t['execution_rate_hz'] != 30 or t['gui_rate_hz'] != 30:
            raise ValueError('reference forecast sender cadence must be explicit 30 Hz target')
        if r['backend']['armed'] is not False:
            raise ValueError('DISARMED reference only; no ARM authority')
        for row in r['pilot_plan']:
            if row['u_cmd'][2] != 0 or row['discrete']:
                raise ValueError('reference forecast supports zero throttle and no discrete actions')
            if row['mode'] not in (None, r['backend']['initial_mode']):
                raise ValueError('reference forecast supports one confirmed constant mode only')
        self.runner.check_sealed_reference()

    @staticmethod
    def _check_first_move(r, sample):
        commit = r['timing']['commit_segment_s']
        if sample['offset_s'] != commit or sample['mode'] != r['backend']['initial_mode']:
            raise ValueError('wrong first move offset/mode')
        if set(sample['channels']) != set(OUTPUT_FIELDS):
            raise ValueError('first move channel set mismatch')
        lo, hi = r['timing']['plant_start_bounds_s']
        for name, field in sample['channels'].items():
            _state(name, field)
            if name in ('manual_control', 'post_mixer_pwm', 'plant_state') and field['status'] == 'unknown':
                raise ValueError('first move comparable output unavailable')
            if field['status'] != 'unknown' and not lo+commit-1e-9 <= field['source']['source_time_s'] <= hi+commit+1e-9:
                raise ValueError('first move source time outside declared interval')
