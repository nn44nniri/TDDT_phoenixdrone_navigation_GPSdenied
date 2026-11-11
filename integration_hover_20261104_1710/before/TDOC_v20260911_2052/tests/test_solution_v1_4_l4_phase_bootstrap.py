from pathlib import Path
import time

from tddt_tdoc.rtmo.artifact_gate import RTMOL4ArtifactGate


def snap(x=0.0, vx=0.0):
    return {
        "policy_version":"P1",
        "std":{"representations":{"MESA_NAV":{"canonical_state":{
            "position_ned_m":[x,0.0,0.0],
            "ground_velocity_ned_m_s":[vx,0.0,0.0],
        }}}},
    }


def req(stage="vertical_takeoff"):
    return {"stage":stage,"context":{
        "offline_knowledge_handoff":{"knowledge_version":"K1"},
        "online_climate_time_alignment":{"forcing_fingerprint":"F1"},
    }}


def verified_result(vx=0.0):
    c={
        "candidate_id":"C1","verified":True,"candidate_verified":True,
        "predicted_trajectory":[
            [0,0,0,vx,0,0],[1,0,0,vx,0,0],[2,0,0,vx,0,0],
        ],
        "verification":{"executed":True,"verified":True},
    }
    return {"snapshot_version":"P1","snapshot_hash":"H1","verified_candidates":[c],"candidates":[c],
            "conformance":{"C_FF_runtime_clean":True},"provenance":{}}


def test_qualified_pre_release_atomically_seeds_fast_lane():
    gate=RTMOL4ArtifactGate(max_age_s=30,position_tolerance_m=20,velocity_tolerance_m_s=4)
    pub=gate.publish_bootstrap(
        result=verified_result(),snapshot=snap(),request=req(),orchestration_version="V1",
        reference_time_s=0.0,l5_final=True,bootstrap_qualified=True,transaction_id="boot-vtol",
    )
    assert pub.ok is True
    assert pub.reason=="bootstrap_artifact_published"
    assert gate.phase_seeded("vertical_takeoff") is True
    st=gate.status()
    assert st["refresh"]["state"]=="READY"
    assert st["artifact_metadata"]["publication_mode"]=="SYNCHRONOUS_PRE_RELEASE"
    got=gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.1)
    assert got.ok is True and got.reason=="verified_artifact_reuse"


def test_bootstrap_never_publishes_without_full_qualification_and_l5_final():
    for qualified,l5_final,expected in [
        (False,True,"bootstrap_not_qualified"),
        (True,False,"bootstrap_l5_not_final"),
    ]:
        gate=RTMOL4ArtifactGate()
        pub=gate.publish_bootstrap(
            result=verified_result(),snapshot=snap(),request=req(),orchestration_version="V1",
            reference_time_s=0.0,l5_final=l5_final,bootstrap_qualified=qualified,transaction_id="x",
        )
        assert pub.ok is False and pub.reason==expected
        assert gate.phase_seeded("vertical_takeoff") is False


def test_unverified_l4_cannot_seed_bootstrap_cache():
    gate=RTMOL4ArtifactGate()
    pub=gate.publish_bootstrap(
        result={"verified_candidates":[],"conformance":{"C_FF_runtime_clean":True}},
        snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.0,
        l5_final=True,bootstrap_qualified=True,transaction_id="x",
    )
    assert pub.ok is False and pub.reason=="bootstrap_l4_not_verified"
    assert gate.status()["has_artifact"] is False


def test_bootstrap_invalidates_older_background_refresh_generation():
    gate=RTMOL4ArtifactGate(max_age_s=30)
    def old_refresh():
        time.sleep(0.15)
        return {"ok":True,"result":verified_result(vx=1.0)}
    assert gate.schedule_refresh(
        invoke=old_refresh,snapshot=snap(vx=1.0),request=req("cruise"),orchestration_version="V1",
        reference_time_s=0.0,trigger_reason="old-phase-refresh",
    )["scheduled"] is True
    pub=gate.publish_bootstrap(
        result=verified_result(vx=0.0),snapshot=snap(),request=req("vertical_takeoff"),
        orchestration_version="V1",reference_time_s=0.1,l5_final=True,bootstrap_qualified=True,
        transaction_id="new-bootstrap",
    )
    assert pub.ok is True
    gate.wait_for_idle(1.0)
    assert gate.phase_seeded("vertical_takeoff") is True
    assert gate.phase_seeded("cruise") is False


def test_online_workflow_uses_phase_entry_bootstrap_independent_of_durable_capability():
    root=Path(__file__).resolve().parents[1]
    src=(root/"scripts/run_training_workflows.py").read_text()
    assert "PRE_RELEASE_CURRENT_PHASE_BOOTSTRAP" in src
    assert "_bootstrap_target=(_next if phase=='on_ground'" in src
    assert "if not orch.rtmo_l4_artifacts.phase_seeded(_bootstrap_target)" in src
    assert "durable_current" not in src[src.index("# P0-4: cold-start/phase-entry"):src.index("# P0-3: historical/durable", src.index("# P0-4: cold-start/phase-entry"))]
    assert "publish_bootstrap(" in src
    assert "next_phase_artifact_seeded" in src


def test_non_actuating_bootstrap_hold_retries_next_cycle_without_ending_episode():
    root=Path(__file__).resolve().parents[1]
    src=(root/"scripts/run_training_workflows.py").read_text()
    start=src.index("if not l4_bootstrap_event['artifact_published']:")
    end=src.index("pre_release_current=phase_bootstrap_evidence.get(phase)",start)
    hold=src[start:end]
    assert "RTMO WAIT_L4_BOOTSTRAP" in hold
    assert "'layer6_runtime':{'status':'NOT_INVOKED'" in hold
    assert "'layer7_status':'NOT_INVOKED'" in hold
    assert "continue" in hold
    assert "\n                                break\n" not in hold
