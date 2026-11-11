from tddt_tdoc.adapters.layer4 import Layer4Adapter
from copy import deepcopy
from types import SimpleNamespace

import pytest

VALID={"NOT_ATTEMPTED","ATTEMPTED_AND_REJECTED","ATTEMPTED_AND_VERIFIED","EXCEPTION"}


def test_p0_8_tdoc_ff_outcome_contract_is_explicit():
    row=Layer4Adapter._ff_outcome(
        "ATTEMPTED_AND_REJECTED","terrain_clearance",
        reason_codes=["terrain_clearance","full_physical_feasibility_gate_failed"],
        attempted=True,executed=True,phase="transition_forward",
    )
    assert row["schema"]=="Layer4-FF-verification-outcome-v1"
    assert row["outcome_status"] in VALID
    assert row["attempted"] is True and row["executed"] is True
    assert row["verified"] is False
    assert row["reason_codes"]==["terrain_clearance","full_physical_feasibility_gate_failed"]


def test_p0_8_verified_outcome_requires_traceable_certificate_by_convention():
    row=Layer4Adapter._ff_outcome(
        "ATTEMPTED_AND_VERIFIED","ff_certificate_reuse",
        reason_codes=["ff_certificate_reuse"],verified=True,
        certificate_id="cert-123",certificate_persisted=True,
        verification_origin="FF_CERT_REUSE",
    )
    assert row["verified"] is True
    assert row["outcome_status"]=="ATTEMPTED_AND_VERIFIED"
    assert row["certificate_id"]=="cert-123"


def test_p0_8_exception_has_error_id_helper():
    eid=Layer4Adapter._ff_error_id("C0002","tdoc_full_fidelity_runtime_failed","boom")
    assert eid.startswith("fferr-") and len(eid)>10


@pytest.mark.parametrize('phase', ['vertical_takeoff', 'hover', 'transition_forward', 'transition_back',
                                   'vertical_landing', 'climb', 'cruise', 'turn', 'approach'])
@pytest.mark.parametrize('mode', ['OFFLINE_SE', 'ONLINE_JSBSIM'])
def test_current_ff_context_rebuild_preserves_actual_tdoc_mode_at_bridge(phase, mode):
    """Exercise the real caller up to its bridge boundary, then deliberately stop.

    No fake successful FF/certificate is supplied. Both certificate lookups miss,
    so this reaches the second phase-context rebuild that previously discarded
    Offline mode. The bridge records its input and rejects further execution.
    """
    from layer4.models import Candidate
    from layer4.multifidelity import CertifiedMultiFidelityManager
    captures = []
    def capture_and_reject(candidate):
        captures.append(deepcopy(candidate))
        return {'ok': False, 'reason': 'test_stop_after_mode_capture'}
    adapter = Layer4Adapter.__new__(Layer4Adapter)
    adapter.layer3_adapter = object()
    adapter.config = SimpleNamespace(get=lambda key, default=None: default)
    adapter.layer5_adapter = SimpleNamespace(full_fidelity_action_bridge=capture_and_reject)
    adapter.engine = SimpleNamespace(
        config=SimpleNamespace(top_k_ff=1, dt=1., get=lambda key, default=None: default),
        adapter=SimpleNamespace(adapt=lambda snap: SimpleNamespace(context={'stage': phase})),
        mf=SimpleNamespace(find=lambda *args: None, find_rejection=lambda *args: None,
                           phase=CertifiedMultiFidelityManager.phase), param_hash='test-no-certificate')
    adapter._verification_template = lambda: {}
    adapter._verification_model_identity = lambda request: 'test-no-positive-FF-authority'
    adapter._scenario_map = lambda snapshot: {}
    candidate = Candidate('mode-carrier', 'unit-scenario', 'unit-v1', 'unit-hash', 1.,
                  [[0.] * 19, [0.] * 19], [], [], [[0.] * 5], {'mission': 0.}, feasible=True).to_dict()
    # Deliberately conflicting incoming metadata must be replaced by TDOC mode.
    candidate['phoenix_operating_context'] = {'tdoc_learning_mode': 'ONLINE_JSBSIM' if mode == 'OFFLINE_SE' else 'OFFLINE_SE'}
    snapshot = {'std': {'context': {'stage': phase}, 'representations': {
        'MicroMetTwin': {'canonical_state': {'air_density_kg_m3': 1.18}}}}}
    result = adapter._tdoc_full_fidelity({'candidates': [candidate]}, snapshot,
                         {'transaction_id': 'mode-boundary-test', 'stage': phase, 'learning_mode': mode})
    assert len(captures) == 1, result
    delivered = captures[0]
    assert delivered['tdoc_phase'] == phase
    assert delivered['phoenix_operating_context']['tdoc_learning_mode'] == mode
    if phase in {'vertical_takeoff', 'hover', 'transition_forward', 'transition_back', 'vertical_landing'}:
        assert delivered['phoenix_operating_context']['air_density_kg_m3'] == 1.18
        assert delivered['phoenix_operating_context']['source'] == 'same_cycle_L3_STD+physical_replay_initial_state'
    assert result['verified_candidates'] == []
    verification = result['candidates'][0]['verification']
    assert verification['verified'] is False and verification['executed'] is False
    assert verification['outcome_status'] == 'NOT_ATTEMPTED'
    assert 'test_stop_after_mode_capture' in verification['reason_codes']
