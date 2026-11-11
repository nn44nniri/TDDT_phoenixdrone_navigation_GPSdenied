"""Fresh Offline FF consumes its real current model, never its future handoff."""
from copy import deepcopy
from pathlib import Path

import pytest

from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.config import TDOCConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def current_model(tmp_path_factory):
    temporary = tmp_path_factory.mktemp("offline_ff_current_model")
    config = TDOCConfig.from_yaml(ROOT / "configs/tdoc.yaml")
    config.data['runtime']['root'] = str(temporary / 'runtime')
    layer2 = Layer2Adapter(config)
    layer5 = Layer5Adapter(config, kstore_root=temporary / 'kstore')
    try:
        layer5._ensure()
        seeds = layer5.offline_vtol_seed_controls({'air_density_kg_m3': 1.225, 'battery_soc': 1.,
                                                 'wind_ned_m_s': [0., 0., 0.]})
        assert seeds['ok'], seeds
        certificate = layer2.derive_offline_phoenix_model_certificate(
            layer5.phoenix_profile_sha256(), phase_seed_controls=seeds,
            airframe_binding_sha256=layer5.phoenix_airframe_binding_sha256())
        assert certificate['ok'], certificate
        assert layer5.apply_offline_model_certificate(certificate)['ok']
        assert layer5.set_verification_model_certificate(certificate)['ok']
        assert not (config.runtime_root / 'knowledge/handoff/offline_latest.json.gz').exists()
        command = [0., 0., certificate['trim']['throttle_request_norm'], 0.]
        candidate = {'candidate_id': 'source-model-FF', 'tdoc_phase': 'cruise',
                     'logical_actions': [command], 'phoenix_operating_context': {'tdoc_learning_mode': 'OFFLINE_SE'}}
        yield layer5, certificate, candidate
    finally:
        layer2.close()
        layer5.close()


def test_actual_source_qualified_offline_bridge_needs_no_future_handoff(current_model, monkeypatch):
    layer5, certificate, candidate = current_model
    def forbidden_prior_lookup():
        raise AssertionError('fresh Offline must not look up its future handoff')
    monkeypatch.setattr(layer5, 'ensure_native_control_binding', forbidden_prior_lookup)
    bridge = layer5.full_fidelity_action_bridge(deepcopy(candidate))
    assert bridge['ok'], bridge
    assert bridge['tdoc_learning_mode'] == 'OFFLINE_SE'
    assert bridge['logical_actions'] == candidate['logical_actions']
    physical = bridge['physical_actions'][0]
    assert physical['left_motor_cmd'] == physical['right_motor_cmd'] == certificate['throttle_path']['trim_norm']
    assert bridge['execution_mapping_separate'] is True
    assert bridge['verification_mapping'] == 'WingMPET simulation-only model-qualified effective-action certificate'
    assert 'layer6_package' not in bridge and 'native_execution_authorized' not in bridge


@pytest.mark.parametrize('mode', ['ONLINE_JSBSIM', None])
def test_actual_bridge_without_valid_online_prior_remains_rejected(current_model, mode):
    layer5, _, candidate = current_model
    candidate = deepcopy(candidate)
    if mode is None:
        candidate['phoenix_operating_context'].pop('tdoc_learning_mode')
    else:
        candidate['phoenix_operating_context']['tdoc_learning_mode'] = mode
    bridge = layer5.full_fidelity_action_bridge(candidate)
    assert not bridge['ok']
    assert bridge['reason'] == 'native_verification_requires_accepted_offline_prior'
    assert bridge['mixer_record']['prior']['ok'] is False


def test_offline_mode_preserves_source_identity_and_command_envelope(current_model, monkeypatch):
    layer5, certificate, candidate = current_model
    stale = deepcopy(certificate)
    stale['model_dependencies']['model_id'] = '0' * 64
    monkeypatch.setattr(layer5, '_verification_model_certificate', stale)
    # Exercise production behavior, without the legacy PYTEST-only mixer double.
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    bridge = layer5.full_fidelity_action_bridge(deepcopy(candidate))
    assert not bridge['ok'] and 'model_dependencies' in bridge['reason']


def test_offline_mode_does_not_expand_model_throttle_bounds(current_model):
    layer5, certificate, candidate = current_model
    candidate = deepcopy(candidate)
    candidate['logical_actions'][0][2] = min(1., certificate['trim']['throttle_request_norm'] + .2)
    bridge = layer5.full_fidelity_action_bridge(candidate)
    assert not bridge['ok'] and bridge['reason'] == 'wingmpet_verification_throttle_outside_qualified_neighborhood'
