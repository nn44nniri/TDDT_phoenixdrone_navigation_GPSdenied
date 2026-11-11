from pathlib import Path

from tddt_tdoc.rtmo.capability_gate import CapabilityManifest
from tddt_tdoc.config import TDOCConfig

ROOT = Path(__file__).resolve().parents[1]


def _manifest(**caps):
    phases = {p: {'capability': False} for p in (
        'on_ground','vertical_takeoff','hover','transition_forward','climb','cruise',
        'turn','approach','transition_back','vertical_landing','touchdown')}
    for p, v in caps.items():
        phases[p]['capability'] = bool(v)
    return CapabilityManifest(phases=phases, source='test')


def _runtime(phase, **overrides):
    ev = {
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


def test_distant_missing_phase_does_not_block_current_phase_release():
    m = _manifest(on_ground=True, vertical_takeoff=True, hover=True)
    assert m.ready_11phase is False
    assert 'cruise' in m.blocked_phases
    r = m.runtime_release('on_ground', next_phase='vertical_takeoff', current_runtime_evidence=_runtime('on_ground'))
    assert r['actuation_release'] is True
    assert r['transition_release'] is True
    assert r['far_phase_capabilities_are_admission_blockers'] is False
    d = m.to_dict()
    assert d['mission_certification']['fully_certified'] is False
    assert d['mission_certification']['admission_authority'] is False


def test_current_phase_without_capability_is_wait_no_actuation():
    m = _manifest(on_ground=True)
    r = m.runtime_release('vertical_takeoff', next_phase='hover')
    assert r['status'] == 'WAIT_CAPABILITY'
    assert r['actuation_release'] is False
    assert 'current_phase_capability_blocked:vertical_takeoff' in r['blockers']


def test_next_phase_missing_blocks_transition_not_current_phase_cycle():
    m = _manifest(on_ground=True, vertical_takeoff=False)
    r = m.runtime_release('on_ground', next_phase='vertical_takeoff', current_runtime_evidence=_runtime('on_ground'))
    assert r['actuation_release'] is True
    assert r['next_phase_capability'] is False
    assert r['transition_release'] is False



def test_durable_capability_alone_is_not_current_runtime_release():
    m = _manifest(on_ground=True, vertical_takeoff=True)
    r = m.runtime_release('on_ground', next_phase='vertical_takeoff')
    assert r['current_phase_durable_capability'] is True
    assert r['current_phase_capability'] is True
    assert r['current_runtime_release'] is False
    assert r['actuation_release'] is False
    assert r['transition_release'] is False
    assert r['status'] == 'WAIT_RUNTIME_RELEASE'
    assert r['durable_capability_is_actuation_authority'] is False


def test_default_config_selects_phase_local_runtime_release():
    cfg = TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    assert cfg.get('tdoc.rtmo.mission.require_all_phase_capabilities') is False
    assert cfg.get('tdoc.rtmo.mission.admission_policy') == 'PHASE_LOCAL_RUNTIME_RELEASE'


def test_workflow_has_no_global_11phase_precycle_abort():
    src = (ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    assert "overall['reason']='MISSION_NOT_READY_11PHASE'" not in src
    assert 'RTMO_MISSION_CERTIFICATION.json' in src
    assert "'status':'WAIT_CAPABILITY'" in src
    assert 'runtime_release(' in src
    assert 'current_runtime_evidence=current_release_evidence' in src
