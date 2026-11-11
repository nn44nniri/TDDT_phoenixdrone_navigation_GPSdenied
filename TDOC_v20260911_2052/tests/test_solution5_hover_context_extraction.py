from tddt_tdoc.adapters.layer4 import Layer4Adapter


def test_solution5_extracts_same_cycle_density_soc_and_fingerprint():
    snap={"std":{"representations":{"MicroMetTwin":{"canonical_state":{"air_density_kg_m3":0.958}}},
                 "context":{"offline_plant_state":{"soc":0.993},"effective_climate_fingerprint":"fp"}}}
    c=Layer4Adapter._phoenix_hover_operating_context(snap)
    assert c["air_density_kg_m3"]==0.958
    assert c["battery_soc"]==0.993
    assert c["environment_fingerprint"]=="fp"
    assert c["simulation_only"] is True and c["not_field_evidence"] is True


def test_vertical_phase_exports_mesa_local_guidance_without_actuation_authority():
    snap={"std":{"representations":{
        "MicroMetTwin":{"canonical_state":{"air_density_kg_m3":0.958}},
        "MESA_NAV":{"canonical_state":{
            "position_ned_m":[4428907.0,474393.0,-1800.4],
            "ground_velocity_ned_m_s":[-1.9,-1.2,-0.7]}}},
        "context":{"stage":"vertical_takeoff",
            "online_phase_reference_targets":{
                "vertical_takeoff":[474395.8,4428910.2,1802.8]}}}}
    c=Layer4Adapter._phoenix_hover_operating_context(snap)
    guidance=c["mesa_local_guidance"]
    assert guidance["schema"]=="TDOC-MESA-local-guidance-v1"
    assert guidance["frame"]=="PROJECTED_NED"
    assert guidance["position_ned_m"]==[4428907.0,474393.0,-1800.4]
    assert guidance["target_position_ned_m"]==[4428910.2,474395.8,-1802.8]
    assert guidance["navigation_authority"]=="MESA_STD"
    assert guidance["truth_injection_to_mesa"] is False
    assert guidance["actuation_authority"] is False


def test_native_static_context_preserves_live_provenance_without_self():
    state={'qw':1.,'qx':0.,'qy':0.,'qz':0.,'p_rate':0.,'q_rate':0.,'r_rate':0.,
           'omega_left':0.,'omega_right':0.,'kinematics_source':'Layer6Client.GUI.telemetry'}
    snap={'std':{'context':{'online_execution_state':state}}}
    context=Layer4Adapter._phoenix_hover_operating_context(snap)
    assert context['attitude_rate_state']['source']=='Layer6/GUI/MAVLink current simulation state'
    assert context['motor_speed_state']['source']=='Layer6/GUI/MAVLink current simulation state'
    assert context['motor_speed_state']['actuation_authority'] is False
    state['kinematics_source']='PT_LIB/FMI'
    assert Layer4Adapter._phoenix_hover_operating_context(snap)['attitude_rate_state']['source']=='PT_LIB/FMI current simulation state'
