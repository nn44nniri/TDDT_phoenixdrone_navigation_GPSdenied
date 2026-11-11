from tddt_tdoc.rtmo.realtime_scheduler import RTClock


def sample(t):
    return {'plant_time_s':t,'frame':'BODY_FRD','source':'MAVLink/SCALED_IMU',
            'gyro_x_rad_s':0.,'gyro_y_rad_s':0.,'gyro_z_rad_s':0.,
            'accel_x_m_s2':9.8,'accel_y_m_s2':0.,'accel_z_m_s2':-.17}


def test_contiguous_measured_samples_preserve_grant_for_slow_decision():
    clock=RTClock(last_sim_time_s=32.327)
    rows=[sample(32.327+i*.02) for i in range(1,500)]
    result=clock.observe_measured_samples(42.326,rows)
    assert result['ok'] and result['state_fresh'] and result['measured_sample_count']==499
    assert result['bound_s']==.15 or abs(result['bound_s']-.15)<1e-10
    assert clock.last_sim_time_s==42.326
    assert abs(result['decision_interval_s']-9.999)<1e-8


def test_gap_and_wrong_source_do_not_advance_clock():
    clock=RTClock(last_sim_time_s=1.)
    assert not clock.observe_measured_samples(2.,[sample(1.02),sample(1.5)])['ok']
    assert clock.last_sim_time_s==1.
    bad=sample(1.02);bad['source']='predicted'
    assert not clock.observe_measured_samples(2.,[bad])['ok']
    assert clock.last_sim_time_s==1.
    assert not clock.observe_measured_samples(2.,[])['ok']
    assert clock.last_sim_time_s==1.
