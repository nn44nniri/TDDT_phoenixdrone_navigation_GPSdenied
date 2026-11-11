"""Synthetic contract tests only; real Native acceptance is recorded separately."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import time

import pytest

from layer4 import native_provider_contract as c
from layer4.native_reference_provider import (NativeReferenceProvider, candidate_digest,
    make_reference_request, POLICY)


def fixture():
    spec = importlib.util.spec_from_file_location('r701_fixture', Path(__file__).with_name('test_r7_01_provider_contract.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old = module.request()
    ctx = {k: old['context'][k] for k in ('model_id', 'binding_hash', 'handoff_hash', 'forcing_sha256',
                                         'native_session_id', 'telemetry_session_id')}
    old['backend']['initial_mode'] = 'FBWA'
    ref = {'branch_session_id': 'unit-private-branch', 'context': ctx, 'backend': old['backend'],
           'initial_state': old['initial_state'], 'clock_mapping_sha256': '7'*64,
           'plant_start_bounds_s': [2., 2.02], 'monotonic_domain_id': 'unit-host-boot'}
    plan = [{'domain': c.DOMAIN, 'u_cmd': [a, -.1, 0., .1], 'mode': 'FBWA', 'discrete': []}
            for a in (.1, -.2, .3)]
    request = make_reference_request(ref, candidate_id='unit-conditional-forecast', pilot_plan=plan)
    return ref, request


class Runner:
    def __init__(self, ref):
        self.reference = ref
        self.calls = 0
        self.stops = 0
        self.fail = None
    def check_sealed_reference(self):
        if self.fail == 'reference':
            raise ValueError('changed native reference')
    def stop(self):
        self.stops += 1
    def forecast(self, request):
        self.calls += 1
        if self.fail == 'clock':
            raise TimeoutError('source clock stalled')
        dt = request['timing']['prediction_dt_s']
        def sample(offset, wire):
            return {'offset_s': offset, 'mode': 'FBWA',
                    'manual_control': [wire[k] for k in ('x', 'y', 'z', 'r')],
                    'pwm': [1500., 1500., 1000., 1000.],
                    'plant_state': [0.]*6+[1., 0., 0., 0.]+[0.]*3,
                    'plant_time_s': 2.+offset, 'rc_time_s': 2.+offset, 'pwm_time_s': 2.+offset}
        rows = [sample(0., {'x':0, 'y':0, 'z':0, 'r':0})]
        for index, command in enumerate(request['pilot_plan']):
            rows.append(sample((index+1)*dt, c.pilot_to_manual(command['u_cmd'], request['backend']['gui_gains'])))
        first = sample(request['timing']['commit_segment_s'], c.pilot_to_manual(request['pilot_plan'][0]['u_cmd'], request['backend']['gui_gains']))
        return {'trace_sha256': 'a'*64, 'branch_session_id': 'unit-private-branch',
                'horizon_samples': rows, 'first_move_sample': first,
                'source_time_witnesses': [], 'plan_application': {'unit_only': True},
                'coordinate_origin': {'unit_only': True}}


def test_first_move_separate_from_counterfactual_horizon():
    ref, request = fixture()
    runner = Runner(ref)
    provider = NativeReferenceProvider(runner)
    prediction = provider.predict(request)
    assert prediction['status'] == 'PREDICTED'
    assert c.validate_prediction(request, prediction)['contract_valid']
    assert [r['offset_s'] for r in prediction['samples']] == [0., .2, .4, .6000000000000001]
    evidence = provider.last_evidence
    assert evidence['first_move']['offset_s'] == .1
    assert evidence['forecast_horizon_s'] == pytest.approx(.6)
    assert evidence['policy'] == POLICY and evidence['horizon_is_counterfactual_not_committed_execution']
    assert prediction['uncertainty']['kind'] == 'unbounded'
    assert not evidence['qualified'] and not evidence['flight_authorized']
    for row in prediction['samples']:
        assert row['channels']['spool']['status'] == 'unknown'
        assert row['channels']['motor_speed']['value'] is None
    assert provider.predict(request)['status'] == 'UNAVAILABLE' and runner.calls == 1


@pytest.mark.parametrize('change', ['expired', 'candidate', 'backend', 'clock', 'state', 'reference',
                                  'armed', 'throttle', 'discrete', 'mode', 'rate', 'too_long', 'too_short', 'mission_phase'])
def test_valid_but_unsupported_or_mismatched_request_never_advances_runner(change):
    ref, r = fixture()
    if change == 'expired': r['timing']['admission_deadline_unix_s'] = time.time()-1
    if change == 'candidate': r['context']['candidate_hash'] = '9'*64
    if change == 'backend': r['backend']['gui_gains']['max_roll'] = .5
    if change == 'clock': r['timing']['clock_mapping_sha256'] = '8'*64
    if change == 'state': r['initial_state']['controller']['uncertainty']['reason'] = 'substituted state'
    if change == 'reference': r['isolation']['initial_reference_sha256'] = '8'*64
    if change == 'armed': r['backend']['armed'] = True
    if change == 'throttle': r['pilot_plan'][0]['u_cmd'][2] = .1
    if change == 'discrete': r['pilot_plan'][0]['discrete'] = ['ARM']
    if change == 'mode': r['pilot_plan'][1]['mode'] = 'QHOVER'
    if change == 'rate': r['timing']['execution_rate_hz'] = 20.
    if change == 'too_long': r['pilot_plan'] *= 10
    if change == 'too_short': r['timing']['prediction_dt_s'] = .1
    if change == 'mission_phase': r['context']['phase'] = 'cruise'
    if change not in ('candidate',):
        r['context']['candidate_hash'] = candidate_digest(r['pilot_plan'], r['timing']['prediction_dt_s'], r['timing']['commit_segment_s'])
    runner = Runner(ref)
    prediction = NativeReferenceProvider(runner).predict(r)
    assert prediction['status'] == 'UNAVAILABLE' and prediction['samples'] == []
    assert c.validate_prediction(r, prediction)['contract_valid']
    assert runner.calls == 0 and runner.stops == 0


def test_backend_failure_yields_no_partial_or_nominal_prediction():
    ref, r = fixture()
    runner = Runner(ref)
    runner.fail = 'clock'
    provider = NativeReferenceProvider(runner)
    p = provider.predict(r)
    assert p['status'] == 'UNAVAILABLE' and p['samples'] == []
    assert runner.stops == 1 and provider.last_evidence is None


def test_candidate_hash_binds_plan_and_both_time_scales():
    _, r = fixture()
    original = r['context']['candidate_hash']
    assert candidate_digest(r['pilot_plan'], .2, .1) == original
    assert candidate_digest(r['pilot_plan'], .2, .15) != original
    assert candidate_digest(r['pilot_plan'], .3, .1) != original
    plan = deepcopy(r['pilot_plan']); plan[1]['u_cmd'][0] += .01
    assert candidate_digest(plan, .2, .1) != original


def test_malformed_request_does_not_reach_native_runner():
    ref, r = fixture()
    r['pilot_plan'][0]['u_cmd'][0] = float('nan')
    runner = Runner(ref)
    with pytest.raises(ValueError, match='invalid native provider request'):
        NativeReferenceProvider(runner).predict(r)
    assert runner.calls == 0


def test_first_move_cannot_hide_a_bad_channel_or_timestamp():
    from layer4.native_reference_provider import output_row
    ref, r = fixture()
    first = output_row(Runner(ref).forecast(r)['first_move_sample'], 'a'*64)
    first['channels']['post_mixer_pwm']['value'][0] = 0
    with pytest.raises(ValueError):
        NativeReferenceProvider._check_first_move(r, first)
    first = output_row(Runner(ref).forecast(r)['first_move_sample'], 'a'*64)
    first['channels']['plant_state']['source']['source_time_s'] = 20.
    with pytest.raises(ValueError, match='source time'):
        NativeReferenceProvider._check_first_move(r, first)
