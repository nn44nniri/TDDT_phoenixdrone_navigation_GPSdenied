"""R2 acceptance matrix: synthetic faults stay in temporary fixtures.

Uses production startup/clock/handoff/policy/trigger/release gates. L2-L5
adapters in the run_cycle harness are non-actuating contract doubles.
"""
from copy import deepcopy
from datetime import datetime,timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.orchestrator import TDOCOrchestrator
from tddt_tdoc.rtmo.capability_gate import CapabilityManifest
from tddt_tdoc.storage import read_json_auto,write_json_gz
from tddt_tdoc.utils import sha256_obj
from test_r2_03_optional_startup import run_start_branch
from test_r2_04_prediction_trigger import make_context,make_partial_policy,run,assert_no_release,forbidden

TDOC=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('r205_workflow',TDOC/'scripts/run_training_workflows.py')
workflow=importlib.util.module_from_spec(spec);spec.loader.exec_module(workflow)

CASES={
    'prior_missing_guide':'GUIDANCE_ONLY','prior_partial_phase':'GUIDANCE_ONLY','prior_supported_phase':'GUIDANCE_ONLY',
    'handoff_hash':'REJECTED','handoff_unreadable':'REJECTED','handoff_list':'REJECTED','handoff_scalar':'REJECTED',
    'handoff_setd_shape':'REJECTED','handoff_finalization_shape':'REJECTED','handoff_reuse_shape':'REJECTED',
    'handoff_coverage_shape':'REJECTED','handoff_quality_shape':'REJECTED','handoff_missing_phases_shape':'REJECTED',
    'handoff_uncommitted':'REJECTED','handoff_false_setd_ok':'REJECTED',
    'setd_unloadable':'REJECTED','setd_identity':'REJECTED',
    'policy_hash':'REJECTED','policy_schema':'REJECTED','policy_algorithm':'REJECTED',
    'policy_handoff':'REJECTED','policy_timeline':'REJECTED','policy_unreadable':'REJECTED',
    'forcing_hash_inside':'REJECTED','forcing_hash_outside':'REJECTED','forcing_window_inside':'REJECTED',
    'forcing_epoch_outside':'REJECTED','forcing_valid_epoch_reuse':'BOUND_CLOCK',
    'forcing_expired':'REJECTED','binding_forcing_hash':'REJECTED',
    'binding_forcing_fingerprint_missing':'REJECTED','prior_cannot_release':'NO_RELEASE',
}


def rehash(data):
    data['handoff_hash']=sha256_obj({k:v for k,v in data.items() if k not in {'handoff_hash','knowledge_id','knowledge_version','ok'}})
    return data


