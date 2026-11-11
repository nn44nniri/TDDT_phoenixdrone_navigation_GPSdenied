from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from layer4 import Layer4Config, AtomicSnapshot, Candidate
from layer4.state import StateCodec
from layer4.vtol import VTOLProfile
from layer4.feasibility import FeasibilityEngine
from layer4.verification import FullFidelityVerifier


def _cfg(root):
    return Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")


def _profile(root):
    return VTOLProfile(_cfg(root))


def _codec(cfg):
    return StateCodec(cfg.package_root / cfg.get("qlpv.state_schema_file"))


def _snapshot(airspeed=0.0):
    return AtomicSnapshot(
        version="p0-5-test",
        reference_time_s=0.0,
        hash="p0-5-test-hash",
        std={
            "representations": {
                "WingMPET": {"canonical_state": {"airspeed_m_s": float(airspeed)}, "validity": 1.0},
                "MESA_NAV": {"canonical_state": {}, "validity": 1.0},
                "MicroMetTwin": {"canonical_state": {"wind_ned_m_s": [0.0, 0.0, 0.0]}, "validity": 1.0},
            }
        },
        center_of_consistency={}, mismatch=0.0, weights={}, couplings={}, topology={},
        consistency_margin=1.0, uncertainty_td={}, guidance={}, constraints={},
        context={"flight_mode": "transition_forward", "altitude_agl_m": 100.0},
        stability={"stable": True}, provenance={}, slow_policy={},
    )


def test_transition_forward_has_dedicated_phase_contract(root):
    p = _profile(root)
    lim = p.limits("transition_forward")
    assert lim["constraint_mode"] == "transition"
    assert lim["airspeed_policy"] == "FORWARD_TRANSITION_PROGRESS_CORRIDOR"
    # Entry from hover is legal; the legacy static 9.1 m/s cruise floor is gone.
    assert lim["min_airspeed_m_s"] == 0.0
    assert lim["terminal_min_airspeed_m_s"] == 3.0
    assert lim["max_airspeed_m_s"] == p.limits("transition")["max_airspeed_m_s"]


def test_transition_forward_corridor_starts_at_current_speed_and_reaches_terminal_floor(root):
    p = _profile(root)
    c0 = p.airspeed_corridor("transition_forward", 0.0, step_index=0, step_count=6)
    c3 = p.airspeed_corridor("transition_forward", 0.0, step_index=3, step_count=6)
    c6 = p.airspeed_corridor("transition_forward", 0.0, step_index=6, step_count=6)
    assert c0["lower_m_s"] == 0.0
    assert abs(c3["lower_m_s"] - 1.5) < 1e-12
    assert abs(c6["lower_m_s"] - 3.0) < 1e-12
    assert c6["reachable"] is True
    # Existing qLPV bound is +2 m/s per 1 s step; required ramp is only 0.5 m/s².
    assert abs(c6["required_accel_m_s2"] - 0.5) < 1e-12
    assert abs(c6["model_accel_limit_m_s2"] - 2.0) < 1e-12


def test_authoritative_higher_terminal_target_is_progressive_not_step0_floor(root):
    p = _profile(root)
    ctx = {"transition_forward_terminal_airspeed_m_s": 7.0}
    c0 = p.airspeed_corridor("transition_forward", 0.0, step_index=0, step_count=6, context=ctx)
    c6 = p.airspeed_corridor("transition_forward", 0.0, step_index=6, step_count=6, context=ctx)
    assert c0["lower_m_s"] == 0.0
    assert c6["lower_m_s"] == 7.0
    assert c6["reachable"] is True
    assert c6["required_accel_m_s2"] < c6["model_accel_limit_m_s2"]


def test_transition_entry_above_terminal_floor_does_not_become_a_speed_hold(root):
    p = _profile(root)
    c0 = p.airspeed_corridor("transition_forward", 13.0, step_index=0, step_count=6)
    c6 = p.airspeed_corridor("transition_forward", 13.0, step_index=6, step_count=6)
    assert c0["lower_m_s"] == 3.0
    assert c6["lower_m_s"] == 3.0
    assert c6["terminal_target_m_s"] == 3.0
    assert c6["required_accel_m_s2"] == 0.0


def test_surrogate_feasibility_accepts_progressive_zero_to_three_profile(root):
    cfg = _cfg(root); codec = _codec(cfg); eng = FeasibilityEngine(cfg, codec); snap = _snapshot(0.0)
    n = codec.dim; X = np.zeros((7, n), dtype=float)
    X[:, codec.index["airspeed_m_s"]] = np.linspace(0.0, 3.0, 7)
    # Keep position constant so AGL remains 100 m and transition uses normal en-route terrain floor.
    X[:, codec.index["mesa_position_ned_m_2"]] = -100.0
    network = [{"J_C": 0.0, "mu": 1.0, "W": {}, "K": {}} for _ in range(7)]
    out = eng.evaluate_trajectory(X, network, snap, terminal={"certified": True})
    assert out["feasible"] is True, out
    assert out["vtol_limits"]["min_airspeed_m_s"] == 0.0
    assert out["vtol_limits"]["terminal_min_airspeed_m_s"] == 3.0
    assert out["per_step"][0]["airspeed_policy"]["lower_m_s"] == 0.0
    assert out["per_step"][-1]["airspeed_policy"]["lower_m_s"] == 3.0


def test_negative_scalar_airspeed_remains_fail_closed_for_p0_6(root):
    cfg = _cfg(root); codec = _codec(cfg); eng = FeasibilityEngine(cfg, codec); snap = _snapshot(0.0)
    n = codec.dim; X = np.zeros((7, n), dtype=float)
    X[:, codec.index["airspeed_m_s"]] = [0.0, -0.2, -0.4, -0.6, -0.8, -1.0, -1.2]
    X[:, codec.index["mesa_position_ned_m_2"]] = -100.0
    network = [{"J_C": 0.0, "mu": 1.0, "W": {}, "K": {}} for _ in range(7)]
    out = eng.evaluate_trajectory(X, network, snap, terminal={"certified": True})
    assert out["feasible"] is False
    assert "airspeed" in out["reasons"]


def test_full_fidelity_postcheck_uses_same_progressive_contract(root):
    cfg = _cfg(root); codec = _codec(cfg); verifier = FullFidelityVerifier(cfg, codec); snap = _snapshot(0.0)
    rows = []
    for step, air in enumerate(np.linspace(0.5, 3.0, 6), start=1):
        rows.append({
            "step": step,
            "snapshot": {
                "std": {"representations": {
                    "WingMPET": {"canonical_state": {"airspeed_m_s": float(air)}},
                    "MicroMetTwin": {"canonical_state": {"wind_ned_m_s": [0.0, 0.0, 0.0]}},
                }},
                "context": {"flight_mode": "transition_forward", "altitude_agl_m": 100.0},
                "guidance": {},
            },
        })
    cand = Candidate(
        candidate_id="C-P0-5", scenario_id="S-P0-5", snapshot_version=snap.version,
        snapshot_hash=snap.hash, probability=1.0, trajectory=[], uncertainty=[],
        network_trajectory=[], actions=[[0.0]*5 for _ in range(6)], objective_vector={},
    )
    out = verifier._full_feasibility(rows, cand, snap, None)
    assert out["safe"] is True, out
    assert out["steps"][0]["airspeed_policy"]["policy"] == "FORWARD_TRANSITION_PROGRESS_CORRIDOR"
    assert out["steps"][-1]["airspeed_policy"]["lower_m_s"] == 3.0
