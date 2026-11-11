from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_problem09_workflow_uses_phase_execution_capability_and_native_mixer():
    s=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    assert 'phase_execution_capability' in s
    assert 'phase_supported' in s
    assert 'vtol_exec_ready' in s
    assert "vtol_autopilot_controller_validated':bool" in s
    assert 'Layer5 external controller -> Layer6 -> ArduPilot authoritative mixer -> JSBSim' in s
    assert 'Layer5 external controller -> PT_LIB -> ArduPilot' not in s
    assert 'direct PWM forbidden' in s


def test_problem09_layer5_adapter_exports_phase_execution_capability():
    s=(ROOT/'src/tddt_tdoc/adapters/layer5.py').read_text(encoding='utf-8')
    assert 'phase_execution_capability' in s
from tddt_tdoc.rtmo import build_capability_manifest

def test_problem09_phase_execution_override_removes_only_l6_blocker():
    overrides={'vertical_takeoff':True,'hover':True,'transition_back':True,'vertical_landing':True}
    m=build_capability_manifest(ROOT/'reports',layer6_forward_only=False,execution_overrides=overrides)
    for p in overrides:
        assert m.phases[p]['execution_supported'] is True
        assert 'layer6_execution_profile_not_validated_for_phase' not in m.phases[p]['blockers']
        # Problem #9 must not synthesize L4/L5 success.
        if not m.phases[p]['l4_verified'] or not m.phases[p]['l5_admissible']:
            assert m.phases[p]['capability'] is False
