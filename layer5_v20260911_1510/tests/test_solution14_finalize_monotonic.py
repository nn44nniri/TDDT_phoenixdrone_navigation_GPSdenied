import tempfile

from layer5.engine import Layer5Engine
from test_layer5 import cfg, REAL


def _engine(td):
    e=Layer5Engine(cfg(td, fixtures=False))
    e.run(REAL, "OFFLINE_SE")
    e.knowledge_quality_counts.update({
        "verified_candidate_cycles": 7,
        "final_authorized_cycles": 7,
        "closed_loop_effect_cycles": 7,
        "full_fidelity_runtime_error_cycles": 0,
    })
    return e

def test_solution14_missing_tdoc_gate_cannot_be_upgraded_to_true():
    with tempfile.TemporaryDirectory() as td:
        e=_engine(td)
        out=e.finalize_offline_episode({"knowledge_quality": {
            "phase_control_coverage": {"all_required_control_phases": True, "missing_control_phases": []}
        }})
        q=out["knowledge_quality"]
        assert q["control_knowledge_valid"] is False
        assert q["control_knowledge_valid_inputs"]["supplied_tdoc_control_valid_present"] is False
        assert q["control_knowledge_valid_inputs"]["false_to_true_upgrade_allowed"] is False

def test_solution14_missing_phase_coverage_cannot_be_upgraded_to_true():
    with tempfile.TemporaryDirectory() as td:
        e=_engine(td)
        out=e.finalize_offline_episode({"knowledge_quality": {"control_knowledge_valid": True}})
        q=out["knowledge_quality"]
        assert q["control_knowledge_valid"] is False
        assert q["control_knowledge_valid_inputs"]["phase_control_coverage_present"] is False

def test_solution14_explicit_false_remains_false_even_with_complete_global_evidence():
    with tempfile.TemporaryDirectory() as td:
        e=_engine(td)
        out=e.finalize_offline_episode({"knowledge_quality": {
            "control_knowledge_valid": False,
            "phase_control_coverage": {"all_required_control_phases": True, "missing_control_phases": []},
        }})
        assert out["knowledge_quality"]["control_knowledge_valid"] is False

def test_solution14_true_requires_all_three_inputs():
    with tempfile.TemporaryDirectory() as td:
        e=_engine(td)
        out=e.finalize_offline_episode({"knowledge_quality": {
            "control_knowledge_valid": True,
            "phase_control_coverage": {"all_required_control_phases": True, "missing_control_phases": []},
            "verified_candidate_cycles": 7,
            "final_authorized_cycles": 7,
            "closed_loop_effect_cycles": 7,
            "full_fidelity_runtime_error_cycles": 0,
        }})
        q=out["knowledge_quality"]
        assert q["control_knowledge_valid"] is True
        assert q["control_knowledge_valid_inputs"]["monotonic_input_ready"] is True
        assert q["control_knowledge_valid_inputs"]["contract"] == "Layer5-offline-control-validity-monotonic-v1"
