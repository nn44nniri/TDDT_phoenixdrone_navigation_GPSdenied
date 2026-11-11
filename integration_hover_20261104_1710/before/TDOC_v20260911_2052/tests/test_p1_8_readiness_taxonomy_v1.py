from pathlib import Path
import json

from tddt_tdoc.rtmo.readiness_taxonomy import build_taxonomy, write_taxonomy


def _infra_ready():
    return {
        "ok": True,
        "status": "READY",
        "infrastructure_status": "INFRASTRUCTURE_READY",
        "reason": "ready_nominal_control",
        "blockers": [],
        "nominal_control_ready": True,
    }


def test_infrastructure_ready_does_not_imply_phase_or_mission_ready():
    tax = build_taxonomy(
        control_readiness=_infra_ready(),
        phase="on_ground",
        release={
            "current_phase_durable_capability": True,
            "current_runtime_release": False,
            "actuation_release": False,
            "transition_release": False,
            "status": "WAIT_RUNTIME_RELEASE",
            "blockers": ["current_runtime_release_missing"],
        },
        mission_certification={
            "fully_certified": False,
            "blocked_phases": ["transition_forward"],
            "admission_authority": False,
            "runtime_release_policy": "CURRENT_PHASE_AND_TRANSITION_LOCAL",
        },
    )
    assert tax["infrastructure"]["status"] == "INFRASTRUCTURE_READY"
    assert tax["infrastructure"]["mission_executability_authority"] is False
    assert tax["current_phase_release"]["status"] == "CURRENT_PHASE_RELEASE_NOT_READY"
    assert tax["current_phase_release"]["ready"] is False
    assert tax["mission_certification"]["status"] == "MISSION_NOT_FULLY_CERTIFIED"
    assert tax["mission_certification"]["ready"] is False
    assert tax["single_ready_flag_forbidden"] is True


def test_current_phase_release_can_be_ready_while_mission_is_not_fully_certified():
    tax = build_taxonomy(
        control_readiness=_infra_ready(),
        phase="hover",
        release={
            "current_phase_durable_capability": True,
            "current_runtime_release": True,
            "actuation_release": True,
            "transition_release": False,
            "status": "RELEASED_CURRENT_PHASE_ONLY",
            "blockers": [],
        },
        mission_certification={"fully_certified": False, "blocked_phases": ["transition_forward"]},
        episode_index=1,
        cycle_in_episode=7,
    )
    assert tax["current_phase_release"]["status"] == "CURRENT_PHASE_RELEASE_READY"
    assert tax["current_phase_release"]["ready"] is True
    assert tax["mission_certification"]["status"] == "MISSION_NOT_FULLY_CERTIFIED"


def test_bootstrap_hold_is_not_current_phase_release_ready():
    tax = build_taxonomy(
        control_readiness=_infra_ready(),
        phase="on_ground",
        release=None,
        bootstrap={"artifact_published": False, "artifact_publish_reason": "no_verified_candidate"},
        hold_reason="WAIT_L4_BOOTSTRAP",
        mission_certification={"fully_certified": False, "blocked_phases": ["transition_forward"]},
    )
    cur = tax["current_phase_release"]
    assert cur["status"] == "CURRENT_PHASE_RELEASE_NOT_READY"
    assert cur["ready"] is False
    assert cur["reason"] == "WAIT_L4_BOOTSTRAP"
    assert "no_verified_candidate" in cur["blockers"]


def test_taxonomy_writer_is_atomic_and_contains_three_independent_axes(tmp_path: Path):
    out = tmp_path / "ONLINE_READINESS_TAXONOMY.json"
    tax = write_taxonomy(
        out,
        control_readiness=_infra_ready(),
        phase=None,
        current_phase_evaluated=False,
        mission_evaluated=False,
    )
    disk = json.loads(out.read_text())
    assert disk == tax
    assert disk["infrastructure"]["status"] == "INFRASTRUCTURE_READY"
    assert disk["current_phase_release"]["status"] == "CURRENT_PHASE_RELEASE_NOT_EVALUATED"
    assert disk["mission_certification"]["status"] == "MISSION_CERTIFICATION_NOT_EVALUATED"
