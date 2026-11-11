from pathlib import Path
import os
from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

ROOT=Path(__file__).parents[1]

class L5:
    def preflight(self): return {'ok':True}

class Client:
    def __init__(self,calls):
        self.calls=calls
        self.shell=type('Shell',(),{})()
        self.shell.cfg={
            '_runtime_payload':{
                'mission':{
                    'airborne_control_start':{'latitude':40.01,'longitude':-105.30,'altitude_msl_m':1701.0},
                    'ground_start':{'latitude':40.01,'longitude':-105.30,'altitude_msl_m':1689.0},
                },
                'simulation':{'initial_alt_msl_m':1701.0,'heading_deg':25.0,'wind_ned_m_s':[1.0,2.0,0.0]},
                'airframe_profile':{'simulation_assumptions':{'analytic_release_seed':{'airspeed_m_s':13.5}}},
            },
            'safety':{'sitl_only':True,'allow_force_arm_sitl':False},
        }
    def set_initial_geospatial_state(self,**kw): self.calls.append('set_initial_geospatial_state'); return {'ok':True,**kw}
    def precheck(self,timeout_s): self.calls.append('precheck'); return {'ready':True}
    def set_mode(self,mode): self.calls.append('set_mode'); return {'confirmed':True,'mode':mode}
    def set_home(self,**kw): self.calls.append('set_home'); return {'ok':True,**kw}
    def arm(self,**kw): self.calls.append('arm'); return {'armed':True}
    def enable_gnss_denied(self,**kw): self.calls.append('enable_gnss_denied'); return {'ready':True}
    def acquire_trim(self,**kw): self.calls.append('acquire_trim'); return {'ready':True}
    def set_environment(self,**kw): self.calls.append('set_environment'); return {'ok':True,**kw}

class PT:
    def __init__(self): self.calls=[]; self.client=None; self.initial_state_record=None; self.control_preparation=None; self.external_control_ready=None
    def probe(self): return {'ok':True}
    def prepare(self): self.calls.append('prepare'); self.client=Client(self.calls); return {'ok':True,'prepared':True}
    def connect(self):
        self.calls.append('connect')
        assert self.initial_state_record and self.initial_state_record['applied'] is True
        return {'ok':True,'connected':True}


def adapter(tmp_path):
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    cfg.data['runtime']['root']=str(tmp_path/'runtime')
    # Native fixture does not activate the retired PT_LIB environment root.
    return Layer6OnlineAdapter(cfg,L5())


def test_native_control_session_is_canonical_and_fail_closed(tmp_path):
    a=adapter(tmp_path)
    assert a.SOURCE == 'Layer6/GUI/MAVLink'
    assert a.MODES == ('ONLINE_JSBSIM',)
    assert not hasattr(a,'_online_guidance_policy')
    assert not hasattr(a,'_open_runtime_session')
    assert not hasattr(a,'_make_ptlib')
    assert a._option('transport',None) is None or a.config.get('layers.layer6.online.transport')=='Layer6Client_GUI_MAVLink'
    assert a.config.get('layers.layer6.online.direct_pwm') is False
    assert a.config.get('layers.layer6.online.execution_rate_hz')==30.0



def test_native_preparation_checks_control_prediction_before_ground_arm(tmp_path,monkeypatch):
    a=adapter(tmp_path)
    calls=[]
    monkeypatch.setattr(a,'prepare_online_transport_session',lambda: calls.append('transport') or
        {'ok':True,'status':'READY','ready':True,'startup_actuation':False})
    class Model:
        def online_control_readiness(self):
            calls.append('layer5_readiness')
            return {'ok':True,'nominal_control_ready':False,
                    'flight_release_blockers':['native_pilot_controller_prediction_unavailable']}
    a.layer5=Model()
    monkeypatch.setattr(a,'_prepare_ground_arm',lambda: calls.append('ARM') or {'ok':True})
    result=a.prepare_online_control_session()
    assert calls==['transport','layer5_readiness']
    assert result['status']=='NOT_READY' and result['transport_ready'] is True
    assert result['reason']=='native_pilot_controller_prediction_unavailable'
    assert result['actuation_executed'] is False
    assert result['startup_actuation'] is False

