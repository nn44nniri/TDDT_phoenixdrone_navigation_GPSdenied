from pathlib import Path
import importlib.util, math

ROOT=Path(__file__).resolve().parents[1]

def _mod():
    spec=importlib.util.spec_from_file_location('wf_solution4_v3',ROOT/'scripts'/'run_training_workflows.py')
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _bare_plant(m):
    p=object.__new__(m.OfflineWingMPETPlant)
    p.state={'x':10.0,'y':20.0,'h':112.0,'air_vx':0.0,'air_vy':0.0,'air_vh':2.0,'soc':0.9,'omega_left':620.0,'omega_right':621.0}
    p.cf=dict(p.state)
    c=math.sqrt(0.5)
    p.shadow={'pn':1.0,'pe':2.0,'pd':-12.0,'vn':1.5,'ve':-2.0,'vd':-2.0,'qw':1.0,'qx':0.0,'qy':0.0,'qz':0.0,'p_rate':.3,'q_rate':-.4,'r_rate':.2}
    p._last_phase='vertical_takeoff'; p._last_wind=(4.0,-3.0,1.25)
    p._vtol_reference={'vertical_takeoff':0.82,'hover':0.81}; p._vtol_reference_source='test_physics_seed'
    return p

def test_hover_phase_entry_preserves_position_energy_and_motor_spool_but_settles_ground_velocity_attitude_rates():
    m=_mod(); p=_bare_plant(m)
    before={k:p.state[k] for k in ('x','y','h','soc','omega_left','omega_right')}
    ev=p.prepare_phase_entry('hover',target_h=112.0)
    assert ev['applied'] is True
    for k,v in before.items(): assert p.state[k]==v
    assert [p.state['air_vx'],p.state['air_vy'],p.state['air_vh']]==[-4.0,3.0,-1.25]
    assert [p.shadow['vn'],p.shadow['ve'],p.shadow['vd']]==[0.0,0.0,0.0]
    assert [p.shadow['p_rate'],p.shadow['q_rate'],p.shadow['r_rate']]==[0.0,0.0,0.0]
    assert abs(p.shadow['qw']-math.sqrt(.5))<1e-12 and abs(p.shadow['qy']-math.sqrt(.5))<1e-12
    assert ev['synthetic_control_proof'] is False

def test_phase_entry_settle_is_only_takeoff_to_hover():
    m=_mod(); p=_bare_plant(m); p._last_phase='hover'
    state=dict(p.state); ev=p.prepare_phase_entry('hover',target_h=112.0)
    assert ev['applied'] is False and p.state==state

def test_vertical_takeoff_command_compensates_wind_to_ground_relative_climb():
    m=_mod(); p=_bare_plant(m)
    cmd=p.command((10.0,20.0,114.0),'vertical_takeoff',(4.0,-3.0,1.25),0.0)
    assert cmd==(-4.0,3.0,0.75)  # +wind => [0,0,+2] ground-relative climb

def test_takeoff_and_hover_reference_use_same_layer5_physics_contract_not_legacy_constants():
    m=_mod(); p=_bare_plant(m)
    to=p._actuator_ref('vertical_takeoff'); hv=p._actuator_ref('hover')
    assert to['left_motor_cmd']==0.82 and to['right_motor_cmd']==0.82
    assert hv['left_motor_cmd']==0.81 and hv['right_motor_cmd']==0.81
    assert to['left_motor_cmd']!=0.78 and hv['left_motor_cmd']!=0.67
    assert hv['vtol_reference_source']=='test_physics_seed'
