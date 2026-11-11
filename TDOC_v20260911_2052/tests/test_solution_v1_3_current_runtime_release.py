from pathlib import Path

from tddt_tdoc.rtmo.capability_gate import CapabilityManifest
from tddt_tdoc.rtmo.phase_manager import PHASE_ORDER

ROOT = Path(__file__).resolve().parents[1]


def _manifest(phase='vertical_takeoff', cap=True):
    phases={p:{'capability':False} for p in PHASE_ORDER}
    phases[phase]={'capability':bool(cap)}
    return CapabilityManifest(phases=phases,source='p0-3-test')


def _runtime(phase='vertical_takeoff', **overrides):
    ev={
        'schema':'TDOC-RTMO-current-runtime-release-evidence-v1',
        'phase':phase,
        'learning_mode':'ONLINE_JSBSIM',
        'transaction_id':f'tx-{phase}',
        'fresh_operational_state':True,
        'current_l4_artifact_verified':True,
        'current_l4_artifact_compatible':True,
        'current_l5_final':True,
        'clock_freshness_ok':True,
        'layer6_ready':True,
    }
    ev.update(overrides)
    return ev


def test_p0_3_durable_capability_is_historical_only():
    r=_manifest().runtime_release('vertical_takeoff')
    assert r['current_phase_durable_capability'] is True
    assert r['current_phase_capability'] is True
    assert r['current_runtime_release'] is False
    assert r['actuation_release'] is False
    assert r['transition_release'] is False
    assert r['status']=='WAIT_RUNTIME_RELEASE'
    assert r['durable_capability_is_actuation_authority'] is False
    assert 'current_runtime_evidence_missing' in r['blockers']


def test_p0_3_current_runtime_release_requires_all_current_cycle_gates():
    keys=(
        'fresh_operational_state','current_l4_artifact_verified',
        'current_l4_artifact_compatible','current_l5_final',
        'clock_freshness_ok','layer6_ready',
    )
    for key in keys:
        r=_manifest().runtime_release(
            'vertical_takeoff', current_runtime_evidence=_runtime(**{key:False})
        )
        assert r['current_runtime_release'] is False, key
        assert r['actuation_release'] is False, key
        assert r['status']=='WAIT_RUNTIME_RELEASE', key


def test_p0_3_full_current_online_evidence_can_release_durable_phase():
    r=_manifest().runtime_release(
        'vertical_takeoff', current_runtime_evidence=_runtime('vertical_takeoff')
    )
    assert r['current_phase_durable_capability'] is True
    assert r['current_runtime_release'] is True
    assert r['actuation_release'] is True
    assert r['status']=='RELEASED'


def test_p0_3_offline_or_wrong_phase_runtime_evidence_is_rejected():
    offline=_runtime('vertical_takeoff',learning_mode='OFFLINE_SE')
    r=_manifest().runtime_release('vertical_takeoff',current_runtime_evidence=offline)
    assert r['actuation_release'] is False
    assert 'current_runtime_evidence_not_online' in r['blockers']

    wrong=_runtime('hover')
    r2=_manifest().runtime_release('vertical_takeoff',current_runtime_evidence=wrong)
    assert r2['actuation_release'] is False
    assert 'current_runtime_evidence_phase_mismatch' in r2['blockers']


def test_p0_3_workflow_builds_explicit_current_runtime_release_evidence():
    src=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    assert "'schema':'TDOC-RTMO-current-runtime-release-evidence-v1'" in src
    assert "'durable_capability_used_as_actuation_authority':False" in src
    assert 'current_runtime_evidence=current_release_evidence' in src
    assert "_wait_status=('WAIT_CAPABILITY' if not bool(release.get('current_phase_capability')) else 'WAIT_RUNTIME_RELEASE')" in src
