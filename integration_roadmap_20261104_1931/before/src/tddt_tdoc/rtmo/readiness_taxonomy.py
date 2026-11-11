from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA = "TDOC-online-readiness-taxonomy-v1"


def infrastructure_axis(control_readiness: dict[str, Any] | None) -> dict[str, Any]:
    r = dict(control_readiness or {})
    ready = bool(r.get("ok"))
    return {
        "status": "INFRASTRUCTURE_READY" if ready else "INFRASTRUCTURE_NOT_READY",
        "ready": ready,
        "scope": "CONTROL_INFRASTRUCTURE_MAPPING_COMMISSIONING_ONLY",
        "reason": r.get("reason"),
        "blockers": list(r.get("blockers") or []),
        "legacy_status": r.get("status"),
        "nominal_control_ready": r.get("nominal_control_ready"),
        "qlpv_nonzero_correction_ready": r.get("qlpv_nonzero_correction_ready"),
        "mission_executability_authority": False,
        "current_phase_release_authority": False,
        "source": "ONLINE_CONTROL_READINESS.json",
    }


def current_phase_axis(
    *,
    phase: str | None,
    release: dict[str, Any] | None = None,
    hold_reason: str | None = None,
    bootstrap: dict[str, Any] | None = None,
    evaluated: bool = True,
    episode_index: int | None = None,
    cycle_in_episode: int | None = None,
) -> dict[str, Any]:
    rel = dict(release or {})
    bst = dict(bootstrap or {})
    if not evaluated:
        return {
            "status": "CURRENT_PHASE_RELEASE_NOT_EVALUATED",
            "ready": None,
            "phase": phase,
            "reason": hold_reason or "runtime_release_not_evaluated",
            "blockers": [],
            "authority": "RTMO_CURRENT_STATE_RUNTIME_RELEASE",
            "episode_index": episode_index,
            "cycle_in_episode": cycle_in_episode,
        }
    ready = bool(rel.get("current_runtime_release") is True and rel.get("actuation_release") is True)
    blockers = list(rel.get("blockers") or [])
    if not ready and not blockers:
        b_reason = bst.get("artifact_publish_reason") or bst.get("reason")
        if b_reason:
            blockers.append(str(b_reason))
        elif hold_reason:
            blockers.append(str(hold_reason))
        else:
            blockers.append("current_runtime_release_not_ready")
    return {
        "status": "CURRENT_PHASE_RELEASE_READY" if ready else "CURRENT_PHASE_RELEASE_NOT_READY",
        "ready": ready,
        "phase": phase,
        "reason": None if ready else (hold_reason or rel.get("status") or "runtime_release_blocked"),
        "blockers": blockers,
        "authority": "RTMO_CURRENT_STATE_RUNTIME_RELEASE",
        "durable_phase_capability": rel.get("current_phase_durable_capability"),
        "current_runtime_release": rel.get("current_runtime_release"),
        "actuation_release": rel.get("actuation_release"),
        "transition_release": rel.get("transition_release"),
        "bootstrap_artifact_published": bst.get("artifact_published"),
        "episode_index": episode_index,
        "cycle_in_episode": cycle_in_episode,
    }


def mission_axis(certification: dict[str, Any] | None, *, evaluated: bool = True) -> dict[str, Any]:
    cert = dict(certification or {})
    if not evaluated:
        return {
            "status": "MISSION_CERTIFICATION_NOT_EVALUATED",
            "ready": None,
            "fully_certified": None,
            "blocked_phases": [],
            "authority": "RTMO_MISSION_CERTIFICATION",
        }
    ready = bool(cert.get("fully_certified") is True)
    return {
        "status": "MISSION_FULLY_CERTIFIED" if ready else "MISSION_NOT_FULLY_CERTIFIED",
        "ready": ready,
        "fully_certified": ready,
        "blocked_phases": list(cert.get("blocked_phases") or []),
        "admission_authority": cert.get("admission_authority"),
        "runtime_release_policy": cert.get("runtime_release_policy"),
        "authority": "RTMO_MISSION_CERTIFICATION",
    }


def build_taxonomy(
    *,
    control_readiness: dict[str, Any] | None,
    phase: str | None = None,
    release: dict[str, Any] | None = None,
    mission_certification: dict[str, Any] | None = None,
    hold_reason: str | None = None,
    bootstrap: dict[str, Any] | None = None,
    current_phase_evaluated: bool = True,
    mission_evaluated: bool = True,
    episode_index: int | None = None,
    cycle_in_episode: int | None = None,
) -> dict[str, Any]:
    infra = infrastructure_axis(control_readiness)
    current = current_phase_axis(
        phase=phase,
        release=release,
        hold_reason=hold_reason,
        bootstrap=bootstrap,
        evaluated=current_phase_evaluated,
        episode_index=episode_index,
        cycle_in_episode=cycle_in_episode,
    )
    mission = mission_axis(mission_certification, evaluated=mission_evaluated)
    return {
        "schema": SCHEMA,
        "status_model": "THREE_INDEPENDENT_READINESS_AXES",
        "single_ready_flag_forbidden": True,
        "infrastructure": infra,
        "current_phase_release": current,
        "mission_certification": mission,
        "invariants": {
            "infrastructure_ready_does_not_imply_current_phase_release_ready": True,
            "infrastructure_ready_does_not_imply_mission_fully_certified": True,
            "durable_phase_capability_does_not_imply_current_runtime_release": True,
            "mission_certification_does_not_override_phase_local_runtime_release": True,
        },
    }


def write_taxonomy(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = build_taxonomy(**kwargs)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, p)
    return payload
