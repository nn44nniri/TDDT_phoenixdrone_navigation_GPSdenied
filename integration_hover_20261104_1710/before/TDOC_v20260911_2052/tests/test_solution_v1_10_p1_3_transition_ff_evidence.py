from tddt_tdoc.adapters.layer4 import Layer4Adapter


def _bridge(ok=True):
    return {
        "ok": ok,
        "reason": "validated_phoenix_action_bridge" if ok else "phoenix_physical_mixer_unvalidated",
        "owner": "TDOC/RuntimeScheduler",
        "binding_source": "PhoenixSpecialization",
        "verification_mapping": "WingMPET simulation-only model-qualified effective-action certificate",
        "physical_actions": [
            {"left_motor_cmd":0.6,"right_motor_cmd":0.6,"left_elevon_rad":0.0,"right_elevon_rad":0.0},
            {"left_motor_cmd":0.62,"right_motor_cmd":0.62,"left_elevon_rad":0.01,"right_elevon_rad":0.01},
        ] if ok else [],
    }


def _rows():
    return [
        {
            "physical_action_consumed": True,
            "physical_action_sha256": "a"*64,
            "physical_replay": {"source":"WingMPET/op27-substepped"},
            "verification_workspace_identity": {"run_id":"run-r1","candidate_id":"candidate-C1","audit_file":"ff_replay_audit.json.gz"},
        },
        {
            "physical_action_consumed": True,
            "physical_action_sha256": "b"*64,
            "physical_replay": {"source":"WingMPET/op27-substepped"},
            "verification_workspace_identity": {"run_id":"run-r1","candidate_id":"candidate-C1","audit_file":"ff_replay_audit.json.gz"},
        },
    ]


def test_p1_3_current_transition_attempt_has_complete_evidence_chain():
    ev=Layer4Adapter._transition_ff_evidence(
        "transition_forward",bridge=_bridge(),rows=_rows(),
        outcome_status="ATTEMPTED_AND_REJECTED",reason_codes=["terrain_clearance"],
    )
    assert ev["schema"]=="TDOC-Transition-FF-evidence-v1"
    assert ev["required"] is True
    assert ev["audit_complete"] is True
    assert ev["bridge_ready"] is True
    assert ev["physical_action_count"]==2
    assert ev["physical_action_consumed_count"]==2
    assert ev["physical_actions_consumed_all"] is True
    assert ev["workspace_count"]==1
    assert ev["physical_actions_sha256"]
    assert ev["workspace_identity_sha256"]
    assert all(src.startswith("WingMPET/op27") for src in ev["replay_sources"])


def test_p1_3_bridge_failure_is_explicit_not_ready_not_fake_verified():
    ev=Layer4Adapter._transition_ff_evidence(
        "transition_forward",bridge=_bridge(False),rows=[],
        outcome_status="NOT_ATTEMPTED",reason_codes=["phoenix_action_bridge_unvalidated"],
    )
    assert ev["audit_complete"] is True
    assert ev["bridge_ready"] is False
    assert ev["physical_action_count"]==0
    assert ev["physical_actions_consumed_all"] is False


def test_p1_3_transition_certificate_reuse_requires_original_physical_chain():
    cert={
        "verified":True,
        "certificate_id":"cert-transition-1",
        "physical_issuance_evidence":{
            "bridge_ready":True,
            "bridge_reason":"validated_phoenix_action_bridge",
            "bridge_owner":"TDOC/RuntimeScheduler",
            "bridge_binding_source":"PhoenixSpecialization",
            "bridge_sha256":"c"*64,
            "physical_actions_consumed":True,
            "physical_action_count":2,
            "physical_actions_sha256":"d"*64,
            "workspace_count":1,
            "workspace_identity_sha256":"e"*64,
            "verdict_authoritative":True,
            "verdict_non_bypassable":True,
            "all_rows_vtol_feasible":True,
            "verdict_sources":["WingMPET/op27-substepped"],
            "transition_audit_complete":True,
        },
    }
    ev=Layer4Adapter._transition_ff_evidence(
        "transition_back",outcome_status="ATTEMPTED_AND_VERIFIED",
        reason_codes=["ff_certificate_reuse"],certificate=cert,
    )
    assert ev["audit_complete"] is True
    assert ev["certificate_reuse"] is True
    assert ev["certificate_id"]=="cert-transition-1"
    assert ev["bridge_ready"] is True
    assert ev["physical_action_count"]==2
    assert ev["workspace_count"]==1
