from __future__ import annotations
from pathlib import Path

from tddt_tdoc.rtmo.capability_gate import online_on_ground_boundary_evidence, build_capability_manifest
from tddt_tdoc.rtmo.capability_registry import LiveCapabilityRegistry


def _prep(*, ready=True, relative_alt_mm=0, groundspeed_ok=True, holding=True):
    return {
        'ok': True,
        'preparation': {
            'ready': True,
            'sequence': [
                {'step':'connect','result':{
                    'physical_readiness':{
                        'ready': ready,
                        'checks':{
                            'bridge_preflight_hold':{'ok':holding,'holding':holding,'released':False,'release_authorized':False},
                            'groundspeed_m_s':{'ok':groundspeed_ok,'value':0.0 if groundspeed_ok else 3.0},
                            'current_bridge_airspeed_m_s':{'ok':True,'value':0.0},
                            'initial_alt_error_m':{'ok':True,'value':0.0},
                        },
                    },
                    'position':{'relative_alt':relative_alt_mm,'vx':0,'vy':0,'vz':0},
                }}
            ],
        },
    }


def test_current_ptlib_ground_state_is_supported():
    ev=online_on_ground_boundary_evidence(_prep())
    assert ev['supported'] is True
    assert ev['source']=='online_ptlib_current_operational_state'
    assert ev['relative_alt_m']==0.0
    assert all(ev['checks'].values())


def test_current_ptlib_ground_state_is_fail_closed():
    ev=online_on_ground_boundary_evidence(_prep(relative_alt_mm=1500))
    assert ev['supported'] is False
    assert ev['checks']['relative_altitude_grounded'] is False
    ev=online_on_ground_boundary_evidence(_prep(groundspeed_ok=False))
    assert ev['supported'] is False
    assert ev['checks']['groundspeed_stationary'] is False


def test_current_boundary_replaces_stale_offline_boundary(tmp_path: Path):
    reg=LiveCapabilityRegistry(tmp_path/'runtime'/'rtmo'/'capability_registry.json.gz')
    reg.reset(run_id='offline-run',episode_id='OFFLINE_SE-001')
    reg.record_cycle(phase='on_ground',cycle_id=1,reference_time_s=0.0,
                     layer4_result={},layer5_result={},control_effect_applied=False,
                     boundary_evidence={'supported':False,'source':'offline_realized_state','agl_m':1.598})
    reg.record_boundary_state(phase='on_ground',boundary_evidence=online_on_ground_boundary_evidence(_prep()))
    snap=reg.snapshot()['phases']['on_ground']
    assert snap['boundary_state_supported'] is True
    assert snap['boundary_evidence']['source']=='online_ptlib_current_operational_state'
    # A later current-state failure must revoke the boundary; history cannot mask it.
    reg.record_boundary_state(phase='on_ground',boundary_evidence=online_on_ground_boundary_evidence(_prep(groundspeed_ok=False)))
    snap=reg.snapshot()['phases']['on_ground']
    assert snap['boundary_state_supported'] is False


def test_current_ground_refresh_does_not_manufacture_controlled_phase_capability(tmp_path: Path):
    reports=tmp_path/'reports'; reports.mkdir()
    reg=LiveCapabilityRegistry(tmp_path/'runtime'/'rtmo'/'capability_registry.json.gz')
    reg.reset(run_id='offline-run',episode_id='OFFLINE_SE-001')
    reg.record_boundary_state(phase='on_ground',boundary_evidence=online_on_ground_boundary_evidence(_prep()))
    man=build_capability_manifest(reports,layer6_forward_only=False,
        execution_overrides={'on_ground':True,'touchdown':True,'vertical_takeoff':True},
        require_certificate_guard=False,capability_registry_path=reg.path,require_live_registry=True)
    assert man.phases['on_ground']['capability'] is True
    assert man.phases['vertical_takeoff']['capability'] is False
    assert 'l4_not_full_fidelity_verified_for_phase' in man.phases['vertical_takeoff']['blockers']
    assert 'l5_not_final_admissible_for_phase' in man.phases['vertical_takeoff']['blockers']

def test_offline_on_ground_contact_blocks_wind_advection():
    import importlib.util
    mod_path=Path(__file__).resolve().parents[1]/'scripts'/'run_training_workflows.py'
    spec=importlib.util.spec_from_file_location('workflow_solution1_test',mod_path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    class FakeFMU:
        def evaluate_operation(self,op,**kw):
            if op==7:
                # Deliberately emulate free-body wind advection to prove the wrapper pins it.
                return {'next_x':kw['x']+(kw['air_velocity_x']+kw['wind_x'])*kw['dt'],
                        'next_y':kw['y']+(kw['air_velocity_y']+kw['wind_y'])*kw['dt'],
                        'next_h':kw['h']+(kw['air_velocity_h']+kw['wind_h'])*kw['dt']}
            if op==28:
                return {'next_omega_left':0.0,'next_omega_right':0.0,'total_thrust':0.0}
            if op==30:
                return {'vtol_envelope_valid':True}
            if op==31:
                return {'next_soc':kw['soc'],'selected_power':0.0}
            if op==27:
                return {f'next_{k}':kw[k] for k in ('pn','pe','pd','vn','ve','vd','qw','qx','qy','qz','p_rate','q_rate','r_rate')} | {'vtol_feasible':True}
            raise AssertionError(op)

    plant=object.__new__(mod.OfflineWingMPETPlant)
    plant.fmu=FakeFMU(); plant.mass=0.65; plant.voltage=11.1; plant.initial_wh=10.0
    plant.state={'x':100.0,'y':200.0,'h':50.0,'air_vx':0.0,'air_vy':0.0,'air_vh':0.0,'soc':1.0,'omega_left':0.0,'omega_right':0.0}
    plant.cf=dict(plant.state)
    plant.shadow={'pn':0.0,'pe':0.0,'pd':0.0,'vn':0.0,'ve':0.0,'vd':0.0,'qw':1.0,'qx':0.0,'qy':0.0,'qz':0.0,'p_rate':0.0,'q_rate':0.0,'r_rate':0.0}
    out=plant.propagate((100.0,200.0,50.0),'on_ground',(4.0,-3.0,1.5),1.1,0.0,dt=1.0,ground=50.0)
    assert out['realized_state']['x']==100.0
    assert out['realized_state']['y']==200.0
    assert out['realized_state']['h']==50.0
    assert out['realized_state']['air_vx']==0.0
    assert out['realized_state']['air_vy']==0.0
    assert out['realized_state']['air_vh']==0.0
    assert out['wind_counterfactual_displacement_m']==0.0
    assert out['agl_m']==0.0
