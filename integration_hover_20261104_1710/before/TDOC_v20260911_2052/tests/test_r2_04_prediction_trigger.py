"""Exercise the production run_cycle trigger with isolated, non-actuating adapters.

L2/L3/L4 are contract doubles, not physical-flight evidence. InvocationGate,
_invoke deadlines, handoff, policy validation and RTMO artifact gate are real.
"""
from copy import deepcopy
import csv
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.invocation import InvocationGate
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from tddt_tdoc.network_state import NetworkStateMirror
from tddt_tdoc.orchestrator import TDOCOrchestrator
from tddt_tdoc.rtmo.artifact_gate import RTMOL4ArtifactGate
from tddt_tdoc.storage import read_json_auto, write_json_gz
from tddt_tdoc.twin_registry import TwinRegistry
from tddt_tdoc.utils import sha256_obj


def forbidden(*args,**kwargs):
    raise AssertionError('No actuation, native session, policy compilation or L7 execution')


class Adapter:
    def __init__(self, invoke, *, ready=True):
        self.callback=invoke;self.ready=ready;self.calls=[]
    def preflight(self):
        return {'ok':self.ready,'online_ready':False,'external_only':True,
                'registration_descriptor':{'trigger_owner':'TDOC','direct_actuation':False}}
    def invoke(self,payload):
        self.calls.append(deepcopy(payload));return self.callback(payload)


def make_context(tmp_path, *, fast=False):
    root=tmp_path/'TDOC';root.mkdir();runtime=root/'runtime'
    config=TDOCConfig(root/'configs/tdoc.yaml',root,{
        'runtime':{'root':'runtime'},'deadlines':{**{f'layer{i}_s':10. for i in range(2,8)},'layer4_pre_release_s':10.},
        'layers':{**{f'layer{i}':{'enabled':True} for i in range(2,8)},
                  'layer6':{'enabled':True,'online':{'transport':'Layer6Client_GUI_MAVLink'}}},
        'tdoc':{'rtmo':{'enabled':True,'l4_artifacts':{'fast_path_reuse':fast}}}})
    handoff=OfflineKnowledgeHandoff(runtime)
    quality={'control_knowledge_valid':False,'joint_control_proof_cycles':0,
             'phase_control_coverage':{'all_required_control_phases':False,'missing_control_phases':['hover']}}
    setd={'ok':True,'manifest':{'committed':True,'package_id':'UNIT-R204','knowledge_quality':quality}}
    forcing=tmp_path/'forcing.csv';forcing.write_text('time,wind\n2026-10-29T00:00:00,1\n')
    record=handoff.record_finalized(ledger_record={'transaction_id':'unit-offline-R204'},
        finalization={'ok':True,'committed':True},setd=setd,kstore_fingerprint={},source_version='unit',
        layer5_root='/fixture',climate_time_alignment={'forcing_file':str(forcing),
        'forcing_file_sha256':hashlib.sha256(forcing.read_bytes()).hexdigest(),'effective_climate_epoch':'2026-10-29T00:00:00'})
    state=SimpleNamespace(x=10.,snapshot_kind='atomic_layer3_network_snapshot',empty=False,l3_ok=True)
    def layer2(request):
        return {'ok':True,'direct_layer_ownership':False,'packets':{
            name:{'twin_id':name,'state':{'x':state.x},'uncertainty':{},'timestamp':request['reference_time_s'],
                  'provenance':{},'context':{}} for name in ('MESA_NAV','WingMPET','MicroMetTwin')}}
    def layer3(payload):
        x=payload['packets']['MESA_NAV']['state']['x']
        snapshot={} if state.empty else {'policy_version':'unit-L3','reference_time_s':payload['request']['reference_time_s'],
            'provenance':{'snapshot_kind':state.snapshot_kind},'std':{'representations':{'MESA_NAV':{
                'canonical_state':{'position_ned_m':[x,0.,-50.],'ground_velocity_ned_m_s':[1.,0.,0.]}}}}}
        return {'ok':state.l3_ok,'snapshot':snapshot,'result':{'snapshot':deepcopy(snapshot)}}
    def layer4(payload):
        # Demonstrates routing from the current snapshot; this is no physical model.
        x=payload['snapshot']['std']['representations']['MESA_NAV']['canonical_state']['position_ned_m'][0]
        return {'ok':True,'status':'OK','result':{'snapshot_version':'unit-L3',
            'candidates':[{'candidate_id':'unit-unverified','verified':False,'trajectory':[[x+1.,0.,-50.]]}],
            'verified_candidates':[],'provenance':{'test_double':True,'actuator_command':False}}}
    held=lambda payload:{'ok':True,'status':'HOLD','result':{'gates':{'FINAL':False},'control_action':None,'layer6_package':None}}
    adapters={2:Adapter(layer2),3:Adapter(layer3),4:Adapter(layer4),5:Adapter(held),6:Adapter(forbidden),7:Adapter(forbidden)}
    adapters[5].load_setd=lambda:deepcopy(setd)
    obj=object.__new__(TDOCOrchestrator)
    obj.config=config;obj.gate=InvocationGate();obj.adapters=adapters;obj.versions=SimpleNamespace(active='unit-TDOC')
    obj.offline_handoff=handoff;obj.knowledge_policy=KnowledgePolicyCompiler(config);obj.knowledge_policy.compile=forbidden
    obj.scheduler=SimpleNamespace(boundary=lambda ref:SimpleNamespace(label=lambda:'unit-fast'))
    obj.feedback_router=SimpleNamespace(pending_context=lambda **kwargs:{})
    obj._pending_l7_l2_ack=None;obj._last_current_online_plant_time_s=None;obj._last_post_execution_observation=None
    obj._online_prior_cache=None;obj._offline_model_control_evidence=None
    obj.twin_registry=TwinRegistry(runtime/'twins.json');obj.network_state=NetworkStateMirror(runtime/'network.json')
    obj.provenance=SimpleNamespace(health=lambda:{'ok':True})
    events=[];obj._event=lambda *args,**kwargs:events.append(deepcopy((args,kwargs)))
    obj.rtmo_l4_artifacts=RTMOL4ArtifactGate();obj.rtmo_control_latency=SimpleNamespace(summary=lambda:{})
    obj.online_persistence=SimpleNamespace(submit=lambda value:{'test_only':True})
    obj.layer_archive=SimpleNamespace(record_cycle=lambda value:{'test_only':True})
    obj._online_simulation_state_ingress=lambda **kwargs:{'ok':True,'online_physical_state':{'pn':9999.},
        'ptlib_ingress':{'source':'UNIT_EXECUTION_ONLY','state':{'plant_time_s':1.}}}
    return SimpleNamespace(root=root,runtime=runtime,orch=obj,state=state,adapters=adapters,events=events,
                           handoff=handoff,record=record,setd=setd,forcing=forcing)


