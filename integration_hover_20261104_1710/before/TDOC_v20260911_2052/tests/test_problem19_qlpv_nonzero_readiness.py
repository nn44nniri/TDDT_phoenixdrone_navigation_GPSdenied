from pathlib import Path
import os, sys

from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer5 import Layer5Adapter

TDOC=Path(__file__).resolve().parents[1]
BUNDLE=TDOC.parent
L5=BUNDLE/'layer5_v20260911_1510'


def _clear_layer5():
    for k in list(sys.modules):
        if k=='layer5' or k.startswith('layer5.'):
            sys.modules.pop(k,None)


def _cert_v2():
    return {
      'schema':'PTLIB-Phoenix-control-commission-v2','ok':True,'sitl_only':True,
      'physics_held_during_commissioning':True,'direct_pwm':False,
      'mixer':{'validated':True,'elevon_matrix_rad_per_norm':[[0.30,0.20],[-0.30,0.20]],'trim_rad':[0.05,0.05]},
      'trim':{'ready':True,'pitch_request_norm':0.20,'throttle_request_norm':0.35},
      'throttle_path':{'validated':True,'operating_request_norm':0.35,
          'operating_left_motor_norm':0.35,'operating_right_motor_norm':0.35,
          'left_gain_motor_norm_per_request_norm':1.0,'right_gain_motor_norm_per_request_norm':1.0},
      'nominal_tracker_authorized':True,
      'servo_dynamics':{'identified':True,'measured':True,'simulation_only':True,
          'scope':'effective_sitl_command_path','tau_s':[0.02,0.021],
          'rate_limit_rad_s':[1.2,1.1]},
      'esc_mapping':{'identified':True,'measured':True,'simulation_only':True,
          'scope':'effective_sitl_command_path','operating_request_norm':0.35,
          'operating_left_motor_norm':0.35,'operating_right_motor_norm':0.35,
          'left_gain_motor_norm_per_request_norm':1.0,'right_gain_motor_norm_per_request_norm':1.0},
      'qlpv_nonzero_correction_authorized':False,
      'authorization_owner':'Layer5 qLPV kernel + PSF/RTA/Governance',
    }


def test_legacy_qlpv_sitl_certificate_does_not_certify_native_pilot_controller(tmp_path,monkeypatch):
    _clear_layer5()
    monkeypatch.setenv('TDDT_LAYER5_ROOT',str(L5))
    cfg=TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml')
    cfg.data['runtime']['root']=str(tmp_path/'runtime')
    assert cfg.get('layers.layer6.online.transport')=='Layer6Client_GUI_MAVLink'
    a=Layer5Adapter(cfg)
    out=a.apply_online_control_certificate(_cert_v2())
    assert out['ok'] is False, out
    assert out['reason']=='certificate_model_dependencies_missing'
    rd=a.online_control_readiness()
    assert rd['readiness_scope']=='NATIVE_INTERFACE_AND_ACCEPTED_MODEL_PRIOR'
    assert rd['nominal_control_ready'] is False
    assert rd['qlpv_nonzero_correction_ready'] is False
    assert rd['execution_mapping_certified'] is False
    assert rd['mission_executability_authority'] is False
    assert rd['current_phase_release_authority'] is False
    assert 'native_pilot_controller_prediction_unavailable' in rd['flight_release_blockers']
    assert rd['claim_scope']['beyond_sitl_claims_allowed'] is False

