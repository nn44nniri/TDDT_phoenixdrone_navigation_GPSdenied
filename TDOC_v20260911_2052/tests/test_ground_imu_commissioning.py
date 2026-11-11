from copy import deepcopy
import pytest
from test_native_imu_retention import setup_reader,current_status,prepare
from test_r5_01_telemetry_receivers import wire_message
from tddt_tdoc.online_imu import prepare_ground_calibration,initial_ground_attitude


def measured_calibration():
    link,_,reader=setup_reader();before=current_status(link)
    start=before['plant_time_s'];stamp=int(start*1000)
    for batch in range(11):
        link.master.queue=[wire_message('SCALED_IMU',changes={'time_boot_ms':stamp+20*(batch*25+i+1),
            'xacc':0,'yacc':0,'zacc':-1000,'xgyro':0,'ygyro':-2,'zgyro':0}) for i in range(25)]
        link.drain();reader.poll_once()
    after=current_status(link)
    for s in (before,after):s.update(armed=False,throttle=0.,landed_state='ON_GROUND',timestamp_monotonic=s['now'])
    pages=reader.window(reader.anchor,reader.last_primary)
    history=prepare(reader,pages,reader.anchor)
    return history,[before,after]


def test_static_wire_imu_initializes_existing_bias_inputs_without_altering_samples():
    history,statuses=measured_calibration();original=deepcopy(history.samples)
    cal=prepare_ground_calibration(history,statuses)
    ingress={'ok':True,'released':True,'source':'Layer6/GUI/MAVLink','gui_state':statuses[-1],
        'state':{'runtime_ready':1.,**{k:statuses[-1]['imu'][k] for k in statuses[-1]['imu']},
                 'imu_source':'MAVLink/SCALED_IMU','imu_frame':'BODY_FRD','imu_time_s':statuses[-1]['plant_time_s']},
        'native_ground_calibration':cal}
    # canonical_imu consumes the explicit public SI state names.
    init={'pn':1.,'pe':2.,'pd':-3.,'vn':0.,'ve':0.,'vd':0.,'qw':1.,'qx':0.,'qy':0.,'qz':0.}
    aligned,evidence=initial_ground_attitude(ingress,init)
    assert aligned['native_imu_bias_initialization']['bgy']==pytest.approx(-.002)
    assert all(aligned[k]==init[k] for k in ('pn','pe','pd','vn','ve','vd'))
    assert history.samples==original
    assert not evidence['ground_calibration']['actuation_authority']
    assert deepcopy(cal) is cal
    import json
    from tddt_tdoc.adapters.layer2 import Layer2Adapter
    encoded=Layer2Adapter._packet({'context':{'calibration':cal}})
    assert json.loads(json.dumps(encoded))['context']['calibration']['samples_sha256']==cal.samples_hash
    with pytest.raises(AttributeError):cal.means=(0.,)*6
    ingress['native_ground_calibration']=cal.to_dict()
    with pytest.raises(ValueError,match='local ground calibration'):initial_ground_attitude(ingress,init)


@pytest.mark.parametrize('fault',['armed','airborne','throttle','session','short'])
def test_ground_calibration_rejects_invalid_lifecycle_or_interval(fault):
    history,statuses=measured_calibration()
    if fault=='armed':statuses[-1]['armed']=True
    elif fault=='airborne':statuses[-1]['landed_state']='IN_AIR'
    elif fault=='throttle':statuses[-1]['throttle']=.1
    elif fault=='session':statuses[-1]['session_id']='c'*32
    with pytest.raises(ValueError):prepare_ground_calibration(history,statuses,duration_s=10. if fault=='short' else 5.)
