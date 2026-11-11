from __future__ import annotations
import time

from tddt_tdoc.rtmo.artifact_gate import RTMOL4ArtifactGate


def snap(x=0.0, vx=10.0):
    return {
        "policy_version":"P1",
        "std":{"representations":{"MESA_NAV":{"canonical_state":{
            "position_ned_m":[x,0.0,0.0],
            "ground_velocity_ned_m_s":[vx,0.0,0.0],
        }}}},
    }


def req(stage="cruise"):
    return {"stage":stage,"context":{
        "offline_knowledge_handoff":{"knowledge_version":"K1"},
        "online_climate_time_alignment":{"forcing_fingerprint":"F1"},
    }}


def verified_result():
    c={
        "candidate_id":"C1","verified":True,"candidate_verified":True,
        "predicted_trajectory":[
            [0,0,0,10,0,0],[10,0,0,10,0,0],[20,0,0,10,0,0],[30,0,0,10,0,0],
        ],
        "verification":{"executed":True,"verified":True},
    }
    return {"snapshot_version":"P1","snapshot_hash":"H1","verified_candidates":[c],"candidates":[c],
            "conformance":{"C_FF_runtime_clean":True},"provenance":{}}


def test_heavy_refresh_never_blocks_fast_gate():
    gate=RTMOL4ArtifactGate(max_age_s=30,position_tolerance_m=20,velocity_tolerance_m_s=4)
    def heavy():
        time.sleep(0.20)
        return {"ok":True,"result":verified_result()}
    t0=time.perf_counter()
    sch=gate.schedule_refresh(invoke=heavy,snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.0,trigger_reason="test")
    schedule_elapsed=time.perf_counter()-t0
    assert sch["scheduled"] is True
    assert schedule_elapsed < 0.05
    t1=time.perf_counter(); pending=gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.1); acquire_elapsed=time.perf_counter()-t1
    assert pending.ok is False and pending.reason=="no_verified_l4_artifact"
    assert acquire_elapsed < 0.05
    assert gate.wait_for_idle(2.0)
    t2=time.perf_counter(); got=gate.acquire(snapshot=snap(5),request=req(),orchestration_version="V1",reference_time_s=0.3); reuse_elapsed=time.perf_counter()-t2
    assert got.ok is True and got.reason=="verified_artifact_reuse"
    assert reuse_elapsed < 0.05
    assert got.result["provenance"]["rtmo_l4_artifact_reuse"]["reuse"] is True
    assert got.prediction_snapshot==snap()
    got.prediction_snapshot['policy_version']='tampered-consumer-copy'
    assert gate.acquire(snapshot=snap(5),request=req(),orchestration_version='V1',reference_time_s=.4).prediction_snapshot==snap()


def test_only_verified_artifact_is_promoted():
    gate=RTMOL4ArtifactGate()
    assert gate.schedule_refresh(invoke=lambda:{"ok":True,"result":{"verified_candidates":[],"conformance":{"C_FF_runtime_clean":True}}},
        snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.0,trigger_reason="test")["scheduled"]
    assert gate.wait_for_idle(1.0)
    got=gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.1)
    assert got.ok is False and got.reason=="no_verified_l4_artifact"
    assert gate.status()["refresh"]["state"]=="UNVERIFIED"


def test_execution_projection_keeps_authority_but_not_full_replay_rows():
    gate=RTMOL4ArtifactGate()
    original=verified_result()
    candidate=original["candidates"][0]
    candidate["verification"].update({
        "certificate_id":"FF-CERT-1",
        "steps":[{"step":index,"payload":"x"*1000} for index in range(20)],
        "commit_segment_steps":[{"step":0}],
    })
    original["provenance"]["full_fidelity_verification"]={
        "owner":"Layer4/WingMPET",
        "audit_complete":True,
        "selected_candidate_ids":["C1"],
        "rows":[{"step":index} for index in range(20)],
    }
    published=gate.publish_bootstrap(
        result=original,
        snapshot=snap(),
        request=req(),
        orchestration_version="V1",
        reference_time_s=0.0,
        l5_final=True,
        bootstrap_qualified=True,
    )
    assert published.ok is True
    got=gate.acquire(
        snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.1,
    )
    assert got.ok is True
    projected=got.result
    assert projected["verified_candidates"]==[{"candidate_id":"C1"}]
    assert projected["candidates"][0]["verification"]["certificate_id"]=="FF-CERT-1"
    assert "steps" not in projected["candidates"][0]["verification"]
    assert "commit_segment_steps" not in projected["candidates"][0]["verification"]
    assert "full_fidelity_verification" not in projected["provenance"]
    assert projected["provenance"]["rtmo_execution_projection"]["full_replay_rows_in_fast_path"] is False
    # Projection must not mutate the authoritative full-fidelity audit object.
    assert len(original["candidates"][0]["verification"]["steps"])==20
    assert len(original["provenance"]["full_fidelity_verification"]["rows"])==20


