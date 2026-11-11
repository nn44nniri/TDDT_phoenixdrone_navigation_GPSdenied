from layer4.multifidelity import CertifiedMultiFidelityManager


def _base():
    return {
        "verification_origin":"FF_CURRENT",
        "physical_actions_consumed":True,
        "physical_runtime_safe":True,
        "physical_mission_safe":True,
        "verdict_authoritative":True,
        "verdict_non_bypassable":True,
        "all_rows_vtol_feasible":True,
        "verdict_sources":["WingMPET/op27-substepped"],
    }


def test_p1_3_base_physical_certificate_contract_unchanged_for_nontransition():
    assert CertifiedMultiFidelityManager._physical_certificate_evidence_ok(_base()) is True


def test_p1_3_transition_certificate_rejects_missing_bridge_workspace_chain():
    assert CertifiedMultiFidelityManager._physical_certificate_evidence_ok(_base(),require_transition_chain=True) is False


def test_p1_3_transition_certificate_accepts_complete_bridge_workspace_chain():
    e=_base()
    e.update({
        "bridge_ready":True,
        "physical_action_count":3,
        "physical_actions_sha256":"a"*64,
        "workspace_count":1,
        "workspace_identity_sha256":"b"*64,
        "transition_audit_complete":True,
    })
    assert CertifiedMultiFidelityManager._physical_certificate_evidence_ok(e,require_transition_chain=True) is True
