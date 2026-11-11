"""No missing/inapplicable learned guide may become a fabricated command."""
from copy import deepcopy
import csv
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from tddt_tdoc.storage import read_json_auto, write_json_gz
from tddt_tdoc.utils import sha256_obj
from layer5.phoenix import PhoenixSpecialization


def fixture_policy(root, *, joint=True):
    runtime=root/'runtime'
    cfg=SimpleNamespace(runtime_root=runtime,package_root=root,get=lambda key,default=None:default,path_value=lambda key:None)
    compiler=KnowledgePolicyCompiler(cfg)
    handoff=OfflineKnowledgeHandoff(runtime)
    handoff.record_finalized(ledger_record={'transaction_id':'fixture-only'},
        finalization={'ok':True,'committed':True},
        setd={'ok':True,'manifest':{'committed':True,'package_id':'UNIT-ONLY',
            'knowledge_quality':{'control_knowledge_valid':False,'phase_control_coverage':{'all_required_control_phases':False,'missing_control_phases':['hover']}}}},
        kstore_fingerprint={},source_version='unit',layer5_root='/fixture')
    rows=[]
    for i in range(3):
        rows.append({'phase':'cruise','status':'OK','transaction_id':f'fixture-{i}',
            **{k:joint for k in ('l4_candidate_verified','l5_final_gate','l5_feedback_accepted','layer2_control_effect_applied','l5_control_present')},
            'reference_time_s':i,'reference_z_msl_m':100+i,'reference_x_m':0,'reference_y_m':i,
            'vehicle_heading_deg':0,'vehicle_airspeed_m_s':10+i,'vehicle_climb_m_s':1,'agl_m':10,
            'vehicle_pitch_deg':0,'vehicle_pitch_rate_rad_s':0,
            **{f'l5_pilot_cmd_{n}':v for n,v in enumerate([.1,-.05,.55,0])}})
    compiler.timeline_path.parent.mkdir(parents=True)
    with compiler.timeline_path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return compiler


@pytest.fixture
def policy(tmp_path):
    return fixture_policy(tmp_path)


def assert_no_guide(result, status='UNAVAILABLE'):
    assert result['ok'] is False and result['status']==status
    assert result['availability']=='UNAVAILABLE' and result['available'] is False
    assert result['artifact'] is None and result['phase_policy'] is None
    assert not result['actuation_authority']
    assert all(key not in result for key in ('u_cmd','command','reference','feedforward_median','trajectory'))


def test_missing_policy_is_unavailable_and_does_not_create_zero_artifact(policy):
    before=set(policy.config.package_root.rglob('*'))
    result=policy.load(handoff_hash='expected')
    assert_no_guide(result)
    assert result['reason']=='knowledge_policy_artifact_missing'
    assert set(policy.config.package_root.rglob('*'))==before


def test_zero_joint_rows_cannot_create_policy_or_delete_previous_bytes(tmp_path):
    compiler=fixture_policy(tmp_path,joint=False)
    result=compiler.compile()
    assert_no_guide(result)
    assert result['reason']=='no_joint_closed_loop_policy_rows' and result['accepted_rows']==0
    assert not compiler.path.exists()
    compiler.path.parent.mkdir(parents=True)
    compiler.path.write_bytes(b'previous evidence, never repaired')
    again=compiler.compile()
    assert_no_guide(again)
    assert compiler.path.read_bytes()==b'previous evidence, never repaired'


def test_available_valid_policy_stays_read_only_prior_and_missing_phase_is_none(policy):
    compiled=policy.compile()
    assert compiled['ok'] and compiled['availability']=='AVAILABLE'
    before=policy.path.read_bytes()
    loaded=policy.load()
    assert loaded['ok'] and loaded['available'] and not loaded['actuation_authority']
    phase=policy.phase_policy('cruise')
    assert phase['ok'] and phase['phase_policy']['support_count']==3
    assert_no_guide(policy.phase_policy('hover'))
    assert policy.path.read_bytes()==before


@pytest.mark.parametrize('change,reason',[
    ('hash','artifact_hash_mismatch'),('schema','schema_mismatch'),('algorithm','compiler_algorithm_version_mismatch'),
    ('handoff','handoff_hash_mismatch'),('timeline','source_timeline_hash_mismatch'),('authority','policy_authority_contract_invalid')])
