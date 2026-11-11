from pathlib import Path
import math
from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _phoenix():
    root=Path(__file__).resolve().parents[1]
    return PhoenixSpecialization(Layer5Config.from_yaml(root/"configs/configuration_phoenix.yaml"))

def _candidate(pd0=0.0,pd1=0.0,vd0=0.0,vd1=0.0):
    a=[0.0]*19; b=[0.0]*19
    a[2]=pd0; b[2]=pd1; a[5]=vd0; b[5]=vd1
    a[9]=2.0; b[9]=2.0
    return {"tdoc_phase":"hover","trajectory":[a,b],"actions":[[0.0]*5,[0.0]*5]}

def test_hover_seed_is_source_motor_force_balance_not_arbitrary_throttle_offset():
    ph=_phoenix(); seed=ph.offline_vtol_seed_controls(); pb=seed["physics_basis"]
    assert seed["ok"] is True
    assert pb["hover_force_balance_valid"] is True
    expected=math.sqrt(pb["required_hover_thrust_N"]/pb["available_total_thrust_N"])
    rec=seed["phases"]["hover"]
    assert rec["operating_point_basis"]=="phoenix_source_motor_law_hover_force_balance"
    assert abs(rec["throttle_request_norm"]-expected)<1e-12

def test_hover_nominal_controller_uses_source_inverse_to_brake_climb():
    ph=_phoenix(); base=ph.offline_vtol_seed_controls()["phases"]["hover"]["throttle_request_norm"]
    seq,diag=ph._trajectory_nominal_controls(_candidate(pd0=10.0,pd1=9.0,vd0=-1.5,vd1=-1.0))
    assert seq[0][2] <= base
    assert seq[0][2] >= base-diag["limits"]["hover_max_feedback_correction_norm"]-1e-12
    assert seq[0][2] >= diag["limits"]["hover_force_balance_floor_norm"]-1e-12
    assert diag["steps"][0]["vertical_collective_synthesis"]["manual_gain_used"] is False
    assert diag["phase"]=="hover"

def test_hover_source_inverse_adds_collective_to_brake_descent():
    ph=_phoenix(); base=ph.offline_vtol_seed_controls()["phases"]["hover"]["throttle_request_norm"]
    seq,diag=ph._trajectory_nominal_controls(_candidate(pd0=10.0,pd1=11.5,vd0=1.2,vd1=0.0))
    assert seq[0][2] > base
    assert seq[0][2]-base <= diag["limits"]["hover_max_feedback_correction_norm"]+1e-12

def test_hover_source_inverse_projects_extreme_horizon_moves_to_existing_trust_region():
    ph=_phoenix(); base=ph.offline_vtol_seed_controls()["phases"]["hover"]["throttle_request_norm"]
    a=[0.0]*19; b=[0.0]*19; c=[0.0]*19
    for x in (a,b,c): x[9]=2.0
    a[5]=-2.68; b[5]=2.69; c[5]=-1.91
    seq,diag=ph._trajectory_nominal_controls({"tdoc_phase":"hover","trajectory":[a,b,c],"actions":[[0.0]*5,[0.0]*5]})
    bound=diag["limits"]["hover_max_feedback_correction_norm"]
    assert len(seq)==2
    assert all(base-bound-1e-12 <= u[2] <= base+bound+1e-12 for u in seq)
    assert all(step["hover_collective_projection_applied"] is True for step in diag["steps"])
    assert diag["steps"][0]["raw_trajectory_collective_norm"] < seq[0][2]
    assert diag["steps"][1]["raw_trajectory_collective_norm"] > seq[1][2]

def test_hover_projection_never_submits_sub_force_balance_action_to_full_fidelity():
    ph=_phoenix()
    seq,diag=ph._trajectory_nominal_controls(
        _candidate(pd0=12.0,pd1=11.0,vd0=-2.4,vd1=0.2))
    floor=diag["limits"]["hover_force_balance_floor_norm"]
    assert diag["steps"][0]["raw_trajectory_collective_norm"] < floor
    assert seq[0][2] == floor
    assert diag["steps"][0]["hover_force_balance_floor_norm"] == floor
    assert "force_balance" in diag["steps"][0]["projection_basis"]

def test_hover_seed_remains_simulation_only_and_no_direct_pwm():
    seed=_phoenix().offline_vtol_seed_controls()
    assert seed["simulation_only"] is True
    assert seed["not_field_evidence"] is True
    assert seed["direct_pwm"] is False
