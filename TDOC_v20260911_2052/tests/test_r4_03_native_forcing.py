"""Protocol rejection and production runtime tests; native SITL evidence is separate."""
from copy import deepcopy
import json
import sys
import time
from types import SimpleNamespace
import pytest
from test_r4_02_binding_consumers import bound,explicit_readonly_reference,NATIVE,ENV
from layer6.binding import wind_parameter_plan,validate_wind_application,environment_status
from phoenix_env_adapter.native_forcing import apply_wind
from phoenix_env_adapter.bound import BoundEnvironment,run_bound_runtime,unsupported_fields


def message(name,value,system=1,component=1):
    return SimpleNamespace(param_id=name.encode()+b'\x00',param_value=value,
        get_srcSystem=lambda:system,get_srcComponent=lambda:component)


class Link:
    """Only the two allowed parameter operations exist; pilot commands fail loudly."""
    def __init__(self,mode='good'):
        self.target_system=1;self.target_component=1;self.mode=mode
        self.queue=[message('SIM_WIND_SPD',999)];self.values={};self.sent=[];self.mav=self
    def param_set_send(self,s,c,name,value,kind):
        key=name.decode();self.values[key]=value;self.sent.append(('SET',key,value))
        self.queue.append(message(key,value))
    def param_request_read_send(self,s,c,name,index):
        key=name.decode();self.sent.append(('GET',key))
        v=self.values[key]
        if self.mode=='missing':return
        if self.mode=='set-echo-only':return
        if self.mode=='wrong-source':self.queue.append(message(key,v,2,1));return
        if self.mode=='wrong-component':self.queue.append(message(key,v,1,2));return
        if self.mode=='nan':v=float('nan')
        if self.mode=='mismatch':v+=1.
        if self.mode=='wrong-name':key='UNRELATED'
        self.queue.append(message(key,v))
    def recv_match(self,**kwargs):return self.queue.pop(0) if self.queue else None


def sample():
    now=time.monotonic()
    return {'model_outputs':{'wind_u_3dw':-3.,'wind_v_3dw':-4.,'formula_valid':1.,'wind_3dw_valid':1.},
        'binding_hash':'bound','plant_time_s':10.,'plant_time_source':'MAVLink/ATTITUDE.time_boot_ms',
        'simulation_time_dataset':'2026-10-28T16:00:10','updated_monotonic':now,'sampled_monotonic':now,
        'actuation_authority':False,'navigation_truth_injection':False,'application_pending':False,
        'unsupported_fields':unsupported_fields(),'environment_position_source':'MAVLink/GLOBAL_POSITION_INT',
        'environment_position_time_s':10.,'environment_position_received_monotonic':now}


def verified():
    state=sample();link=Link();state['application']=apply_wind(link,state,timeout=.001)
    state.update(readback_verified=True,updated_monotonic=time.monotonic())
    return state,link


@pytest.mark.parametrize('u,v,direction',[ (0.,-4.,0.),(-4.,0.,90.),(0.,4.,180.),(4.,0.,270.),(0.,0.,0.)])
def test_true_meteorological_direction_and_no_second_height_law(u,v,direction):
    plan=wind_parameter_plan({'wind_u_3dw':u,'wind_v_3dw':v,'formula_valid':1.,'wind_3dw_valid':1.})
    assert plan['SIM_WIND_DIR']==direction and plan['SIM_WIND_T']==1.
    assert plan['SIM_WIND_TURB']==0. and plan['SIM_WIND_TC']==.1 and plan['SIM_WIND_DIR_Z']==0.


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),True,None,'3'])
def test_nonfinite_and_untyped_wind_rejected(bad):
    outputs=sample()['model_outputs'];outputs['wind_u_3dw']=bad
    with pytest.raises(ValueError):wind_parameter_plan(outputs)


def test_positive_protocol_requires_independent_read_for_every_parameter():
    state,link=verified()
    assert validate_wind_application(state)
    assert len([r for r in link.sent if r[0]=='GET'])==6
    assert state['application']['parameters']['SIM_WIND_SPD']['readback']==5.
    assert not state['application']['instantaneous_atmosphere_verified']
    assert not state['application']['atomic_parameter_transaction']


@pytest.mark.parametrize('mode',['missing','set-echo-only','wrong-source','wrong-component','nan','mismatch','wrong-name'])
def test_set_echo_wrong_source_bad_value_and_missing_get_never_verify(mode):
    with pytest.raises(RuntimeError,match='readback unavailable'):
        apply_wind(Link(mode),sample(),timeout=.001,retries=1)


@pytest.mark.parametrize('change',[
    lambda s:s.update(updated_monotonic=time.monotonic()-2.),
    lambda s:s.update(sampled_monotonic=time.monotonic()-2.),
    lambda s:s.update(readback_verified=False),
    lambda s:s.update(application_pending=True),
    lambda s:s.update(plant_time_s=0.),
    lambda s:s.update(plant_time_source='wall-clock'),
    lambda s:s['application'].update(binding_hash='other'),
    lambda s:s['application']['parameters']['SIM_WIND_SPD'].update(readback=999.),
    lambda s:s['application']['parameters']['SIM_WIND_DIR'].update(source_system=2),
    lambda s:s['application']['parameters']['SIM_WIND_T'].update(verified=False),
    lambda s:s['unsupported_fields']['vertical_wind'].update(verified=True),
    lambda s:s.update(environment_position_source='declared-binding-origin'),
    lambda s:s.update(environment_position_time_s=0.),
])
def test_incomplete_stale_cross_binding_and_unobserved_evidence_fail_closed(change):
    state,_=verified();change(state)
    with pytest.raises(ValueError):validate_wind_application(state)


