"""Optional learned guidance cannot become startup or execution authority."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.knowledge_assessment import offline_validation_view
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from tddt_tdoc.knowledge_startup import optional_policy_admission
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.orchestrator import TDOCOrchestrator
from tddt_tdoc.storage import read_json_auto,write_json_gz

TDOC=Path(__file__).resolve().parents[1]


def forbidden(*args,**kwargs):
    raise AssertionError('No implicit policy compilation, flight or mutable knowledge operation')


@pytest.fixture
def context(tmp_path):
    root=tmp_path/'TDOC'
    root.mkdir()
    runtime=root/'runtime'
    h=OfflineKnowledgeHandoff(runtime)
    forcing=tmp_path/'forcing.csv';forcing.write_text('time,wind\n2026-10-29T00:00:00,1\n')
    quality={'control_knowledge_valid':False,'joint_control_proof_cycles':0,
             'phase_control_coverage':{'all_required_control_phases':False,'missing_control_phases':['hover']}}
    setd={'ok':True,'manifest':{'committed':True,'package_id':'P-R203','knowledge_quality':quality}}
    record=h.record_finalized(ledger_record={'transaction_id':'unit-R203'},finalization={'ok':True,'committed':True},
        setd=setd,kstore_fingerprint={},source_version='unit',layer5_root='/fixture',
        climate_time_alignment={'forcing_file':str(forcing),'forcing_file_sha256':hashlib.sha256(forcing.read_bytes()).hexdigest(),
                                'effective_climate_epoch':'2026-10-29T00:00:00'})
    cfg=TDOCConfig(root/'configs/tdoc.yaml',root,{'runtime':{'root':'runtime'}})
    compiler=KnowledgePolicyCompiler(cfg)
    compiler.compile=forbidden
    orch=SimpleNamespace(offline_handoff=h,knowledge_policy=compiler,config=cfg,
        adapters={5:SimpleNamespace(load_setd=lambda:deepcopy(setd)),6:SimpleNamespace()})
    orch.ensure_online_knowledge_policy=lambda **kw:TDOCOrchestrator.ensure_online_knowledge_policy(orch,**kw)
    return SimpleNamespace(root=root,runtime=runtime,h=h,record=record,setd=setd,cfg=cfg,
                           compiler=compiler,orch=orch,forcing=forcing,tmp=tmp_path)


def no_guide(reason='knowledge_policy_artifact_missing'):
    return KnowledgePolicyCompiler._no_guide(reason)


@pytest.mark.parametrize('reason',['knowledge_policy_artifact_missing','no_joint_closed_loop_policy_rows','phase_policy_support_missing'])
def test_benign_absence_is_startup_ready_without_guide_or_authority(reason):
    original=no_guide(reason);copy=deepcopy(original)
    result=optional_policy_admission(original)
    assert result['ok'] and result['status']=='READY' and not result['policy_available']
    assert result['artifact_hash'] is None and not result['actuation_authority'] and not result['flight_ready']
    assert result['policy_diagnostics']['status']=='UNAVAILABLE'
    assert original==copy


@pytest.mark.parametrize('status',['REJECTED','FAILED','NOT_READY'])
def test_bad_or_ambiguous_policy_is_not_benign_absence(status):
    result=optional_policy_admission({**no_guide(), 'status':status})
    assert not result['ok'] and not result['policy_available']


@pytest.mark.parametrize('result',[None,{}, {'ok':True}, {'ok':False,'status':'UNAVAILABLE'},
                                  no_guide('unknown_reason'),no_guide('offline_timeline_missing'),
                                  {**no_guide(),'artifact':{}},{**no_guide(),'phase_policy':{'command':[0,0,0,0]}}])
def test_unknown_results_do_not_silently_admit_startup(result):
    assert not optional_policy_admission(result)['ok']


def test_available_policy_is_only_guidance_not_payload_or_flight_permission():
    loaded=KnowledgePolicyCompiler._guide_available(ok=True,status='READY',artifact_hash='hash',
        artifact_path='/fixture/policy',artifact={'large':'payload'})
    result=optional_policy_admission(loaded)
    assert result['ok'] and result['policy_available']
    assert not result['actuation_authority'] and 'artifact' not in result['policy_diagnostics']


def test_online_policy_check_is_read_only_and_does_not_compile_missing_guide(context):
    before=context.h.latest_path.read_bytes()
    result=context.orch.ensure_online_knowledge_policy()
    assert result['ok'] and result['reason']=='OPTIONAL_POLICY_UNAVAILABLE'
    assert not result['compiled'] and not result['reused'] and not result['policy_available']
    assert not context.compiler.path.exists() and context.h.latest_path.read_bytes()==before


def test_corrupt_policy_still_blocks_online_start_without_repair(context):
    context.compiler.path.parent.mkdir(parents=True)
    context.compiler.path.write_bytes(b'broken prior artifact')
    result=context.orch.ensure_online_knowledge_policy()
    assert not result['ok'] and result['policy_diagnostics']['status']=='FAILED'
    assert context.compiler.path.read_bytes()==b'broken prior artifact'


def test_corrupt_handoff_blocks_before_optional_policy_lookup(context):
    data=read_json_auto(context.h.latest_path);data['source_version']='tampered'
    write_json_gz(context.h.latest_path,data)
    context.compiler.load=forbidden
    result=context.orch.ensure_online_knowledge_policy()
    assert not result['ok'] and result['reason']=='offline_handoff_not_ready'


def finalize_fixture(context,policy):
    record=deepcopy(context.record)
    setd=deepcopy(context.setd)
    fake=SimpleNamespace(knowledge=SimpleNamespace(latest=lambda mode:{'transaction_id':'unit'}),
        adapters={5:SimpleNamespace(finalize_offline_episode=lambda knowledge:{'ok':True,'committed':True},
                  load_setd=lambda:setd,kstore_fingerprint=lambda:{})},
        offline_handoff=SimpleNamespace(record_finalized=lambda **kwargs:deepcopy(record),latest_path=context.h.latest_path),
        knowledge_policy=SimpleNamespace(compile=lambda **kwargs:deepcopy(policy)),
        versions=SimpleNamespace(active='unit'),config=context.cfg)
    return TDOCOrchestrator.finalize_offline_knowledge(fake)


def test_finalization_succeeds_without_policy_but_does_not_promote_control(context):
    result=finalize_fixture(context,no_guide('no_joint_closed_loop_policy_rows'))
    assert result['ok'] and result['committed'] and not result['policy_available']
    assert not result['handoff']['control_knowledge_valid'] and not result['actuation_authority']
    validation=offline_validation_view(result,{'OFFLINE_CONTROL_READY':True})
    assert validation['OFFLINE_KNOWLEDGE_VALID'] and validation['OFFLINE_PIPELINE_READY']
    assert not validation['OFFLINE_POLICY_AVAILABLE'] and not validation['OFFLINE_CONTROL_READY']


def test_rejected_policy_cannot_mark_finalization_ready(context):
    result=finalize_fixture(context,KnowledgePolicyCompiler._no_guide('hash_mismatch',status='REJECTED'))
    assert not result['ok'] and result['knowledge_policy_admission']['reason']=='OPTIONAL_POLICY_REJECTED'


@pytest.mark.parametrize('upstream,control,coverage,expected',[
    (True,True,True,True),(False,True,True,False),
    (True,False,True,False),(True,True,False,False)])
def test_pipeline_readiness_preserves_explicit_control_proof(upstream,control,coverage,expected):
    final={'ok':True,'handoff':{'control_knowledge_valid':control,
                               'offline_control_coverage_complete':coverage}}
    assert offline_validation_view(final,{'OFFLINE_CONTROL_READY':upstream})['OFFLINE_CONTROL_READY'] is expected


def run_start_branch(context, *, setd_override=None):
    script=TDOC/'scripts/run_training_workflows.py'
    tree=ast.parse(script.read_text())
    branch=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and
        any(isinstance(c,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='online_start_gate' for t in c.targets)
            and 'validate_online_start' in ast.unparse(c.value) for c in n.body))
    fn=ast.FunctionDef(name='startup',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),
                       body=[ast.Assign(targets=[ast.Name(id='rc',ctx=ast.Store())],value=ast.Constant(value=0)),
                             branch,ast.Return(value=ast.Tuple(elts=[ast.Name(id='rc',ctx=ast.Load()),ast.Name(id='online_start_gate',ctx=ast.Load())],ctx=ast.Load()))],decorator_list=[])
    if setd_override is not None:
        context.orch.adapters[5].load_setd=lambda:deepcopy(setd_override)
    saved,blocked=[],[]
    ns={'need_online':True,'rc':0,'args':SimpleNamespace(online_training=True,offline_knowledge_manifest=None,
         online_episodes=0,online_cycles=1,online_max_cycles_per_episode=1),
        'cfg':context.cfg,'orch':context.orch,'report_root':context.root/'reports',
        'Path':Path,'json':json,'overall':{'modes':{}},'offline_finalize':None,
        '_write_workflow_summary':lambda value,*a,**kw:saved.append(deepcopy(value)),
        'write_online_not_ready':lambda *a,**kw:blocked.append((a,kw)) or {}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'<actual-start-gate>','exec'),ns)
    rc,gate=ns['startup']()
    return rc,gate,saved,blocked


def test_actual_workflow_start_branch_admits_sound_prior_without_policy(context):
    rc,gate,saved,blocked=run_start_branch(context)
    assert rc==0 and gate['ok'] and not gate['policy_available']
    assert not gate['policy_required_for_startup'] and not gate['actuation_authority']
    assert not gate['offline_control_coverage_complete'] and not blocked
    assert saved[-1]['online_start_knowledge_gate']['knowledge_policy_gate']['policy_diagnostics']['status']=='UNAVAILABLE'


def test_workflow_still_rejects_loaded_setd_identity_mismatch(context):
    wrong=deepcopy(context.setd);wrong['manifest']['package_id']='wrong'
    context.orch.ensure_online_knowledge_policy=forbidden
    rc,gate,saved,blocked=run_start_branch(context,setd_override=wrong)
    assert rc==2 and not gate['ok'] and gate['reason']=='VALID_FINALIZED_SETD_INTEGRITY_REQUIRED'
    assert 'setd:setd_identity_match' in gate['blockers']


def test_workflow_rejected_optional_cache_is_reported_not_ignored(context):
    context.compiler.path.parent.mkdir(parents=True);context.compiler.path.write_bytes(b'bad')
    rc,gate,saved,blocked=run_start_branch(context)
    assert rc==2 and gate['reason']=='KNOWLEDGE_POLICY_REJECTED'
    assert 'knowledge_policy:knowledge_policy_artifact_unreadable' in gate['blockers']


def binding_config(context,monkeypatch):
    import tddt_tdoc.native_environment as environment
    region=context.root/'region/region_v1.geojson';region.parent.mkdir()
    region.write_text(json.dumps({'features':[{'properties':{'feature_role':role},
        'geometry':{'coordinates':coord}} for role,coord in [('start',[-105.,40.]),('destination',[-104.9,40.1])]]}))
    native=context.tmp/'native'
    cfg=SimpleNamespace(package_root=context.root,runtime_root=context.runtime,
        get=lambda key,default=None:default,
        path_value=lambda key,required=False:native if key=='TDDT_LAYER6_ROOT' else None)
    monkeypatch.setattr(environment,'load_mission_prior',lambda *a:{'endpoint_ground_msl_m':{'origin':1800.},'mission_heading_rad':0.,'map_crs':'EPSG:32613'})
    return cfg,native


def test_environment_binding_does_not_read_or_require_policy(context,monkeypatch):
    cfg,native=binding_config(context,monkeypatch)
    monkeypatch.setattr(KnowledgePolicyCompiler,'__init__',forbidden)
    before=context.h.latest_path.read_bytes(),context.forcing.read_bytes()
    binding=prepare_binding(cfg)
    assert binding['forcing_file_sha256']==hashlib.sha256(context.forcing.read_bytes()).hexdigest()
    assert not binding['policy_required_for_binding'] and not binding['actuation_authority']
    assert (native/'runtime/tdoc_environment_binding.json').is_file()
    assert before==(context.h.latest_path.read_bytes(),context.forcing.read_bytes())


def test_forcing_mismatch_still_blocks_preparation_before_binding_write(context,monkeypatch):
    cfg,native=binding_config(context,monkeypatch)
    context.forcing.write_text('wrong')
    with pytest.raises(ValueError,match='forcing identity'):
        prepare_binding(cfg)
    assert not native.exists()


def test_bad_handoff_still_blocks_preparation(context,monkeypatch):
    cfg,native=binding_config(context,monkeypatch)
    data=read_json_auto(context.h.latest_path);data['created_at']='corrupt'
    write_json_gz(context.h.latest_path,data)
    with pytest.raises(ValueError,match='verified Offline handoff'):
        prepare_binding(cfg)
    assert not native.exists()


def test_layer4_prior_admission_is_separate_from_guide_availability():
    text=(TDOC/'src/tddt_tdoc/orchestrator.py').read_text()
    assert 'atomic and l4_prior_admission.get("ok",False)' in text
