"""Real-order equal-epoch clock/position routing, without FCU or external datasets."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ENV = Path(__file__).resolve().parents[1]
NATIVE = ENV.parents[1]
sys.path.insert(0, str(ENV))
sys.path.insert(0, str(NATIVE / 'src'))
from phoenix_env_adapter import bound
from layer6 import binding as api


class Timeline:
    now = 100.


def message(channel, epoch, *, system=1, component=1):
    return SimpleNamespace(time_boot_ms=epoch, lat=400000000, lon=-1050000000, alt=1900000,
                           get_type=lambda: channel, get_srcSystem=lambda: system,
                           get_srcComponent=lambda: component)


class Link:
    def __init__(self, timeline, schedule, readback_delay=0.):
        self.timeline = timeline
        self.schedule = list(schedule)
        self.readback_delay = readback_delay
        self.delayed = False
        self.parameters = {}
        self.replies = []
        self.closed = False
        self.mav = self

    def wait_heartbeat(self, **kwargs):
        return message('HEARTBEAT', 6000)

    def recv_match(self, **kwargs):
        if kwargs.get('type') == 'PARAM_VALUE':
            return self.replies.pop(0) if self.replies else None
        if not self.schedule:
            return None
        stamp, value = self.schedule.pop(0)
        self.timeline.now = stamp
        return value

    def param_set_send(self, system, component, name, value, kind):
        self.parameters[name] = value
        self.reply(name, value)

    def param_request_read_send(self, system, component, name, index):
        if not self.delayed:
            self.timeline.now += self.readback_delay
            self.delayed = True
        self.reply(name, self.parameters[name])

    def reply(self, name, value):
        self.replies.append(SimpleNamespace(param_id=name, param_value=value,
                            get_srcSystem=lambda: 1, get_srcComponent=lambda: 1))

    def close(self):
        self.closed = True


class Provider:
    def __init__(self, timeline):
        self.timeline = timeline
        self.api = api
        self.samples = []
        self.published = []
        self.context_checks = 0
        self.binding = {
            'binding_id': 'test-binding', 'binding_hash': 'bound', 'context_sha256': 'context',
            'model_id': 'model', 'handoff_hash': 'handoff', 'forcing_file_sha256': 'forcing',
            'effective_climate_epoch': '2026-11-04T00:00:00', 'plant_epoch_s': 0.,
            'forcing': {'common_start': '2026-11-04T00:00:00', 'common_end': '2026-11-05T00:00:00'},
            'time_contract': {'clock_domain': 'accepted-local-naive', 'timezone_status': 'unspecified',
                              'utc_mapping_verified': False}}

    def sample(self, plant, *, source, **position):
        self.samples.append((plant, source, deepcopy(position)))
        timestamp, elapsed = api.forcing_time(self.binding, plant)
        return {**api.consumption_identity(self.binding), 'plant_time_s': plant, 'plant_time_source': source,
                'mission_elapsed_s': elapsed, 'simulation_time_dataset': timestamp,
                'updated_monotonic': self.timeline.now, 'environment_query': {'time': timestamp, **position},
                'clock_domain': 'accepted-local-naive', 'timezone_status': 'unspecified',
                'UTC_mapping_verified': False, 'runtime_clock_synchronization_verified': False,
                'actuation_authority': False, 'navigation_truth_injection': False,
                'unsupported_fields': bound.unsupported_fields(), 'ok': False,
                'model_outputs': {'wind_u_3dw': -3., 'wind_v_3dw': -4., 'formula_valid': 1., 'wind_3dw_valid': 1.}}

    def check_context(self):
        self.context_checks += 1

    def publish(self, path, state):
        self.published.append(deepcopy(state))
        bound.write_state(path, state)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    timeline = Timeline()
    provider = Provider(timeline)
    monkeypatch.setattr(bound, 'BoundEnvironment', lambda _: provider)
    monkeypatch.setattr(bound, '_layer6', lambda: api)
    monkeypatch.setattr(bound.time, 'monotonic', lambda: timeline.now)

    def run(schedule, *, updates=1, readback_delay=0.):
        link = Link(timeline, schedule, readback_delay)
        monkeypatch.setitem(sys.modules, 'pymavlink', SimpleNamespace(
            mavutil=SimpleNamespace(mavlink_connection=lambda _: link)))
        path = tmp_path / 'environment.json'
        return link, path, lambda: bound.run_bound_runtime('mock-binding', path,
                     mavlink='mock-only', max_updates=updates)

    return timeline, provider, run


def test_one_hz_attitude_then_same_epoch_gpi_produces_two_verified_updates(runtime):
    timeline, provider, run = runtime
    link, path, execute = run([
        (100., message('ATTITUDE', 6000)), (100.01, message('GLOBAL_POSITION_INT', 6000)),
        (101.02, message('ATTITUDE', 7000)), (101.03, message('GLOBAL_POSITION_INT', 7000))], updates=2)
    state = execute()
    assert state['ok'] and link.closed and provider.context_checks == 2
    ready = [s for s in provider.published if s.get('ok')]
    assert len(ready) == 2
    assert [sample[0] for sample in provider.samples] == [6., 7.]
    assert all(sample[1] == 'MAVLink/ATTITUDE.time_boot_ms' for sample in provider.samples)
    assert [s['clock_mapping']['observation']['received_monotonic_s'] for s in ready] == [100., 101.02]
    assert [s['environment_position_received_monotonic'] for s in ready] == [100.01, 101.03]
    assert state['clock_mapping']['measured_rate_diagnostic']['delta_plant_s'] == 1.
    assert not state['clock_mapping']['first_packet_rebase']
    assert json.loads(path.read_text())['ok']


@pytest.mark.parametrize('fault', ['no_position', 'duplicate_position', 'reversed_attitude', 'foreign_position', 'stale_position'])
def test_bad_missing_or_unadvanced_observations_never_refresh_readiness(runtime, fault):
    timeline, provider, run = runtime
    if fault == 'no_position':
        schedule = [(100., message('ATTITUDE', 6000)), (101., message('ATTITUDE', 7000)),
                    (102.1, message('ATTITUDE', 8000))]
    elif fault == 'foreign_position':
        schedule = [(100., message('ATTITUDE', 6000)), (100.01, message('GLOBAL_POSITION_INT', 6000, system=2)),
                    (101., message('ATTITUDE', 7000)), (102.1, message('ATTITUDE', 8000))]
    else:
        schedule = [(100., message('ATTITUDE', 6000)), (100.01, message('GLOBAL_POSITION_INT', 6000))]
        if fault == 'duplicate_position':
            schedule += [(101., message('GLOBAL_POSITION_INT', 6000)), (102.1, message('GLOBAL_POSITION_INT', 6000))]
        elif fault == 'reversed_attitude':
            schedule += [(101., message('ATTITUDE', 7000)), (101.01, message('ATTITUDE', 6500))]
        else:  # A newer clock cannot reuse position older than the .25 s readback reserve.
            schedule += [(100.5, message('ATTITUDE', 6500))]
    link, path, execute = run(schedule, updates=2)
    with pytest.raises((RuntimeError, ValueError)):
        execute()
    assert link.closed and not json.loads(path.read_text())['ok']
    assert len(provider.samples) == (0 if fault in ('no_position', 'foreign_position') else 1)


@pytest.mark.parametrize('position_delay,readback_delay', [(1.1, 0.), (.01, 1.1)])
def test_late_clock_witness_or_slow_readback_cannot_be_published_ready(runtime, position_delay, readback_delay):
    timeline, provider, run = runtime
    _, _, execute = run([(100., message('ATTITUDE', 6000)),
                         (100. + position_delay, message('GLOBAL_POSITION_INT', 6000))],
                         readback_delay=readback_delay)
    state = execute()
    assert not state['ok']
    assert state['clock_mapping']['observation']['received_monotonic_s'] == 100.
    assert not any(s.get('ok') for s in provider.published)


def test_standard_overlay_install_and_repeat_preserve_fixed_runtime(tmp_path):
    helper = NATIVE / 'scripts/apply_r4_02_environment_binding.py'
    spec = importlib.util.spec_from_file_location('epoch_overlay_installer', helper)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    assets = NATIVE / 'resources/r4_02_v1'
    manifest = json.loads((assets / 'OVERLAY_MANIFEST.json').read_text())
    # Existing accepted CLI plus freshly extracted, absent bound/native modules.
    cli = tmp_path / 'phoenix_env_adapter/cli.py'
    cli.parent.mkdir()
    cli.write_bytes((assets / 'phoenix_env_adapter/cli.py').read_bytes())
    assert installer.apply(tmp_path)['status'] == 'PASS'
    assert installer.apply(tmp_path)['status'] == 'PASS'
    deployed = tmp_path / 'phoenix_env_adapter/bound.py'
    canonical = assets / 'phoenix_env_adapter/bound.py'
    assert deployed.read_bytes() == canonical.read_bytes() == (ENV / 'phoenix_env_adapter/bound.py').read_bytes()
    assert hashlib.sha256(deployed.read_bytes()).hexdigest() == manifest['files']['phoenix_env_adapter/bound.py']['after_sha256']
    assert '54e3167034f27a5d87674fe3e35f849f82654faa7121bc8a27a3641241d3d3f7' in manifest['files']['phoenix_env_adapter/bound.py']['upgrade_from_sha256']
