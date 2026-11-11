"""Explicit UNIT fixtures for the positive/negative contract, not Native qualification.

The fake issuer/guard below have no production entry point. They never claim a
real bounded controller model, run ArduPilot, issue ARM or accept R7.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from layer4.config import Layer4Config
from layer4.engine import Layer4Engine
from layer4.native_operational import CERT_SCHEMA,REQUEST_SCHEMA,USAGE
from layer4.native_provider_contract import DOMAIN,digest,BACKEND_HASH_FIELDS,REQUIRED_PARAMETERS,VECTOR_SPECS
from layer4.snapshot import Layer3SnapshotAdapter
from layer5.config import Layer5Config
from layer5.engine import Layer5Engine
from layer6.timebase import monotonic_domain
from tddt_tdoc.native_execution import NativeExecutionRechecker
from tddt_tdoc.adapters.layer5 import Layer5Adapter

ROOT=Path(__file__).resolve().parents[2]


def fixture(tmp_path,monkeypatch):
    # Earlier adapter regression tests intentionally unload layer5 modules.
    # Resolve the engine after that boundary so a cached class cannot mix an
    # old assembler with the newly imported in-process capability class.
    from layer5.engine import Layer5Engine
    from layer5.config import Layer5Config
    cfg4=Layer4Config.from_yaml(ROOT/'layer4_v20260907_0608/configs/configuration_boulder.yaml')
    e4=Layer4Engine.__new__(Layer4Engine);e4.config=cfg4
    stamp=time.time();mono=time.monotonic();domain=monotonic_domain()
    origin={'latitude_deg':40.01,'longitude_deg':-105.3,'terrain_elevation_msl_m':1798.51}
    safety={'schema':'Native-pilot-safety-envelope-v1','phase':'on_ground','ARM_authorized':False,
        'state_lower':[-1.1]*13+[900.]*4,'state_upper':[1.1]*13+[2100.,2100.,1000.,1000.],
        'pilot_domain':{'lower':[-.2,-.2,0.,-.2],'upper':[.2,.2,0.,.2]}}
    snapshot={'policy_version':'unit-policy','reference_time_s':1.,'std':{},'topology':{},'weights':{},
        'couplings':{},'center_of_consistency':{},'uncertainty_td':{},'guidance':{},
        'constraints':{'native_safety_policy':safety},
        'context':{'snapshot_epoch_s':stamp,'phase':'on_ground','rtmo_data_authority':{'navigation':'MESA_STD'},
                   'truth_injection_to_mesa':False,'coordinate_origin_sha256':digest(origin)},
        'provenance':{'snapshot_kind':'atomic_layer3_network_snapshot','fast_index':1}}
    snapshot['snapshot_hash']=Layer3SnapshotAdapter(cfg4).canonical_hash(snapshot)
    params={k:0. for k in REQUIRED_PARAMETERS}
    for i,f in enumerate((77,78,73,74),1):
        for k,v in (('FUNCTION',f),('MIN',1000.),('TRIM',1500.),('MAX',2000.)):params['SERVO%d_%s'%(i,k)]=v
    backend={k:'b'*64 for k in BACKEND_HASH_FIELDS}
    backend.update(parameters=params,parameter_snapshot_sha256=digest(params),armed=False,initial_mode='QHOVER')
    r={'schema':REQUEST_SCHEMA,'snapshot':snapshot,'candidate_id':'unit-candidate','transaction_id':'unit-tx',
       'phase':'on_ground','prediction_dt_s':.2,'commit_segment_s':.1,'backend':backend,
       'pilot_plan':[{'domain':DOMAIN,'mode':'QHOVER','discrete':[],'u_cmd':[.1,-.1,0.,.1]} for _ in range(3)]}
    p={'request_sha256':digest(r),'uncertainty':{'kind':'bounded','qualification_artifact_sha256':'a'*64},
       'coordinate_origin_sha256':digest(origin),
       'components':VECTOR_SPECS['plant_state'][1]+VECTOR_SPECS['post_mixer_pwm'][1],
       'units':VECTOR_SPECS['plant_state'][2]+VECTOR_SPECS['post_mixer_pwm'][2],
       'tube':[{'offset_s':offset,'frame':'NED_BODY_FRD_AND_PWM_US',
                'center':[0.]*6+[1.,0.,0.,0.]+[0.]*3+[1500.,1500.,1000.,1000.],
                'lower':[-.001]*6+[.999]+[-.001]*6+[1499.,1499.,1000.,1000.],
                'upper':[.001]*6+[1.001]+[.001]*6+[1501.,1501.,1000.,1000.]} for offset in (.1,.2,.4,.6)],
       'risk_upper_bound':.01,'risk_derivation':'EXPLICIT_UNIT_FIXTURE_NOT_NATIVE_RISK',
       'risk_evidence_sha256':'d'*64,'network_trajectory':[{'J_C':.1,'mu':1.} for _ in range(3)],
       'objective_vector':{k:.01 for k in ('mission','time','energy','risk','uncertainty',
            'consistency','margin','control_effort','sensitivity')}}
    binding={'model_id':'e'*64,'model_dependencies':{'fixture_only':True},'binding_hash':'f'*64,
             'handoff_hash':'1'*64,'forcing_file_sha256':'2'*64,'mission_origin':origin}
    cert={'schema':CERT_SCHEMA,'claims':{'request_sha256':digest(r),'snapshot_hash':snapshot['snapshot_hash'],
        'candidate_id':r['candidate_id'],'transaction_id':r['transaction_id'],'prediction_sha256':digest(p),
        'qualification_artifact_sha256':'a'*64,'backend':backend,
        'coordinate_origin_sha256':digest(origin),
        'network_trajectory_sha256':digest(p['network_trajectory']),
        'model_dependencies':binding['model_dependencies'],
        'environment_identity':{k:v for k,v in binding.items() if k!='model_dependencies'},
        'timing':{'monotonic_domain_id':domain,'admission_deadline_unix_s':stamp+30},
        'scope':{'usage':USAGE,'future_rollouts_covered':True,'operational_prediction_qualified':True,
          'bounded_error_model_qualified':True,'flight_authorized':False,'ARM_authorized':False}},
       'validity':{'issued_unix_s':stamp,'expires_unix_s':stamp+30,'issued_monotonic_s':mono,
         'expires_monotonic_s':mono+30,'lifetime_s':30.,'monotonic_domain_id':domain}}
    class UnitIssuer:
        def __init__(self):self.prediction=deepcopy(p);self.certificate=deepcopy(cert);self.backend=deepcopy(backend)
        def evaluate(self,request):
            assert request==r
            return {'schema':'TDOC-L4-native-execution-proof-v1','request_sha256':digest(r),
                'prediction':deepcopy(self.prediction),'certificate':deepcopy(self.certificate),
                'certificate_sha256':digest(self.certificate),'consumption':{'status':'VALID_OPERATIONAL_PREDICTION',
                 'certificate_sha256':digest(self.certificate)}}
        def current_backend(self):return deepcopy(self.backend)
    service=UnitIssuer();e4.native_execution_service=service
    class Adapter4:
        def verify_native_execution(self,request,dep):
            assert dep is service
            return e4.run(request).to_dict()
    checker=NativeExecutionRechecker.__new__(NativeExecutionRechecker)
    checker.guard=SimpleNamespace(check=lambda:deepcopy(binding))
    checker.layer4=Adapter4();checker.service=service;checker._decisions={};checker._last_source=None
    monkeypatch.setenv('TDDT_KSTORE_ROOT',str(tmp_path/'isolated_kstore'))
    e5=Layer5Engine(Layer5Config.from_yaml(ROOT/'layer5_v20260911_1510/configs/configuration_phoenix.yaml'))
    e5.native_execution_checker=checker
    return e4,e5,r,service,checker


def test_positive_contract_uses_real_engines_gates_assembler_and_consumer(tmp_path,monkeypatch):
    e4,e5,r,service,checker=fixture(tmp_path,monkeypatch)
    l4=e4.run(r).to_dict();assert l4['verification']['verified'],l4
    out=e5.run(l4,learning_mode='ONLINE_JSBSIM').to_dict()
    assert out['gates']['FINAL'] is True,out['blockers']
    assert out['D_exec']==out['layer6_package'] and not out['environment_feedback']['executed']
    assert all(out['gates'][g]['ok'] is True for g in ('CI','ADM','PSF','RTA','governance','budget'))
    a=Layer5Adapter.__new__(Layer5Adapter);a.engine=e5
    v=a.verify_native_execution_evidence(out,transaction_id=r['transaction_id'])
    assert v['ok'] and v['native_execution_model_matched'] and v['execution_confirmed'] is False
    assert out['D_exec']['u_cmd']==r['pilot_plan'][0]['u_cmd']
    assert out['D_exec']['execution_segment']['max_duration_s']==.1


@pytest.mark.parametrize('change',['review','unbounded','expiry','tube','risk','network','backend','missing_service'])
def test_invalid_qualification_never_produces_operational_candidate(tmp_path,monkeypatch,change):
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    if change=='review':s.certificate['claims']['scope']['usage']='EXPERIMENTAL_REFERENCE_REVIEW'
    if change=='unbounded':s.prediction['uncertainty']['kind']='unbounded'
    if change=='expiry':s.certificate['validity']['expires_unix_s']=time.time()-1
    if change=='tube':s.prediction['tube'][0]['lower'][0]=float('nan')
    if change=='risk':s.prediction['risk_upper_bound']=None
    if change=='network':s.prediction['network_trajectory'][0].pop('mu')
    if change=='backend':r['backend']['parameters'].pop('SERVO1_MIN')
    if change=='missing_service':e4.native_execution_service=None
    if change in ('unbounded','tube','risk','network'):
        s.certificate['claims']['prediction_sha256']=digest(s.prediction) if change!='tube' else '0'*64
    l4=e4.run(r).to_dict()
    assert not l4['verification']['verified'] and l4['verified_candidates']==[],l4
    out=e5.run(l4,learning_mode='ONLINE_JSBSIM').to_dict()
    assert not out['gates']['FINAL'] and out['D_exec'] is out['layer6_package'] is None


@pytest.mark.parametrize('gate',['CI','ADM','PSF','RTA','governance','budget'])
def test_not_assessed_gate_cannot_become_pre_execution_authority(tmp_path,monkeypatch,gate):
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    l4=e4.run(r).to_dict();out=e5.run(l4,learning_mode='ONLINE_JSBSIM').to_dict()
    assert out['gates']['FINAL'],out
    bad=deepcopy(out['gates']);bad[gate]['ok']=None
    with pytest.raises(ValueError):c.verify_package(out['D_exec'],l4,bad)


@pytest.mark.parametrize('change',['transaction','command','alias','snapshot','deadline','certificate','gate','FINAL','decision'])
def test_registered_decision_cannot_be_mutated_or_rebound(tmp_path,monkeypatch,change):
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    out=e5.run(e4.run(r).to_dict(),learning_mode='ONLINE_JSBSIM').to_dict()
    assert out['gates']['FINAL'],out
    if change=='transaction':out['D_exec']['transaction_id']='other'
    if change=='command':out['D_exec']['u_cmd'][2]=.5
    if change=='alias':out['D_exec']['command'][0]=-.5
    if change=='snapshot':out['D_exec']['snapshot_hash']='0'*64
    if change=='deadline':out['D_exec']['tau_deadline']=time.time()-1
    if change=='certificate':out['D_exec']['certificate_sha256']='0'*64
    if change=='gate':out['gates']['ADM']['ok']=None
    if change=='FINAL':out['gates']['FINAL']=False
    if change=='decision':out['decision']['candidate_id']='other'
    assert not c.verify_result(out,transaction_id=r['transaction_id'])['ok']


def test_changed_live_backend_revokes_registered_native_decision(tmp_path,monkeypatch):
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    out=e5.run(e4.run(r).to_dict(),learning_mode='ONLINE_JSBSIM').to_dict()
    assert out['gates']['FINAL'],out
    s.backend['parameters']['SERVO1_TRIM']+=1
    rejected=c.verify_result(out,transaction_id=r['transaction_id'])
    assert not rejected['ok'] and 'live Native' in rejected['reason']


def test_unbound_adapter_and_json_permit_cannot_authorize_assembly(tmp_path,monkeypatch):
    a=Layer5Adapter.__new__(Layer5Adapter)
    assert not a.verify_native_execution_evidence({'gates':{'FINAL':True}},transaction_id='tx')['ok']
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='untrusted'):
        e5.l6assembler.assemble([0.,0.,0.,0.],[],{}, {},0.,time.time()+1,1.,{},execution_authorization={'ok':True})


def test_current_governance_revocation_rejects_previously_registered_decision(tmp_path,monkeypatch):
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    out=e5.run(e4.run(r).to_dict(),learning_mode='ONLINE_JSBSIM').to_dict()
    assert out['gates']['FINAL'],out
    e5.config.data.setdefault('governance',{})['authority_valid']=False
    rejected=c.verify_result(out,transaction_id=r['transaction_id'])
    assert not rejected['ok'] and 'governance' in rejected['reason']


def test_actual_adapter_invoke_uses_current_native_authority_not_legacy_model_certificate(tmp_path,monkeypatch):
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    a=Layer5Adapter.__new__(Layer5Adapter);a.engine=e5;a._ensure=lambda:None
    a.config=SimpleNamespace(get=lambda k,d=None:'Layer6Client_GUI_MAVLink' if k=='layers.layer6.online.transport' else d)
    a.root=ROOT/'layer5_v20260911_1510';a.kstore_root=tmp_path/'isolated_kstore';a.module=SimpleNamespace()
    def forbidden(cert):raise AssertionError('legacy model-only certificate must not authorize new native lane')
    a._certificate_model_gate=forbidden
    out=a.invoke({'learning_mode':'ONLINE_JSBSIM','layer4_result':e4.run(r).to_dict()})['result']
    assert out['gates']['FINAL'],out
    assert not out['environment_feedback']['executed']


def test_new_issuer_roundtrip_rechecks_model_and_never_renews_automatically(tmp_path,monkeypatch):
    from tddt_tdoc.native_operational_certificate import NativeOperationalAuthority,NativeOperationalService
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    dep={'fixture_only':True,'model_id':'e'*64}
    identity=s.certificate['claims']['environment_identity']
    class UnitBoundedModel:
        def current_backend(self):return deepcopy(r['backend'])
        def predict(self,request):assert request==r;return deepcopy(s.prediction)
        def verify_qualification(self,request,model,environment):
            assert request==r and environment==identity
            return {'schema':'Native-bounded-model-qualification-v1','status':'VALIDATED_FOR_FUTURE',
                'uncertainty_kind':'bounded','request_sha256':digest(r),'qualification_artifact_sha256':'a'*64,
                'independent_trace_sha256':['c'*64],'model_id':model['model_id'],'backend_sha256':digest(r['backend'])}
    issuer=NativeOperationalAuthority.__new__(NativeOperationalAuthority)
    issuer.bounded_model=UnitBoundedModel();issuer.layer4_config=e4.config;issuer._issued={}
    issuer._current=lambda:(deepcopy(dep),deepcopy(identity),monotonic_domain())
    issuer._history=lambda:{'fixture_only':True,'known_out_of_budget_rejection_preserved':True}
    service=NativeOperationalService(issuer)
    proof=service.evaluate(r)
    assert proof['certificate']['claims']['scope']['usage']==USAGE
    assert proof['certificate']['claims']['scope']['ARM_authorized'] is False
    assert proof['consumption']['status']=='VALID_OPERATIONAL_PREDICTION'
    assert service.evaluate(r)['certificate_sha256']==proof['certificate_sha256']
    issuer._current=lambda:({**dep,'model_id':'3'*64},deepcopy(identity),monotonic_domain())
    with pytest.raises(ValueError,match='changed'):service.evaluate(r)


def test_new_issuer_cannot_issue_a_certificate_without_a_qualified_model():
    from tddt_tdoc.native_operational_certificate import NativeOperationalAuthority
    issuer=NativeOperationalAuthority.__new__(NativeOperationalAuthority);issuer.bounded_model=None
    with pytest.raises(ValueError,match='not qualified/installed'):issuer.issue({})


def test_tdoc_layer4_invoke_binds_source_and_preserves_atomic_snapshot(tmp_path,monkeypatch):
    from tddt_tdoc.adapters.layer4 import Layer4Adapter
    e4,e5,r,s,c=fixture(tmp_path,monkeypatch)
    a=Layer4Adapter.__new__(Layer4Adapter);a.engine=e4;a._ensure=lambda:None
    a.config=SimpleNamespace(get=lambda k,d=None:'Layer6Client_GUI_MAVLink' if k=='layers.layer6.online.transport' else d)
    a.bind_native_execution_source(lambda snap,req:(deepcopy(r),s))
    payload={'snapshot':r['snapshot'],'request':{'learning_mode':'ONLINE_JSBSIM','transaction_id':r['transaction_id'],'stage':'on_ground'}}
    out=a.invoke(payload)['result']
    assert out['verification']['verified'] and len(out['verified_candidates'])==1,out
    changed=deepcopy(r);changed['snapshot']['context']['phase']='hover'
    a.bind_native_execution_source(lambda snap,req:(changed,s))
    with pytest.raises(ValueError,match='snapshot'):a.invoke(payload)
