"""Live observer faults and real-wire segment metadata must remain fail-closed."""
from copy import deepcopy
import os
import time

import pytest

from layer6 import native_session as native
from layer6.status import remember_observation_context
from layer6.telemetry_source import native_source_contract
from test_r6_03_first_move_segment import start, feed, sync_gui_segment


def test_bounded_real_wire_snapshot_preserves_validated_native_metadata():
    link, ctx, gui, sent = start(native_source_contract())
    assert sent is not False
    original_time = time.monotonic() - .2
    environment = {'binding_hash': 'bound', 'updated_monotonic': original_time,
                   'sampled_monotonic': original_time, 'ok': True, 'binding_consumed': True}
    quality = {'native_session_id': 'a' * 32, 'native_GUI_pid': os.getpid(),
               'actuation_authority': False, 'physical_HOLD_verified': False}
    expected = deepcopy(environment)
    remember_observation_context(link, environment, quality)
    environment['binding_hash'] = 'mutated-after-observation'
    quality['native_session_id'] = 'b' * 32
    event = link.command_correlation.records[link.command_correlation.current]
    feed(link, 1020, event['barrier_nonce'], True)
    feed(link, 1060)
    sync_gui_segment(gui)
    row = link.segments.current_record()
    feed(link, 1080, row['close']['nonce'], True)
    feed(link, 1090, all_channels=True)
    assert row['state'] == 'CLOSED'
    snapshot = row['snapshot']
    assert snapshot['environment_contract'] == expected
    assert snapshot['environment_contract']['updated_monotonic'] == original_time
    assert snapshot['status_quality']['native_session_id'] == 'a' * 32
    assert snapshot['status_quality']['execution_correlation_available'] is True
    assert snapshot['status_quality']['manual_output_inhibited'] is True
    assert snapshot['flight_ready'] is False


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(native, 'RUNTIME', tmp_path)
    sid = 'a' * 32
    folder = tmp_path / 'sessions' / sid
    native.write(folder / 'session.json', {'schema': native.SCHEMA, 'session_id': sid, 'uid': os.getuid()})
    native.write(tmp_path / 'flightstack_state.json', {'session_id': sid, 'session_dir': str(folder)})
    items = [{'pid': index + 10, 'role': role, 'start_ticks': index + 100}
             for index, role in enumerate((*native.ROLES, 'terminal4'))]
    monkeypatch.setattr(native, 'processes', lambda _: items)
    owner = next(p for p in items if p['role'] == 'mavproxy')
    backend = next(p for p in items if p['role'] == 'JSBSim')
    viewer = next(p for p in items if p['role'] == 'terminal4')
    native.write(folder / 'mavproxy_process.json', {'pid': owner['pid']})
    panel = {'schema': 'Layer6-native-panels-v1', 'session_id': sid, 'producer_pid': owner['pid'],
             'master': 'tcp:127.0.0.1:5760', 'recorded_monotonic_s': 99.9,
             'heartbeat': {'received_monotonic_s': 99.9, 'target_system': 1, 'mode': 'QHOVER'},
             'position': {'received_monotonic_s': 99.9, 'target_system': 1, 'time_boot_ms': 1000},
             'position_updates': 2,
             'map': {'alive': True, 'wx_ready': True, 'position_matches': True, 'child_pid': 101},
             'console': {'alive': True, 'child_pid': 102}}
    native.write(folder / 'native_panels.json', panel)
    window = {'schema': 'Layer6-JSBSim-window-v1', 'session_id': sid, 'producer_pid': viewer['pid'],
              'producer_start_ticks': viewer['start_ticks'], 'recorded_monotonic_s': 99.9,
              'jsbsim_pid': backend['pid'], 'jsbsim_start_ticks': backend['start_ticks'],
              'source_log': str(folder / 'arduplane.log'), 'telemetry_master': 'tcp:127.0.0.1:5760',
              'telemetry_ready': True, 'simulator_spawned': False}
    native.write(folder / 'jsbsim_window.json', window)
    return sid, folder, items, panel, window


def test_health_requires_fresh_components_map_console_and_separate_backend_window(session):
    sid, folder, _, _, _ = session
    result = native.session_health(sid, now=100.)
    assert result['ok'] and result['session_ready']
    assert result['jsbsim_window']['evidence']['simulator_spawned'] is False
    assert result['visual_acceptance_verified'] is False


@pytest.mark.parametrize('fault', ['map_closed', 'console_closed', 'stale_panels', 'cross_session',
                                  'component_missing', 'window_closed', 'wrong_backend', 'window_stale',
                                  'window_no_telemetry', 'stop_requested'])
def test_health_faults_never_report_ready(session, fault):
    sid, folder, items, panel, window = session
    if fault == 'map_closed': panel['map']['alive'] = False
    elif fault == 'console_closed': panel['console']['alive'] = False
    elif fault == 'stale_panels': panel['recorded_monotonic_s'] = 90.
    elif fault == 'cross_session': panel['session_id'] = 'b' * 32
    elif fault == 'component_missing': items[:] = [p for p in items if p['role'] != 'environment']
    elif fault == 'window_closed': items[:] = [p for p in items if p['role'] != 'terminal4']
    elif fault == 'wrong_backend': window['jsbsim_pid'] += 100
    elif fault == 'window_stale': window['recorded_monotonic_s'] = 90.
    elif fault == 'window_no_telemetry': window['telemetry_ready'] = False
    elif fault == 'stop_requested': (folder / 'stop.request').touch()
    native.write(folder / 'native_panels.json', panel)
    native.write(folder / 'jsbsim_window.json', window)
    result = native.session_health(sid, now=100.)
    assert result['ok'] is False and result['reason']


def test_health_rejects_switched_registry_and_missing_evidence(session):
    sid, folder, *_ = session
    assert not native.session_health('b' * 32, now=100.)['ok']
    (folder / 'native_panels.json').unlink()
    assert not native.session_health(sid, now=100.)['ok']


def test_backend_terminal_observes_without_launching_a_simulator(session, monkeypatch):
    _, folder, *_ = session
    calls = []
    monkeypatch.setattr(native, 'jsbsim_observer', lambda path, data: calls.append((path, data['session_id'])))
    monkeypatch.setattr(native.subprocess, 'Popen', lambda *a, **kw: pytest.fail('observer spawned a simulator'))
    native.worker(folder, 'terminal4')
    assert calls == [(folder, 'a' * 32)]
