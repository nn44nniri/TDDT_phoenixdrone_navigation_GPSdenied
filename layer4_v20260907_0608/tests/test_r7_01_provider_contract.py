"""Contract tests; every synthetic document is explicitly non-qualification."""
import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from layer4 import native_provider_contract as c
from layer4.native_controller_contract import readiness


def request():
    p={}
    for i,function in enumerate((77,78,73,74),1):
        p.update({f'SERVO{i}_FUNCTION':function,f'SERVO{i}_MIN':1000.,
                  f'SERVO{i}_MAX':2000.,f'SERVO{i}_TRIM':1500. if i<=2 else 1000.,f'SERVO{i}_REVERSED':0.})
    p.update(RC_OVERRIDE_TIME=3.,FS_GCS_ENABL=0.,AHRS_EKF_TYPE=10.,Q_ENABLE=1.,Q_TAILSIT_ENABLE=1.,
             RCMAP_ROLL=1.,RCMAP_PITCH=2.,RCMAP_THROTTLE=3.,RCMAP_YAW=4.)
    return {'schema':c.REQUEST_SCHEMA,'request_id':'UNIT_ONLY_NOT_SITE_EVIDENCE',
        'context':{**{k:'1'*64 for k in c.HASH_FIELDS},'snapshot_id':'s','snapshot_version':'v1','candidate_id':'C',
                   'phase':'on_ground','native_session_id':'mission-boot','telemetry_session_id':'telemetry-boot'},
        'backend':{**{k:'2'*64 for k in c.BACKEND_HASH_FIELDS},'parameters':p,
                   'parameter_snapshot_sha256':c.digest(p),'gui_gains':{'max_pitch':1.,'max_roll':1.,'max_yaw':1.},
                   'initial_mode':'MANUAL','armed':False},
        'pilot_plan':[{'domain':c.DOMAIN,'u_cmd':[.2,-.1,.3,.4],'mode':'QHOVER','discrete':[]} for _ in range(2)],
        'timing':{'plant_clock':'FCU_BOOT','clock_mapping_sha256':'7'*64,'plant_start_bounds_s':[2.,2.02],'prediction_dt_s':.2,
                  'commit_segment_s':.1,'close_guard_s':.04,'execution_rate_hz':30.,'gui_rate_hz':30.,
                  'gui_ttl_s':.15,'admission_deadline_unix_s':1793457700.,'monotonic_domain_id':'host-boot'},
        'initial_state':{k:c.unknown_state(k,'not observable in ordinary status') for k in c.INITIAL_FIELDS},
        'isolation':{'policy':'ISOLATED_REPLAY_ONLY','initial_reference_sha256':'3'*64}}


def unavailable(r):
    return {'schema':c.PREDICTION_SCHEMA,'request_sha256':c.digest(r),'status':'UNAVAILABLE',
            'reason':'provider not implemented in R7.01','samples':[],
            'isolation':{'branch_session_id':None,'initial_reference_sha256':r['isolation']['initial_reference_sha256'],
                         'active_mission_advanced':False,'trace_sha256':None},
            'uncertainty':{'kind':'unbounded','reason':'controller/spool unobservable'}}


def known(name, value, kind='NATIVE_REPLAY'):
    f=c.unknown_state(name,'unit fixture');f.update(status='observed',value=value)
    if name in c.INTERNAL_FRAMES:f.update(components=['fixture_component'],units=['1'])
    f['source']={'kind':kind,'artifact_sha256':'4'*64,'source_time_s':2.,'clock_domain':'FCU_BOOT'}
    return f


def predicted(r):
    p=unavailable(r);p.update(status='PREDICTED',reason='SYNTHETIC_UNIT_SHAPE_ONLY')
    p['isolation'].update(branch_session_id='independent-branch',trace_sha256='5'*64)
    for i in range(3):
        fields={k:c.unknown_state(k,'unit only') for k in c.OUTPUT_FIELDS}
        fields.update(manual_control=known('manual_control',[-100.,200.,300.,400.]),
                      post_mixer_pwm=known('post_mixer_pwm',[1500.,1500.,1000.,1000.]),
                      plant_state=known('plant_state',[0.]*6+[1.,0.,0.,0.]+[0.]*3))
        for field in fields.values():
            if field['status'] != 'unknown':field['source']['source_time_s']=2.+i*.2
        p['samples'].append({'offset_s':i*.2,'mode':'MANUAL','channels':fields})
    return p


