"""Contract tests only. Synthetic inputs never constitute Native qualification."""
from copy import deepcopy
from pathlib import Path
import itertools
import pytest

from layer4.native_state_observer import NativeStateObserver, aircraft_contract, lag_step
from layer4.native_hybrid_prediction import NativeControllerResponse, NativeHybridPredictor
from layer4.native_provider_contract import DOMAIN

ROOT = Path(__file__).resolve().parents[2]
AIRCRAFT = ROOT/'layer6_v20261007_1032/FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'


def observer():
    p = {('SERVO%d_%s' % (i, k)): v for i in range(1, 5) for k, v in (('MIN', 1000.), ('MAX', 2000.))}
    p.update({'Q_A_RAT_%s_IMAX' % axis: .5 for axis in ('RLL', 'PIT', 'YAW')})
    return NativeStateObserver({'parameters': p, 'initial_mode': 'QHOVER', 'armed': False},
                               aircraft_contract(AIRCRAFT), session_id='unit-observer')


def sample(t=1.):
    return {'session_id': 'unit-observer', 'mode': 'QHOVER', 'armed': False,
            'plant_time_s': t, 'plant_state': [0.]*6+[1.,0.,0.,0.,0.,0.,0.],
            'pwm': [1500.,1500.,1000.,1000.], 'pilot': [0.,0.,0.,0.],
            'source_times_s': {'plant': t, 'pwm': t, 'pilot': t}, 'navigation_role': 'NATIVE_EVALUATION_ONLY'}


def test_motor_lag_is_read_from_actual_model_not_invented():
    c = aircraft_contract(AIRCRAFT)
    assert c['lag_c1_s_inverse'] == [55.555556]*2
    assert c['lag_support'] == [0.,1.] and not c['RPM_observable']


@pytest.mark.parametrize('dt', [.0025,.05,.1])
def test_tustin_interval_contains_all_endpoint_and_interior_images(dt):
    lo, hi = lag_step([.1,.7], [.2,.8], 55.555556, dt)
    a = 55.555556*dt/(2+55.555556*dt)
    b = (2-55.555556*dt)/(2+55.555556*dt)
    for x,u,v in itertools.product((.1,.3,.7),(.2,.4,.8),(.2,.5,.8)):
        value = max(0.,min(1.,b*x+a*(u+v)))
        assert lo-1e-12 <= value <= hi+1e-12


def test_quiet_pwm_cannot_fabricate_zero_integrator_spool_or_motor_state():
    o = observer()
    for t in (1.,1.05,1.1): result = o.update(sample(t))
    assert result['controller_integrators']['RLL']['interval'] == [-.5,.5]
    assert not result['controller_integrators']['RLL']['observed']
    assert len(result['FCU_spool']['possible']) == 5 and not result['FCU_spool']['observed']
    assert result['motor_lag_position_intervals'] == [[0.,1.],[0.,1.]]
    assert result['risk_upper_bound'] is None and not result['future_output_support_qualified']


@pytest.mark.parametrize('change', ['session','armed','mode','nan','quaternion','cohort','time','gap','throttle'])
def test_observer_rejects_invalid_or_mixed_history(change):
    o = observer();o.update(sample());row=sample(1.05)
    if change=='session':row['session_id']='other'
    if change=='armed':row['armed']=True
    if change=='mode':row['mode']='FBWA'
    if change=='nan':row['pwm'][0]=float('nan')
    if change=='quaternion':row['plant_state'][6]=0.
    if change=='cohort':row['source_times_s']['pwm']=.5
    if change=='time':row=sample()
    if change=='gap':row=sample(1.3)
    if change=='throttle':row['pilot'][2]=.01
    with pytest.raises(ValueError):o.update(row)


class UnitPhysics:
    def identity(self):return {'unit_only':True}
    def rollout(self, initial, schedule, offsets, environment):
        out=[]
        for t in offsets:
            x=list(initial);x[0]+=t*(schedule[0]['pwm_us'][0]-1500)/500
            out.append({'offset_s':t,'plant_state':x})
        return out


def predictor():
    o=observer();o.update(sample())
    c=[[0.]*12 for _ in range(4)];c[0][5]=30.
    response=NativeControllerResponse(o.backend,c,['a'*64])
    return NativeHybridPredictor(o,UnitPhysics(),response,current_backend=lambda:deepcopy(o.backend))


def plan(axis):
    return [{'domain':DOMAIN,'mode':'QHOVER','discrete':[],'u_cmd':[axis,0.,0.,0.]} for _ in range(3)]


