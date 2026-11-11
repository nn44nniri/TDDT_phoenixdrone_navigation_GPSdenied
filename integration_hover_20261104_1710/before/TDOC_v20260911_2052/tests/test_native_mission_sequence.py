"""Mission lifecycle ordering uses actual decoded FCU observations before any stick write."""
from copy import deepcopy
import time

import pytest

from layer6.command_correlation import context_from_state, identity
from layer6.contracts import DecisionEnvelope, PilotCommand
from layer6.executor import DigitalPilotExecutor
from layer6.segments import contract
from layer6.status import SCHEMA, project_status
from layer6.telemetry_source import native_source_contract
from test_r5_01_telemetry_receivers import wire_message
from test_r5_05_command_correlation import driver


class MissionClient:
    def __init__(self):
        self.events = []
        self.link = driver(native_source_contract())
        self.link.master.mode_mapping = lambda: {'QHOVER': 18, 'FBWA': 5}
        self.observation = self.observe(mode=5, armed=False)

    def observe(self, *, mode=18, armed=False):
        self.link.master.queue = [wire_message('HEARTBEAT', changes={
            'autopilot': 3, 'type': 1, 'custom_mode': mode, 'base_mode': 128 if armed else 0})]
        self.link.drain()
        p = project_status(self.link)
        return {**p, 'schema': SCHEMA, 'schema_version': 1, 'ok': True,
                'mavlink_connected': p['connected'], 'monotonic_domain_id': p['domain'],
                'telemetry_metadata': p['metadata'], 'target_system': 1, 'target_component': 1}

    def status(self, **kwargs):
        self.events.append(('status', kwargs))
        return self.observation() if callable(self.observation) else self.observation

    def action(self, action, transaction_id, **kwargs):
        self.events.append(('action', action, transaction_id, kwargs))

    def state(self, command, *, transaction_id, command_instance_id, **kwargs):
        self.events.append(('state', command, transaction_id, {'command_instance_id': command_instance_id, **kwargs}))
        # Exercise the real source-clock segment latch and actual encoded MANUAL_CONTROL write.
        segment = kwargs.get('execution_segment')
        state = {**command.as_gui_state(), 'transaction_id': transaction_id,
                 'command_instance_id': command_instance_id, '_gui_activation_id': '2' * 32,
                 'command_mode': command.mode, 'command_discrete': list(command.discrete),
                 'command_id': identity(transaction_id, command.as_gui_state(), command.mode, command.discrete, segment),
                 'execution_segment': segment, 'control_session_id': 'a' * 32,
                 'admission_deadline_unix': kwargs.get('admission_deadline')}
        context = context_from_state(state)
        assert context is not None
        return self.link.send_manual(command.elevator, command.aileron, command.throttle, command.rudder,
                                     command_context=context)

    def cancel(self, *, transaction_id, command_instance_id, reason):
        self.events.append(('cancel', transaction_id, command_instance_id, reason))
        return {'ok': True, 'cancelled': True, 'replay_fence_installed': True,
                'transaction_id': transaction_id, 'command_instance_id': command_instance_id}

    def segment_notices(self):
        return []


def envelope(*, mode='QHOVER', discrete=('ARM',)):
    return DecisionEnvelope('mission-tx', PilotCommand(.1, -.1, .6, 0., mode, discrete),
                            tau_deadline=time.time() + 20., constraints={'direct_pwm': False},
                            safety={'psf': True, 'rta': True},
                            provenance={'execution_scope': 'SITL_CLOSED_LOOP'},
                            execution_segment=contract({'execution_segment_s': .1}))


def actions(client):
    return [event[1] for event in client.events if event[0] == 'action']


def assert_no_pilot_write(client):
    assert not [event for event in client.events if event[0] == 'state']
    assert not client.link.master.port.wires
    assert not client.link.segments.records


def test_mode_then_actual_armed_heartbeat_then_first_state_and_segment():
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    command = envelope()
    executor.accept(command)
    instance = executor.active_instance_id
    assert actions(client) == ['qhover']
    assert_no_pilot_write(client)
    executor.tick()  # FCU remains in FBWA: no ARM and no stick movement.
    assert set(actions(client)) == {'qhover'}
    assert_no_pilot_write(client)
    client.observation = client.observe(armed=False)
    executor._last_lifecycle_action = 0.
    executor.tick()  # Actual mode is QHOVER; request ARM, still no sticks.
    assert actions(client)[-1] == 'arm'
    assert_no_pilot_write(client)
    client.observation = client.observe(armed=True)
    executor.tick()
    states = [event for event in client.events if event[0] == 'state']
    assert len(states) == 1
    assert states[0][3]['execution_segment'] == command.execution_segment
    assert executor.active_instance_id == instance
    assert all(event[2] == 'mission-tx' and event[3]['command_instance_id'] == instance
               for event in client.events if event[0] in ('action', 'state'))
    assert set(actions(client)) == {'qhover', 'arm'}  # No force-arm command or flag exists.
    assert all(set(event[3]) == {'command_instance_id'} for event in client.events if event[0] == 'action')
    assert len(client.link.segments.records) == 1
    segment = client.link.segments.current_record()
    assert segment['state'] == 'ACTIVE' and segment['context']['transaction_id'] == 'mission-tx'