def test_descriptor_roundtrip_and_fail_closed_readiness():
    spec=json.loads(json.dumps(c.contract()));assert spec['implemented_provider'] is False
    assert spec['domain']==c.DOMAIN and spec['pilot_order']==['aileron','elevator','throttle','rudder']
    r=c.validate_request(request());assert r['contract_valid'] and 'spool' in r['unbounded_initial_fields']
    assert not r['provider_available'] and not r['flight_authorized'] and not r['native_controller_certified']
    s=SimpleNamespace(context={'pilot_command':{},'native_controller_evidence':{'verified':True}},provenance={})
    d=readiness(s);assert d['reason']=='native_pilot_controller_prediction_unavailable'
    assert d['provider_contract_revision']==c.REVISION and not d['provider_contract_is_qualification']
    assert not d['native_controller_certified'] and not d['flight_authorized']


@pytest.mark.parametrize('path,value',[
    (('pilot_plan',0,'domain'),'ACTUATOR_PWM'),(('pilot_plan',0,'u_cmd'),[0,0,-.1,0]),
    (('pilot_plan',0,'u_cmd'),[True,0,0,0]),(('pilot_plan',0,'u_cmd'),[0,float('nan'),0,0]),
    (('pilot_plan',0,'u_cmd'),[0,0,0,0,0]),(('pilot_plan',0,'mode'),'GUIDED'),
    (('pilot_plan',0,'discrete'),['ARM','DISARM']),
    (('timing','plant_clock'),'UNIX_WALL'),(('timing','plant_start_bounds_s'),[3,2]),
    (('timing','clock_mapping_sha256'),None),
    (('timing','commit_segment_s'),.3),(('timing','close_guard_s'),.1),
    (('timing','gui_ttl_s'),1.),(('timing','execution_rate_hz'),0.),
    (('timing','prediction_dt_s'),1e308),(('timing','admission_deadline_unix_s'),float('inf')),
    (('isolation','policy'),'LIVE_MISSION'),(('context','snapshot_hash'),'bad'),
    (('backend','initial_mode'),'AUTO'),(('backend','armed'),1),
    (('backend','gui_gains','max_pitch'),float('nan')),
])
def test_malformed_or_wrong_domain_requests_rejected(path,value):
    r=request();target=r
    for key in path[:-1]:target=target[key]
    target[path[-1]]=value
    assert not c.validate_request(r)['contract_valid']


def test_parameter_values_cannot_change_under_same_hash_or_omit_critical_state():
    r=request();r['backend']['parameters']['RC_OVERRIDE_TIME']=4.
    assert not c.validate_request(r)['contract_valid']
    r['backend']['parameter_snapshot_sha256']=c.digest(r['backend']['parameters'])
    assert c.validate_request(r)['contract_valid']  # shape only, not an approved parameter set
    del r['backend']['parameters']['SERVO1_FUNCTION'];r['backend']['parameter_snapshot_sha256']=c.digest(r['backend']['parameters'])
    assert not c.validate_request(r)['contract_valid']


def test_missing_spool_and_zero_filled_unknown_rejected():
    r=request();del r['initial_state']['spool'];assert not c.validate_request(r)['contract_valid']
    r=request();r['initial_state']['spool']['value']=[0.];assert not c.validate_request(r)['contract_valid']
    r=request();r['initial_state']['spool']['uncertainty']={'kind':'bounded','lower':[0.],'upper':[0.],'evidence_sha256':'a'*64}
    assert not c.validate_request(r)['contract_valid']


@pytest.mark.parametrize('name', ['controller','mixer','spool','actuator_dynamics','rc_override','motor_speed'])
def test_pwm_or_standard_telemetry_does_not_observe_internal_state(name):
    r=request();n=2 if name=='motor_speed' else 1
    for source in ('PWM_DERIVED','FCU_TELEMETRY'):
        r['initial_state'][name]=known(name,[0.]*n,source)
        assert not c.validate_request(r)['contract_valid']


def test_truth_cannot_replace_navigation_and_quaternion_not_normalized_silently():
    r=request();v=[0.]*6+[1.,0.,0.,0.]+[0.]*3
    r['initial_state']['navigation']=known('navigation',v,'NATIVE_REPLAY')
    assert not c.validate_request(r)['contract_valid']
    r['initial_state']['navigation']['source']['kind']='MESA_L3';assert c.validate_request(r)['contract_valid']
    r['initial_state']['navigation']['value'][6]=2.;assert not c.validate_request(r)['contract_valid']


def test_uncertainty_bounds_units_provenance_and_order_are_mandatory():
    r=request();r['initial_state']['previous_pilot']=known('previous_pilot',[0.,0.,.2,0.],'FCU_TELEMETRY')
    f=r['initial_state']['previous_pilot'];f['uncertainty']={'kind':'bounded','lower':[-.1,-.1,0.,-.1],
        'upper':[.1,.1,.3,.1],'evidence_sha256':'6'*64}
    assert c.validate_request(r)['contract_valid']
    for key,value in [('units',['rad']*4),('components',['elevator','aileron','throttle','rudder']),('value',[0.,0.,.8,0.])]:
        changed=deepcopy(r);changed['initial_state']['previous_pilot'][key]=value
        assert not c.validate_request(changed)['contract_valid']


