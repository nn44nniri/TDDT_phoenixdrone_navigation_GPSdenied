from __future__ import annotations

import gzip, json
from pathlib import Path

from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff


def _setd(*, missing=("vertical_takeoff","hover","vertical_landing")):
    required=["vertical_takeoff","hover","transition_forward","climb","cruise","turn","approach","transition_back","vertical_landing"]
    cov={p:{"control_capable":p not in missing,"joint_proof_cycles":1 if p not in missing else 0} for p in required}
    return {
        "ok":True,
        "manifest":{
            "committed":True,"package_id":"SETD-S4V2","episode_id":"E-S4V2",
            "knowledge_quality":{
                "control_knowledge_valid":False,
                "phase_control_coverage":{
                    "all_required_control_phases":False,
                    "required_control_phases":required,
                    "missing_control_phases":list(missing),
                    "coverage":cov,
                },
            },
        },
        "knowledge":{"knowledge":{"knowledge_version":"v4-v2"}},
    }


def test_incomplete_future_phase_coverage_is_prior_reusable_but_not_legacy_valid(tmp_path: Path):
    h=OfflineKnowledgeHandoff(tmp_path)
    out=h.record_finalized(
        ledger_record={"transaction_id":"off-s4v2","hashes":{}},
        finalization={"ok":True,"committed":True},setd=_setd(),
        kstore_fingerprint={},source_version="v4-v2",layer5_root="/layer5",
        climate_time_alignment={"effective_climate_epoch":"2026-09-17T11:00:00","forcing_start":"2026-09-17T11:00:00","forcing_file_sha256":"abc"},
    )
    assert out["setd_integrity_valid"] is True
    assert out["offline_control_coverage_complete"] is False
    assert out["valid_setd"] is False
    assert out["ok"] is True
    assert out["control_knowledge_valid_by_phase"]["cruise"] is True
    assert out["control_knowledge_valid_by_phase"]["vertical_takeoff"] is False
    gate=h.validate_online_start({"offline_knowledge_manifest":out["manifest_path"]})
    assert gate["ok"] is True
    assert gate["mission_certification_status"] == "NOT_FULLY_CERTIFIED"
    assert gate["missing_control_phases"] == ["vertical_takeoff","hover","vertical_landing"]


def test_corrupt_or_unfinalized_prior_still_fails_closed(tmp_path: Path):
    h=OfflineKnowledgeHandoff(tmp_path)
    out=h.record_finalized(
        ledger_record={"transaction_id":"off-bad","hashes":{}},
        finalization={"ok":False,"committed":True},setd=_setd(),
        kstore_fingerprint={},source_version="v4-v2",layer5_root="/layer5",
    )
    assert out["setd_integrity_valid"] is False
    gate=h.validate_online_start({"offline_knowledge_manifest":out["manifest_path"]})
    assert gate["ok"] is False
    assert gate["reason"] == "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED"


def test_workflow_online_start_does_not_require_global_control_coverage():
    root=Path(__file__).resolve().parents[1]
    text=(root/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    gate=text[text.index("online_start_gate=orch.offline_handoff.validate_online_start"):text.index("if need_online and rc==0:", text.index("online_start_gate=orch.offline_handoff.validate_online_start"))]
    checks=gate[gate.index("_setd_checks={"):gate.index("_setd_blockers=")]
    assert "control_knowledge_valid" not in checks
    assert "phase_coverage_consistent" not in checks
    assert "'setd_loaded'" in checks and "'setd_identity_match'" in checks
    assert "'mission_certification_status':'FULLY_CERTIFIED' if _phase_consistent else 'NOT_FULLY_CERTIFIED'" in gate


def test_rtmo_runtime_release_remains_phase_local_authority():
    # Runtime behavior, not a legacy local-variable name: durable capability
    # alone must NOT authorize actuation; current evidence is also required.
    from tddt_tdoc.rtmo.capability_gate import CapabilityManifest
    m=CapabilityManifest(phases={'hover':{'capability':True},'cruise':{'capability':False}},source='test')
    blocked=m.runtime_release('hover',next_phase='cruise')
    assert not blocked['actuation_release'] and not blocked['transition_release']
    ev={'schema':'TDOC-RTMO-current-runtime-release-evidence-v1','phase':'hover','learning_mode':'ONLINE_JSBSIM',
        'transaction_id':'unit-test-current','fresh_operational_state':True,'current_l4_artifact_verified':True,
        'current_l4_artifact_compatible':True,'current_l5_final':True,'clock_freshness_ok':True,'layer6_ready':True}
    current=m.runtime_release('hover',next_phase='cruise',current_runtime_evidence=ev)
    assert current['actuation_release'] is True
    assert current['transition_release'] is False
    assert current['far_phase_capabilities_are_admission_blockers'] is False


def test_legacy_solution16_style_handoff_with_false_valid_setd_can_be_prior(tmp_path: Path):
    from tddt_tdoc.storage import write_json_gz
    from tddt_tdoc.utils import sha256_obj
    h=OfflineKnowledgeHandoff(tmp_path)
    m={
        "schema":h.SCHEMA,"source_learning_mode":"OFFLINE_SE","source_transaction_id":"legacy-s16",
        "source_version":"TDOC-runtime-v1","created_at":"2026-09-17T11:00:00Z",
        "valid_setd":False,"control_knowledge_valid":False,
        "phase_control_coverage_complete":False,"phase_coverage_consistent":False,
        "phase_control_coverage":{"all_required_control_phases":False,"missing_control_phases":["vertical_takeoff","hover","vertical_landing"]},
        "knowledge_quality":{},"ledger_record_hash":"ledger","layer_hashes":{},"layer5_root":"/layer5",
        "setd":{"ok":True,"package_id":"SETD-legacy","package_manifest_sha256":"abc"},
        "kstore_fingerprint":{},"finalization":{"committed":True,"ok":True},
        "reuse_contract":{"climate_time_alignment":{"effective_climate_epoch":"2026-09-17T11:00:00","forcing_start":"2026-09-17T11:00:00","forcing_file_sha256":"abc"}},
    }
    m["handoff_hash"]=sha256_obj(m); m["knowledge_id"]="SETD:legacy:"+m["handoff_hash"][:16]; m["knowledge_version"]="legacy"; m["ok"]=False
    p=tmp_path/'legacy_solution16.json.gz'; write_json_gz(p,m,indent=2)
    gate=h.validate_online_start({"offline_knowledge_manifest":str(p)})
    assert gate["ok"] is True
    assert gate["setd_integrity_valid"] is True
    assert gate["offline_control_coverage_complete"] is False
    assert gate["legacy_valid_setd"] is False
    assert gate["mission_certification_status"] == "NOT_FULLY_CERTIFIED"