@pytest.mark.parametrize('observation', ['timeout', 'wrong_mode', 'disarmed', 'stale', 'missing_heartbeat'])
def test_unconfirmed_lifecycle_never_moves_sticks_or_opens_segment(observation):
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    executor.accept(envelope())
    client.observation = client.observe(mode=5 if observation == 'wrong_mode' else 18,
                                        armed=observation != 'disarmed')
    if observation == 'timeout': client.observation = None
    elif observation == 'stale':
        client.observation = deepcopy(client.observation)
        client.observation['telemetry_metadata']['HEARTBEAT']['freshness']['fresh'] = False
    elif observation == 'missing_heartbeat': client.observation['telemetry_metadata'].pop('HEARTBEAT')
    for _ in range(3):
        assert executor.tick() is True
    assert_no_pilot_write(client)
    if observation in ('timeout', 'stale', 'missing_heartbeat', 'wrong_mode'):
        assert 'arm' not in actions(client)


def test_disarmed_fcu_without_arm_authority_cannot_be_armed_by_executor():
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    executor.accept(envelope(discrete=()))
    client.observation = client.observe(armed=False)
    executor.tick()
    assert 'arm' not in actions(client)
    assert_no_pilot_write(client)


@pytest.mark.parametrize('armed', [False, True])
def test_zero_touchdown_without_discrete_preserves_current_armed_state(armed):
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    command = envelope(discrete=())
    command.command = PilotCommand(0., 0., 0., 0., 'QHOVER', ())
    executor.accept(command)
    client.observation = client.observe(armed=armed)
    assert executor.tick() is True
    assert actions(client) == ['qhover']
    assert len([event for event in client.events if event[0] == 'state']) == 1
    assert len(client.link.segments.records) == 1


def test_deadline_expiring_during_status_query_cancels_without_action_or_state(monkeypatch):
    import layer6.executor as module
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    now = [1000.]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    executor.accept(envelope())
    initial_actions = actions(client)

    def delayed_status():
        now[0] += 21.
        return client.observe(armed=False)

    client.observation = delayed_status
    assert executor.tick() is False
    assert executor.active is None and executor.last_timeout_stop['cancelled']
    assert actions(client) == initial_actions
    assert_no_pilot_write(client)


@pytest.mark.parametrize('remaining,expected_timeout', [(.04, .04), (.5, .12)])
def test_startup_status_timeout_respects_gui_cadence_and_remaining_lifetime(monkeypatch, remaining, expected_timeout):
    import layer6.executor as module
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    monkeypatch.setattr(module.time, 'time', lambda: 1000.)
    command = envelope()
    command.tau_deadline = 1000. + remaining
    executor.accept(command)
    client.observation = None
    assert executor.tick() is True
    queries = [event[1] for event in client.events if event[0] == 'status']
    assert queries == [{'timeout': pytest.approx(expected_timeout)}]
    assert_no_pilot_write(client)


def test_disarm_is_confirmed_by_telemetry_before_final_zero_state():
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    command = envelope(discrete=('DISARM',))
    command.command = PilotCommand(0., 0., 0., 0., 'QHOVER', ('DISARM',))
    executor.accept(command)
    client.observation = client.observe(armed=True)
    executor.tick()
    assert actions(client)[-1] == 'disarm'
    assert_no_pilot_write(client)
    client.observation = client.observe(armed=False)
    executor.tick()
    assert [event for event in client.events if event[0] == 'state']


@pytest.mark.parametrize('mode,discrete', [('UNKNOWN', ('ARM',)), ('QHOVER', ('ARM', 'DISARM'))])
def test_invalid_mission_lifecycle_rejects_before_any_action(mode, discrete):
    client = MissionClient()
    executor = DigitalPilotExecutor(client)
    with pytest.raises(ValueError, match='mission lifecycle'):
        executor.accept(envelope(mode=mode, discrete=discrete))
    assert client.events == [] and executor.active is None
