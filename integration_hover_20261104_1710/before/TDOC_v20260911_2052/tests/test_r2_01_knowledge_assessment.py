"""Identity checks cannot manufacture offline control quality, or vice versa."""
import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tddt_tdoc.knowledge_assessment import (
    CONTROL_REQUIRED_PHASES, assess_offline_knowledge, offline_validation_view,
)
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.storage import read_json_auto, write_json_gz


def manifest(joint=0, *, complete=False):
    counts = {p: {'joint_proof_cycles':1 if complete else 0, 'control_capable':complete} for p in CONTROL_REQUIRED_PHASES}
    pc = {'required_control_phases':list(CONTROL_REQUIRED_PHASES), 'coverage':counts,
          'all_required_control_phases':complete,
          'missing_control_phases':[] if complete else list(CONTROL_REQUIRED_PHASES)}
    return {'setd_integrity_valid':True,'valid_setd':complete,'control_knowledge_valid':complete,
            'offline_control_coverage_complete':complete,
            'setd':{'package_id':'P'},'finalization':{'ok':True,'committed':True},
            'knowledge_quality':{'joint_control_proof_cycles':joint,
                'control_knowledge_valid':complete,'safety_knowledge_valid':True,
                'phase_control_coverage':pc},'phase_control_coverage':pc}


def row(phase, **overrides):
    return {'phase':phase,'l4_candidate_verified':True,'l5_final_gate':True,
            'layer2_control_effect_applied':True,**overrides}


def test_zero_joint_is_not_confused_with_integrity_failure_or_flight_authority():
    source=manifest();before=deepcopy(source)
    result=assess_offline_knowledge(source,handoff_hash_verified=True,handoff_gate_ok=True)
    assert result['integrity']['existing_handoff_gate_ok'] is True
    assert result['quality']['classification']=='NO_JOINT_CONTROL_PROOF'
    assert result['quality']['joint_control_proof_cycles']==0
    assert result['coverage']['assessed_complete'] is False
    assert result['coverage']['missing_control_phases']==list(CONTROL_REQUIRED_PHASES)
    assert source==before and not result['actuation_authority']
    assert not result['online_matching_offline_sample_required']


def test_separate_nonzero_counters_cannot_make_same_cycle_proof():
    source=manifest()
    source['knowledge_quality'].update(verified_candidate_cycles=1,final_authorized_cycles=1,closed_loop_effect_cycles=1)
    rows=[row('hover',l5_final_gate=False,layer2_control_effect_applied=False),
          row('hover',l4_candidate_verified=False,layer2_control_effect_applied=False),
          row('hover',l4_candidate_verified=False,l5_final_gate=False)]
    result=assess_offline_knowledge(source,timeline_rows=rows)
    assert result['quality']['joint_control_proof_cycles']==0


def test_csv_false_is_not_python_truthy_proof_and_phase_labels_are_not_coverage():
    rows=[row(p,l4_candidate_verified='False',l5_final_gate='False',layer2_control_effect_applied='False') for p in CONTROL_REQUIRED_PHASES]
    result=assess_offline_knowledge(manifest(),timeline_rows=rows)
    assert result['quality']['count_source']=='timeline_recomputed'
    assert result['quality']['joint_control_proof_cycles']==0
    assert not result['coverage']['assessed_complete']


def test_complete_proof_stays_offline_evidence_and_does_not_authorize_online():
    rows=[row(p,l4_candidate_verified='True') for p in CONTROL_REQUIRED_PHASES]
    result=assess_offline_knowledge(manifest(9,complete=True),timeline_rows=rows)
    assert result['quality']['classification']=='COMPLETE_OFFLINE_CONTROL_PROOF_REPORTED'
    assert result['coverage']['assessed_complete'] is True
    assert not result['actuation_authority']


def test_partial_proof_is_not_upgraded_by_seen_phases():
    source=manifest(1)
    source['phase_control_coverage']['coverage']['hover']['joint_proof_cycles']=1
    result=assess_offline_knowledge(source)
    assert result['quality']['classification']=='PARTIAL_OFFLINE_CONTROL_PROOF'
    assert 'hover' not in result['coverage']['missing_control_phases']
    assert result['legacy_flags']['control_knowledge_valid'] is False


def test_absent_counters_remain_unknown_instead_of_zero():
    result=assess_offline_knowledge({})
    assert result['quality']['joint_control_proof_cycles'] is None
    assert result['quality']['classification']=='CONTROL_EVIDENCE_UNAVAILABLE'
    assert result['coverage']['assessed_complete'] is None
    assert result['coverage']['unknown_control_phases']==list(CONTROL_REQUIRED_PHASES)


@pytest.mark.parametrize('invalid',[-1,True,1.2,'0'])
def test_invalid_joint_count_is_not_silently_coerced(invalid):
    source=manifest(invalid)
    result=assess_offline_knowledge(source)
    assert result['quality']['joint_control_proof_cycles'] is None
    assert 'invalid_joint_control_proof_cycles' in result['consistency_issues']


def test_complete_and_control_valid_claim_with_zero_proof_is_explicitly_inconsistent():
    source=manifest(0,complete=True)
    result=assess_offline_knowledge(source)
    assert result['quality']['classification']=='INCONSISTENT_CONTROL_EVIDENCE'
    assert 'control_valid_claim_with_zero_joint_proof' in result['consistency_issues']
    assert result['legacy_flags']['control_knowledge_valid'] is True  # source claim is preserved, not repaired
    assert not result['actuation_authority']


