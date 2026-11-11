import json
from pathlib import Path
from tddt_tdoc.rtmo.capability_registry import LiveCapabilityRegistry
from tddt_tdoc.rtmo.capability_gate import build_capability_manifest

PHASES=("vertical_takeoff","hover","vertical_landing")

def _cert(phase):
    return {
        "schema":"Layer4-FF-certificate-v4","origin":"FF_CURRENT","verified":True,"phase":phase,
        "model_hash":"m7","parameter_hash":"p7","climate_hash":"c7",
        "context_signature":"ctx7","scenario_semantics_hash":"sem7","ff_evidence_hash":"ff7",
        "freshness_clock":"mission_reference","reference_time_s":10.0,"freshness_s":300.0,
        "state_envelope":{"lower":[0.0],"upper":[1.0]},
        "action_envelope":{"lower":[[0.0]],"upper":[[1.0]]},
        "bank_persisted":True,"issuance_contract":"Layer4-FF-vertical-certificate-issuance-v1",
        "physical_verdict_hash":"pv7",
        "physical_issuance_evidence":{
            "verification_origin":"FF_CURRENT","physical_actions_consumed":True,
            "physical_runtime_safe":True,"physical_mission_safe":True,
            "verdict_authoritative":True,"verdict_non_bypassable":True,
            "all_rows_vtol_feasible":True,"verdict_sources":["WingMPET/op27-substepped"],
        },
    }

def _l4_result():
    return {"verified_candidates":[{"candidate_id":"c"}],"candidates":[{"feasible":True}],
            "snapshot_version":"v","snapshot_hash":"h",
            "provenance":{"model_hash":"m7","parameter_hash":"p7"}}

def _l5_result(final=True):
    return {"gates":{"FINAL":bool(final)},"decision":{"decision_id":"d"}}

def test_all_three_vertical_phases_require_physical_certificate_contract(tmp_path:Path):
    bank=tmp_path/'certs.jsonl'
    bank.write_text("\n".join(json.dumps(_cert(p)) for p in PHASES)+"\n")
    regp=tmp_path/'cap.json.gz'; reg=LiveCapabilityRegistry(regp); reg.reset(run_id='r',episode_id='e')
    for p in PHASES:
        # L4/L5 evidence exists, but no realized control effect => no joint proof.
        reg.record_cycle(phase=p,cycle_id=1,reference_time_s=10.0,
                         layer4_result=_l4_result(),layer5_result=_l5_result(),
                         control_effect_applied=False,
                         context={"effective_climate_fingerprint":"c7"})
    cap=build_capability_manifest(tmp_path,layer6_forward_only=False,
                                  certificate_bank_path=bank,require_certificate_guard=True,
                                  capability_registry_path=regp,require_live_registry=True)
    for p in PHASES:
        rec=cap.phases[p]
        assert rec['certificate_guard']['certificate_contract_ready'] is True
        assert rec['certificate_guard']['reusable_certificate_count']==1
        assert rec['capability'] is False
        assert 'no_same_cycle_joint_control_proof_for_phase' in rec['blockers']

def test_joint_proof_plus_vertical_certificate_can_make_each_vertical_phase_capable(tmp_path:Path):
    bank=tmp_path/'certs.jsonl'
    bank.write_text("\n".join(json.dumps(_cert(p)) for p in PHASES)+"\n")
    regp=tmp_path/'cap.json.gz'; reg=LiveCapabilityRegistry(regp); reg.reset(run_id='r',episode_id='e')
    for p in PHASES:
        reg.record_cycle(phase=p,cycle_id=1,reference_time_s=10.0,
                         layer4_result=_l4_result(),layer5_result=_l5_result(),
                         control_effect_applied=True,
                         context={"effective_climate_fingerprint":"c7"})
    cap=build_capability_manifest(tmp_path,layer6_forward_only=False,
                                  certificate_bank_path=bank,require_certificate_guard=True,
                                  capability_registry_path=regp,require_live_registry=True)
    for p in PHASES:
        rec=cap.phases[p]
        assert rec['certificate_guard']['certificate_contract_ready'] is True
        assert rec['same_cycle_joint_control_proof'] is True
        assert rec['capability'] is True
