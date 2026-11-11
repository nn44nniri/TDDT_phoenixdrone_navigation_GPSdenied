import copy
import math
from pathlib import Path
from tddt_tdoc.online_imu import compact_replay_context
from tddt_tdoc.online_mission import load_mission_prior
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter


def test_compaction_preserves_contract_and_original_buffer():
    samples=[{'plant_time_s':1.,'accel_x_m_s2':9.8}]*2048
    ctx={'air_density':1.1,'flight_phase':'vertical_takeoff',
         'online_state_ingress':{'ok':True,'ptlib_ingress':{'imu_samples':samples,
            'state':{'runtime_ready':1.},'environment_contract':{'ok':True},
            'gui_state':{'imu_samples':samples,'armed':True}}}}
    before=copy.deepcopy(ctx);result=compact_replay_context(ctx)
    assert ctx==before and result['air_density']==ctx['air_density']
    ingress=result['online_state_ingress']['ptlib_ingress']
    assert 'imu_samples' not in ingress and 'imu_samples' not in ingress['gui_state']
    assert ingress['state']==ctx['online_state_ingress']['ptlib_ingress']['state']
    assert ingress['environment_contract']=={'ok':True} and ingress['gui_state']['armed'] is True


def test_actual_mesa_preserves_every_state_and_tep_gate(tmp_path):
    adapter=Layer2Adapter(TDOCConfig.from_yaml('configs/tdoc.yaml'))
    sessions=[adapter.open_isolated_session() for _ in range(2)]
    try:
        samples=[{'plant_time_s':i*.02,'frame':'BODY_FRD','source':'MAVLink/SCALED_IMU'} for i in range(2048)]
        ctx={'flight_phase':'vertical_takeoff','online_state_ingress':{'ptlib_ingress':{
            'imu_samples':samples,'gui_state':{'imu_samples':samples},'released':True}}}
        lean=compact_replay_context(ctx)
        for i,session in enumerate(sessions):session['mesa'].evidence_log=tmp_path/f'mesa-{i}.jsonl'
        for n in range(64):
            inputs={'dt':.02,'imu_ax':9.80665,'imu_ay':.001*n,'imu_az':-.16671305,
                    'imu_gx':0.,'imu_gy':.0001*n,'imu_gz':0.}
            results=[]
            for session,c in zip(sessions,(ctx,lean)):
                session['mesa'].fmu.set(**{k:v for k,v in inputs.items() if k!='dt'})
                results.append(session['mesa'].evaluate_packet(5,inputs={'dt':.02},context=c,stage_id='vertical_takeoff',communication_step=.02).to_dict())
            for key in ('state','validity','quality','uncertainty','reliability'):assert results[0][key]==results[1][key]
        assert (tmp_path/'mesa-1.jsonl').stat().st_size<(tmp_path/'mesa-0.jsonl').stat().st_size/10
    finally:
        for session in sessions:adapter.close_isolated_session(session)
        adapter.close()


def test_known_native_launch_pitch_matches_sim_jsbsim(tmp_path):
    cfg=TDOCConfig.from_yaml('configs/tdoc.yaml')
    # A reference fixture only: this test verifies the configured launch attitude,
    # and must neither require nor modify an operational Offline report.
    report=tmp_path/'reports';mission=report/'offline/mission';mission.mkdir(parents=True)
    (mission/'MISSION_TIMELINE.csv').write_text(
        'phase,reference_x_m,reference_y_m,reference_z_msl_m\n'
        'on_ground,0,0,1800\nvertical_takeoff,0,0,1812\n')
    prior=load_mission_prior(cfg,report,[-105.3,40.01],[-105.28,40.025])
    init=prior['initial_state'];w,x,y,z=[init[k] for k in ('qw','qx','qy','qz')]
    assert math.isclose(math.asin(2*(w*y-x*z)),math.radians(89.),abs_tol=1e-12)
    assert init['initialization_only'] and init['gnss_telemetry_used'] is False
    assert prior['truth_injection_to_mesa'] is False
