from __future__ import annotations

from pathlib import Path

from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.storage import write_json_gz
from tddt_tdoc.utils import sha256_obj


def _setd(*, control_valid: bool, phase_complete: bool):
    quality = {
        "control_knowledge_valid": control_valid,
        "phase_control_coverage": {
            "all_required_control_phases": phase_complete,
            "missing_control_phases": [] if phase_complete else ["vertical_takeoff", "hover"],
        },
    }
    return {
        "ok": True,
        "manifest": {
            "committed": True,
            "package_id": "P13",
            "episode_id": "E13",
            "knowledge_quality": quality,
        },
        "knowledge": {"knowledge": {"knowledge_version": "v13"}},
    }


def test_record_finalized_normalizes_contradictory_layer5_claim(tmp_path: Path):
    h = OfflineKnowledgeHandoff(tmp_path)
    out = h.record_finalized(
        ledger_record={"transaction_id": "off-13", "hashes": {}},
        finalization={"committed": True, "ok": True},
        setd=_setd(control_valid=True, phase_complete=False),
        kstore_fingerprint={}, source_version="v13", layer5_root="/layer5",
    )
    assert out["source_control_knowledge_valid_claim"] is True
    assert out["phase_control_coverage_complete"] is False
    assert out["control_knowledge_valid"] is False
    assert out["valid_setd"] is False
    assert out["setd_integrity_valid"] is True
    assert out["offline_control_coverage_complete"] is False
    assert out["ok"] is True
    assert out["handoff_validity_invariant"]["holds"] is True
    assert out["handoff_validity_invariant"]["source_claim_downgraded"] is True


def test_resolve_rejects_hash_valid_internally_contradictory_legacy_handoff(tmp_path: Path):
    h = OfflineKnowledgeHandoff(tmp_path)
    m = {
        "schema": h.SCHEMA,
        "source_learning_mode": "OFFLINE_SE",
        "source_transaction_id": "legacy-13",
        "source_version": "legacy",
        "created_at": "2026-09-15T00:00:00Z",
        "valid_setd": True,
        "control_knowledge_valid": True,
        "phase_control_coverage_complete": False,
        "phase_control_coverage": {
            "all_required_control_phases": False,
            "missing_control_phases": ["vertical_takeoff", "hover"],
        },
        "knowledge_quality": {},
        "ledger_record_hash": "ledger",
        "layer_hashes": {},
        "layer5_root": "/layer5",
        "setd": {"ok": True, "package_id": "P-legacy"},
        "kstore_fingerprint": {},
        "finalization": {"committed": True, "ok": True},
        "reuse_contract": {},
    }
    # Reproduce the historical handoff hash algorithm exactly so rejection is
    # due to semantic inconsistency, not hash corruption.
    m["handoff_hash"] = sha256_obj(m)
    m["knowledge_id"] = "SETD:P-legacy:" + m["handoff_hash"][:16]
    m["knowledge_version"] = "legacy"
    m["ok"] = True
    p = tmp_path / "legacy_inconsistent.json.gz"
    write_json_gz(p, m, indent=2)

    out = h.resolve({"offline_knowledge_manifest": str(p)})
    assert out["ok"] is False
    assert out["status"] == "FAILED"
    assert out["reason"] == "offline_knowledge_manifest_internal_inconsistency"
    assert "control_knowledge_valid_without_complete_phase_coverage" in out["consistency_errors"]
    assert "valid_setd_without_complete_phase_coverage" in out["consistency_errors"]


def test_resolve_accepts_consistent_valid_handoff(tmp_path: Path):
    h = OfflineKnowledgeHandoff(tmp_path)
    out = h.record_finalized(
        ledger_record={"transaction_id": "off-good-13", "hashes": {}},
        finalization={"committed": True, "ok": True},
        setd=_setd(control_valid=True, phase_complete=True),
        kstore_fingerprint={}, source_version="v13", layer5_root="/layer5",
    )
    resolved = h.resolve({"offline_knowledge_manifest": out["manifest_path"]})
    assert resolved["ok"] is True
    assert resolved["status"] == "READY"
    assert resolved["manifest"]["control_knowledge_valid"] is True
    assert resolved["manifest"]["phase_control_coverage_complete"] is True
