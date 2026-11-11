from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import pytest

from layer3.state_authority import bind_state_authority, StateAuthorityError
from layer3.stores import SnapshotStore
from layer3.engine import Layer3Engine

H='a'*64

def example():
    def domain(name, source, role, time, signals):
        return {'producer':source,'role':role,'epoch_id':'sitl-epoch-1','snapshot_id':'txn-27',
                'source_sha256':H,'source_time_s':time,'signals':signals}
    def sig(name,value,unit,frame,role,sigma=.05):
        return {'value':value,'unit':unit,'frame':frame,'semantic':name,
                'uncertainty':sigma,'data_role':role}
    return {'schema':'TDDT-L3-StateAuthority-v1','epoch_id':'sitl-epoch-1','snapshot_id':'txn-27',
            'states':{
                'navigation':domain('nav','MESA_NAV','navigation_current_state',9.99,{
                    'ground_velocity_ned_m_s':sig('ground_velocity_ned_m_s',[2,1,-.5],'m/s','NED','navigation_current_state')}),
                'execution':domain('jsbsim','JSBSIM_PLANT','simulation_observation',9.99,{
                    'left_motor_pos_norm':sig('left_motor_pos_norm',.12,'1','SCALAR','simulation_observation')}),
                'controller':domain('fcu','ARDUPILOT_FCU','controller_observation',9.99,{
                    'left_servo_pwm_us':sig('left_servo_pwm_us',1200,'us','SCALAR','controller_observation')})}}

def bind(x): return bind_state_authority(x, reference_time_s=10.,max_age_s=.5,max_future_skew_s=.05)

def test_valid_triplet_is_labelled_and_never_certified():
    o=bind(example()); assert o['navigation_authority']=='MESA_NAV'
    assert o['domains']['navigation']['authoritative'] is False
    assert o['domains']['controller']['producer']=='ARDUPILOT_FCU'
    assert o['domains']['controller']['status']=='SOURCE_DECLARED_UNVERIFIED'
    assert o['flight_authorized'] is False and o['controller_prediction_eligible'] is False
    assert len(o['binding_sha256'])==64 and o['binding_sha256']==bind(example())['binding_sha256']

def test_controller_missing_is_not_synthesized():
    x=example();del x['states']['controller'];o=bind(x)
    assert o['domains']['controller']['status']=='UNOBSERVED'
    assert o['native_controller_certified'] is False

def test_execution_missing_is_not_synthesized():
    x=example();del x['states']['execution']; assert bind(x)['domains']['execution']['status']=='UNOBSERVED'

@pytest.mark.parametrize('part,field,value,reason',[
    ('navigation','producer','JSBSIM_PLANT','source_authority_mismatch'),
    ('controller','producer','MESA_NAV','source_authority_mismatch'),
    ('controller','source_sha256','fake','expected_lowercase_sha256'),
    ('navigation','epoch_id','another','mixed_epoch'),
    ('execution','snapshot_id','another','mixed_snapshot_id'),
    ('execution','source_time_s',8.0,'stale_timestamp'),
    ('controller','source_time_s',11.,'future_timestamp'),
    ('controller','source_time_s',float('nan'),'non_finite'),
    ('execution','role','navigation_current_state','source_authority_mismatch'),
])
def test_negative_record(part,field,value,reason):
    x=example();x['states'][part][field]=value
    with pytest.raises(StateAuthorityError,match=reason):bind(x)

@pytest.mark.parametrize('domain,key,field,value,reason',[
    ('navigation','ground_velocity_ned_m_s','unit','ft/s','unit_frame_mismatch'),
    ('navigation','ground_velocity_ned_m_s','frame','ENU','frame_mismatch'),
    ('navigation','ground_velocity_ned_m_s','uncertainty',None,'not_numeric'),
    ('controller','left_servo_pwm_us','semantic','motor_rpm','semantic_mismatch'),
    ('execution','left_motor_pos_norm','unit','rpm','unit_mismatch'),
    ('controller','left_servo_pwm_us','data_role','navigation_current_state','role_mismatch'),
    ('execution','left_motor_pos_norm','value',float('nan'),'non_finite'),
])
def test_negative_signal(domain,key,field,value,reason):
    x=example(); x['states'][domain]['signals'][key][field]=value
    with pytest.raises(StateAuthorityError,match=reason):bind(x)

def test_navigation_gps_truth_injection_rejected():
    x=example();x['states']['navigation']['signals']['gps_truth_position']={
        'value':[1,2,3],'unit':'m','frame':'NED','semantic':'gps_truth_position',
        'uncertainty':0,'data_role':'navigation_current_state'}
    with pytest.raises(StateAuthorityError,match='gps_truth_cannot_override_navigation_authority'):bind(x)