def test_dynamic_response_depends_on_candidate_and_never_calls_hull_a_bound():
    m=predictor()
    left=m.predict_reference(plan(-.1),.2,.1,{})
    right=m.predict_reference(plan(.1),.2,.1,{})
    assert left['samples'][0]['pwm'][0] < right['samples'][0]['pwm'][0]
    assert left['samples'][0]['plant_state'][0] < right['samples'][0]['plant_state'][0]
    assert right['uncertainty']['finite_hull_is_outer_bound'] is False
    assert right['uncertainty']['risk_upper_bound'] is None
    assert not right['operational_prediction_qualified']
    with pytest.raises(ValueError,match='qualification not established'):m.verify_qualification({}, {}, {})
    with pytest.raises(ValueError,match='qualification not established'):m.predict({})


@pytest.mark.parametrize('change',['throttle','ARM','mode','grid','backend'])
def test_hybrid_model_domain_and_live_backend_changes_reject(change):
    m=predictor();p=plan(.1);dt=.2
    if change=='throttle':p[0]['u_cmd'][2]=.1
    if change=='ARM':p[0]['discrete']=['ARM']
    if change=='mode':p[0]['mode']='QSTABILIZE'
    if change=='grid':dt=.12
    if change=='backend':m.backend_reader=lambda:{}
    with pytest.raises(ValueError):m.predict_reference(p,dt,.1,{})


def test_response_fit_rejects_unregistered_cadence_and_missing_lineage():
    o=observer();rows=[{'pwm':[1500.]*4,'pilot':[0.]*4,'rates':[0.]*3,'next_pwm':[1500.]*4,'dt_s':.05} for _ in range(24)]
    fitted=NativeControllerResponse.fit(o.backend,rows,['a'*64])
    assert fitted.step([1500.]*4,[0.]*4,[0.]*3,.05)==[1500.]*4
    rows[-1]['dt_s']=.1
    with pytest.raises(ValueError,match='cadence'):NativeControllerResponse.fit(o.backend,rows,['a'*64])
    with pytest.raises(ValueError,match='lineage'):NativeControllerResponse(o.backend,[[0.]*12 for _ in range(4)],[])


def test_observer_retains_real_imu_source_and_rejects_wrong_frame_or_time():
    row=sample();row['imu']={'source':'MAVLink/SCALED_IMU','frame':'BODY_FRD',
        'specific_force_m_s2':[9.8,0.,0.],'angular_rate_rad_s':[0.,0.,0.],'source_time_s':.98}
    o=observer();assert o.update(row)['IMU_observed']
    bad=deepcopy(row);bad['imu']['frame']='ENU'
    with pytest.raises(ValueError,match='IMU'):observer().update(bad)
    bad=deepcopy(row);bad['imu']['source_time_s']=.5
    with pytest.raises(ValueError,match='IMU'):observer().update(bad)


def test_predicted_rates_feed_the_next_controller_step():
    class RatePhysics(UnitPhysics):
        def rollout(self, initial, schedule, offsets, environment):
            rows=super().rollout(initial,schedule,offsets,environment)
            for r in rows:r['plant_state'][10]=.2
            return rows
    m=predictor();m.physics=RatePhysics();m.response.coefficients[0][9]=10.
    out=m.predict_reference(plan(.1),.2,.1,{})
    pwms=[r['pwm_us'][0] for r in out['prediction_schedule']]
    assert pwms[1]-pwms[0] == pytest.approx(5.)
    assert pwms[0]-1500. == pytest.approx(3.)


def test_native_vertical_frame_does_not_double_count_cg_or_move_atmosphere_to_home_altitude():
    from layer4.native_physics import native_vertical_origin
    environment={'origin':{'latitude_deg':40.01,'longitude_deg':-105.3,
        'altitude_msl_m':1798.51,'terrain_elevation_msl_m':1798.515783},
        'vertical_contract':{'schema':'Native-JSBSim-AGL-export-v1','native_connector_sha256':'a'*64,
            'JSBSim_local_terrain_m':0.,'home_export_offset_msl_m':1798.51}}
    lat,lon,local_origin,ground=native_vertical_origin(environment,'a'*64)
    assert local_origin-(-.13)==pytest.approx(.13)
    assert ground==0. and lat==40.01 and lon==-105.3
    with pytest.raises(ValueError):native_vertical_origin(environment,'b'*64)
