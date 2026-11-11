from pathlib import Path
import os
import pytest
from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

ROOT=Path(__file__).parents[1]

class L5:
    def preflight(self): return {'ok':True}

class ControllerCache:
    def __init__(self): self._external_control_prepared=None

class Client:
    def __init__(self,calls, *, prechecks=None, normal_arms=None, trims=None, health=None, allow_force=True):
        self.calls=calls
        self.prechecks=list(prechecks or [{'ready':True,'telemetry_ready':True,'physics_ready':True,'autopilot_prearm_ready':True}])
        self.normal_arms=list(normal_arms or [{'armed':True}])
        self.trims=list(trims or [{'ready':True}])
        self.health=list(health or [{'ready':True},{'ready':True}])
        self.shell=type('Shell',(),{})()
        self.shell.cfg={
            '_runtime_payload':{
                'mission':{'ground_start':{'latitude':40.01,'longitude':-105.30,'altitude_msl_m':1689.0}},
                'simulation':{'initial_alt_msl_m':1689.0,'heading_deg':25.0,'wind_ned_m_s':[1.0,2.0,0.0]},
                'airframe_profile':{'simulation_assumptions':{'analytic_release_seed':{'airspeed_m_s':13.5}}},
            },
            'safety':{'sitl_only':True,'allow_force_arm_sitl':allow_force},
        }
    def set_initial_geospatial_state(self,**kw): self.calls.append(('set_initial_geospatial_state',kw)); return {'ok':True,**kw}
    def commissioning_health(self,**kw):
        self.calls.append(('commissioning_health',kw)); return self.health.pop(0) if self.health else {'ready':False}
    def precheck(self,timeout_s):
        self.calls.append(('precheck',timeout_s)); return self.prechecks.pop(0) if self.prechecks else {'ready':False}
    def set_mode(self,mode): self.calls.append(('set_mode',mode)); return {'confirmed':True,'mode':mode}
    def set_home(self,**kw): self.calls.append(('set_home',kw)); return {'ok':True,**kw}
    def arm(self,**kw):
        self.calls.append(('arm',kw))
        if kw.get('force'): return {'armed':True,'force':True,'ack_accepted':True}
        return self.normal_arms.pop(0) if self.normal_arms else {'armed':False,'force':False}
    def enable_gnss_denied(self,**kw): self.calls.append(('enable_gnss_denied',kw)); return {'ready':True}
    def acquire_trim(self,**kw):
        self.calls.append(('acquire_trim',kw)); return self.trims.pop(0) if self.trims else {'ready':False}
    def set_environment(self,**kw): self.calls.append(('set_environment',kw)); return {'ok':True,**kw}

class PT:
    def __init__(self,client):
        self.calls=client.calls; self.client=client; self.initial_state_record=None
        self.control_preparation=None; self.external_control_ready=None; self.controller=ControllerCache()
    def probe(self): return {'ok':True}
    def prepare(self): self.calls.append(('prepare',{})); return {'ok':True,'prepared':True}
    def connect(self): self.calls.append(('connect',{})); return {'ok':True,'connected':True}

def adapter(tmp_path):
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    cfg.data['runtime']['root']=str(tmp_path/'runtime')
    pr=tmp_path/'pt_lib_v20260914_1308'; pr.mkdir(parents=True,exist_ok=True)
    os.environ['TDDT_PTLIB_ROOT']=str(pr)
    return Layer6OnlineAdapter(cfg,L5())

def prepare(a,pt,tmp_path,monkeypatch):
    a._pt=pt
    monkeypatch.setattr(a,'_ptlib_log_target',lambda: tmp_path/'logs')
    monkeypatch.setattr(a,'_pin_ptlib_logging',lambda _pt: {'ok':True,'directory':str(tmp_path/'logs')})
    monkeypatch.setattr(a,'_ptlib_log_evidence',lambda _pt=None: {'ok':True})
    a._session_open=False
    a._open_runtime_session(pt)
    return a._execute_tdoc_control_preparation(pt)

def test_native_control_prediction_blocker_prevents_any_startup_arm(tmp_path):
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    a.prepare_online_transport_session=lambda:{'ok':True,'ready':True,'startup_actuation':False}
    result=a.prepare_online_control_session()
    assert result['status']=='NOT_READY'
    assert result['reason']=='native_pilot_controller_prediction_unavailable'
    assert not result['actuation_executed'] and client.sent==[]


def test_retired_ground_arm_does_not_use_a_client_or_status_as_authority():
    from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    def forbidden():
        raise AssertionError('no client, status poll or submit before current D_exec')
    a._get_client=forbidden
    with pytest.raises(RuntimeError,match='ground_arm_requires_current_verified_D_exec'):
        a._prepare_ground_arm()


def test_native_nonready_transport_cannot_prepare_control():
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    a.prepare_online_transport_session=lambda:{'ok':False,'status':'NOT_READY','stage':'fcu_ready','actuation_executed':False}
    out=a.prepare_online_control_session()
    assert not out['ok'] and out['stage']=='fcu_ready'
    assert not out['actuation_executed'] and client.sent==[]


def test_native_session_control_policy_remains_readiness_only():
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    a.prepare_online_transport_session=lambda:{'ok':True,'ready':True,'startup_actuation':False}
    out=a.prepare_online_control_session()
    assert out['transport_ready'] is True and not out['ok']
    assert out['layer5']['flight_release_blockers']==['native_pilot_controller_prediction_unavailable']
    assert client.sent==[]