def test_actual_runtime_publishes_readback_and_explicit_unsupported_fields(bound,tmp_path,monkeypatch,capsys):
    path,b=bound;link=Link();link.queue=[];link.closed=False
    home=b['mission_origin']
    def clock():return SimpleNamespace(time_boot_ms=10000,get_type=lambda:'GLOBAL_POSITION_INT',get_srcSystem=lambda:1,get_srcComponent=lambda:1,
        lat=round(home['latitude_deg']*1e7),lon=round(home['longitude_deg']*1e7),alt=round(home['altitude_msl_m']*1000))
    physical=SimpleNamespace(get_type=lambda:'SIM_STATE',get_srcSystem=lambda:1,get_srcComponent=lambda:1,
        lat_int=round(home['latitude_deg']*1e7),lon_int=round(home['longitude_deg']*1e7),
        lat=round(home['latitude_deg']*1e7),lon=round(home['longitude_deg']*1e7),alt=home['altitude_msl_m'])
    after=clock();after.time_boot_ms=10050
    frames=[clock(),physical,after]
    original=link.recv_match
    link.recv_match=lambda **kw:frames.pop(0) if isinstance(kw.get('type'),list) else original(**kw)
    link.wait_heartbeat=lambda **kw:clock();link.close=lambda:setattr(link,'closed',True)
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=lambda endpoint:link)))
    target=tmp_path/'state.json'
    state=run_bound_runtime(path,target,mavlink='mock-protocol',max_updates=1)
    assert state['ok'] and environment_status(path,target)['ok'] and link.closed
    assert all(r['value'] is None and not r['verified'] and not r['applied'] for r in state['unsupported_fields'].values())
    assert not state['runtime_clock_synchronization_verified'] and not state['UTC_mapping_verified']
    assert not state['actuation_authority'] and not state['navigation_truth_injection']
    assert state['environment_position_source']=='MAVLink/SIM_STATE physical forcing only'
    assert state['environment_position_time_bounds_s']==[10.,10.05]
    assert state['navigation_observation_generated'] is False


def test_runtime_failure_replaces_preexisting_success(bound,tmp_path,monkeypatch):
    path,_=bound;dest=tmp_path/'state.json';dest.write_text(json.dumps({'ok':True,'readback_verified':True}))
    link=SimpleNamespace(wait_heartbeat=lambda **kw:None,close=lambda:None)
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=lambda endpoint:link)))
    with pytest.raises(RuntimeError,match='heartbeat unavailable'):
        run_bound_runtime(path,dest,mavlink='mock-failure',max_updates=1)
    result=json.loads(dest.read_text());assert not result['ok'] and not result['readback_verified']
    assert not environment_status(path,dest)['ok']


def test_connection_exception_also_invalidates_previous_success(bound,tmp_path,monkeypatch):
    dest=tmp_path/'state.json';dest.write_text('{"ok":true,"readback_verified":true}')
    def fail(endpoint):raise OSError('unreachable native FCU')
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=fail)))
    with pytest.raises(OSError,match='unreachable'):
        run_bound_runtime(bound[0],dest,mavlink='unreachable',max_updates=1)
    state=json.loads(dest.read_text());assert not state['ok'] and not state['readback_verified']


def test_live_clock_without_position_never_applies_binding_origin(bound,tmp_path,monkeypatch):
    from phoenix_env_adapter import native_forcing
    msg=SimpleNamespace(time_boot_ms=10000,get_type=lambda:'ATTITUDE',get_srcSystem=lambda:1,get_srcComponent=lambda:1)
    queue=[msg,None]
    link=SimpleNamespace(wait_heartbeat=lambda **kw:msg,recv_match=lambda **kw:queue.pop(0),close=lambda:None)
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=lambda endpoint:link)))
    monkeypatch.setattr(native_forcing,'apply_wind',lambda *a,**kw:pytest.fail('no forcing before observed position'))
    with pytest.raises(RuntimeError,match='clock unavailable'):
        run_bound_runtime(bound[0],tmp_path/'state.json',mavlink='clock-without-position',max_updates=1)
    assert not json.loads((tmp_path/'state.json').read_text())['readback_verified']


def test_physical_weather_position_never_accepts_unbounded_or_navigation_injecting_state():
    state,_=verified();state.update(environment_position_source='MAVLink/SIM_STATE physical forcing only',
        environment_position_time_bounds_s=[9.95,10.],environment_position_time_s=9.95,
        navigation_observation_generated=False)
    assert validate_wind_application(state)
    state['environment_position_time_bounds_s']=[9.,10.]
    with pytest.raises(ValueError,match='conservative native clock bounds'):validate_wind_application(state)
    state['environment_position_time_bounds_s']=[9.95,10.];state['navigation_observation_generated']=True
    with pytest.raises(ValueError,match='no navigation injection'):validate_wind_application(state)
