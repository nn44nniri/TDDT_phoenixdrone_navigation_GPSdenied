"""Startup consumes verified knowledge; a learned policy is an optional guide.

Missing guidance is distinct from a rejected/unreadable policy. This admission
result has no flight/control authority and never manufactures a policy payload.
"""
from collections.abc import Mapping
from copy import deepcopy


OPTIONAL_POLICY_REASONS = frozenset({
    "knowledge_policy_artifact_missing",
    "no_joint_closed_loop_policy_rows",
    "phase_policy_support_missing",
})


def optional_policy_admission(policy):
    """Classify R2.02 results without relaxing integrity or treating errors as absence."""
    if not isinstance(policy, Mapping):
        policy = {"ok": False, "reason": "policy_result_not_mapping"}
    available = bool(policy.get("ok") is True and policy.get("available") is True
                     and policy.get("availability") == "AVAILABLE"
                     and policy.get("status") in {"READY", "COMPILED"}
                     and policy.get("offline_prior_only") is True
                     and policy.get("actuation_authority") is False
                     and policy.get("artifact_hash"))
    absent = bool(policy.get("ok") is False and policy.get("available") is False
                  and policy.get("availability") == "UNAVAILABLE"
                  and policy.get("status") == "UNAVAILABLE"
                  and policy.get("reason") in OPTIONAL_POLICY_REASONS
                  and policy.get("offline_prior_only") is True
                  and policy.get("actuation_authority") is False
                  and policy.get("artifact") is None and policy.get("phase_policy") is None)
    accepted = available or absent
    return {
        "ok": accepted,
        "status": "READY" if accepted else "NOT_READY",
        "reason": ("OPTIONAL_POLICY_AVAILABLE" if available else "OPTIONAL_POLICY_UNAVAILABLE" if absent
                   else "OPTIONAL_POLICY_REJECTED"),
        "readiness_scope": "OFFLINE_KNOWLEDGE_STARTUP_ONLY; not flight/control readiness",
        "policy_required_for_startup": False,
        "policy_available": available,
        "artifact_path": policy.get("artifact_path") if available else None,
        "artifact_hash": policy.get("artifact_hash") if available else None,
        "policy_diagnostics": deepcopy({k: v for k, v in policy.items() if k not in {"artifact", "phase_policy"}}),
        "actuation_authority": False,
        "flight_ready": False,
    }