def test_timeline_mismatch_is_a_reported_problem_not_a_manifest_rewrite():
    source=manifest();before=deepcopy(source)
    result=assess_offline_knowledge(source,timeline_rows=[row('hover')])
    assert result['quality']['joint_control_proof_cycles']==1
    assert 'declared_joint_count_differs_from_timeline' in result['consistency_issues']
    assert source==before


def record(tmp_path):
    h=OfflineKnowledgeHandoff(tmp_path)
    source=manifest()
    result=h.record_finalized(ledger_record={'transaction_id':'off-zero'},
        finalization={'ok':True,'committed':True},
        setd={'ok':True,'manifest':{'committed':True,'package_id':'P','knowledge_quality':source['knowledge_quality']}},
        kstore_fingerprint={},source_version='test',layer5_root='/fixture')
    return h,result


def test_new_projection_stays_outside_signed_manifest_and_readers_do_not_write(tmp_path):
    h,result=record(tmp_path)
    assert 'knowledge_assessment' in result
    stored=read_json_auto(h.latest_path)
    assert 'knowledge_assessment' not in stored
    before=h.latest_path.read_bytes()
    resolved=h.resolve()
    gate=h.validate_online_start()
    assert resolved['ok'] and gate['ok']
    assert resolved['knowledge_assessment']['quality']['joint_control_proof_cycles']==0
    assert not gate['offline_control_coverage_complete']
    assert h.latest_path.read_bytes()==before


def test_hash_failure_is_still_rejected_and_claims_are_marked_unverified(tmp_path):
    h,result=record(tmp_path)
    stored=read_json_auto(h.latest_path);stored['knowledge_quality']['joint_control_proof_cycles']=999
    write_json_gz(h.latest_path,stored)
    gate=h.validate_online_start()
    assert not gate['ok'] and gate['reason']=='offline_handoff_hash_mismatch'
    assert gate['knowledge_assessment']['claims_trust']=='unverified_or_not_checked'
    assert not gate['knowledge_assessment']['actuation_authority']


def test_control_claim_cannot_override_false_integrity(tmp_path):
    h=OfflineKnowledgeHandoff(tmp_path)
    source=manifest(9,complete=True)
    result=h.record_finalized(ledger_record={'transaction_id':'bad-integrity'},
        finalization={'ok':False,'committed':True},
        setd={'ok':True,'manifest':{'committed':True,'package_id':'P','knowledge_quality':source['knowledge_quality']}},
        kstore_fingerprint={},source_version='test',layer5_root='/fixture')
    gate=h.validate_online_start()
    assert not result['setd_integrity_valid'] and not gate['ok']
    assert not gate['knowledge_assessment']['integrity']['existing_handoff_gate_ok']


def test_policy_failure_does_not_relabel_sound_setd_as_corrupt():
    final={'ok':False,'committed':True,'setd':{'ok':True},
           'knowledge_policy_compilation':{'ok':False,'reason':'no_joint_closed_loop_policy_rows'},
           'handoff':{'setd_integrity_valid':True,'knowledge_assessment':assess_offline_knowledge(manifest())}}
    original=deepcopy(final)
    result=offline_validation_view(final,{'OFFLINE_CONTROL_READY':True,'OFFLINE_MISSION_VALID':True})
    assert result['OFFLINE_KNOWLEDGE_VALID'] is True
    assert result['OFFLINE_PIPELINE_READY'] is False and result['OFFLINE_POLICY_AVAILABLE'] is False
    assert result['OFFLINE_CONTROL_READY'] is False  # original conjunction preserved
    assert result['OFFLINE_MISSION_VALID'] is True
    assert final==original


def test_missing_handoff_cannot_become_valid_from_pipeline_ok():
    result=offline_validation_view({'ok':True}, {'OFFLINE_CONTROL_READY':False})
    assert not result['OFFLINE_KNOWLEDGE_VALID']
    assert result['OFFLINE_PIPELINE_READY'] is True


def test_historical_finalization_gets_quality_projection_without_false_hash_verification():
    final={'ok':False,'committed':True,'setd':{'ok':True},'handoff':manifest()}
    result=offline_validation_view(final,{'OFFLINE_CONTROL_READY':False})
    assert result['OFFLINE_KNOWLEDGE_VALID'] is True
    assert result['knowledge_assessment']['quality']['joint_control_proof_cycles']==0
    assert result['knowledge_assessment']['integrity']['handoff_hash_verified'] is None
    assert result['knowledge_assessment']['claims_trust']=='unverified_or_not_checked'


def test_workflow_consumes_separate_assessment_in_offline_validation():
    p=Path(__file__).resolve().parents[1]/'scripts/run_training_workflows.py'
    tree=ast.parse(p.read_text())
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='offline_validation_view']
    assert len(calls)==1 and [ast.unparse(n) for n in calls[0].args]==['offline_finalize','oval']
    phase_constant=next(n.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CONTROL_REQUIRED_PHASES' for t in n.targets))
    assert tuple(ast.literal_eval(phase_constant))==CONTROL_REQUIRED_PHASES
