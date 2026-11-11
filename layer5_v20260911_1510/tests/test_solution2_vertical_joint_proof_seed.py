from __future__ import annotations

from pathlib import Path

from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _phoenix():
    root=Path(__file__).resolve().parents[1]
    return PhoenixSpecialization(Layer5Config.from_yaml(root/"configs/configuration_phoenix.yaml"))


def _candidate(phase):
    traj=[[0.0]*19,[0.0]*19]
    traj[0][9]=2.0; traj[1][9]=2.0
    return {"tdoc_phase":phase,"trajectory":traj,"actions":[[0.0]*5]}


def test_vertical_seed_is_source_bound_and_not_control_evidence():
    p=_phoenix(); seed=p.offline_vtol_seed_controls()
    assert seed["ok"] is True
    assert seed["simulation_only"] is True
    assert seed["not_field_evidence"] is True
    assert seed["direct_pwm"] is False
    assert 0.79 < seed["equilibrium_collective_norm"] < 0.81
    for phase in ("vertical_takeoff","hover"):
        u=seed["phases"][phase]["throttle_request_norm"]
        assert seed["equilibrium_collective_norm"] < u < 0.90


def test_flat_vertical_trajectories_use_source_equilibrium_inverse():
    p=_phoenix(); seed=p.offline_vtol_seed_controls()
    takeoff,rt=p._trajectory_nominal_controls(_candidate("vertical_takeoff"))
    assert takeoff == [[0.0,0.0,seed["equilibrium_collective_norm"],0.0]]
    hover,rh=p._trajectory_nominal_controls(_candidate("hover"))
    assert hover == [[0.0,0.0,seed["equilibrium_collective_norm"],0.0]]
    assert rt["phase"] == "vertical_takeoff" and rh["phase"] == "hover"
    assert "Layer4 FF" in rt["safety_semantics"] and "Layer6 executor" in rt["safety_semantics"]
    assert rh["steps"][0]["vertical_collective_synthesis"]["manual_gain_used"] is False


def test_forward_flight_tracker_is_not_replaced_by_solution2():
    p=_phoenix()
    seq,rec=p._trajectory_nominal_controls(_candidate("cruise"))
    assert rec.get("vertical_seed") is None
    assert seq[0][2] != p.offline_vtol_seed_controls()["phases"]["hover"]["throttle_request_norm"]