def test_incompatible_or_corrupt_policy_is_rejected_instead_of_benign_absence(policy,change,reason):
    policy.compile();artifact=read_json_auto(policy.path)
    expected=artifact['handoff_hash']
    if change=='hash':artifact['accepted_rows']=999
    elif change=='schema':artifact['schema']='wrong'
    elif change=='algorithm':artifact['algorithm_version']='wrong'
    elif change=='authority':artifact['policy_contract']['actuation_authority']=True
    elif change=='timeline':policy.timeline_path.write_text('changed')
    elif change=='handoff':expected='different'
    if change!='hash':
        artifact['artifact_hash']=sha256_obj({k:v for k,v in artifact.items() if k!='artifact_hash'})
    write_json_gz(policy.path,artifact)
    before=policy.path.read_bytes()
    result=policy.load(handoff_hash=expected)
    assert_no_guide(result,'REJECTED')
    assert reason in result['reasons']
    assert policy.path.read_bytes()==before


def test_malformed_artifact_is_explicit_without_zero_placeholder(policy):
    policy.path.parent.mkdir(parents=True)
    policy.path.write_bytes(b'not gzip')
    assert_no_guide(policy.load(),'FAILED')
    write_json_gz(policy.path,[])
    assert_no_guide(policy.load(),'REJECTED')


def test_empty_support_never_yields_eligible_phase(policy):
    policy.compile();artifact=read_json_auto(policy.path)
    artifact['phases']['cruise']['prototypes']=[]
    artifact['artifact_hash']=sha256_obj({k:v for k,v in artifact.items() if k!='artifact_hash'})
    write_json_gz(policy.path,artifact)
    assert_no_guide(policy.phase_policy('cruise'))


def test_phase_label_mismatch_is_not_aliased_to_current_phase(policy):
    policy.compile();artifact=read_json_auto(policy.path)
    artifact['phases']['cruise']['phase']='hover'
    artifact['artifact_hash']=sha256_obj({k:v for k,v in artifact.items() if k!='artifact_hash'})
    write_json_gz(policy.path,artifact)
    result=policy.phase_policy('cruise')
    assert_no_guide(result)
    assert result['reason']=='phase_policy_mismatch'


def test_corrupt_handoff_precedes_missing_timeline(policy):
    data=read_json_auto(policy.handoff_path);data['source_transaction_id']='tampered'
    write_json_gz(policy.handoff_path,data)
    policy.timeline_path.unlink()
    result=policy.compile()
    assert_no_guide(result,'REJECTED')
    assert result['reason']=='offline_handoff_hash_mismatch'
    assert not policy.path.exists()


@pytest.mark.parametrize('field,value',[('source_learning_mode','ONLINE_JSBSIM'),('setd_integrity_valid',False)])
def test_hash_valid_but_invalid_source_handoff_cannot_compile_guide(policy,field,value):
    data=read_json_auto(policy.handoff_path);data[field]=value
    data['handoff_hash']=sha256_obj({k:v for k,v in data.items() if k not in {'handoff_hash','knowledge_id','knowledge_version','ok'}})
    write_json_gz(policy.handoff_path,data)
    assert_no_guide(policy.compile(),'REJECTED')
    assert not policy.path.exists()


def states():
    cur=[0.,0.,0.,0.,0.,-1.,0.,0.,0.,10.,0.,0.]
    nxt=list(cur);nxt[0]=1.
    context={'altitude_agl_m':10.,'attitude_rate_state':{'qw':1.,'qx':0.,'qy':0.,'qz':0.,'q_rate':0.}}
    return cur,nxt,context


def evaluate(candidate, *, cur=None, context=None, use_context=True):
    initial,nxt,default=states()
    specialization=object.__new__(PhoenixSpecialization)
    return specialization._compiled_policy_action(candidate,initial if cur is None else cur,nxt,'cruise',
        default if context is None and use_context else context)


def test_layer5_missing_guide_returns_none_not_neutral_or_trim():
    before={}
    command,diag=evaluate(before)
    assert command is None and before=={}
    assert diag['status']=='UNAVAILABLE' and not diag['used'] and not diag['actuation_authority']