def evaluate_case(case,tmp_path,monkeypatch):
    c=make_context(tmp_path);c.cfg=c.orch.config
    outcome=None;reason=None;extra={}
    if case.startswith('prior_') and case!='prior_cannot_release':
        if case!='prior_missing_guide':make_partial_policy(c)
        before=c.handoff.latest_path.read_bytes()
        startup=TDOCOrchestrator.ensure_online_knowledge_policy(c.orch)
        result=run(c,'cruise' if case=='prior_supported_phase' else 'hover')
        assert startup['ok'] and result['layer_status']['4']['ready']
        assert_no_release(c,result)
        assert c.handoff.latest_path.read_bytes()==before
        assert not result['provenance']['offline_knowledge_handoff'].get('actuation_authority')
        assert not c.record['control_knowledge_valid'] and not c.record['valid_setd']
        extra={'policy_available':result['layer_status']['4']['evidence']['prior_admission']['policy_available'],
               'l4_invoked':bool(c.adapters[4].calls),'final':False,'actuation_executed':False}
        outcome='GUIDANCE_ONLY';reason=result['layer_status']['4']['evidence']['prior_admission']['reason']
    elif case.startswith('handoff_'):
        m=read_json_auto(c.handoff.latest_path)
        if case=='handoff_hash':m['source_version']='tampered'
        elif case=='handoff_unreadable':c.handoff.latest_path.write_bytes(b'bad gzip')
        elif case=='handoff_list':m=[]
        elif case=='handoff_scalar':m=1
        elif case=='handoff_uncommitted':m['finalization']['committed']=False;rehash(m)
        elif case=='handoff_false_setd_ok':m['setd']['ok']=False;rehash(m)
        elif case=='handoff_missing_phases_shape':m['phase_control_coverage']['missing_control_phases']='hover';rehash(m)
        else:
            field={'handoff_setd_shape':'setd','handoff_finalization_shape':'finalization',
                   'handoff_reuse_shape':'reuse_contract','handoff_coverage_shape':'phase_control_coverage',
                   'handoff_quality_shape':'knowledge_quality'}[case]
            m[field]=['bad'];rehash(m)
        if case!='handoff_unreadable':write_json_gz(c.handoff.latest_path,m)
        before=c.handoff.latest_path.read_bytes()
        c.orch.knowledge_policy.load=forbidden
        gate=c.handoff.validate_online_start({})
        result=TDOCOrchestrator.ensure_online_knowledge_policy(c.orch)
        assert c.handoff.latest_path.read_bytes()==before
        outcome='REJECTED' if not gate['ok'] and not result['ok'] else 'INCORRECTLY_ADMITTED'
        reason=gate.get('handoff_reason') or gate.get('reason')
    elif case.startswith('setd_'):
        changed=deepcopy(c.setd)
        if case=='setd_unloadable':changed['ok']=False
        else:changed['manifest']['package_id']='different-package'
        c.orch.ensure_online_knowledge_policy=forbidden
        rc,gate,_,_=run_start_branch(c,setd_override=changed)
        outcome='REJECTED' if rc==2 and not gate['ok'] else 'INCORRECTLY_ADMITTED'
        reason=gate.get('reason')
    elif case.startswith('policy_'):
        compiler=make_partial_policy(c);m=read_json_auto(compiler.path)
        if case=='policy_unreadable':compiler.path.write_bytes(b'bad gzip')
        elif case=='policy_timeline':compiler.timeline_path.write_text('changed')
        else:
            field={'policy_hash':'accepted_rows','policy_schema':'schema','policy_algorithm':'algorithm_version',
                   'policy_handoff':'handoff_hash'}[case]
            m[field]='tampered'
            if case!='policy_hash':m['artifact_hash']=sha256_obj({k:v for k,v in m.items() if k!='artifact_hash'})
            write_json_gz(compiler.path,m)
        before=compiler.path.read_bytes()
        gate=TDOCOrchestrator.ensure_online_knowledge_policy(c.orch)
        result=run(c,'cruise');assert_no_release(c,result)
        assert compiler.path.read_bytes()==before and not c.adapters[4].calls
        outcome='REJECTED' if not gate['ok'] and not result['layer_status']['4']['ready'] else 'INCORRECTLY_ADMITTED'
        reason=gate['policy_diagnostics']['reason']
    elif case.startswith('forcing_'):
        start=datetime(2026,10,29);end=start+timedelta(hours=23);epoch=start+timedelta(hours=2)
        m=read_json_auto(c.handoff.latest_path)
        contract=m['reuse_contract']['climate_time_alignment']
        contract.update(forcing_start=start.isoformat(),forcing_end=end.isoformat(),effective_climate_epoch=epoch.isoformat())
        if case=='forcing_epoch_outside':contract['effective_climate_epoch']=(end+timedelta(hours=2)).isoformat()
        write_json_gz(c.handoff.latest_path,rehash(m))
        if case.startswith('forcing_hash_'):c.forcing.write_text('different forcing with same time window')
        current=(start+timedelta(hours=1),end+timedelta(hours=1)) if case=='forcing_window_inside' else (start,end)
        configured=start-timedelta(days=1) if case=='forcing_hash_outside' else start+timedelta(hours=1)
        monkeypatch.setattr(workflow,'resolve_climate_forcing',lambda *a,**kw:c.forcing)
        monkeypatch.setattr(workflow,'_layer3_mission_epoch_utc',lambda:configured)
        alignment=workflow._load_online_climate_alignment(c.runtime,c.handoff.latest_path,current,c.root)
        before=c.handoff.latest_path.read_bytes(),c.forcing.read_bytes()
        req={'climate':{},'context':{}}
        try:
            t=workflow._apply_online_climate_time(req,current,24*3600 if case=='forcing_expired' else 90.,alignment)
        except RuntimeError as exc:
            outcome='REJECTED';reason=alignment.get('reason') or str(exc)
            assert req=={'climate':{},'context':{}}
        else:
            if case=='forcing_valid_epoch_reuse':
                outcome='BOUND_CLOCK' if t==epoch+timedelta(seconds=90) else 'INCORRECT_EPOCH'
            else:outcome='INCORRECTLY_ADMITTED'
            reason=req['context'].get('climate_time_alignment_basis')
        assert before==(c.handoff.latest_path.read_bytes(),c.forcing.read_bytes())
        extra={'alignment_ok':alignment['ok'],'configured_inside':case!='forcing_hash_outside',
               'expected_epoch':epoch.isoformat(),'commissioning_invoked':False,'actuation_executed':False}
    elif case.startswith('binding_'):
        if case=='binding_forcing_hash':c.forcing.write_text('changed forcing')
        else:
            m=read_json_auto(c.handoff.latest_path)
            m['reuse_contract']['climate_time_alignment'].pop('forcing_file_sha256')
            write_json_gz(c.handoff.latest_path,rehash(m))
        target=tmp_path/'native_never_created'
        config=SimpleNamespace(package_root=c.root,runtime_root=c.runtime,
            path_value=lambda *a,**kw:target,get=lambda key,default=None:default)
        before=c.handoff.latest_path.read_bytes(),c.forcing.read_bytes()
        try:prepare_binding(config)
        except ValueError as exc:
            outcome='REJECTED';reason=str(exc)
        else:outcome='INCORRECTLY_ADMITTED'
        assert not target.exists() and before==(c.handoff.latest_path.read_bytes(),c.forcing.read_bytes())
        extra={'binding_created':False,'actuation_executed':False}
    elif case=='prior_cannot_release':
        caps=CapabilityManifest(phases={'hover':{'capability':True}},source='offline_fixture_only')
        released=caps.runtime_release('hover',current_runtime_evidence=None)
        assert not released['actuation_release'] and not released['transition_release']
        make_partial_policy(c)
        # A supported policy can supply nonzero prior values but no FINAL field.
        from layer5.phoenix import PhoenixSpecialization
        binding=c.orch.knowledge_policy.phase_policy('cruise')
        cur=[0.,0.,0.,0.,0.,-1.,0.,0.,0.,10.,0.,0.];nxt=list(cur);nxt[0]=1.
        operating={'altitude_agl_m':10.,'attitude_rate_state':{'qw':1.,'qx':0.,'qy':0.,'qz':0.,'q_rate':0.}}
        command,diag=object.__new__(PhoenixSpecialization)._compiled_policy_action({'compiled_online_policy':binding},cur,nxt,'cruise',operating)
        assert command is not None and not diag['actuation_authority'] and 'FINAL' not in diag
        outcome='NO_RELEASE';reason='durable_prior_and_nonzero_guide_do_not_grant_current_runtime_release'
        extra={'nonzero_guide_available':True,'final_in_guide':False,'actuation_release':False}
    else:raise AssertionError(case)
    return {'case':case,'outcome':outcome,'reason':reason,'expected':CASES[case],**extra}


@pytest.mark.parametrize('case',CASES)
def test_prior_acceptance_matrix(case,tmp_path,monkeypatch):
    result=evaluate_case(case,tmp_path,monkeypatch)
    assert result['outcome']==result['expected'],result
