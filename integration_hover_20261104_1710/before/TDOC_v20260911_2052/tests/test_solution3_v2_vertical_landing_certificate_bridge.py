from pathlib import Path
from types import MethodType, SimpleNamespace
import yaml

from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _real_seed():
    root=Path(__file__).resolve().parents[2]/'layer5_v20260911_1510'
    ph=PhoenixSpecialization(Layer5Config.from_yaml(root/'configs/configuration_phoenix.yaml'))
    return ph.offline_vtol_seed_controls({'air_density_kg_m3':0.958,'battery_soc':0.993,
        'environment_fingerprint':'solution3-v2','vertical_landing_descent_rate_ref_m_s':1.0})


def _fake_certificate(tmp_path:Path, seed):
    twins=tmp_path/'twins'; (twins/'config').mkdir(parents=True); (twins/'fmus').mkdir(parents=True)
    (twins/'config/phoenix_drone_v3.yaml').write_text(yaml.safe_dump({'control_limits':{'elevon_joint_limit_rad':0.53}}))
    (twins/'fmus/WingMPET.fmu').write_bytes(b'fake-wing-fmu')
    a=Layer2Adapter.__new__(Layer2Adapter); a.twins_root=twins; a.wing=object(); a._ensure=lambda:None
    def fake_integrate(self, wing, state, action, **kwargs):
        out=dict(state); out.update({'vn':13.0,'ve':0.0,'vd':0.0,'p_rate':0.0,'q_rate':0.0,'r_rate':0.0})
        return out, {'ok':True,'vtol_feasible':True}
    a._wing27_integrate=MethodType(fake_integrate,a)
    cert=a.derive_offline_phoenix_model_certificate(phoenix_profile_sha256='profile-A',phase_seed_controls=seed,airframe_binding_sha256='airframe-A')
    return cert


def _bridge(cert):
    a=Layer5Adapter.__new__(Layer5Adapter); a.config=SimpleNamespace(get=lambda key, default=None: 'OFFLINE_SE' if key == 'layers.layer6.online.transport' else default)
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    a._ensure=lambda:None
    a.phoenix_profile_sha256=lambda:'profile-A'
    a.phoenix_airframe_binding_sha256=lambda:'airframe-A'
    a.wingmpet_fmu_sha256=lambda:cert.get('wingmpet_fmu_sha256')
    a._verification_model_certificate=cert
    return a


def test_landing_certificate_producer_and_bridge_share_same_physics_contract(tmp_path):
    seed=_real_seed(); cert=_fake_certificate(tmp_path,seed)
    assert cert['ok'] is True
    lp=cert['phase_throttle_paths']['vertical_landing']
    assert lp['model_qualified'] is True
    assert lp['certificate_schema']=='Layer5-Phoenix-vertical-landing-command-map-v1'
    assert lp['bound_phase']=='vertical_landing'
    assert isinstance(lp['physics_basis'],dict)
    lb=lp['physics_basis']['vertical_landing_physics_basis']
    assert lb['schema']=='Phoenix-vertical-landing-physics-basis-v1'
    assert lb['terminal_agl_corridor_valid'] is True
    operating=lp['operating_request_norm']
    out=_bridge(cert)._wingmpet_verification_mix([0.0,0.0,operating,0.0],phase='vertical_landing')
    assert out['ok'] is True
    assert out['phase']=='vertical_landing'
    assert out['phase_throttle_qualification']=='vertical_landing_command_mapping_only'
    assert out['physical']==[0.0,0.0,operating,operating]


def test_landing_bridge_remains_fail_closed_when_landing_basis_missing(tmp_path):
    seed=_real_seed(); cert=_fake_certificate(tmp_path,seed)
    cert['phase_throttle_paths']['vertical_landing']['physics_basis'].pop('vertical_landing_physics_basis')
    operating=cert['phase_throttle_paths']['vertical_landing']['operating_request_norm']
    out=_bridge(cert)._wingmpet_verification_mix([0.0,0.0,operating,0.0],phase='vertical_landing')
    assert out['ok'] is False
    assert out['reason']=='wingmpet_verification_vertical_landing_physics_basis_missing'
