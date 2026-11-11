"""Native completion needs live ground evidence; goal geometry remains MESA-owned."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import time

import pytest
from pyproj import Transformer

from layer6.command_correlation import digest
from layer6.feedback import build_execution_evidence
from layer6.status import remember_observation_context
from layer6.telemetry_source import native_source_contract
from test_r5_01_telemetry_receivers import wire_message
from test_r6_03_first_move_segment import start, feed, sync_gui_segment


@pytest.fixture(scope='module')
def workflow():
    path = Path(__file__).resolve().parents[1] / 'scripts/run_training_workflows.py'
    spec = importlib.util.spec_from_file_location('native_touchdown_workflow', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DESTINATION = [-105.3, 40.01]


def cycle_with_contact(*, landed=1, altitude=.1, groundspeed=0., climb=0.):
    link, context, gui, _ = start(native_source_contract())
    boot = 'b' * 32
    remember_observation_context(link, {'ok': True}, {'native_session_id': boot})
    link.master.queue = [wire_message('EXTENDED_SYS_STATE', changes={'landed_state': landed}),
                         wire_message('VFR_HUD', changes={'groundspeed': groundspeed, 'climb': climb}),
                         wire_message('GLOBAL_POSITION_INT', changes={'time_boot_ms': 1001,
                             'relative_alt': round(altitude * 1000), 'lat': 0, 'lon': 0})]
    link.drain()
    event = link.command_correlation.records[link.command_correlation.current]
    feed(link, 1020, event['barrier_nonce'], True)
    feed(link, 1060)
    sync_gui_segment(gui)
    proof = link.segments.current_record()
    feed(link, 1080, proof['close']['nonce'], True)
    # Complete the post-barrier wire interval with the actual diagnostic altitude.
    # FCU lat/lon deliberately differ from MESA and never supply navigation.
    link.master.queue = [wire_message('SCALED_IMU', changes={'time_boot_ms': 1090}),
                         wire_message('ATTITUDE', changes={'time_boot_ms': 1090}),
                         wire_message('ATTITUDE_QUATERNION', changes={'time_boot_ms': 1090}),
                         wire_message('GLOBAL_POSITION_INT', changes={'time_boot_ms': 1090,
                             'relative_alt': round(altitude * 1000), 'lat': 0, 'lon': 0}),
                         wire_message('SERVO_OUTPUT_RAW', changes={'time_usec': 1090000})]
    link.drain()
    assert proof['state'] == 'CLOSED'
    snapshot = proof['snapshot']
    evidence = build_execution_evidence({'transaction_id': 'tx', 'u_cmd': [0, 0, 0, 0]}, snapshot)
    evidence.update(source='Layer6/GUI/MAVLink', schema='TDOC-L6-online-native-execution-evidence-v1',
                    segment_closure_proof=proof, execution_segment_policy=context['execution_segment'],
                    native_session_health={'ok': True, 'session_id': boot, 'checked_monotonic_s': time.monotonic()},
                    io_snapshot={'latitude_deg': 0., 'longitude_deg': 0., 'relative_altitude_m': altitude})
    east, north = Transformer.from_crs(4326, 32613, always_xy=True).transform(*DESTINATION)
    return {'transaction_id': 'tx', 'layer2_packets': {'MESA_NAV': {'state': {
                'position_n': north, 'position_e': east, 'position_d': -1900.,
                'velocity_n': 0., 'velocity_e': 0., 'velocity_d': 0.}}},
            'downstream_layers': {'6': {'output': {'execution_evidence': evidence}}}}


def evidence(cycle):
    return cycle['downstream_layers']['6']['output']['execution_evidence']


def terminal(workflow, cycle):
    return workflow._online_terminal(cycle, DESTINATION, 10., 2.5)


def test_real_landed_observation_accepts_at_mesa_goal_and_ignores_fcu_position(workflow):
    cycle = cycle_with_contact()
    result = terminal(workflow, cycle)
    assert result['reached'] and result['ground_contact_verified']
    assert result['navigation_authority'] == 'MESA_STD'
    assert result['simulator_truth_used_as_navigation_observation'] is False
    assert result['goal_distance_m'] < .01
    assert abs(result['position']['lon'] - DESTINATION[0]) < 1e-8


@pytest.mark.parametrize('landed', [2, 3, 4])
def test_low_altitude_airborne_reports_never_pass(workflow, landed):
    cycle = cycle_with_contact(landed=landed, altitude=.01)
    evidence(cycle)['io_snapshot']['text'] = 'touchdown landed on ground'
    result = terminal(workflow, cycle)
    assert not result['reached'] and not result['ground_contact_verified']


def test_altitude_and_landed_text_alone_never_prove_touchdown(workflow):
    cycle = cycle_with_contact(landed=0, altitude=1.5)
    evidence(cycle)['io_snapshot']['text'] = 'not landed; waiting for touchdown'
    result = terminal(workflow, cycle)
    assert result['altitude_ok'] and not result['reached']


@pytest.mark.parametrize('speed,climb', [(2., 0.), (0., 1.)])
def test_no_landed_channel_fallback_requires_stationary_near_ground_evidence(workflow, speed, climb):
    assert not terminal(workflow, cycle_with_contact(landed=0, groundspeed=speed, climb=climb))['reached']


def test_finite_stationary_ground_contact_fallback_accepts_without_landed_flag(workflow):
    assert terminal(workflow, cycle_with_contact(landed=0))['reached']


@pytest.mark.parametrize('fault', ['stale_core', 'stale_landed', 'native_session', 'receiver_session',
                                  'current_state_old', 'health_old', 'raw_flags_only', 'invalid_proof',
                                  'contradictory_current_state', 'nonfinite_mesa', 'missing_mesa_altitude'])
def test_missing_stale_mismatched_or_contradictory_native_proof_never_passes(workflow, fault):
    cycle = cycle_with_contact()
    ev = evidence(cycle)
    snapshot = ev['segment_closure_proof']['snapshot']
    if fault == 'stale_core': snapshot['telemetry_metadata']['SCALED_IMU']['freshness']['fresh'] = False
    elif fault == 'stale_landed': snapshot['telemetry_metadata']['EXTENDED_SYS_STATE']['freshness']['fresh'] = False
    elif fault == 'native_session': ev['provenance']['native_session_id'] = 'c' * 32
    elif fault == 'receiver_session': ev['current_state']['session_id'] = 'd' * 32
    elif fault == 'current_state_old': ev['current_state']['timestamp'] -= 10.
    elif fault == 'health_old': ev['native_session_health']['checked_monotonic_s'] -= 10.
    elif fault == 'raw_flags_only':
        del ev['segment_closure_proof']
        ev['current_state'].update(landed_state='ON_GROUND', ground_contact=True)
    elif fault == 'invalid_proof': ev['segment_closure_proof']['qualified'] = False
    elif fault == 'contradictory_current_state': ev['current_state']['landed_state'] = 'IN_AIR'
    elif fault == 'nonfinite_mesa': cycle['layer2_packets']['MESA_NAV']['state']['position_n'] = float('nan')
    elif fault == 'missing_mesa_altitude': del cycle['layer2_packets']['MESA_NAV']['state']['position_d']
    if fault in ('stale_core', 'stale_landed'):
        proof = ev['segment_closure_proof']
        proof['result_hash'] = digest({k: v for k, v in proof.items() if k != 'result_hash'})
    assert not terminal(workflow, cycle)['reached']


def test_landed_far_from_mesa_goal_cannot_pass_even_when_fcu_coordinates_match_goal(workflow):
    cycle = cycle_with_contact()
    cycle['layer2_packets']['MESA_NAV']['state']['position_e'] += 1000.
    evidence(cycle)['io_snapshot'].update(latitude_deg=DESTINATION[1], longitude_deg=DESTINATION[0])
    assert not terminal(workflow, cycle)['reached']


def test_same_cycle_passive_gui_observation_can_prove_ground_contact(workflow):
    cycle = cycle_with_contact()
    ev = evidence(cycle)
    snapshot = ev.pop('segment_closure_proof')['snapshot']
    cycle['layer2_packets']['MESA_NAV']['context'] = {
        'online_state_ingress': {'ptlib_ingress': {'gui_state': snapshot}}}
    result = terminal(workflow, cycle)
    assert result['reached'] and result['touchdown_evidence_source'].endswith('gui_state')


def test_legacy_non_native_terminal_remains_compatible(workflow):
    cycle = cycle_with_contact()
    cycle['downstream_layers']['6']['output']['execution_evidence'] = {
        'source': 'Layer6/PT_LIB', 'io_snapshot': {'latitude_deg': DESTINATION[1],
            'longitude_deg': DESTINATION[0], 'relative_altitude_m': .1}}
    assert terminal(workflow, cycle)['reached']
