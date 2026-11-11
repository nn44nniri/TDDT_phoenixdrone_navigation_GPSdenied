import json
from layer4 import Layer4Engine
from layer4.verification import FullFidelityVerifier

VALID={"NOT_ATTEMPTED","ATTEMPTED_AND_REJECTED","ATTEMPTED_AND_VERIFIED","EXCEPTION"}


def test_p0_8_outcome_schema_has_exact_auditable_state_and_reasons():
    row=FullFidelityVerifier.outcome("NOT_ATTEMPTED","budget_not_selected",reason_codes=["budget_not_selected"])
    assert row["schema"]=="Layer4-FF-verification-outcome-v1"
    assert row["outcome_status"] in VALID
    assert row["reason_codes"]==["budget_not_selected"]
    assert row["verification_reasons"]==row["reason_codes"]
    assert row["verified"] is False and row["attempted"] is False


def test_p0_8_every_candidate_has_nonempty_verification_outcome(root,cfg,tmp_path):
    cfg.data.setdefault("verification",{})["enabled"]=False
    cfg.data["registry"]["path"]=str(tmp_path/"candidates.jsonl")
    cfg.data["registry"]["provenance_path"]=str(tmp_path/"prov.jsonl")
    data=json.loads((root/"examples/layer3_snapshot_example.json").read_text())
    result=Layer4Engine(cfg).run(data)
    assert result.candidates
    for c in result.candidates:
        v=c["verification"]
        assert isinstance(v,dict) and v
        assert v["schema"]=="Layer4-FF-verification-outcome-v1"
        assert v["outcome_status"] in VALID
        assert v["reason_codes"]
        assert c["verified"] is False
    assert result.conformance["C_FF_audit_complete"] is True


def test_p0_8_exception_has_stable_error_id():
    err="RuntimeError('boom')"
    a=FullFidelityVerifier._error_id("C0001","full_fidelity_runtime_failed",err)
    b=FullFidelityVerifier._error_id("C0001","full_fidelity_runtime_failed",err)
    assert a==b and a.startswith("fferr-") and len(a)>10
