from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer3 import Layer3Adapter


def test_wing_simulation_packet_uses_mission_time_and_preserves_local_fmi_time():
    p={'twin_id':'WingMPET','timestamp':227.619,'state':{'airspeed_norm':13.0},'provenance':{}}
    out=Layer2Adapter._bind_wing_simulation_packet_to_mission_time(
        p,reference_time_s=153.001,learning_mode='OFFLINE_SE')
    assert out['timestamp'] == 153.001
    rec=out['provenance']['tdoc_wing_mission_time_alignment']
    assert rec['local_fmi_timestamp_s'] == 227.619
    assert rec['mission_reference_time_s'] == 153.001
    assert rec['not_field_evidence'] is True


def test_wing_non_simulation_packet_is_not_relabelled():
    p={'twin_id':'WingMPET','timestamp':227.619,'provenance':{}}
    out=Layer2Adapter._bind_wing_simulation_packet_to_mission_time(
        p,reference_time_s=153.001,learning_mode='FIELD')
    assert out['timestamp'] == 227.619
    assert 'tdoc_wing_mission_time_alignment' not in out['provenance']


def test_post_coupling_wing_timestamp_is_rebound_for_simulation():
    class L2:
        _bind_wing_simulation_packet_to_mission_time=staticmethod(Layer2Adapter._bind_wing_simulation_packet_to_mission_time)
    p={'WingMPET':{'twin_id':'WingMPET','timestamp':227.619,'state':{},'provenance':{}}}
    out=Layer3Adapter._rebind_wing_after_layer3_couplings(
        p,reference_time_s=153.001,learning_mode='OFFLINE_SE',layer2_adapter=L2())
    assert out['WingMPET']['timestamp'] == 153.001
    assert out['WingMPET']['provenance']['tdoc_post_layer3_coupling_wing_time_rebind']['simulation_only'] is True
