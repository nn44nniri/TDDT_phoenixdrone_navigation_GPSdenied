from __future__ import annotations
import importlib.util, math
from pathlib import Path


def _workflow_module():
    mod_path=Path(__file__).resolve().parents[1]/'scripts'/'run_training_workflows.py'
    spec=importlib.util.spec_from_file_location('workflow_solution9_test',mod_path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


class FakeFMU:
    def evaluate_operation(self,op,**kw):
        if op==7:
            # Deliberately free-body/advecting to prove the wrapper enforces contact.
            return {'next_x':kw['x']+(kw['air_velocity_x']+kw['wind_x'])*kw['dt'],
                    'next_y':kw['y']+(kw['air_velocity_y']+kw['wind_y'])*kw['dt'],
                    'next_h':kw['h']+(kw['air_velocity_h']+kw['wind_h'])*kw['dt']}
        if op==28:
            return {'next_omega_left':0.0,'next_omega_right':0.0,'total_thrust':0.0}
        if op==30: return {'vtol_envelope_valid':True}
        if op==31: return {'next_soc':kw['soc'],'selected_power':0.0}
        if op==27:
            # Deliberately report motion/rates; passive policy must normalize it.
            out={f'next_{k}':kw[k] for k in ('pn','pe','pd','vn','ve','vd','qw','qx','qy','qz','p_rate','q_rate','r_rate')}
            out.update({'next_vn':4.0,'next_ve':-3.0,'next_vd':1.5,'next_p_rate':0.1,'next_q_rate':0.2,'next_r_rate':0.3,'vtol_feasible':True})
            return out
        raise AssertionError(op)


def _plant(mod):
    plant=object.__new__(mod.OfflineWingMPETPlant)
    plant.fmu=FakeFMU(); plant.mass=0.65; plant.voltage=11.1; plant.initial_wh=10.0
    plant.state={'x':100.0,'y':200.0,'h':50.0,'air_vx':0.0,'air_vy':0.0,'air_vh':0.0,'soc':1.0,'omega_left':0.0,'omega_right':0.0}
    plant.cf=dict(plant.state)
    plant.shadow={'pn':12.0,'pe':34.0,'pd':0.0,'vn':0.0,'ve':0.0,'vd':0.0,'qw':1.0,'qx':0.0,'qy':0.0,'qz':0.0,'p_rate':0.0,'q_rate':0.0,'r_rate':0.0}
    plant._vtol_reference={'vertical_takeoff':0.82,'hover':0.82,'vertical_landing':0.82}
    plant._vtol_reference_source='test'; plant._landing_descent_ref_m_s=1.0
    return plant


def test_on_ground_and_touchdown_share_same_contact_physics():
    mod=_workflow_module()
    results={}
    for phase in ('on_ground','touchdown'):
        plant=_plant(mod)
        results[phase]=plant.propagate((100.0,200.0,50.0),phase,(4.0,-3.0,1.5),1.1,0.0,dt=1.0,ground=50.0)
    for out in results.values():
        st=out['realized_state']; cf=out['counterfactual_state']; six=out['shadow_6dof_op27']
        assert (st['x'],st['y'],st['h'])==(100.0,200.0,50.0)
        assert (cf['x'],cf['y'],cf['h'])==(100.0,200.0,50.0)
        assert (st['air_vx'],st['air_vy'],st['air_vh'])==(0.0,0.0,0.0)
        assert out['wind_counterfactual_displacement_m']==0.0
        assert out['agl_m']==0.0
        assert (six['next_vn'],six['next_ve'],six['next_vd'])==(0.0,0.0,0.0)
        assert (six['next_p_rate'],six['next_q_rate'],six['next_r_rate'])==(0.0,0.0,0.0)
        assert math.isclose(six['next_qw'],math.sqrt(0.5))
        assert math.isclose(six['next_qy'],math.sqrt(0.5))


def test_source_contains_one_explicit_passive_boundary_policy():
    src=(Path(__file__).resolve().parents[1]/'scripts'/'run_training_workflows.py').read_text()
    assert "phase in ('on_ground','touchdown')" in src
    assert 'TDOC-passive-ground-contact-v1' in src
    assert 'offline_realized_passive_ground_contact' in src


def test_offline_report_capability_evidence_uses_symmetric_policy(tmp_path):
    import csv, json
    from tddt_tdoc.rtmo.capability_gate import build_capability_manifest
    m=tmp_path/'offline'/'mission'; m.mkdir(parents=True)
    with (m/'PHASE_HISTORY.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['cycle','phase']); w.writeheader(); w.writerow({'cycle':1,'phase':'on_ground'}); w.writerow({'cycle':2,'phase':'touchdown'})
    (m/'VTOL_PHASE_METRICS.json').write_text(json.dumps({'starts_on_ground':True,'touchdown_seen':True,'final_agl_m':0.0,'final_goal_distance_m':0.0}))
    with (m/'LAYER4_CANDIDATES.csv').open('w',newline='',encoding='utf-8') as f:
        csv.DictWriter(f,fieldnames=['cycle','feasible','verified']).writeheader()
    man=build_capability_manifest(tmp_path,layer6_forward_only=True,execution_overrides={'on_ground':True,'touchdown':True})
    a=man.phases['on_ground']['boundary_evidence']; b=man.phases['touchdown']['boundary_evidence']
    assert a['physics_policy']==b['physics_policy']=='TDOC-passive-ground-contact-v1'
    assert a['ground_velocity_zero'] is b['ground_velocity_zero'] is True
    assert a['wind_advection_suppressed'] is b['wind_advection_suppressed'] is True
    assert a['terminal_location_required'] is False
    assert b['terminal_location_required'] is True