@pytest.fixture
def context(tmp_path):
    return make_context(tmp_path)


def request(stage='hover', **values):
    return {'transaction_id':'unit-R204','learning_mode':'ONLINE_JSBSIM','online_training':True,
            'require_offline_knowledge':True,'stage':stage,'reference_time_s':1.,'context':{},**values}


def run(fixture,stage='hover',**values):
    return fixture.orch.run_cycle(request(stage,**values)).to_dict()


def assert_no_release(context,result):
    assert result['provenance']['online_actuation_executed'] is False
    assert not context.adapters[6].calls and not context.adapters[7].calls
    output=result['downstream_layers']['5']['output']
    if output.get('result'):assert output['result']['gates']['FINAL'] is False
    if not result['provenance']['rtmo_pre_release_verify']['enabled']:
        assert result['status']!='OK'


def make_partial_policy(context):
    compiler=context.orch.knowledge_policy
    rows=[{'phase':'cruise','status':'OK','transaction_id':f'unit-{i}',
        **{k:True for k in ('l4_candidate_verified','l5_final_gate','l5_feedback_accepted','layer2_control_effect_applied','l5_control_present')},
        'reference_time_s':i,'reference_z_msl_m':100+i,'reference_x_m':0,'reference_y_m':i,
        'vehicle_heading_deg':0,'vehicle_airspeed_m_s':10+i,'vehicle_climb_m_s':1,'agl_m':10,
        'vehicle_pitch_deg':0,'vehicle_pitch_rate_rad_s':0,
        **{f'l5_pilot_cmd_{n}':v for n,v in enumerate([.1,-.05,.55,0])}} for i in range(3)]
    compiler.timeline_path.parent.mkdir(parents=True,exist_ok=True)
    with compiler.timeline_path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    result=KnowledgePolicyCompiler.compile(compiler)
    assert result['ok']
    return compiler


@pytest.mark.parametrize('stage',['vertical_takeoff','hover','transition_forward','climb','cruise','turn','approach','transition_back','vertical_landing'])
def test_missing_policy_admits_current_prediction_in_each_control_phase(context,stage):
    before=context.handoff.latest_path.read_bytes()
    result=run(context,stage)
    gate=result['layer_status']['4']
    assert gate['ready'] and gate['evidence']['knowledge_policy_gate']['status']=='UNAVAILABLE'
    assert gate['evidence']['prior_admission']['ok'] and not gate['evidence']['prior_admission']['policy_available']
    payload=context.adapters[4].calls[-1]
    assert payload['snapshot']['std']['representations']['MESA_NAV']['canonical_state']['position_ned_m'][0]==10.
    assert 'knowledge_policy' not in payload['request']['context']
    assert 'online_physical_state' not in context.adapters[2].calls[-1]['context']
    assert context.handoff.latest_path.read_bytes()==before and not context.orch.knowledge_policy.path.exists()
    assert_no_release(context,result)


