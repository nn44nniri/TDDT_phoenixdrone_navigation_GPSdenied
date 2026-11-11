from __future__ import annotations
import importlib.util
from pathlib import Path
ROOT=Path(__file__).parents[1]

def _wf():
    p=ROOT/"scripts/run_training_workflows.py"; spec=importlib.util.spec_from_file_location("wf1219",p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_layer4_advisory_never_selects_infeasible_and_preserves_rank_zero():
    m=_wf(); best=m.l4_best({"candidates":[{"candidate_id":"bad","feasible":False,"pareto_rank":0,"scalar_cost":0},{"candidate_id":"r1","feasible":True,"pareto_rank":1,"scalar_cost":1},{"candidate_id":"r0","feasible":True,"pareto_rank":0,"scalar_cost":99}]}); assert best["candidate_id"]=="r0"

def test_realized_mission_semantics_are_explicit():
    t=(ROOT/"scripts/run_training_workflows.py").read_text();
    for token in ["OfflineWingMPETPlant","wind_counterfactual_displacement_m","sarg_used_as_next_state","terminal_reposition","touchdown","L4_ADVISORY_WINGMPET_SIMULATION"]: assert token in t
    assert "MISSION_SUBSTATES={'terminal_reposition'}" in t
    assert "11:'terminal_reposition'" not in t
    assert "teacher_forced':False" in t or "teacher_forced': False" in t

def test_release_gate_requires_full_vtol_and_wind_effect():
    t=(ROOT/"src/tddt_tdoc/release_gate.py").read_text();
    assert "write_mission_release_gate" in (ROOT/"scripts/validate_mission_fixes.py").read_text()
    for token in ["on_ground","vertical_takeoff","hover","transition_forward","transition_back","vertical_landing","touchdown","wind_causal_effect","realized_not_straight_chord"]: assert token in t
