"""Native closed-loop boundary tests: no flight, GUI, sockets or knowledge writes."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tddt_tdoc.layer6_bridge import Layer6Bridge, Layer6BridgeError
from tddt_tdoc.online_imu import canonical_imu, measured_intervals

ROOT=Path(__file__).resolve().parents[1]
NATIVE=ROOT.parent/'layer6_v20261007_1032'


def sample(t, ax=1.):
    return {'plant_time_s':t,'frame':'BODY_FRD','source':'MAVLink/SCALED_IMU',
            'gyro_x_rad_s':0.,'gyro_y_rad_s':0.,'gyro_z_rad_s':0.,
            'accel_x_m_s2':ax,'accel_y_m_s2':0.,'accel_z_m_s2':-9.80665}


def ingress(samples):
    last=samples[-1]
    return {'ok':True,'source':'Layer6/GUI/MAVLink','released':True,'imu_samples':samples,
            'state':{**last,'runtime_ready':1.,'imu_time_s':last['plant_time_s'],
                     'imu_frame':last['frame'],'imu_source':last['source'],
                     'latitude_deg':999.,'sim_time_s':999.,'north_m':999.}}


def test_native_imu_ignores_truth_and_uses_imu_clock():
    packet=canonical_imu(ingress([sample(.1)]))
    assert packet['plant_time_s']==.1 and packet['values']['imu_ax']==1.
    assert len(packet['values'])==6
    assert not packet['truth_position_used'] and not packet['gnss_used']


def test_buffer_replays_real_samples_without_large_integration_step():
    data=ingress([sample(i/50,i) for i in range(1,21)])
    packet=canonical_imu(data)
    steps=measured_intervals(data,0.,packet,.15)
    assert len(steps)==20 and sum(dt for dt,_ in steps)==pytest.approx(.4)
    assert max(dt for dt,_ in steps)<.021
    assert [v['imu_ax'] for _,v in steps]==list(range(1,21))


def test_missing_buffer_samples_still_reject_actual_gap():
    data=ingress([sample(.4)])
    with pytest.raises(ValueError,match='interval'):
        measured_intervals(data,0.,canonical_imu(data),.15)


def test_normal_cadence_uses_existing_single_sample_path():
    data=ingress([sample(.1)])
    assert measured_intervals(data,0.,canonical_imu(data),.15) is None


def packet():
    return {'transaction_id':'tx','layer6_package':{'transaction_id':'tx','tau_deadline':time.time()+1}}


def test_intake_ack_without_execution_cancels_latch():
    cancelled=[]
    client=SimpleNamespace(submit=lambda *a,**k:SimpleNamespace(ok=True,transaction_id='tx'),
        gui_status=lambda **k:{},cancel=lambda **k:cancelled.append(k))
    bridge=Layer6Bridge(client,fresh=lambda s:True,confirmed=lambda *args:False)
    with pytest.raises(Layer6BridgeError,match='post-command'):
        bridge.exchange(packet(),before={},timeout_s=.005,segment_s=.1)
    assert cancelled[0]['transaction_id']=='tx'


def test_prediction_and_snapshot_are_forwarded_after_real_segment():
    captured={}
    def build(*args,**kwargs):
        captured.update(kwargs)
        return {'ExecutionEvidence':{}}
    client=SimpleNamespace(submit=lambda *a,**k:SimpleNamespace(ok=True,transaction_id='tx'),
        gui_status=lambda **k:{'command_first_sent_plant_time_s':2.,'plant_time_s':2.1},
        build_layer7_input=build,cancel=lambda **k:SimpleNamespace(ok=True,raw={'ok':True,'cancelled':True}))
    result=Layer6Bridge(client,fresh=lambda s:True,confirmed=lambda *args:True).exchange(
        packet(),before={},timeout_s=.02,segment_s=.09,
        prediction_evidence={'candidate':'real'},network_snapshot={'hash':'snapshot'})
    assert result['layer7_input']['ExecutionEvidence']['execution_segment_closed'] is True
    assert result['layer7_input']['ExecutionEvidence']['remaining_horizon_executed'] is False
    assert captured['prediction_evidence']=={'candidate':'real'}
    assert captured['network_snapshot']=={'hash':'snapshot'}
    assert result['layer7_input']['ExecutionEvidence']['native_execution_segment_s']==pytest.approx(.1)


def test_large_measured_imu_history_round_trip():
    # Current IPC pages raw history; it does not fragment a giant status JSON.
    from test_native_paged_imu_replay import source_case
    from layer6.telemetry_history import encode_response, MAX_RESPONSE_BYTES
    pages, _, _, records=source_case(1000,1000)
    recovered=[]
    for page in pages:
        encoded=encode_response(page)
        assert len(encoded)<=MAX_RESPONSE_BYTES
        recovered.extend(json.loads(encoded)['samples'])
    assert recovered==records[1:]  # First sample is the explicit exclusive cursor.


def test_bound_forcing_hash_rejects_modified_input(tmp_path):
    import hashlib
    from tddt_tdoc.native_binding import verify_bound_input_files
    path=tmp_path/'forcing.csv'
    path.write_text('accepted immutable forcing')
    binding={'input_files':[{'path':str(path),'file_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}
    assert verify_bound_input_files(binding)
    path.write_text('changed')
    with pytest.raises(ValueError):
        verify_bound_input_files(binding)


def test_segment_cancellation_is_transaction_bound_and_required():
    cancelled=[]
    def cancel(**kwargs):
        cancelled.append(kwargs)
        return SimpleNamespace(ok=True,raw={'ok':True,'cancelled':True})
    client=SimpleNamespace(submit=lambda *a,**k:SimpleNamespace(ok=True,transaction_id='tx'),
        gui_status=lambda **k:{'command_first_sent_plant_time_s':2.,'plant_time_s':2.1},
        build_layer7_input=lambda *a,**k:{'ExecutionEvidence':{}},cancel=cancel)
    bridge=Layer6Bridge(client,fresh=lambda s:True,confirmed=lambda *a:True)
    result=bridge.exchange(packet(),before={},timeout_s=.02,segment_s=.09)
    assert cancelled==[{'transaction_id':'tx','reason':'verified_execution_segment_complete'}]
    assert result['status']['plant_time_s']==2.1
    client.cancel=lambda **k:SimpleNamespace(ok=True,raw={'ok':True,'cancelled':False})
    with pytest.raises(Exception,match='latch cancellation not confirmed'):
        bridge.exchange(packet(),before={},timeout_s=.02,segment_s=.09)
