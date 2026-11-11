from pathlib import Path
from types import SimpleNamespace
from tddt_tdoc.adapters.layer2 import Layer2Adapter

BASE=0.8208448785

def test_solution7_hover_certificate_is_model_airframe_climate_state_action_bound(tmp_path):
    root=Path(__file__).resolve().parents[2]/'twins_layer2_v20260820_1233'
    a=Layer2Adapter.__new__(Layer2Adapter)
    a.config=SimpleNamespace(get=lambda key,default=None:default)  # Standalone qualification unit.
    a.twins_root=root; a.wing=SimpleNamespace(); a._ensure=lambda: None
    def integrate(_wing,state,action,**kw):
        st=dict(state); st.update({'vn':13.0,'ve':0.0,'vd':0.0,'p_rate':0.0,'q_rate':0.0,'r_rate':0.0})
        return st,{'vtol_feasible':True}
    a._wing27_integrate=integrate
    physics={
      'physics_derived':True,'source_motor_law':True,'force_balance_valid':True,'hover_force_balance_valid':True,
      'air_density_context_valid':True,'battery_context_valid':True,'environment_fingerprint':'epoch-A',
      'hover_state_envelope':{'airspeed_m_s':[0.0,9.0],'air_density_kg_m3':[0.7,1.35],'battery_soc':[0.2,1.0]},
      'hover_action_envelope':{'throttle_delta_norm':0.04,'surface_trim_rad':[0.0,0.0]},
      'pilot_to_motor_mapping':{'schema':'Phoenix-pilot-to-motor-normalization-v1','motor_norm_gain':1.0,'motor_norm_offset':0.0},
      'vertical_landing_physics_basis':{'schema':'Phoenix-vertical-landing-physics-basis-v1','phase':'vertical_landing','physics_derived':True,'source_motor_law':True,'landing_force_reference_valid':True,'air_density_context_valid':True,'battery_context_valid':True,'terminal_agl_corridor_valid':True,'terminal_agl_corridor_authority':'Layer4 verified landing corridor','descent_rate_target_m_s':1.0,'max_collective_reduction_norm':0.08,'max_braking_boost_norm':0.06,'pilot_to_motor_mapping':{'schema':'Phoenix-pilot-to-motor-normalization-v1','motor_norm_gain':1.0,'motor_norm_offset':0.0}}}
    seeds={'ok':True,'physics_basis':physics,'phases':{
      'vertical_takeoff':{'throttle_request_norm':BASE,'operating_point_basis':'phoenix_source_motor_law_force_balance'},
      'hover':{'throttle_request_norm':BASE,'operating_point_basis':'phoenix_source_motor_law_hover_force_balance'},
      'vertical_landing':{'throttle_request_norm':BASE,'operating_point_basis':'phoenix_source_motor_law_landing_force_balance_reference'}}}
    cert=a.derive_offline_phoenix_model_certificate('profile-A',phase_seed_controls=seeds,airframe_binding_sha256='airframe-A')
    hp=cert['phase_throttle_paths']['hover']
    assert cert['schema']=='Layer5-Phoenix-offline-model-certificate-v4'
    assert hp['certificate_schema']=='Layer5-Phoenix-hover-command-map-v2'
    assert hp['bound_phase']=='hover'
    assert hp['phoenix_profile_sha256']=='profile-A'
    assert hp['airframe_binding_sha256']=='airframe-A'
    assert len(hp['wingmpet_fmu_sha256'])==64
    assert hp['climate_fingerprint']=='epoch-A'
    assert hp['state_envelope']['airspeed_m_s']==[0.0,9.0]
    assert hp['action_envelope']['pilot_throttle_norm'][0] > 0.05
    assert hp['legacy_hover_operating_point_invalidated'] is True
