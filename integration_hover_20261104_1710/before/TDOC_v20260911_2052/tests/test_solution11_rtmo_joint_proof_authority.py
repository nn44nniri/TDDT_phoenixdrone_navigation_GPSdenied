from pathlib import Path

from tddt_tdoc.rtmo.capability_registry import LiveCapabilityRegistry
from tddt_tdoc.rtmo.capability_gate import build_capability_manifest


def _l4(verified: bool, n: int):
    return {
        'snapshot_version': f'v{n}', 'snapshot_hash': f'h{n}',
        'verified_candidates': [{'candidate_id': 'C1'}] if verified else [],
        'candidates': [{'candidate_id': 'C1', 'feasible': True}],
        'provenance': {'model_hash': 'model-A', 'parameter_hash': 'param-A'},
    }


def _l5(final: bool, n: int):
    return {
        'gates': {'FINAL': bool(final)},
        'decision': {'decision_id': f'D{n}', 'approval': {'final_gate': bool(final)}},
    }


def test_split_l4_l5_evidence_cannot_promote_rtmo_capability(tmp_path: Path):
    reg = LiveCapabilityRegistry(tmp_path/'runtime/rtmo/capability_registry.json.gz')
    reg.reset(run_id='run-s11', episode_id='OFFLINE_SE-001')
    # Cycle 1: L4 verified, but L5 not FINAL.
    reg.record_cycle(phase='hover', cycle_id=1, reference_time_s=1.0,
                     layer4_result=_l4(True, 1), layer5_result=_l5(False, 1),
                     control_effect_applied=False)
    # Cycle 2: L5 FINAL, but no L4 verification/control effect.
    reg.record_cycle(phase='hover', cycle_id=2, reference_time_s=2.0,
                     layer4_result=_l4(False, 2), layer5_result=_l5(True, 2),
                     control_effect_applied=False)
    man = build_capability_manifest(tmp_path/'reports', layer6_forward_only=False,
                                    capability_registry_path=reg.path,
                                    require_live_registry=True, require_certificate_guard=False)
    h = man.phases['hover']
    assert h['l4_verified'] is True
    assert h['l5_admissible'] is True
    assert h['same_cycle_joint_control_proof'] is False
    assert h['capability'] is False
    assert 'no_same_cycle_joint_control_proof_for_phase' in h['blockers']


def test_same_cycle_joint_proof_naturally_enables_rtmo_control_evidence(tmp_path: Path):
    reg = LiveCapabilityRegistry(tmp_path/'runtime/rtmo/capability_registry.json.gz')
    reg.reset(run_id='run-s11', episode_id='OFFLINE_SE-001')
    reg.record_cycle(phase='hover', cycle_id=8, reference_time_s=7.001,
                     layer4_result=_l4(True, 8), layer5_result=_l5(True, 8),
                     control_effect_applied=True,
                     context={'effective_climate_epoch': 'epoch-A', 'effective_climate_fingerprint': 'climate-A'})
    man = build_capability_manifest(tmp_path/'reports', layer6_forward_only=False,
                                    capability_registry_path=reg.path,
                                    require_live_registry=True, require_certificate_guard=False)
    h = man.phases['hover']
    assert h['joint_control_proof_cycles'] == [8]
    assert h['same_cycle_joint_control_proof'] is True
    # No RTMO bypass: capability follows actual same-cycle upstream evidence.
    assert h['capability'] is True
    assert h['blockers'] == []


def test_capability_manifest_schema_exposes_joint_proof_contract(tmp_path: Path):
    reg = LiveCapabilityRegistry(tmp_path/'runtime/rtmo/capability_registry.json.gz')
    reg.reset(run_id='run-s11', episode_id='OFFLINE_SE-001')
    reg.record_cycle(phase='vertical_takeoff', cycle_id=3, reference_time_s=2.001,
                     layer4_result=_l4(True, 3), layer5_result=_l5(True, 3),
                     control_effect_applied=True)
    obj = build_capability_manifest(tmp_path/'reports', layer6_forward_only=False,
                                    capability_registry_path=reg.path,
                                    require_live_registry=True, require_certificate_guard=False).to_dict()
    assert obj['schema'] == 'TDOC-RTMO-phase-capability-v7'
    assert obj['phases']['vertical_takeoff']['evidence_type'].startswith('LIVE_RUNTIME_JOINT_CONTROL_PROOF')
