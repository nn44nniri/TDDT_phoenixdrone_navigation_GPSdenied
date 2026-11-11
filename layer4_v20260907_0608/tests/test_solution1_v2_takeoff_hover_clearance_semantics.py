import copy, json
from types import SimpleNamespace

import numpy as np

from layer4 import Layer4Config
from layer4.feasibility import FeasibilityEngine
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.state import StateCodec
from layer4.verification import FullFidelityVerifier
from layer4.vtol import VTOLProfile


def _ff_rows(agls, mode):
    return [
        {"step": i + 1, "snapshot": {"context": {"flight_mode": mode, "altitude_agl_m": a}, "std": {"representations": {}}, "guidance": {}}}
        for i, a in enumerate(agls)
    ]


def test_hover_12m_is_not_rejected_by_enroute_30m_floor(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    verifier = FullFidelityVerifier(cfg, None)
    cand = SimpleNamespace(actions=np.zeros((3, 5)))
    snap = SimpleNamespace(context={"flight_mode": "hover", "altitude_agl_m": 12.0})
    out = verifier._full_feasibility(_ff_rows([12.0, 12.1, 11.9], "hover"), cand, snap, None)
    assert out["safe"]
    assert "terrain_clearance" not in out["reasons"]
    assert "hover_agl_corridor" not in out["reasons"]
    assert all(x["terrain_policy"]["policy"] == "HOVER_TARGET_AGL_CORRIDOR" for x in out["steps"])


def test_hover_still_fails_outside_its_phase_target_corridor(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    verifier = FullFidelityVerifier(cfg, None)
    cand = SimpleNamespace(actions=np.zeros((2, 5)))
    snap = SimpleNamespace(context={"flight_mode": "hover", "altitude_agl_m": 12.0, "hover_target_agl_m": 12.0})
    out = verifier._full_feasibility(_ff_rows([12.0, 16.5], "hover"), cand, snap, None)
    assert not out["safe"]
    assert "hover_agl_corridor" in out["reasons"]


def test_vertical_takeoff_starts_near_ground_without_enroute_floor(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    verifier = FullFidelityVerifier(cfg, None)
    cand = SimpleNamespace(actions=np.zeros((4, 5)))
    snap = SimpleNamespace(context={"flight_mode": "vertical_takeoff", "altitude_agl_m": 0.0})
    out = verifier._full_feasibility(_ff_rows([0.0, 0.8, 1.8, 3.0], "vertical_takeoff"), cand, snap, None)
    assert out["safe"]
    assert "terrain_clearance" not in out["reasons"]
    assert "takeoff_ground_corridor" not in out["reasons"]


def test_takeoff_explicit_target_builds_progressive_ground_referenced_corridor(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    p = VTOLProfile(cfg)
    c0 = p.terrain_corridor("vertical_takeoff", 0.0, step_index=0, step_count=4, context={"takeoff_target_agl_m": 12.0})
    c3 = p.terrain_corridor("vertical_takeoff", 0.0, step_index=3, step_count=4, context={"takeoff_target_agl_m": 12.0})
    assert c0["policy"] == "GROUND_REFERENCED_TAKEOFF_CORRIDOR"
    assert c3["reference_m"] > c0["reference_m"]
    assert c3["target_m"] == 12.0


def test_zero_airspeed_takeoff_not_rejected_by_symmetric_tube_lower_tightening(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    raw = json.loads((root / "examples/layer3_snapshot_example.json").read_text())
    snap = Layer3SnapshotAdapter(cfg).adapt(raw)
    snap.context = copy.deepcopy(snap.context)
    snap.context["flight_mode"] = "vertical_takeoff"
    snap.context["altitude_agl_m"] = 0.0
    codec = StateCodec(root / "resources/state_schema.json")
    fe = FeasibilityEngine(cfg, codec)
    x = codec.encode(snap)
    H = cfg.horizon
    X = np.tile(x, (H + 1, 1))
    X[:, codec.index["airspeed_m_s"]] = 0.0
    net = [{"J_C": 0.0, "mu": 1.0, "W": snap.weights, "K": snap.couplings} for _ in range(H + 1)]
    tube = np.zeros_like(X)
    tube[:, codec.index["airspeed_m_s"]] = 2.0
    out = fe.evaluate_trajectory(X, net, snap, actions=np.zeros((H, 5)), tube=tube)
    assert "airspeed" not in out["reasons"]
    assert all(not r["tube_tightening"]["lower_airspeed_tightening_applied"] for r in out["per_step"])


def test_takeoff_ground_contact_projects_only_impossible_downward_tube_tail(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    raw = json.loads((root / "examples/layer3_snapshot_example.json").read_text())
    snap = Layer3SnapshotAdapter(cfg).adapt(raw)
    snap.context = copy.deepcopy(snap.context)
    snap.context["flight_mode"] = "vertical_takeoff"
    snap.context["altitude_agl_m"] = 0.0
    codec = StateCodec(root / "resources/state_schema.json")
    fe = FeasibilityEngine(cfg, codec)
    x = codec.encode(snap)
    X = np.tile(x, (cfg.horizon + 1, 1))
    net = [{"J_C": 0.0, "mu": 1.0, "W": snap.weights, "K": snap.couplings} for _ in range(cfg.horizon + 1)]
    tube = np.zeros_like(X)
    tube[:, codec.index["mesa_position_ned_m_2"]] = 0.8
    out = fe.evaluate_trajectory(X, net, snap, actions=np.zeros((cfg.horizon, 5)), tube=tube)
    assert "takeoff_ground_corridor" not in out["reasons"]
    assert all(r["tube_tightening"]["effective_downward_position_radius_m"] == 0.0 for r in out["per_step"])
    assert all(r["tube_tightening"]["unilateral_ground_contact_projection_applied"] for r in out["per_step"])

    X[1, codec.index["mesa_position_ned_m_2"]] += 0.1
    penetrated = fe.evaluate_trajectory(X, net, snap, actions=np.zeros((cfg.horizon, 5)), tube=tube)
    assert "takeoff_ground_corridor" in penetrated["reasons"]


def test_cruise_keeps_existing_30m_enroute_floor(root):
    cfg = Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")
    verifier = FullFidelityVerifier(cfg, None)
    cand = SimpleNamespace(actions=np.zeros((1, 5)))
    snap = SimpleNamespace(context={"flight_mode": "cruise", "altitude_agl_m": 12.0})
    out = verifier._full_feasibility(_ff_rows([12.0], "cruise"), cand, snap, None)
    assert not out["safe"]
    assert "terrain_clearance" in out["reasons"]
