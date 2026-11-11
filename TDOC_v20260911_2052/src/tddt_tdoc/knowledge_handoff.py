from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping

from .storage import read_json_auto, write_json_gz
from .utils import sha256_obj, utc_now
from .knowledge_assessment import assess_offline_knowledge


class OfflineKnowledgeHandoff:
    """TDOC-owned binding from finalized OFFLINE_SE SETD-KStore to ONLINE_JSBSIM.

    This object never substitutes for Layer-5 SS/SETD KStore.  It records the exact
    verified SETD identity, hashes, source offline ledger record, and external KStore
    fingerprint so TDOC can fail closed unless online training loads the same committed
    offline knowledge package required by Formalism_layer5_v4 (L5-SE-7/L5-K-11).
    """
    SCHEMA = "TDOC-offline-online-knowledge-handoff-v1"

    def __init__(self, runtime_root: Path):
        self.root = Path(runtime_root) / "knowledge" / "handoff"
        self.root.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.root / "offline_latest.json.gz"

    @staticmethod
    def _shape_errors(manifest: Mapping[str, Any]) -> list[str]:
        """Reject malformed containers before hash/quality projections consume them."""
        errors = []
        for key in ("setd", "finalization", "reuse_contract", "knowledge_quality", "phase_control_coverage",
                    "control_knowledge_valid_by_phase", "layer_hashes", "kstore_fingerprint"):
            if key in manifest and manifest[key] is not None and not isinstance(manifest[key], Mapping):
                errors.append(key + ":mapping_required")
        quality = manifest.get("knowledge_quality")
        phases = [("phase_control_coverage", manifest.get("phase_control_coverage"))]
        if isinstance(quality, Mapping):
            phases.append(("knowledge_quality.phase_control_coverage", quality.get("phase_control_coverage")))
        for prefix, pc in phases:
            if pc is None:
                continue
            if not isinstance(pc, Mapping):
                errors.append(prefix + ":mapping_required")
                continue
            for key in ("required_control_phases", "missing_control_phases"):
                if key in pc and (not isinstance(pc[key], list) or any(not isinstance(p, str) for p in pc[key])):
                    errors.append(prefix + "." + key + ":string_list_required")
            coverage = pc.get("coverage")
            if coverage is not None and (not isinstance(coverage, Mapping)
                    or any(not isinstance(rec, Mapping) for rec in coverage.values())):
                errors.append(prefix + ".coverage:mapping_records_required")
        reuse = manifest.get("reuse_contract")
        if isinstance(reuse, Mapping) and reuse.get("climate_time_alignment") is not None:
            if not isinstance(reuse["climate_time_alignment"], Mapping):
                errors.append("reuse_contract.climate_time_alignment:mapping_required")
        return list(dict.fromkeys(errors))

    def record_finalized(
        self,
        *,
        ledger_record: Mapping[str, Any],
        finalization: Mapping[str, Any],
        setd: Mapping[str, Any],
        kstore_fingerprint: Mapping[str, Any],
        source_version: str | None,
        layer5_root: str | None,
        climate_time_alignment: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest = (setd.get("manifest") or {}) if isinstance(setd, Mapping) else {}
        stage = (setd.get("knowledge") or {}) if isinstance(setd, Mapping) else {}
        payload = (stage.get("knowledge") or {}) if isinstance(stage, Mapping) else {}
        quality = dict(manifest.get("knowledge_quality") or {})
        phase_control = dict(quality.get("phase_control_coverage") or {})
        phase_control_complete = bool(phase_control.get("all_required_control_phases") is True)
        missing_control_phases = list(phase_control.get("missing_control_phases") or [])
        # Solution-15: OnlineStart consumes a single phase-level consistency bit.
        # A handoff cannot claim complete coverage while simultaneously listing
        # missing controlled phases.  Older manifests without the explicit bit are
        # interpreted from these same authoritative fields by validate_online_start().
        phase_coverage_consistent = bool(phase_control_complete and not missing_control_phases)
        source_control_claim = bool(quality.get("control_knowledge_valid") is True)
        # Solution-13/14 audit invariant remains strict: the global control flag is
        # true only when every required controlled phase has proof.  Solution-4-v2
        # separates that coverage statement from SETD package integrity so Offline
        # knowledge can be used as a prior without becoming actuation authority.
        effective_control_valid = bool(source_control_claim and phase_coverage_consistent)
        by_phase = {
            str(phase): bool(isinstance(rec, Mapping) and rec.get("control_capable") is True)
            for phase, rec in dict(phase_control.get("coverage") or {}).items()
        }
        setd_identity_present = bool(manifest.get("package_id"))
        setd_integrity_valid = bool(
            finalization.get("ok", finalization.get("committed"))
            and finalization.get("committed")
            and setd.get("ok")
            and manifest.get("committed", True)
            and setd_identity_present
        )
        # Legacy strict diagnostic retained for compatibility.  It is deliberately
        # NOT the OnlineStart authority under the phase-local policy.
        valid = bool(setd_integrity_valid and effective_control_valid and phase_coverage_consistent)
        base = {
            "schema": self.SCHEMA,
            "source_learning_mode": "OFFLINE_SE",
            "source_transaction_id": ledger_record.get("transaction_id"),
            "source_version": source_version,
            "created_at": utc_now(),
            "valid_setd": valid,
            "setd_integrity_valid": setd_integrity_valid,
            "offline_control_coverage_complete": phase_coverage_consistent,
            "control_knowledge_valid": effective_control_valid,
            "control_knowledge_valid_by_phase": by_phase,
            "source_control_knowledge_valid_claim": source_control_claim,
            "knowledge_quality": quality,
            "phase_control_coverage": phase_control,
            "phase_control_coverage_complete": phase_control_complete,
            "phase_coverage_consistent": phase_coverage_consistent,
            "handoff_validity_invariant": {
                "contract": "TDOC-offline-handoff-validity-invariant-v1",
                "rule": "control_knowledge_valid_implies_all_required_control_phases",
                "holds": bool((not effective_control_valid) or phase_coverage_consistent),
                "source_claim_downgraded": bool(source_control_claim and not effective_control_valid),
                "online_start_authority": "SETD_INTEGRITY_ONLY",
                "runtime_control_authority": "RTMO_PHASE_LOCAL_CURRENT_AND_NEXT",
            },
            "ledger_record_hash": sha256_obj(dict(ledger_record)),
            "layer_hashes": dict(ledger_record.get("hashes") or {}),
            "layer5_root": layer5_root,
            "setd": {
                "ok": bool(setd.get("ok")),
                "package_id": manifest.get("package_id"),
                "episode_id": manifest.get("episode_id") or stage.get("episode_id"),
                "experience_count": manifest.get("experience_count") or stage.get("experience_count"),
                "last_record_hash": manifest.get("last_record_hash") or stage.get("last_record_hash"),
                "package_manifest": manifest.get("package_manifest"),
                "package_manifest_sha256": manifest.get("package_manifest_sha256"),
                "knowledge_version": payload.get("knowledge_version"),
                "knowledge_hash": payload.get("knowledge_hash"),
                "reference_bundle": payload.get("CRP_SARG_TEP_references") or {},
            },
            "kstore_fingerprint": dict(kstore_fingerprint),
            "finalization": {
                "committed": bool(finalization.get("committed")),
                "ok": bool(finalization.get("ok", finalization.get("committed"))),
                "reason": finalization.get("reason"),
            },
            "reuse_contract": {
                "required_for_online_training": True,
                "read_only_online_load": True,
                "online_retraining_episodes": 0,
                "persistent_learning_authority": "Layer7",
                "layer5_online_persistent_write": False,
                "online_start_authority": "SETD_INTEGRITY_ONLY",
                "mission_certification_source": "offline_control_coverage_complete",
                "runtime_release_authority": "RTMO_PHASE_LOCAL_CURRENT_AND_NEXT",
                "climate_time_alignment": dict(climate_time_alignment or {}),
            },
        }
        base["handoff_hash"] = sha256_obj(base)
        base["knowledge_id"] = f"SETD:{base['setd'].get('package_id') or 'unknown'}:{base['handoff_hash'][:16]}"
        base["knowledge_version"] = str(base["setd"].get("knowledge_version") or source_version or "unknown")
        base["ok"] = setd_integrity_valid
        write_json_gz(self.latest_path, base, indent=2)
        tx = str(base.get("source_transaction_id") or "offline")
        write_json_gz(self.root / f"{tx}.json.gz", base, indent=2)
        # Reporting projection stays outside the hash-bound/persisted manifest. It
        # can evolve without rewriting accepted handoffs or changing their hash.
        return {**base, "manifest_path": str(self.latest_path),
                "knowledge_assessment": assess_offline_knowledge(base,
                    handoff_hash_verified=True, handoff_gate_ok=setd_integrity_valid)}

    def resolve(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        req = request or {}
        p = Path(str(req.get("offline_knowledge_manifest") or self.latest_path))
        if not p.is_absolute():
            p = p.resolve()
        if not p.is_file():
            return {"ok": False, "status": "NOT_READY", "reason": "offline_knowledge_manifest_missing", "manifest_path": str(p)}
        try:
            m = read_json_auto(p)
        except Exception as exc:
            return {"ok": False, "status": "FAILED", "reason": "offline_knowledge_manifest_unreadable", "error": repr(exc), "manifest_path": str(p)}
        if not isinstance(m, Mapping) or m.get("schema") != self.SCHEMA or str(m.get("source_learning_mode")).upper() != "OFFLINE_SE":
            return {"ok": False, "status": "FAILED", "reason": "offline_knowledge_manifest_contract_mismatch", "manifest_path": str(p)}
        shape_errors = self._shape_errors(m)
        if shape_errors:
            return {"ok": False, "status": "FAILED", "reason": "offline_knowledge_manifest_contract_mismatch",
                    "contract_errors": shape_errors, "manifest_path": str(p)}

        # Verify TDOC handoff content before interpreting either integrity or
        # phase-coverage semantics.  This keeps the Offline prior immutable.
        expected = m.get("handoff_hash")
        chk = dict(m)
        for k in ("handoff_hash", "knowledge_id", "knowledge_version", "ok"):
            chk.pop(k, None)
        actual = sha256_obj(chk)
        if not expected or actual != expected:
            return {"ok": False, "status": "FAILED", "reason": "offline_handoff_hash_mismatch", "expected_hash": expected, "actual_hash": actual, "manifest_path": str(p),
                    "knowledge_assessment": assess_offline_knowledge(m, handoff_hash_verified=False, handoff_gate_ok=False)}

        # Solution-13: reject contradictory strict diagnostics, but Solution-4-v2
        # does not treat incomplete future-phase coverage as corruption of a sound
        # SETD prior.
        phase_complete = bool(m.get("phase_control_coverage_complete") is True)
        phase_control = dict(m.get("phase_control_coverage") or {})
        missing_control_phases = list(phase_control.get("missing_control_phases") or [])
        explicit_consistent = m.get("phase_coverage_consistent")
        phase_consistent = bool(
            explicit_consistent is True
            if explicit_consistent is not None
            else (phase_complete and not missing_control_phases)
        )
        control_valid = bool(m.get("control_knowledge_valid") is True)
        valid_setd = bool(m.get("valid_setd") is True)
        finalization = dict(m.get("finalization") or {})
        setd = dict(m.get("setd") or {})
        explicit_integrity = m.get("setd_integrity_valid")
        # Backward-compatible derivation lets a pre-Solution4-v2 handoff whose
        # package/finalization are sound become a prior even when legacy valid_setd
        # is false solely because future-phase coverage is incomplete.
        derived_integrity = bool(
            setd.get("ok") is True
            and setd.get("package_id")
            and finalization.get("ok", finalization.get("committed"))
            and finalization.get("committed")
        )
        # An explicit integrity claim cannot override contradictory source facts.
        setd_integrity_valid = bool(derived_integrity and (explicit_integrity is True if explicit_integrity is not None else True))
        consistency_errors = []
        if phase_complete and missing_control_phases:
            consistency_errors.append("phase_coverage_complete_but_missing_phases_listed")
        if control_valid and not phase_complete:
            consistency_errors.append("control_knowledge_valid_without_complete_phase_coverage")
        if control_valid and not phase_consistent:
            consistency_errors.append("control_knowledge_valid_with_inconsistent_phase_coverage")
        if valid_setd and not control_valid:
            consistency_errors.append("valid_setd_without_control_knowledge_valid")
        if valid_setd and not phase_complete:
            consistency_errors.append("valid_setd_without_complete_phase_coverage")
        if valid_setd and not bool(finalization.get("ok", finalization.get("committed"))):
            consistency_errors.append("valid_setd_without_successful_finalization")
        if valid_setd and not bool(finalization.get("committed")):
            consistency_errors.append("valid_setd_without_committed_finalization")
        if valid_setd and not phase_consistent:
            consistency_errors.append("valid_setd_with_inconsistent_phase_coverage")
        if valid_setd and not bool(setd.get("ok")):
            consistency_errors.append("valid_setd_without_setd_ok")
        if consistency_errors:
            return {
                "ok": False,
                "status": "FAILED",
                "reason": "offline_knowledge_manifest_internal_inconsistency",
                "consistency_errors": consistency_errors,
                "manifest_path": str(p),
                "manifest": m,
                "knowledge_assessment": assess_offline_knowledge(m, handoff_hash_verified=True, handoff_gate_ok=False),
            }
        if not setd_integrity_valid or not setd.get("package_id"):
            return {"ok": False, "status": "NOT_READY", "reason": "valid_finalized_SETD_integrity_required_for_online", "manifest_path": str(p), "manifest": m,
                    "knowledge_assessment": assess_offline_knowledge(m, handoff_hash_verified=True, handoff_gate_ok=False)}
        return {
            "ok": True, "status": "READY", "manifest": m, "manifest_path": str(p),
            "setd_integrity_valid": True,
            "offline_control_coverage_complete": phase_consistent,
            "missing_control_phases": missing_control_phases,
            "legacy_valid_setd": valid_setd,
            "knowledge_assessment": assess_offline_knowledge(m, handoff_hash_verified=True, handoff_gate_ok=True),
        }

    def validate_online_start(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """OnlineStart gate for immutable Offline prior integrity (Solution 4 v2).

        Offline experience is a prior, not mission-wide actuation authority.  This
        gate admits only an immutable, finalized, loadable-identity SETD handoff.
        Phase coverage remains audit/MissionCertification data; current/next-phase
        actuation stays fail-closed under RTMO RuntimeRelease and fresh L4/L5 gates.
        Climate/forcing identity is checked separately by the existing Online climate
        alignment gate before PT_LIB commissioning.
        """
        resolved = self.resolve(request)
        if not resolved.get("ok"):
            underlying = resolved.get("reason")
            structural = underlying in {
                "offline_knowledge_manifest_missing",
                "offline_knowledge_manifest_unreadable",
                "offline_knowledge_manifest_contract_mismatch",
                "offline_handoff_hash_mismatch",
            }
            return {
                "ok": False,
                "status": "NOT_READY",
                "reason": underlying if structural else "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED",
                "handoff_reason": underlying,
                "manifest_path": resolved.get("manifest_path"),
                "consistency_errors": list(resolved.get("consistency_errors") or []),
                "resolved": resolved,
                "knowledge_assessment": resolved.get("knowledge_assessment"),
            }
        m = dict(resolved.get("manifest") or {})
        fin = dict(m.get("finalization") or {})
        setd = dict(m.get("setd") or {})
        pc = dict(m.get("phase_control_coverage") or {})
        missing = list(pc.get("missing_control_phases") or [])
        phase_complete = bool(m.get("phase_control_coverage_complete") is True)
        phase_consistent = bool(
            m.get("phase_coverage_consistent") is True
            if "phase_coverage_consistent" in m
            else (phase_complete and not missing)
        )
        explicit_integrity = m.get("setd_integrity_valid")
        integrity = bool(
            explicit_integrity is True
            if explicit_integrity is not None
            else (setd.get("ok") and setd.get("package_id") and fin.get("ok", fin.get("committed")) and fin.get("committed"))
        )
        checks = {
            "setd_integrity_valid": integrity,
            "setd_package_id_present": bool(setd.get("package_id")),
            "finalization_ok": bool(fin.get("ok", fin.get("committed")) is True),
            "finalization_committed": bool(fin.get("committed") is True),
            "handoff_hash_verified": True,
        }
        blockers = [k for k, v in checks.items() if not v]
        if blockers:
            return {
                "ok": False, "status": "NOT_READY",
                "reason": "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED",
                "blockers": blockers, "checks": checks,
                "missing_control_phases": missing,
                "offline_control_coverage_complete": phase_consistent,
                "manifest_path": resolved.get("manifest_path"), "manifest": m,
                "knowledge_assessment": resolved.get("knowledge_assessment"),
            }
        return {
            "ok": True, "status": "READY",
            "reason": "VALID_FINALIZED_SETD_PRIOR_READY",
            "checks": checks,
            "setd_integrity_valid": True,
            "offline_control_coverage_complete": phase_consistent,
            "mission_certification_status": "FULLY_CERTIFIED" if phase_consistent else "NOT_FULLY_CERTIFIED",
            "missing_control_phases": missing,
            "control_knowledge_valid_by_phase": dict(m.get("control_knowledge_valid_by_phase") or {
                str(k): bool(isinstance(v, Mapping) and v.get("control_capable") is True)
                for k, v in dict(pc.get("coverage") or {}).items()
            }),
            "legacy_valid_setd": bool(m.get("valid_setd") is True),
            "manifest_path": resolved.get("manifest_path"), "manifest": m,
            "knowledge_assessment": resolved.get("knowledge_assessment"),
        }