def _primed_gate():
    gate=RTMOL4ArtifactGate(max_age_s=30,position_tolerance_m=20,velocity_tolerance_m_s=4)
    gate.schedule_refresh(invoke=lambda:{"ok":True,"result":verified_result()},snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.0,trigger_reason="prime")
    assert gate.wait_for_idle(1.0)
    return gate


def test_reuse_is_phase_version_freshness_and_envelope_gated():
    gate=_primed_gate()
    assert gate.acquire(snapshot=snap(5),request=req("turn"),orchestration_version="V1",reference_time_s=1).reason=="artifact_phase_mismatch"
    assert gate.acquire(snapshot=snap(5),request=req(),orchestration_version="V2",reference_time_s=1).reason=="artifact_version_mismatch"
    assert gate.acquire(snapshot=snap(5),request=req(),orchestration_version="V1",reference_time_s=31).reason=="artifact_not_fresh"
    assert gate.acquire(snapshot=snap(100),request=req(),orchestration_version="V1",reference_time_s=1).reason=="artifact_envelope_mismatch"


def test_running_refresh_does_not_invalidate_last_good_artifact():
    gate=_primed_gate()
    def heavy_new():
        time.sleep(0.20)
        return {"ok":True,"result":verified_result()}
    sch=gate.schedule_refresh(invoke=heavy_new,snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=1.0,trigger_reason="age")
    assert sch["scheduled"] is True
    t0=time.perf_counter(); got=gate.acquire(snapshot=snap(5),request=req(),orchestration_version="V1",reference_time_s=1.1); elapsed=time.perf_counter()-t0
    assert got.ok is True
    assert elapsed < 0.05
    assert gate.wait_for_idle(2.0)


def test_newer_completed_unverified_refresh_invalidates_old_execution_artifact():
    gate=_primed_gate()
    sch=gate.schedule_refresh(
        invoke=lambda:{"ok":True,"result":{"verified_candidates":[],"conformance":{"C_FF_runtime_clean":True}}},
        snapshot=snap(1),request=req(),orchestration_version="V1",reference_time_s=1.0,
        trigger_reason="current-state-refresh",
    )
    assert sch["scheduled"] is True
    assert gate.wait_for_idle(1.0)
    got=gate.acquire(snapshot=snap(1),request=req(),orchestration_version="V1",reference_time_s=1.1)
    assert got.ok is False
    assert got.reason=="newer_refresh_unverified"
    assert got.metadata["newer_refresh_invalidates_execution"] is True


def test_episode_reset_invalidates_artifact_and_old_refresh_generation():
    gate=_primed_gate()
    assert gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.2).ok
    reset=gate.reset("episode-2")
    assert reset["ok"] is True
    assert gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.3).ok is False


def test_verified_artifact_authorizes_one_first_move_per_transaction():
    gate=_primed_gate()
    first=gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.1)
    assert first.ok is True
    consumed=gate.mark_first_move_executed(
        metadata=first.metadata,
        transaction_id="tx-1",
        execution_status="EXECUTED",
        actuation_executed=True,
    )
    assert consumed["ok"] is True and consumed["consumed"] is True
    assert consumed["artifact_reusable_after_transaction"] is True
    assert gate.phase_seeded("cruise") is True
    second=gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.2)
    assert second.ok is True
    duplicate=gate.mark_first_move_executed(
        metadata=second.metadata,transaction_id="tx-1",
        execution_status="EXECUTED",actuation_executed=True,
    )
    assert duplicate["ok"] is False
    assert duplicate["reason"]=="transaction_first_move_already_consumed"
    next_tx=gate.mark_first_move_executed(
        metadata=second.metadata,transaction_id="tx-2",
        execution_status="EXECUTED",actuation_executed=True,
    )
    assert next_tx["ok"] is True and next_tx["consumed"] is True


def test_hold_or_mismatched_ack_does_not_consume_current_artifact():
    gate=_primed_gate()
    first=gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.1)
    held=gate.mark_first_move_executed(
        metadata=first.metadata,
        transaction_id="tx-held",
        execution_status="HELD",
        actuation_executed=False,
    )
    assert held["consumed"] is False
    assert gate.acquire(snapshot=snap(),request=req(),orchestration_version="V1",reference_time_s=0.2).ok is True
