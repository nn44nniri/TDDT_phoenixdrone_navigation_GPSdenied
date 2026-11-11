"""R2.01 read-only projection: identity integrity is separate from control quality.

No hashes, hash-bound flags, source records or readiness gates are repaired here.
Missing counters remain unknown. This projection never authorizes actuation.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

CONTROL_REQUIRED_PHASES = (
    "vertical_takeoff", "hover", "transition_forward", "climb", "cruise",
    "turn", "approach", "transition_back", "vertical_landing",
)
PROOF_FIELDS = ("l4_candidate_verified", "l5_final_gate", "layer2_control_effect_applied")


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _true(value):
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def assess_offline_knowledge(manifest, *, handoff_hash_verified=None, handoff_gate_ok=None, timeline_rows=None):
    """Return independent integrity facts, reported quality and observed coverage.

    The caller supplies the result of existing handoff checks. Package bytes,
    compatibility and freshness are not re-verified by this reporting function.
    Optional timeline rows recompute the existing three-way same-cycle proof.
    """
    m = _mapping(manifest)
    quality = _mapping(m.get("knowledge_quality"))
    pc = _mapping(m.get("phase_control_coverage") or quality.get("phase_control_coverage"))
    declared = _mapping(pc.get("coverage"))
    required = list(dict.fromkeys([*CONTROL_REQUIRED_PHASES, *(pc.get("required_control_phases") or [])]))
    issues = []
    joint = _count(quality.get("joint_control_proof_cycles"))
    phase_counts = {p: _count(_mapping(declared.get(p)).get("joint_proof_cycles")) for p in required}
    count_source = "manifest_claim" if joint is not None else "unavailable"
    row_count = None
    if timeline_rows is not None:
        rows = list(timeline_rows)
        row_count = len(rows)
        proof = [r for r in rows if str(r.get("phase")) in required and all(_true(r.get(k)) for k in PROOF_FIELDS)]
        observed = len(proof)
        if joint is not None and joint != observed:
            issues.append("declared_joint_count_differs_from_timeline")
        observed_phases = {p: sum(str(r.get("phase")) == p for r in proof) for p in required}
        for p, count in phase_counts.items():
            if count is not None and count != observed_phases[p]:
                issues.append("declared_phase_count_differs_from_timeline:" + p)
        joint, phase_counts, count_source = observed, observed_phases, "timeline_recomputed"
    elif "joint_control_proof_cycles" in quality and joint is None:
        issues.append("invalid_joint_control_proof_cycles")
    phase_sum = sum(n for n in phase_counts.values() if n is not None)
    if all(n is not None for n in phase_counts.values()) and joint is not None and phase_sum != joint:
        issues.append("joint_count_differs_from_phase_sum")
    if any(n == 0 for n in phase_counts.values()):
        complete = False
    elif all(n is not None for n in phase_counts.values()):
        complete = True
    else:
        complete = None
    missing = [p for p, n in phase_counts.items() if n == 0]
    unknown = [p for p, n in phase_counts.items() if n is None]
    if pc.get("all_required_control_phases") is True and complete is False:
        issues.append("declared_complete_without_required_phase_joint_proof")
    if quality.get("control_knowledge_valid") is True and joint == 0:
        issues.append("control_valid_claim_with_zero_joint_proof")
    if joint is None:
        classification = "CONTROL_EVIDENCE_UNAVAILABLE"
    elif issues:
        classification = "INCONSISTENT_CONTROL_EVIDENCE"
    elif joint == 0:
        classification = "NO_JOINT_CONTROL_PROOF"
    elif complete is True:
        classification = "COMPLETE_OFFLINE_CONTROL_PROOF_REPORTED"
    else:
        classification = "PARTIAL_OFFLINE_CONTROL_PROOF"
    fin = _mapping(m.get("finalization"))
    setd = _mapping(m.get("setd"))
    return {
        "schema": "TDOC-offline-knowledge-assessment-v1",
        "integrity": {
            "handoff_hash_verified": handoff_hash_verified,
            "existing_handoff_gate_ok": handoff_gate_ok,
            "declared_setd_integrity_valid": m.get("setd_integrity_valid"),
            "setd_package_id": setd.get("package_id"),
            "finalization_ok": fin.get("ok", fin.get("committed")),
            "finalization_committed": fin.get("committed"),
            "package_bytes_verified_here": False,
            "verification_scope": "caller-supplied handoff checks; package/forcing/version/freshness checks remain separate",
        },
        "quality": {
            "classification": classification,
            "count_source": count_source,
            "joint_control_proof_cycles": joint,
            "timeline_rows": row_count,
            "reported_counters": {k: deepcopy(quality.get(k)) for k in (
                "safety_hold_cycles", "verified_candidate_cycles", "final_authorized_cycles",
                "closed_loop_effect_cycles", "joint_control_proof_cycles", "control_learning_cycles",
                "non_control_learning_cycles", "full_fidelity_runtime_error_cycles")},
            "reported_safety_knowledge_valid": quality.get("safety_knowledge_valid"),
            "reported_control_knowledge_valid": quality.get("control_knowledge_valid"),
            "proof_definition": "same-cycle L4 verified + L5 FINAL + Layer2 control effect; phase labels/HOLD/geometry alone are not proof",
        },
        "coverage": {
            "required_control_phases": required,
            "joint_proof_cycles_by_phase": phase_counts,
            "assessed_complete": complete,
            "missing_control_phases": missing,
            "unknown_control_phases": unknown,
            "declared_complete": pc.get("all_required_control_phases"),
            "declared_missing_control_phases": deepcopy(pc.get("missing_control_phases")),
        },
        "consistency_issues": issues,
        "legacy_flags": {k: deepcopy(m.get(k)) for k in (
            "valid_setd", "setd_integrity_valid", "control_knowledge_valid", "offline_control_coverage_complete")},
        "claims_trust": "handoff_hash_verified" if handoff_hash_verified is True else "unverified_or_not_checked",
        "actuation_authority": False,
        "online_matching_offline_sample_required": False,
        "runtime_control_authority": "fresh Layer2/Layer3 -> Layer4 -> Layer5 -> Layer6 -> Layer7 under TDOC",
    }


def offline_validation_view(finalization, validation):
    """Separate integrity diagnostics from legacy policy/pipeline readiness.

    Finalization readiness is not control readiness. Preserve the upstream
    control check and require the finalized handoff's explicit control proof.
    """
    fin = _mapping(finalization)
    handoff = _mapping(fin.get("handoff"))
    result = deepcopy(dict(validation))
    legacy_pipeline_ok = bool(fin.get("ok"))
    result["OFFLINE_KNOWLEDGE_VALID"] = bool(handoff.get("setd_integrity_valid") is True
        and fin.get("committed") is True and _mapping(fin.get("setd")).get("ok") is True)
    result["OFFLINE_KNOWLEDGE_VALID_SCOPE"] = "finalized handoff and loaded SETD identity; policy/control quality excluded"
    result["OFFLINE_PIPELINE_READY"] = legacy_pipeline_ok
    result["OFFLINE_POLICY_AVAILABLE"] = bool(_mapping(fin.get("knowledge_policy_compilation")).get("ok"))
    result["OFFLINE_CONTROL_READY"] = bool(result.get("OFFLINE_CONTROL_READY") and legacy_pipeline_ok
        and handoff.get("control_knowledge_valid") is True
        and handoff.get("offline_control_coverage_complete") is True)
    existing = handoff.get("knowledge_assessment")
    # Historical finalization reports lack this projection. Read their claims
    # without pretending that this reporting function verified their hash.
    result["knowledge_assessment"] = (deepcopy(existing) if isinstance(existing, Mapping)
                                      else assess_offline_knowledge(handoff))
    result["knowledge_finalization"] = deepcopy(dict(fin))
    return result
