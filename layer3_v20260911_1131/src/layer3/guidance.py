from __future__ import annotations

import copy
from typing import Any, Mapping

from .config import Layer3Config
from .utils import clamp, safe_float


class MesoGuidanceManager:
    """Build transient Meso Reference Guidance (MRG), never actuator commands.

    A nearest CRP/SARG reference may still be recorded for diagnostics when the
    context is OOD, but it is not allowed to inject reference constraints into
    the next Fast window unless the CRP acceptance gate admits it.
    """

    def __init__(self, config: Layer3Config):
        self.config = config

    def build(self, window_index: int, aggregate: Mapping[str, Any], crp_match: Mapping[str, Any],
              sarg_reference: Mapping[str, Any], sarg_deviation: Mapping[str, Any],
              candidate_topology: Mapping[str, Any]) -> dict[str, Any]:
        margin = safe_float(aggregate.get("min_consistency_margin"), 1.0)
        consistency_risk = clamp(-margin, 0.0, 1.0)
        crp_admissible = bool(crp_match.get("matched"))
        sarg_admissible = bool(sarg_reference.get("selected")) and bool(sarg_reference.get("reference_admissible", crp_admissible))
        # Diagnostic-only SARG rows must not alter guidance.  Their deviation and
        # confidence remain visible in provenance, but they are excluded from the
        # control-relevant MRG risk/coupling/objective path.
        deviation = safe_float(sarg_deviation.get("score"), 0.0) if sarg_admissible else 0.0
        crp_q = safe_float(crp_match.get("quality_score"), 0.0) if crp_match.get("selected") else 0.0
        pattern = sarg_reference.get("pattern") or {}
        sarg_conf = safe_float(pattern.get("pattern_confidence"), 0.0) if sarg_admissible else 0.0
        ood_risk = 1.0 if not crp_admissible else 0.0
        confidence_floor = max(crp_q, sarg_conf) if sarg_admissible else crp_q
        risk = clamp(max(consistency_risk, deviation, ood_risk, 1.0 - confidence_floor), 0.0, 1.0)
        load = clamp(safe_float(aggregate.get("fault_count"), 0.0) /
                     max(safe_float(aggregate.get("fast_count"), 1.0), 1.0), 0.0, 1.0)
        k_delta = {key: -0.15 * risk * safe_float(k, 0.0)
                   for key, k in (aggregate.get("mean_couplings") or {}).items()}
        return {
            "window_index": window_index,
            "crp_match": dict(crp_match), "sarg_reference": dict(sarg_reference),
            "sarg_deviation": dict(sarg_deviation), "decision_weight_delta": {},
            "constraint_delta": {
                "consistency_margin_guard": max(0.0, -margin),
                "reference_min_clearance_m": pattern.get("min_clearance_m") if sarg_admissible else None,
                "reference_airspeed_m_s": pattern.get("airspeed_ref_m_s") if sarg_admissible else None,
            },
            "optimizer_delta": {"reference_objective": pattern.get("objective") if sarg_admissible else None},
            "transient_coupling_delta": k_delta, "candidate_topology": dict(candidate_topology),
            "risk_signal": risk, "load_signal": load,
            "reference_admissible": sarg_admissible,
            "reference_mode": "accepted" if sarg_admissible else "diagnostic_nearest_only",
            "sarg_injected_into_guidance": bool(sarg_admissible),
            "provenance": {"role": "Meso Reference Guidance", "not_actuator_command": True,
                           "topology_commit": False, "ood_reference_injection": False,
                           "invalid_sarg_guidance_injection": False},
        }


class SlowPolicyManager:
    """Produce the slow supervisory envelope held during the next fast period."""

    def __init__(self, config: Layer3Config): self.config = config

    def update(self, current: Mapping[str, Any], slow_knowledge: Mapping[str, Any], period_index: int) -> dict[str, Any]:
        policy = copy.deepcopy(dict(current))
        policy["parent_version"] = current.get("version")
        policy["version"] = f"L3-policy-slow-{period_index:05d}"
        risk = clamp(safe_float(slow_knowledge.get("risk"), 0.0), 0.0, 1.0)
        cb = copy.deepcopy(policy.get("coupling_bounds") or self.config.policy.get("coupling_bounds") or {})
        if risk > safe_float(policy.get("risk_threshold"), 0.8):
            factor = max(0.55, 1.0 - 0.30 * risk)
            for key, bounds in cb.items():
                lo, hi = float(bounds[0]), float(bounds[1])
                cb[key] = [lo, max(lo, hi * factor)]
        policy["coupling_bounds"] = cb
        policy["slow_supervisory"] = {
            "period_index": period_index, "risk": risk,
            "mean_mismatch": slow_knowledge.get("mean_mismatch"),
            "worst_mismatch": slow_knowledge.get("worst_mismatch"),
            "sarg_deviation": slow_knowledge.get("sarg_deviation"),
        }
        policy["sample_and_hold"] = True
        policy["permanent_adaptation_committed"] = False
        return policy


class SafeCommitManager:
    """Synchronization-barrier + authorization gate for Layer-7 adaptations."""

    def __init__(self, require_structural_authorization: bool = True):
        self.require_structural_authorization = bool(require_structural_authorization)
        self.last_commit: dict[str, Any] | None = None

    def commit(self, current_policy: Mapping[str, Any], adaptation: Mapping[str, Any], *,
               approved: bool, at_barrier: bool, authorization: str | None = None) -> dict[str, Any]:
        if not approved:
            return {"committed": False, "reason": "not_approved", "policy": dict(current_policy)}
        if not at_barrier:
            return {"committed": False, "reason": "not_at_synchronization_barrier", "policy": dict(current_policy)}
        structural = bool(adaptation.get("topology_changes"))
        if structural and self.require_structural_authorization and not str(authorization or "").strip():
            return {"committed": False, "reason": "structural_authorization_required", "policy": dict(current_policy)}
        merged = copy.deepcopy(dict(current_policy))
        for key, value in adaptation.items():
            if key in {"topology_changes", "authorization"}: continue
            merged[key] = copy.deepcopy(value)
        merged["version"] = str(adaptation.get("version") or f"{current_policy.get('version','policy')}+safe")
        merged["permanent_adaptation_committed"] = True
        self.last_commit = {"adaptation": dict(adaptation), "policy_version": merged["version"],
                            "authorization": authorization}
        return {"committed": True, "reason": "approved_authorized_at_barrier", "policy": merged,
                "topology_changes": dict(adaptation.get("topology_changes") or {}),
                "authorization_recorded": bool(authorization)}
