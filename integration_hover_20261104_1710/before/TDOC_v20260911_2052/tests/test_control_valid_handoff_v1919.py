from __future__ import annotations
from pathlib import Path
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff


def _setd(control_valid: bool, phase_complete: bool | None = None):
    if phase_complete is None:
        phase_complete = bool(control_valid)
    q={
        "control_knowledge_valid":control_valid,
        "phase_control_coverage":{
            "all_required_control_phases":bool(phase_complete),
            "missing_control_phases":[] if phase_complete else ["hover"],
        },
    }
    return {"ok":True,"manifest":{"committed":True,"package_id":"P1","episode_id":"E1","knowledge_quality":q},"knowledge":{"knowledge":{"knowledge_version":"v1"}}}


def test_safety_only_setd_is_integrity_valid_prior_but_not_control_complete(tmp_path: Path):
    h=OfflineKnowledgeHandoff(tmp_path)
    out=h.record_finalized(ledger_record={"transaction_id":"off-1","hashes":{}},finalization={"committed":True,"ok":True},setd=_setd(False),kstore_fingerprint={},source_version="v1",layer5_root="/x")
    assert out["valid_setd"] is False
    assert out["setd_integrity_valid"] is True
    assert out["offline_control_coverage_complete"] is False
    assert out["control_knowledge_valid"] is False
    assert out["ok"] is True


def test_control_valid_setd_creates_valid_online_handoff(tmp_path: Path):
    h=OfflineKnowledgeHandoff(tmp_path)
    out=h.record_finalized(ledger_record={"transaction_id":"off-2","hashes":{}},finalization={"committed":True,"ok":True},setd=_setd(True),kstore_fingerprint={},source_version="v1",layer5_root="/x")
    assert out["valid_setd"] is True
    assert out["control_knowledge_valid"] is True
    assert out["ok"] is True


def test_control_valid_handoff_persists_climate_time_alignment_contract(tmp_path):
    h=OfflineKnowledgeHandoff(tmp_path)
    climate={
        'basis':'active_offline_forcing_window_start',
        'configured_layer3_epoch':'2026-08-24T11:00:00',
        'effective_climate_epoch':'2026-09-11T16:00:00',
        'forcing_start':'2026-09-11T16:00:00',
        'forcing_end':'2026-09-12T15:00:00',
        'epoch_rebased_to_forcing_start':True,
        'forcing_file_sha256':'abc123',
    }
    out=h.record_finalized(ledger_record={'transaction_id':'off-time','hashes':{}},finalization={'committed':True,'ok':True},setd=_setd(True),kstore_fingerprint={},source_version='v1',layer5_root='/x',climate_time_alignment=climate)
    assert out['ok'] is True
    assert out['reuse_contract']['climate_time_alignment']==climate
    resolved=h.resolve({'offline_knowledge_manifest':out['manifest_path']})
    assert resolved['ok'] is True
    assert resolved['manifest']['reuse_contract']['climate_time_alignment']['forcing_file_sha256']=='abc123'


def test_control_valid_flag_cannot_override_incomplete_phase_coverage(tmp_path: Path):
    h=OfflineKnowledgeHandoff(tmp_path)
    out=h.record_finalized(ledger_record={"transaction_id":"off-phase","hashes":{}},finalization={"committed":True,"ok":True},setd=_setd(True,False),kstore_fingerprint={},source_version="v1",layer5_root="/x")
    assert out["control_knowledge_valid"] is False
    assert out["source_control_knowledge_valid_claim"] is True
    assert out["phase_control_coverage_complete"] is False
    assert out["handoff_validity_invariant"]["source_claim_downgraded"] is True
    assert out["valid_setd"] is False
    assert out["setd_integrity_valid"] is True
    assert out["offline_control_coverage_complete"] is False
    assert out["ok"] is True
