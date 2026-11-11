"""Reference admission, identity and one-way observation boundaries."""
from pathlib import Path

import pytest

from layer6.native_replay import (NativeReferenceReplay, ObservationFeed, compare_parameters,
                                 json_observation, map_observation_current, parameters, ports, validate_plan)


def plan():
    return {'candidate_id': 'reference-A', 'mode': 'FBWA', 'u_cmd': [.2, -.1, 0., .1], 'duration_s': .5}


@pytest.mark.parametrize('field,value', [
    ('mode', 'AUTO'), ('mode', 'ARM'), ('u_cmd', [0, 0, .001, 0]),
    ('u_cmd', [0, 0, 0, 0, 0]), ('u_cmd', [2, 0, 0, 0]),
    ('u_cmd', [0, float('nan'), 0, 0]), ('u_cmd', [True, 0, 0, 0]),
    ('duration_s', True), ('duration_s', float('inf')), ('duration_s', .01),
    ('duration_s', 10), ('candidate_id', ''), ('candidate_id', None),
])
def test_bad_candidate_rejected(field, value):
    p = plan()
    p[field] = value
    with pytest.raises(ValueError):
        validate_plan(p)


def test_arm_and_unknown_fields_rejected():
    p = plan()
    p['ARM'] = True
    with pytest.raises(ValueError):
        validate_plan(p)


def test_plan_detached():
    p = plan()
    frozen = validate_plan(p)
    p['u_cmd'][0] = .9
    assert frozen['u_cmd'][0] == .2


@pytest.mark.parametrize('instance', [0, 1, True, 19, 81, 42.0])
def test_mission_ports_unavailable(instance):
    with pytest.raises(ValueError):
        ports(instance)


def test_reference_ports_are_separate():
    assert not set(ports(42).values()) & {5760, 5504, 5505, 5506, 14551, 14552, 14660, 14661}


def construct(tmp_path, **overrides):
    root = tmp_path/'layer6'
    root.mkdir(exist_ok=True)
    binding = tmp_path/'binding.json'
    binding.write_text('{}')
    parm = tmp_path/'reference.parm'
    parm.write_text('Q_ENABLE 1\n')
    opts = dict(layer6_root=root, output=tmp_path/'TDOC_v20260911_2052/reports/branch', binding_file=binding,
                parameter_file=parm, backend_hashes={}, mission_session_id='a'*32)
    opts.update(overrides)
    return NativeReferenceReplay(**opts)


def test_existing_output_refused(tmp_path):
    out = tmp_path/'TDOC_v20260911_2052/reports/branch'
    out.mkdir(parents=True)
    with pytest.raises(ValueError, match='must be new'):
        construct(tmp_path)


def test_private_output_cannot_be_source(tmp_path):
    with pytest.raises(ValueError, match='private reports'):
        construct(tmp_path, output=Path('/var/tddt-reference-unwritable'))


def test_no_candidate_on_unstarted_branch(tmp_path):
    branch = construct(tmp_path)
    with pytest.raises(RuntimeError, match='DISARMED'):
        branch.run_candidate(plan())


def test_no_reuse_of_internal_state(tmp_path):
    branch = construct(tmp_path)
    branch.candidate_consumed = True
    with pytest.raises(RuntimeError, match='one candidate'):
        branch.run_candidate(plan())


@pytest.mark.parametrize('text', ['A 1\nA 2\n', 'A nan\n', ''])
def test_invalid_parameters(tmp_path, text):
    path = tmp_path/'p'
    path.write_text(text)
    with pytest.raises(ValueError):
        parameters(path)


def test_parameter_comments(tmp_path):
    path = tmp_path/'p'
    path.write_text('# comment\nA 1 # annotation\nB 0.1\n')
    assert parameters(path) == {'A': 1., 'B': .1}


def test_map_return_packets_are_discarded():
    # The fake socket injects bytes representing an attempted map command.
    # There is no FCU socket or send callback in the return path.
    class Sock:
        def __init__(self):
            self.incoming = [b'attempted ARM', b'attempted SET_MODE']
            self.outgoing = []
        def recvfrom(self, size):
            if self.incoming:
                return self.incoming.pop(), ('127.0.0.1', 20420)
            raise BlockingIOError
        def sendto(self, raw, addr):
            self.outgoing.append((raw, addr))
    feed = object.__new__(ObservationFeed)
    feed.socket = Sock()
    feed.target = ('127.0.0.1', 20420)
    feed.forwarded = feed.discarded = 0
    feed.publish(b'FCU telemetry')
    assert feed.discarded == 2
    assert feed.socket.outgoing == [(b'FCU telemetry', feed.target)]


def test_only_enumerated_boot_fields_may_differ():
    reference = {'STAT_BOOTCNT': 1., 'Q_A_RAT_PIT_P': .3, 'BARO1_GND_PRESS': 81500.}
    found = {**reference, 'STAT_BOOTCNT': 2., 'BARO1_GND_PRESS': 101325.}
    result = compare_parameters(reference, found, {})
    assert not result['mismatches']
    assert set(result['boot_derived_observations']) == {'STAT_BOOTCNT', 'BARO1_GND_PRESS'}
    found['Q_A_RAT_PIT_P'] = .4
    assert set(compare_parameters(reference, found, {})['mismatches']) == {'Q_A_RAT_PIT_P'}


def test_missing_extra_and_nonfinite_parameter_rejected():
    with pytest.raises(ValueError, match='nonfinite'):
        compare_parameters({'A': 1., 'B': 2.}, {'B': float('nan'), 'C': 3.}, {})
    result = compare_parameters({'A': 1., 'B': 2.}, {'B': 5., 'C': 3.}, {})
    assert result['missing'] == ['A'] and result['extra'] == ['C']
    assert set(result['mismatches']) == {'A', 'B'}


def test_compass_auto_declination_requires_enabled_parameter():
    assert 'COMPASS_DEC' in compare_parameters({'COMPASS_DEC': 0., 'COMPASS_AUTODEC': 0.},
        {'COMPASS_DEC': .1, 'COMPASS_AUTODEC': 0.}, {})['mismatches']


def test_nonfinite_telemetry_remains_explicitly_unavailable():
    raw = {'optional': float('nan'), 'imu': [1., float('inf')], 'wire': b'\xff'}
    result = json_observation(raw)
    assert result == {'optional': {'nonfinite_MAVLink_value': 'nan'},
                      'imu': [1., {'nonfinite_MAVLink_value': 'inf'}], 'wire': {'raw_bytes_hex': 'ff'}}


def test_map_repaint_does_not_make_old_telemetry_fresh():
    obs = {'received_monotonic_s': 100., 'source_fcu_boot_s': 10., 'session_id': 'a'*32,
           'map_alive': True, 'wx_ready': True, 'position_matches_map': True}
    assert map_observation_current(obs, 'a'*32, 10.2, 100.2)
    assert not map_observation_current(obs, 'a'*32, 10.2, 102.)
    assert not map_observation_current(obs, 'a'*32, 12., 100.2)
    assert not map_observation_current(obs, 'b'*32, 10.2, 100.2)


def test_map_invalid_timestamps_rejected():
    obs = {'received_monotonic_s': float('nan'), 'source_fcu_boot_s': 10., 'session_id': 'a'*32,
           'map_alive': True, 'wx_ready': True, 'position_matches_map': True}
    assert not map_observation_current(obs, 'a'*32, 10.2, 100.2)
    assert not map_observation_current({}, 'a'*32, 10.2, 100.2)