def test_incomplete_phase_policy_does_not_replace_live_snapshot(context):
    compiler=make_partial_policy(context);before=compiler.path.read_bytes()
    for index,x in enumerate((11.,88.)):
        context.state.x=x
        result=run(context,'hover',transaction_id=f'unit-fresh-{index}',reference_time_s=1.+index,
                   context={'knowledge_policy':{'phase_policy':{'phase':'cruise'},'old':True}})
        gate=result['layer_status']['4']
        assert gate['ready'] and gate['evidence']['knowledge_policy_gate']['reason']=='phase_policy_support_missing'
        payload=context.adapters[4].calls[-1]
        assert 'knowledge_policy' not in payload['request']['context']
        assert result['layer4_result']['candidates'][0]['trajectory'][0][0]==x+1.
        assert_no_release(context,result)
    assert compiler.path.read_bytes()==before


def test_verified_available_guide_is_attached_as_prior_only(context):
    compiler=make_partial_policy(context);before=compiler.path.read_bytes()
    result=run(context,'cruise')
    prior=context.adapters[4].calls[-1]['request']['context']['knowledge_policy']
    assert prior['phase_policy']['phase']=='cruise' and prior['offline_prior_only']
    assert not prior['actuation_authority'] and result['layer_status']['4']['evidence']['prior_admission']['policy_available']
    assert compiler.path.read_bytes()==before
    assert_no_release(context,result)


@pytest.mark.parametrize('fault',['hash','handoff','schema','timeline','unreadable'])
def test_rejected_or_failed_policy_still_blocks_prediction(context,fault):
    compiler=make_partial_policy(context)
    artifact=read_json_auto(compiler.path)
    if fault=='unreadable':compiler.path.write_bytes(b'bad cache')
    elif fault=='timeline':compiler.timeline_path.write_text('changed')
    else:
        if fault=='hash':artifact['accepted_rows']=999
        elif fault=='handoff':artifact['handoff_hash']='mismatch'
        elif fault=='schema':artifact['schema']='mismatch'
        if fault!='hash':artifact['artifact_hash']=sha256_obj({k:v for k,v in artifact.items() if k!='artifact_hash'})
        write_json_gz(compiler.path,artifact)
    before=compiler.path.read_bytes();result=run(context)
    gate=result['layer_status']['4']
    assert not gate['ready'] and not context.adapters[4].calls
    assert gate['evidence']['knowledge_policy_gate']['status'] in ('REJECTED','FAILED')
    assert compiler.path.read_bytes()==before
    assert_no_release(context,result)


def test_invalid_handoff_blocks_before_phase_lookup(context):
    data=read_json_auto(context.handoff.latest_path);data['source_version']='tampered'
    write_json_gz(context.handoff.latest_path,data)
    context.orch.knowledge_policy.phase_policy=forbidden
    result=run(context)
    assert not result['layer_status']['4']['ready'] and not context.adapters[4].calls
    assert_no_release(context,result)


@pytest.mark.parametrize('fault',['empty','non_atomic','layer3_failed','layer4_health','route_disabled'])
def test_current_snapshot_health_and_route_checks_remain(context,fault):
    if fault=='empty':context.state.empty=True
    elif fault=='non_atomic':context.state.snapshot_kind='partial'
    elif fault=='layer3_failed':context.state.l3_ok=False
    elif fault=='layer4_health':context.adapters[4].ready=False
    elif fault=='route_disabled':context.orch.config.data['layers']['layer4']['enabled']=False
    result=run(context)
    assert not result['layer_status']['4']['ready'] and not context.adapters[4].calls
    assert_no_release(context,result)


def test_layer4_deadline_still_discards_late_prediction(context):
    context.orch.config.data['deadlines']['layer4_s']=1e-12
    result=run(context)
    assert result['layer_status']['4']['ready'] and context.adapters[4].calls
    assert not result['layer_status']['5']['ready']
    assert any('DEADLINE_EXCEEDED' in str(e) for e in context.events)
    assert_no_release(context,result)


def test_pre_release_enters_fresh_prediction_without_guide_and_never_actuates(context):
    context.orch.config.data['tdoc']['rtmo']['l4_artifacts']['fast_path_reuse']=True
    result=run(context,rtmo_pre_release_verify=True)
    assert context.adapters[4].calls and result['layer_status']['4']['ready']
    assert result['downstream_layers']['6']['status']=='NOT_INVOKED_PRE_RELEASE_VERIFY'
    assert_no_release(context,result)


def test_fast_path_without_verified_artifact_remains_held(context):
    context.orch.config.data['tdoc']['rtmo']['l4_artifacts']['fast_path_reuse']=True
    result=run(context)
    assert result['layer_status']['4']['ready'] and not context.adapters[4].calls
    assert not result['layer_status']['5']['ready']
    assert any('no_verified_l4_artifact' in str(e) for e in context.events)
    assert_no_release(context,result)


@pytest.mark.parametrize('stage',['on_ground','touchdown'])
def test_fast_passive_contact_boundary_remains_non_actuating(context,stage):
    context.orch.config.data['tdoc']['rtmo']['l4_artifacts']['fast_path_reuse']=True
    context.orch.knowledge_policy.phase_policy=forbidden
    result=run(context,stage)
    assert not context.adapters[4].calls and not result['layer_status']['5']['ready']
    assert_no_release(context,result)
