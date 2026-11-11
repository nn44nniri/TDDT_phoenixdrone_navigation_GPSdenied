"""No interpolation, invented frames, time substitution or PWM-as-RPM."""
from copy import deepcopy
import pytest

from layer6.native_forecast import select_sample, source_time


def fixture():
    def row(**msg):
        return {'message': msg, 'raw_MAVLink_hex': 'unit fixture', 'received_monotonic_s': 100.}
    h = {
        'LOCAL_POSITION_NED': [row(time_boot_ms=1200, x=1., y=2., z=3., vx=.1, vy=.2, vz=.3)],
        'ATTITUDE_QUATERNION': [row(time_boot_ms=1200, q1=1., q2=0., q3=0., q4=0.,
                                    rollspeed=.01, pitchspeed=.02, yawspeed=.03)],
        'SERVO_OUTPUT_RAW': [row(time_usec=1200166, servo1_raw=1490, servo2_raw=1510, servo3_raw=1000, servo4_raw=1000)],
        'RC_CHANNELS': [row(time_boot_ms=1200, chan1_raw=1580, chan2_raw=1460, chan3_raw=1100, chan4_raw=1540)]}
    p = {**{'RCMAP_'+k: i for i,k in enumerate(('ROLL', 'PITCH', 'THROTTLE', 'YAW'), 1)},
         **{'RC%d_%s' % (i, s): v for i in range(1,5) for s,v in (('MIN',1100),('MAX',1900))}}
    wire = {'x':100, 'y':200, 'z':0, 'r':100}
    return h, p, wire


def test_exact_position_quaternion_tick_and_original_per_channel_times():
    h, p, w = fixture()
    sample = select_sample(h, 1.2, 1.25, w, p, 'FBWA', .2)
    assert sample['plant_time_s'] == 1.2
    assert sample['pwm_time_s'] == 1.200166
    assert sample['plant_state'] == [1.,2.,3.,.1,.2,.3,1.,0.,0.,0.,.01,.02,.03]
    assert sample['pwm'] == [1490,1510,1000,1000]


@pytest.mark.parametrize('kind', ['LOCAL_POSITION_NED', 'ATTITUDE_QUATERNION', 'SERVO_OUTPUT_RAW', 'RC_CHANNELS'])
def test_missing_channel_not_filled(kind):
    h, p, w = fixture()
    h[kind] = []
    assert select_sample(h,1.2,1.25,w,p,'FBWA',.2) is None


def test_no_interpolation_of_unmatched_physical_ticks():
    h, p, w = fixture()
    h['ATTITUDE_QUATERNION'][0]['message']['time_boot_ms'] = 1201
    assert select_sample(h,1.2,1.25,w,p,'FBWA',.2) is None


def test_wrong_RC_readback_not_accepted_as_applied_command():
    h, p, w = fixture()
    h['RC_CHANNELS'][0]['message']['chan2_raw'] = 1540
    assert select_sample(h,1.2,1.25,w,p,'FBWA',.2) is None


def test_stale_sample_not_relabelled_to_future_grid():
    h, p, w = fixture()
    assert select_sample(h,1.3,1.35,w,p,'FBWA',.3) is None
    assert h['LOCAL_POSITION_NED'][0]['message']['time_boot_ms'] == 1200


@pytest.mark.parametrize('name,field,value', [('LOCAL_POSITION_NED','vx',float('nan')),
    ('ATTITUDE_QUATERNION','q1',2.),('SERVO_OUTPUT_RAW','servo3_raw',0)])
def test_bad_physical_sample_rejected(name,field,value):
    h, p, w = fixture()
    h[name][0]['message'][field] = value
    with pytest.raises(ValueError, match='invalid required'):
        select_sample(h,1.2,1.25,w,p,'FBWA',.2)


def test_invalid_clock_rejected():
    with pytest.raises(ValueError, match='clock'):
        source_time('LOCAL_POSITION_NED', {'message': {'time_boot_ms': -1}})
