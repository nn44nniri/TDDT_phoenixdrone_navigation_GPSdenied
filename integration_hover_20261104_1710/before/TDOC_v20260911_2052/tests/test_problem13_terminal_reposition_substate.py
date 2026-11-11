from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT=Path(__file__).parents[1]

def _wf():
    p=ROOT/"scripts/run_training_workflows.py"
    spec=importlib.util.spec_from_file_location("wf_problem13",p)
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_problem13_taxonomy_is_exactly_eleven_phases():
    m=_wf()
    from tddt_tdoc.rtmo.phase_manager import PHASE_ORDER
    assert len(PHASE_ORDER)==11
    assert "terminal_reposition" not in PHASE_ORDER
    assert "terminal_reposition" not in set(m.PHASES.values())
    assert m.MISSION_SUBSTATES=={"terminal_reposition"}

def test_problem13_terminal_reposition_stays_transition_back_until_capture():
    m=_wf()
    phase,substate,captured=m._offline_terminal_phase(horizontal_m=80.0,agl_m=70.0,capture_m=25.0,terminal_captured=False)
    assert phase=="transition_back"
    assert substate=="terminal_reposition"
    assert captured is False
    phase2,substate2,captured2=m._offline_terminal_phase(horizontal_m=10.0,agl_m=20.0,capture_m=25.0,terminal_captured=False)
    assert phase2=="vertical_landing" and substate2 is None and captured2 is True

def test_problem13_terminal_reposition_cannot_enter_layer4_as_a_phase():
    from tddt_tdoc.adapters.layer4 import Layer4Adapter
    c=Layer4Adapter._full_fidelity_stage_contract({"stage":"terminal_reposition"})
    assert c["applicable"] is False
    assert c["passive_boundary_phase"] is False
    assert c["reason"]=="full_fidelity_not_applicable_phase"

def test_problem13_reports_have_substate_field_and_canonical_phase_only():
    t=(ROOT/"scripts/run_training_workflows.py").read_text()
    assert "'tdoc_substate':tdoc_substate" in t
    assert "'PHASE_HISTORY.csv':['cycle','flight_elapsed_s','reference_time_s','phase','tdoc_substate'" in t
    assert "req['context']['tdoc_substate']=tdoc_substate" in t
