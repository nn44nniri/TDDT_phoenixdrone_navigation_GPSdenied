from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=spec_from_file_location("wf_solution1", ROOT/"scripts"/"run_training_workflows.py")
wf=module_from_spec(spec); spec.loader.exec_module(wf)

def _row(phase, *, proof=False, agl=10.0, goal=100.0, hold=False):
    return {
        "phase":phase,
        "l4_candidate_verified":bool(proof),
        "l5_final_gate":bool(proof),
        "layer2_control_effect_applied":bool(proof),
        "l5_mode":"HOLD" if hold else ("AUTO" if proof else "HOLD"),
        "agl_m":float(agl),
        "goal_distance_m":float(goal),
    }

def test_mission_taxonomy_completion_does_not_promote_incomplete_control_learning():
    rows=[_row("on_ground",agl=0.0,goal=1000.0)]
    rows += [_row(p,proof=p in {"transition_forward","climb","cruise"},hold=p not in {"transition_forward","climb","cruise"}) for p in wf.CONTROL_REQUIRED_PHASES]
    rows.append(_row("touchdown",agl=0.0,goal=0.0))
    completion=wf._mission_phase_completion(rows)
    readiness=wf._offline_control_readiness(rows,route_control_readiness_eligible=True,ff_runtime_error_cycles=0)
    assert completion["all_required_phases_seen"] is True
    assert completion["touchdown_seen"] is True
    assert completion["final_goal_distance_m"] == 0.0
    assert readiness["offline_control_ready"] is False
    assert readiness["mission_completion_used_for_control_readiness"] is False
    assert set(readiness["phase_control_coverage"]["missing_control_phases"]) == set(wf.CONTROL_REQUIRED_PHASES)-{"transition_forward","climb","cruise"}

def test_hold_or_state_propagation_never_counts_as_control_learning():
    rows=[_row("vertical_takeoff",proof=False,hold=True)]
    readiness=wf._offline_control_readiness(rows,route_control_readiness_eligible=True,ff_runtime_error_cycles=0)
    assert readiness["joint_control_proof_cycles"] == 0
    assert readiness["control_learning_cycles"] == 0
    assert readiness["non_control_learning_cycles"] == 1
    assert readiness["offline_control_ready"] is False

def test_all_required_control_phases_with_same_cycle_joint_proof_are_control_ready():
    rows=[_row(p,proof=True) for p in wf.CONTROL_REQUIRED_PHASES]
    readiness=wf._offline_control_readiness(rows,route_control_readiness_eligible=True,ff_runtime_error_cycles=0)
    assert readiness["joint_control_proof_cycles"] == len(wf.CONTROL_REQUIRED_PHASES)
    assert readiness["offline_control_ready"] is True
