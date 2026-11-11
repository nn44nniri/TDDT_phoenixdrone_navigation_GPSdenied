from pathlib import Path

from tddt_tdoc.rtmo.capability_gate import CapabilityManifest, build_capability_manifest
from tddt_tdoc.rtmo.capability_registry import LiveCapabilityRegistry
from tddt_tdoc.rtmo.phase_manager import PHASE_ORDER


def _manifest():
    phases={p:{"capability":False} for p in PHASE_ORDER}
    phases["on_ground"]={"capability":True}
    return CapabilityManifest(phases=phases,source="test")


def _evidence(phase, *, qualified=True):
    return {
        "schema":"TDOC-RTMO-pre-release-verify-v1",
        "phase":phase,
        "qualified":qualified,
        "non_actuating":True,
        "layer2_current_state":True,
        "layer3_atomic_state":True,
        "l4_full_fidelity_verified":True,
        "l5_final":True,
        "control_effect_applied":False,
        "transaction_id":f"pre-{phase}",
    }


def _runtime(phase, **overrides):
    ev={
        "schema":"TDOC-RTMO-current-runtime-release-evidence-v1",
        "phase":phase,
        "learning_mode":"ONLINE_JSBSIM",
        "transaction_id":f"tx-{phase}",
        "fresh_operational_state":True,
        "current_l4_artifact_verified":True,
        "current_l4_artifact_compatible":True,
        "current_l5_final":True,
        "clock_freshness_ok":True,
        "layer6_ready":True,
    }
    ev.update(overrides)
    return ev


def test_missing_current_phase_pre_release_qualifies_capability_but_not_actuation_by_itself():
    m=_manifest()
    r=m.runtime_release("vertical_takeoff",next_phase="hover",pre_release_evidence=_evidence("vertical_takeoff"))
    assert r["status"]=="WAIT_RUNTIME_RELEASE"
    assert r["actuation_release"] is False
    assert r["current_phase_capability"] is True
    assert r["current_phase_durable_capability"] is False
    assert r["current_phase_pre_release_qualified"] is True
    assert r["current_phase_capability_source"]=="PRE_RELEASE_VERIFY_CURRENT_STATE"
    assert r["current_runtime_release"] is False
    assert r["next_phase_capability"] is False
    assert r["transition_release"] is False
    assert r["pre_release_verify_is_durable_joint_proof"] is False
    assert r["pre_release_verify_is_actuation_authority"] is False

    r2=m.runtime_release(
        "vertical_takeoff",next_phase="hover",
        pre_release_evidence=_evidence("vertical_takeoff"),
        current_runtime_evidence=_runtime("vertical_takeoff"),
    )
    assert r2["status"]=="RELEASED"
    assert r2["actuation_release"] is True
    assert r2["current_runtime_release"] is True


def test_invalid_or_wrong_phase_bootstrap_remains_fail_closed():
    m=_manifest()
    bad=_evidence("hover")
    r=m.runtime_release("vertical_takeoff",pre_release_evidence=bad)
    assert r["status"]=="WAIT_CAPABILITY"
    assert r["actuation_release"] is False
    assert "current_phase_capability_blocked:vertical_takeoff" in r["blockers"]


def test_imminent_phase_bootstrap_can_release_transition_without_durable_promotion():
    phases={p:{"capability":False} for p in PHASE_ORDER}
    phases["transition_back"]={"capability":True}
    m=CapabilityManifest(phases=phases,source="test")
    r=m.runtime_release(
        "transition_back",next_phase="vertical_landing",
        next_pre_release_evidence=_evidence("vertical_landing"),
        current_runtime_evidence=_runtime("transition_back"),
    )
    assert r["actuation_release"] is True
    assert r["next_phase_durable_capability"] is False
    assert r["next_phase_pre_release_qualified"] is True
    assert r["transition_release"] is True


def test_pre_release_registry_record_is_audit_only_and_never_joint_control_proof(tmp_path:Path):
    rp=tmp_path/"capability_registry.json.gz"
    reg=LiveCapabilityRegistry(rp)
    reg.reset(run_id="r",episode_id="e")
    reg.record_pre_release_verification(phase="vertical_takeoff",evidence=_evidence("vertical_takeoff"))
    snap=reg.snapshot()["phases"]["vertical_takeoff"]
    assert snap["pre_release_verification"]["qualified"] is True
    assert snap["l4_verified"] is False
    assert snap["l5_final"] is False
    assert snap["joint_control_proof_cycles"]==[]
    cap=build_capability_manifest(tmp_path,layer6_forward_only=False,execution_overrides={p:True for p in PHASE_ORDER},capability_registry_path=rp,require_live_registry=True,require_certificate_guard=False)
    assert cap.phases["vertical_takeoff"]["capability"] is False
    assert "no_same_cycle_joint_control_proof_for_phase" in cap.phases["vertical_takeoff"]["blockers"]


def test_orchestrator_pre_release_lane_explicitly_skips_layer6_and_layer7():
    src=(Path(__file__).resolve().parents[1]/"src/tddt_tdoc/orchestrator.py").read_text()
    assert 'pre_release_verify=bool(mode=="ONLINE_JSBSIM"' in src
    assert 'NOT_INVOKED_PRE_RELEASE_VERIFY' in src
    assert 'pre_release_verify_non_actuating' in src
    assert 'not pre_release_verify' in src  # fresh L4, not fast artifact reuse
