from pathlib import Path
import importlib.util, math

ROOT=Path(__file__).resolve().parents[1]

def _mod():
    spec=importlib.util.spec_from_file_location('wf_solution5_v3',ROOT/'scripts'/'run_training_workflows.py')
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _bare(m, prev='transition_back'):
    p=object.__new__(m.OfflineWingMPETPlant)
    p.state={'x':10.0,'y':20.0,'h':112.0,'air_vx':12.0,'air_vy':3.0,'air_vh':-0.5,'soc':0.87,'omega_left':650.0,'omega_right':651.0}
    p.cf=dict(p.state)
    p.shadow={'pn':3.0,'pe':4.0,'pd':-12.0,'vn':12.0,'ve':3.0,'vd':0.5,'qw':1.0,'qx':0.0,'qy':0.0,'qz':0.0,'p_rate':.2,'q_rate':-.3,'r_rate':.1}
    p._last_phase=prev; p._last_wind=(4.0,-3.0,1.25)
    p._vtol_reference={'vertical_takeoff':0.82,'hover':0.81,'vertical_landing':0.80}; p._vtol_reference_source='test_physics_seed'; p._landing_descent_ref_m_s=1.0
    return p

def test_landing_entry_captures_transition_state_without_moving_position_energy_or_spool():
    m=_mod(); p=_bare(m)
    keep={k:p.state[k] for k in ('x','y','h','soc','omega_left','omega_right')}
    ev=p.prepare_phase_entry('vertical_landing',target_h=100.0)
    assert ev['applied'] is True and ev['schema']=='TDOC-Phoenix-vertical-landing-phase-entry-v1'
    for k,v in keep.items(): assert p.state[k]==v
    # Wind-compensated air-relative state corresponds to [0,0,-1] m/s ground velocity ENU.
    assert [p.state['air_vx'],p.state['air_vy'],p.state['air_vh']]==[-4.0,3.0,-2.25]
    assert [p.shadow['vn'],p.shadow['ve'],p.shadow['vd']]==[0.0,0.0,1.0]
    assert [p.shadow['p_rate'],p.shadow['q_rate'],p.shadow['r_rate']]==[0.0,0.0,0.0]
    assert abs(p.shadow['qw']-math.sqrt(.5))<1e-12 and abs(p.shadow['qy']-math.sqrt(.5))<1e-12
    # Priority 6: the authoritative offline_plant_state consumed by Layer4 must
    # carry the same vertical attitude/rates as the diagnostic shadow.
    assert abs(p.state['qw']-math.sqrt(.5))<1e-12 and abs(p.state['qy']-math.sqrt(.5))<1e-12
    assert [p.state['qx'],p.state['qz']]==[0.0,0.0]
    assert [p.state['p_rate'],p.state['q_rate'],p.state['r_rate']]==[0.0,0.0,0.0]
    assert ev['synthetic_control_proof'] is False

def test_landing_entry_is_bounded_near_ground_and_not_applied_from_unrelated_phase():
    m=_mod(); p=_bare(m,prev='approach'); p.state['h']=100.35
    ev=p.prepare_phase_entry('vertical_landing',target_h=100.0)
    assert abs(ev['ground_vertical_rate_m_s']+0.35)<1e-12
    q=_bare(m,prev='cruise'); before=dict(q.state)
    ev2=q.prepare_phase_entry('vertical_landing',target_h=100.0)
    assert ev2['applied'] is False and q.state==before

def test_vertical_landing_simulation_reference_uses_layer5_landing_seed_not_legacy_point52():
    m=_mod(); p=_bare(m)
    a=p._actuator_ref('vertical_landing')
    assert a['left_motor_cmd']==0.80 and a['right_motor_cmd']==0.80
    assert a['left_motor_cmd']!=0.52
    assert a['left_elevon_rad']==0.0 and a['right_elevon_rad']==0.0

def test_landing_command_is_ground_relative_and_descent_bounded():
    m=_mod(); p=_bare(m)
    cmd=p.command((10.0,20.0,100.0),'vertical_landing',(4.0,-3.0,1.25),0.0)
    # command + wind gives [0,0,-1] m/s, the same Layer5 landing reference.
    assert cmd==(-4.0,3.0,-2.25)
