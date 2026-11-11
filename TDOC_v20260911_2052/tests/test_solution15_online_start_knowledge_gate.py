from __future__ import annotations

from pathlib import Path

from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.storage import write_json_gz
from tddt_tdoc.utils import sha256_obj


def _setd(*, control_valid=True, complete=True, missing=None):
    if missing is None:
        missing = [] if complete else ["vertical_takeoff", "hover"]
    return {
        "ok": True,
        "manifest": {
            "committed": True,
            "package_id": "SETD-S15",
            "episode_id": "E-S15",
            "knowledge_quality": {
                "control_knowledge_valid": bool(control_valid),
                "phase_control_coverage": {
                    "all_required_control_phases": bool(complete),
                    "missing_control_phases": list(missing),
                },
            },
        },
        "knowledge": {"knowledge": {"knowledge_version": "v15"}},
    }


def _record(tmp_path: Path, *, final_ok=True, committed=True, control_valid=True, complete=True, missing=None):
    h = OfflineKnowledgeHandoff(tmp_path)
    out = h.record_finalized(
        ledger_record={"transaction_id": "off-s15", "hashes": {}},
        finalization={"ok": bool(final_ok), "committed": bool(committed)},
        setd=_setd(control_valid=control_valid, complete=complete, missing=missing),
        kstore_fingerprint={},
        source_version="v15",
        layer5_root="/layer5",
    )
    return h, out


def test_online_start_accepts_only_complete_finalized_control_handoff(tmp_path: Path):
    h, out = _record(tmp_path)
    gate = h.validate_online_start({"offline_knowledge_manifest": out["manifest_path"]})
    assert gate["ok"] is True
    assert gate["reason"] == "VALID_FINALIZED_SETD_PRIOR_READY"
    assert gate["checks"] == {
        "setd_integrity_valid": True,
        "setd_package_id_present": True,
        "finalization_ok": True,
        "finalization_committed": True,
        "handoff_hash_verified": True,
    }
    assert gate["offline_control_coverage_complete"] is True


def test_online_start_accepts_integrity_valid_prior_with_incomplete_future_phase_coverage(tmp_path: Path):
    h, out = _record(tmp_path, control_valid=True, complete=False)
    gate = h.validate_online_start({"offline_knowledge_manifest": out["manifest_path"]})
    assert gate["ok"] is True
    assert gate["reason"] == "VALID_FINALIZED_SETD_PRIOR_READY"
    assert gate["offline_control_coverage_complete"] is False
    assert gate["mission_certification_status"] == "NOT_FULLY_CERTIFIED"
    assert gate["missing_control_phases"] == ["vertical_takeoff", "hover"]


def test_record_finalized_keeps_integrity_separate_from_inconsistent_phase_coverage(tmp_path: Path):
    h, out = _record(tmp_path, control_valid=True, complete=True, missing=["hover"])
    assert out["phase_control_coverage_complete"] is True
    assert out["phase_coverage_consistent"] is False
    assert out["control_knowledge_valid"] is False
    assert out["valid_setd"] is False
    assert out["setd_integrity_valid"] is True
    assert out["offline_control_coverage_complete"] is False
    gate = h.validate_online_start({"offline_knowledge_manifest": out["manifest_path"]})
    assert gate["ok"] is False
    assert gate["reason"] == "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED"
    assert gate["handoff_reason"] == "offline_knowledge_manifest_internal_inconsistency"


def test_online_start_rejects_finalization_ok_false_even_when_committed(tmp_path: Path):
    h, out = _record(tmp_path, final_ok=False, committed=True)
    assert out["valid_setd"] is False
    gate = h.validate_online_start({"offline_knowledge_manifest": out["manifest_path"]})
    assert gate["ok"] is False
    assert gate["reason"] == "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED"


def test_resolve_rejects_hash_valid_legacy_valid_setd_when_finalization_ok_false(tmp_path: Path):
    h = OfflineKnowledgeHandoff(tmp_path)
    m = {
        "schema": h.SCHEMA,
        "source_learning_mode": "OFFLINE_SE",
        "source_transaction_id": "legacy-s15",
        "source_version": "legacy",
        "created_at": "2026-09-15T00:00:00Z",
        "valid_setd": True,
        "control_knowledge_valid": True,
        "phase_control_coverage_complete": True,
        "phase_coverage_consistent": True,
        "phase_control_coverage": {"all_required_control_phases": True, "missing_control_phases": []},
        "knowledge_quality": {},
        "ledger_record_hash": "ledger",
        "layer_hashes": {},
        "layer5_root": "/layer5",
        "setd": {"ok": True, "package_id": "P-legacy-s15"},
        "kstore_fingerprint": {},
        "finalization": {"committed": True, "ok": False},
        "reuse_contract": {},
    }
    m["handoff_hash"] = sha256_obj(m)
    m["knowledge_id"] = "SETD:P-legacy-s15:" + m["handoff_hash"][:16]
    m["knowledge_version"] = "legacy"
    m["ok"] = True
    p = tmp_path / "legacy_s15.json.gz"
    write_json_gz(p, m, indent=2)
    out = h.validate_online_start({"offline_knowledge_manifest": str(p)})
    assert out["ok"] is False
    assert out["reason"] == "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED"
    assert out["handoff_reason"] == "offline_knowledge_manifest_internal_inconsistency"
    assert "valid_setd_without_successful_finalization" in out["consistency_errors"]


def test_training_workflow_places_knowledge_gate_before_commissioning_and_rtmo():
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_training_workflows.py"
    text = script.read_text(encoding="utf-8")
    gate = text.index("ONLINE_START_KNOWLEDGE_GATE.json")
    commissioning = text.index("ONLINE CONTROL COMMISSIONING", gate)
    rtmo = text.index("build_capability_manifest(report_root", gate)
    assert gate < commissioning < rtmo
    assert "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED" in text
