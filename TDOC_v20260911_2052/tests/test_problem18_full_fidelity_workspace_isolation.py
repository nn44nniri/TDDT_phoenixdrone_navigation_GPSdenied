from pathlib import Path
from types import SimpleNamespace
import json

from tddt_tdoc.adapters.layer3 import Layer3Adapter
from tddt_tdoc.storage import CompressedPersistentWorkspace


def _adapter(tmp_path: Path) -> Layer3Adapter:
    a = Layer3Adapter.__new__(Layer3Adapter)
    a.config = SimpleNamespace(runtime_root=tmp_path / "runtime")
    return a


def _req(cycle: int, phase: str = "cruise", run: str = "run-A", episode: str = "E1"):
    return {
        "transaction_id": f"workflow-online_jsbsim-e0001-c{cycle:04d}",
        "learning_mode": "ONLINE_JSBSIM",
        "online_episode_id": episode,
        "online_cycle_in_episode": cycle,
        "stage": phase,
        "context": {
            "rtmo_run_context": {
                "run_id": run,
                "episode_id": episode,
                "cycle_id": cycle,
                "phase_id": phase,
            }
        },
    }


def test_same_candidate_id_isolated_by_cycle_phase_run(tmp_path):
    a = _adapter(tmp_path)
    candidate = {"candidate_id": "C0006", "scenario_id": "S001"}
    i1 = a._full_fidelity_workspace_identity(candidate, _req(1, "cruise"))
    i2 = a._full_fidelity_workspace_identity(candidate, _req(2, "cruise"))
    i3 = a._full_fidelity_workspace_identity(candidate, _req(1, "turn"))
    i4 = a._full_fidelity_workspace_identity(candidate, _req(1, "cruise", run="run-B"))
    roots = {str(a._full_fidelity_persistent_root(i)) for i in (i1, i2, i3, i4)}
    assert len(roots) == 4
    assert all("/runs/run_" in r for r in roots)
    assert all("candidate_C0006/native" in r for r in roots)
    assert not any("layer4_verification/C0006/native" in r for r in roots)


def test_persistent_restore_cannot_cross_cycle_namespace(tmp_path):
    a = _adapter(tmp_path)
    candidate = {"candidate_id": "C0006", "scenario_id": "S001"}
    p1 = a._full_fidelity_persistent_root(a._full_fidelity_workspace_identity(candidate, _req(10, "turn")))
    p2 = a._full_fidelity_persistent_root(a._full_fidelity_workspace_identity(candidate, _req(11, "turn")))
    w1 = CompressedPersistentWorkspace(p1, prefix="p18-a-")
    try:
        (w1.working_root / "provenance.json").write_text(json.dumps({"cycle": 10}))
    finally:
        w1.close()
    w2 = CompressedPersistentWorkspace(p2, prefix="p18-b-")
    try:
        assert not (w2.working_root / "provenance.json").exists()
    finally:
        w2.close()


def test_offline_explicit_context_produces_canonical_namespace(tmp_path):
    a = _adapter(tmp_path)
    candidate = {"candidate_id": "C0001", "scenario_id": "S001"}
    req = {
        "transaction_id": "mission-offline-0154",
        "learning_mode": "OFFLINE_SE",
        "stage": "approach",
        "context": {
            "tdoc_run_id": "offline-abc",
            "offline_episode_id": "OFFLINE_SE-001",
            "offline_cycle_id": 154,
        },
    }
    ident = a._full_fidelity_workspace_identity(candidate, req)
    root = a._full_fidelity_persistent_root(ident)
    assert ident["cycle_id"] == "cycle_000154"
    assert ident["phase_id"] == "phase_approach"
    assert "run_offline-abc" in str(root)
    assert "episode_OFFLINE_SE-001" in str(root)


def test_missing_run_context_never_falls_back_to_shared_candidate_directory(tmp_path):
    a = _adapter(tmp_path)
    c = {"candidate_id": "C0003", "scenario_id": "S001"}
    r1 = {"transaction_id": "tx-one", "learning_mode": "OFFLINE_SE", "stage": "cruise", "context": {}}
    r2 = {"transaction_id": "tx-two", "learning_mode": "OFFLINE_SE", "stage": "cruise", "context": {}}
    p1 = a._full_fidelity_persistent_root(a._full_fidelity_workspace_identity(c, r1))
    p2 = a._full_fidelity_persistent_root(a._full_fidelity_workspace_identity(c, r2))
    assert p1 != p2
    assert p1.parent.name == "candidate_C0003"
    assert "transaction-" in str(p1)


def test_online_full_fidelity_wind_scenario_preserves_realized_ground_velocity():
    physical = {
        "vn": 0.00056,
        "ve": 0.00058,
        "vd": 0.00523,
        "air_velocity_ned_m_s": [-1.99944, -0.99942, 0.00523],
    }
    scenario_wind = [0.47758, -1.32804, -0.05700]
    ground, evidence = Layer3Adapter._online_ff_initial_velocity(physical, scenario_wind)
    assert ground == {"vn": physical["vn"], "ve": physical["ve"], "vd": physical["vd"]}
    assert evidence["inertial_ground_velocity_preserved"] is True
    assert evidence["scenario_wind_substitution"] is False
    assert evidence["scenario_air_velocity_ned_m_s"] == [
        physical["vn"] - scenario_wind[0],
        physical["ve"] - scenario_wind[1],
        physical["vd"] - scenario_wind[2],
    ]