def test_unavailable_is_explicit_and_never_certification():
    r=request();p=unavailable(r);v=c.validate_prediction(r,p)
    assert v['contract_valid'] and not v['native_controller_certified'] and not v['flight_authorized']
    p['samples']=[{}];assert not c.validate_prediction(r,p)['contract_valid']


@pytest.mark.parametrize('change', ['wrong_request','live_branch','advance_mission','wrong_reference','wrong_offset',
                                  'wrong_units','missing_output','fake_RPM','bounded_unknown','fake_FINAL','wrong_source_clock','wrong_source_time'])
def test_predicted_shape_rejects_correlation_and_uncertainty_errors(change):
    r=request();p=predicted(r);assert c.validate_prediction(r,p)['contract_valid']
    if change=='wrong_request':r['pilot_plan'][0]['u_cmd'][0]=.3
    elif change=='live_branch':p['isolation']['branch_session_id']=r['context']['native_session_id']
    elif change=='advance_mission':p['isolation']['active_mission_advanced']=True
    elif change=='wrong_reference':p['isolation']['initial_reference_sha256']='9'*64
    elif change=='wrong_offset':p['samples'][1]['offset_s']=.1
    elif change=='wrong_units':p['samples'][0]['channels']['post_mixer_pwm']['units']=['s']*4
    elif change=='missing_output':p['samples'][0]['channels']['plant_state']=c.unknown_state('plant_state','no trace')
    elif change=='fake_RPM':p['samples'][0]['channels']['motor_speed']=known('motor_speed',[100.,100.],'PWM_DERIVED')
    elif change=='bounded_unknown':p['uncertainty']['kind']='bounded'
    elif change=='fake_FINAL':p['FINAL']=True
    elif change=='wrong_source_clock':p['samples'][0]['channels']['plant_state']['source']['clock_domain']='JSBSIM'
    elif change=='wrong_source_time':p['samples'][1]['channels']['plant_state']['source']['source_time_s']=2.
    assert not c.validate_prediction(r,p)['contract_valid']


@pytest.mark.parametrize('u', [[.2,-.1,.3,.4],[-1.,1.,0.,-1.],[.0005,.0015,.9995,-.0005]])
def test_projection_matches_actual_GUI_method_and_PilotCommand(u):
    # Execute the actual source method with inert UI objects; this is a semantic
    # conformance test, not a fake FlightStack or a physical acceptance claim.
    import yaml
    from layer6.contracts import PilotCommand
    root=Path(__file__).resolve().parents[2];l6=root/'layer6_v20261007_1032'
    cfg=yaml.safe_load((l6/'gui/config/f310_phoenix.yaml').read_text())
    tree=ast.parse((l6/'src/gui/phoenix_f310_gui.py').read_text())
    cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='PhoenixGUI')
    method=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='control_tick')
    ns={'time':time,'clamp':lambda x,lo,hi:max(lo,min(hi,x))}
    exec(compile(ast.Module(body=[method],type_ignores=[]),'<actual-GUI-control_tick>','exec'),ns)
    noop=SimpleNamespace(set_xy=lambda *x:None,set_hat=lambda *x:None,setValue=lambda *x:None,setText=lambda *x:None)
    sent=[];s=SimpleNamespace(last_t=time.monotonic(),_poll_virtual_pilot=lambda:None,
        chk_keyboard=SimpleNamespace(isChecked=lambda:False),_gamepad_axes=lambda dt:(0,0,0,0,(0,0)),
        cfg=cfg,virtual_state=PilotCommand.from_vector(u).as_gui_state(),set_throttle=lambda v:None,
        throttle=1000*u[2],left_stick=noop,right_stick=noop,dpad=noop,lt_bar=noop,rt_bar=noop,
        lt_fraction=0.,rt_fraction=0.,lbl_lt=noop,lbl_rt=noop,throttle_bar=noop,lbl_values=noop,
        lbl_mode=noop,lbl_arm=noop,_send_pilot_command=lambda *args:sent.append(args),
        link=SimpleNamespace(drain=lambda:None,mode='MANUAL',armed=False))
    ns['control_tick'](s)
    expected=c.pilot_to_manual(u,{k:cfg['controller'][k] for k in ('max_pitch','max_roll','max_yaw')})
    assert sent==[(expected['x'],expected['y'],expected['z'],expected['r'])]
    assert expected['buttons']==0