def test_rpm_cannot_be_fabricated_from_pwm():
    x=example();x['states']['controller']['signals']['motor_rpm']={
        'value':3000,'unit':'rpm','frame':'SCALAR','semantic':'motor_rpm','uncertainty':1,'data_role':'controller_observation'}
    with pytest.raises(StateAuthorityError,match='controller_omega_unverified'):bind(x)

def test_mixed_reference_must_fail():
    x=example(); x['schema']='wrong'
    with pytest.raises(StateAuthorityError,match='schema_missing_or_mismatch'):bind(x)

def test_snapshot_is_atomic_sha_bound_and_no_staging_remains(tmp_path):
    store=SnapshotStore(tmp_path); original={'reference_time_s':10.,'provenance':{'source':'test'},'std':{'representations':{}}}
    target=store.write(1,original)
    loaded=json.loads(target.read_text());fingerprint=loaded['provenance'].pop('snapshot_payload_sha256')
    canonical=json.dumps(loaded,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    assert hashlib.sha256(canonical.encode()).hexdigest()==fingerprint
    assert original['provenance']=={'source':'test'}
    assert list(tmp_path.glob('.snapshot_staging_*'))==[]
    with pytest.raises(ValueError):store.write(1,{'reference_time_s':float('nan'),'provenance':{}})
    assert target.read_text() and fingerprint in target.read_text()

def test_hold_last_safe_never_refreshes_old_evidence():
    e=object.__new__(Layer3Engine)
    class HM: pass
    hm=HM();hm.last_safe_snapshot={'policy_version':'v','reference_time_s':7.,'std':{},'topology':{},
        'weights':{},'couplings':{},'center_of_consistency':{},'mismatch':None,'consistency_margin':None,
        'consistency_region':{},'stability':{},'emergence':{},'hidden_loop':{},'guidance':{},'slow_policy':{},
        'uncertainty_td':{},'constraints':{},'context':{},'provenance':{},'decision_route':{'action_candidate':{'throttle':1}}}
    e.health_manager=hm
    result=e._hold_last_safe(12.,{'kind':'fast'},{'mode':'HOLD_LAST_SAFE'})
    assert result.snapshot.reference_time_s==7.
    assert result.snapshot.provenance['held_at_reference_time_s']==12.
    assert result.snapshot.health['execution_eligible'] is False
    assert result.snapshot.decision_route['action_candidate']=={}

def test_alignment_rejects_bad_time_and_mesagps_injection():
    from layer3 import Layer3Config
    from layer3.alignment import STDAlignment
    root=Path(__file__).resolve().parents[1]
    cfg=Layer3Config.from_yaml(root/'configs/configuration_boulder.yaml',package_root=root)
    deps=root.parent
    cfg.paths.tdoc_root=str(deps/'TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914')
    cfg.alignment.strict_units=False;cfg.alignment.strict_frames=False;cfg.alignment.strict_semantics=False
    a=STDAlignment(cfg)
    p={'twin_id':'MESA_NAV','timestamp':'definitely-wrong','state':{'velocity_n':1,'velocity_e':0,'velocity_d':0},
       'quality':{'score':1},'validity':{'score':1},'reliability':{'posterior':1},'uncertainty':{},'provenance':{}}
    out=a.align_packet(p,10.)
    assert out.gate==0 and out.freshness==0 and out.provenance['invalid_or_missing_timestamp']
    p['timestamp']=10.
    p['canonical_state']={'ground_velocity_ned_m_s':[500,0,0]}
    with pytest.raises(StateAuthorityError,match='MESA_navigation_authority'):a.align_packet(p,10.)
    p['canonical_state']={'gps_truth_position':[1,2,3]}
    with pytest.raises(StateAuthorityError,match='gps_truth'):a.align_packet(p,10.)

def test_alignment_bind_optional_context():
    from layer3 import Layer3Config
    from layer3.alignment import STDAlignment
    root=Path(__file__).resolve().parents[1];cfg=Layer3Config.from_yaml(root/'configs/configuration_boulder.yaml',package_root=root)
    cfg.paths.tdoc_root=str(root.parent/'TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914')
    a=STDAlignment(cfg)
    empty=a.build_std([],10.)
    assert empty.control_status['state_authority']['status']=='NOT_PROVIDED'
    wrapped=a.build_std([],10.,context={'layer3_state_authority_v1':example()})
    assert wrapped.control_status['state_authority']['domains']['navigation']['producer']=='MESA_NAV'
    assert wrapped.control_status['state_authority']['native_controller_certified'] is False