def test_layer5_does_not_consume_data_hidden_under_unavailable_status(policy):
    policy.compile();binding=policy.phase_policy('cruise')
    binding.update(ok=False,available=False,availability='UNAVAILABLE',status='UNAVAILABLE')
    command,diag=evaluate({'compiled_online_policy':binding})
    assert command is None and diag['status']=='UNAVAILABLE'


@pytest.mark.parametrize('status',['FAILED','REJECTED'])
def test_layer5_does_not_downgrade_corruption_to_benign_absence(status):
    result={'ok':False,'status':status,'availability':'UNAVAILABLE','reason':'source_integrity_problem'}
    command,diag=evaluate({'compiled_online_policy':result})
    assert command is None and diag['status']==status and diag['reason']=='source_integrity_problem'


def test_supported_current_state_gets_only_prior_without_authority(policy):
    policy.compile();candidate={'compiled_online_policy':policy.phase_policy('cruise')}
    original=deepcopy(candidate)
    command,diag=evaluate(candidate)
    assert command==pytest.approx([.1,-.05,.55,0.])
    assert diag['availability']=='AVAILABLE' and diag['used'] and not diag['actuation_authority']
    assert candidate==original and 'FINAL' not in diag


def test_outside_support_does_not_replay_nearest_sample(policy):
    policy.compile();candidate={'compiled_online_policy':policy.phase_policy('cruise')}
    cur,_,_=states();cur[9]=100.
    command,diag=evaluate(candidate,cur=cur)
    assert command is None and diag['reason']=='outside_compiled_policy_support'
    assert diag['status']=='UNAVAILABLE' and diag['nearest_distance']>diag['support_radius']


def test_wrong_phase_is_unavailable_without_alias_reference(policy):
    policy.compile();binding=policy.phase_policy('cruise');binding['phase_policy']['phase']='hover'
    command,diag=evaluate({'compiled_online_policy':binding})
    assert command is None and diag['status']=='UNAVAILABLE' and diag['reason']=='compiled_policy_phase_mismatch'


def test_no_feature_support_cannot_replay_unconditional_first_prototype(policy):
    policy.compile();binding=policy.phase_policy('cruise');binding['phase_policy']['feature_order']=[]
    command,diag=evaluate({'compiled_online_policy':binding})
    assert command is None and diag['reason']=='compiled_policy_feature_support_empty'


@pytest.mark.parametrize('kind',['absent','nan','invalid_type'])
def test_missing_or_nonfinite_current_features_are_not_imputed(policy,kind):
    policy.compile();candidate={'compiled_online_policy':policy.phase_policy('cruise')}
    _,_,context=states()
    if kind=='absent':context.pop('attitude_rate_state')
    elif kind=='nan':context['altitude_agl_m']=float('nan')
    else:context=[]
    command,diag=evaluate(candidate,context=context)
    assert command is None and diag['status']=='UNAVAILABLE'


@pytest.mark.parametrize('kind',['command','feature','scale','radius','quantiles','quantile_domain','authority'])
def test_invalid_learned_payload_cannot_be_clipped_into_usable_command(policy,kind):
    policy.compile();binding=policy.phase_policy('cruise');item=binding['phase_policy']
    if kind=='command':item['prototypes'][0]['pilot_command']=[0,0,99,0]
    elif kind=='feature':item['prototypes'][0]['features']['airspeed_m_s']=float('nan')
    elif kind=='scale':item['feature_stats']['airspeed_m_s']['scale']=0
    elif kind=='radius':item['support_radius_normalized']=float('inf')
    elif kind=='quantiles':item['command_q05']=[0]
    elif kind=='quantile_domain':item['command_q95']=[2,2,2,2]
    else:binding['policy_contract']['actuation_authority']=True
    command,diag=evaluate({'compiled_online_policy':binding})
    assert command is None and diag['status']=='REJECTED' and not diag['actuation_authority']


def test_policy_availability_is_separate_from_layer4_and_startup_admission():
    root=Path(__file__).resolve().parents[1]
    orchestrator=(root/'src/tddt_tdoc/orchestrator.py').read_text()
    assert 'atomic and l4_prior_admission.get("ok",False)' in orchestrator
    workflow=(root/'scripts/run_training_workflows.py').read_text()
    assert 'VERSIONED_KNOWLEDGE_POLICY_REQUIRED' not in workflow
    assert "online_start_gate['reason']='KNOWLEDGE_POLICY_REJECTED'" in workflow
