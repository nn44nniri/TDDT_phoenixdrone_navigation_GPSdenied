"""Native execution migration contracts. No GUI, network, or flight is started."""
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT.parent/'layer6_v20261007_1032'

class Config:
    package_root = ROOT
    def __init__(self, overrides=None): self.overrides=overrides or {}
    def path_value(self,key): return NATIVE if key=='TDDT_LAYER6_ROOT' else None
    def get(self,key,default=None): return self.overrides.get(key,default)


def status(tx=None, sent=None):
    now=time.monotonic()
    return {'mavlink_connected':True,'mode':'QHOVER','armed':False,
            'latitude_deg':40.,'longitude_deg':-105.,'altitude_msl_m':1900.,
            'airspeed_m_s':0.,'groundspeed_m_s':0.,'heading_deg':0.,
            'plant_time_s':1.101 if tx else 1.,'attitude':{'roll':0.,'pitch':0.,'yaw':0.},
            'applied_transaction_id':tx,'manual_sent_monotonic':sent,
            'command_first_sent_monotonic':sent,'command_first_sent_plant_time_s':1.0 if tx else None,
            'status_quality':{'native_session_id':'mock-native-session'},
            'telemetry_age_s':{k:0. for k in ('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT','SERVO_OUTPUT_RAW')},
            'telemetry_received_monotonic':{'ATTITUDE':now,'SERVO_OUTPUT_RAW':now},
            'applied_pilot_command':{'aileron':0.,'elevator':0.,'throttle':0.,'rudder':0.}}

class Client:
    def __init__(self,confirm=True):
        self.sent=[]; self.confirm=confirm; self.cancelled=[];self._owned_native_session=None
    def gui_status(self,**kwargs):
        return status('cycle-1',time.monotonic()-0.01) if self.sent and self.confirm else status()
    def cancel(self,**kwargs):
        self.cancelled.append(kwargs)
        return SimpleNamespace(ok=True,raw={'cancelled':True,'transaction_id':kwargs.get('transaction_id')})
    def native_session_status(self):
        return {'process_ready':True,'session_id':'mock-native-session'}
    def submit(self,packet,**kwargs):
        self.sent.append(packet)
        return SimpleNamespace(ok=True,transaction_id='cycle-1',raw={})
    def build_layer7_input(self,packet,**kwargs):
        from layer6.feedback import build_layer7_input
        return build_layer7_input(packet,kwargs['status'])
    def close(self): pass


def adapter(confirm=True,*,verified=False):
    # Exercise the legacy observation bridge with explicitly synthetic statuses.
    # Live-session enforcement is covered by test_online_native_health; these
    # mock values must never be treated as a closed_loop_sitl Native session.
    config=Config({'layers.layer6.online.execution_confirmation_timeout_s':0.04,
                   'layers.layer6.online.execution_profile':'legacy_unit_fixture'})
    layer5=SimpleNamespace(online_control_readiness=lambda:{'ok':True})
    if verified:
        layer5.verify_native_execution_evidence=lambda *a,**k:{'ok':True,'native_execution_model_matched':True}
    a=Layer6OnlineAdapter(config,layer5)
    a._module()
    a._client=Client(confirm)
    a._session_open=True
    return a


def payload(final=True):
    now=time.time()
    return {'learning_mode':'ONLINE_JSBSIM','transaction_id':'cycle-1',
            'layer5_output':{'result':{'gates':{'FINAL':final},'layer6_package':{
                'u_cmd':[0,0,0,0], 'C_exec':{'direct_pwm':False}, 'S_safe':{'psf':True,'rta':True},
                'tau_apply':now-0.01,'tau_deadline':now+5,
                'execution_intent':{'requested_pilot_mode':'QHOVER','discrete':[]}}}}}


def test_preflight_reports_native_api_gaps_without_retired_roots(monkeypatch):
    monkeypatch.delenv('TDDT_PTLIB_ROOT',raising=False)
    monkeypatch.delenv('EXT_FMU_SIM_ROOT',raising=False)
    report=Layer6OnlineAdapter(Config(),SimpleNamespace()).preflight()
    probe=report['layer6_client']
    assert report['ok'] is True and probe['runtime_started'] is False
    assert probe['missing_api']==[]
    assert probe['signature_mismatches']==[]
    assert probe['expected_version']==probe['actual_version']=='2026.10.7.0937'


def test_hold_does_not_submit():
    a=adapter()
    result=a.invoke(payload(False))
    assert result['ok'] and result['actuation_executed'] is False
    assert a._client.sent==[]


def test_ack_without_execution_correlation_fails_closed():
    a=adapter(False)
    result=a.invoke(payload())
    assert result['ok'] is False and result['actuation_executed'] is False
    # Stage-11 policy: infrastructure readiness / forged FINAL is not native
    # controller evidence. Reject before intake rather than waiting for ACK.
    assert a._client.sent==[]
    assert 'native_execution_model_not_verified' in result['error']


def test_correlated_fresh_execution_produces_evidence():
    a=adapter(verified=True)
    result=a.invoke(payload())
    assert result['status']=='EXECUTED'
    ex=result['execution_evidence']
    assert ex['source']=='Layer6/GUI/MAVLink' and ex['evidence_type']=='simulation'
    assert ex['u_applied_pilot']['throttle']==0.


@pytest.mark.parametrize('mutation', ['expired','direct_pwm','safety','range','missing_time','nan_time','alias_conflict','unknown_mode','missing_safety'])
def test_invalid_command_never_enters_intake(mutation):
    a=adapter(verified=True); p=payload(); cmd=p['layer5_output']['result']['layer6_package']
    if mutation=='expired': cmd['tau_deadline']=time.time()-1
    elif mutation=='direct_pwm': cmd['C_exec']['direct_pwm']=True
    elif mutation=='safety': cmd['S_safe']['psf']=False
    elif mutation=='range': cmd['u_cmd'][2]=2
    elif mutation=='nan_time': cmd['tau_deadline']=float('nan')
    elif mutation=='alias_conflict': cmd['command']=[0,0,1,0]
    elif mutation=='unknown_mode': cmd['execution_intent']['requested_pilot_mode']='UNKNOWN'
    elif mutation=='missing_safety': cmd['S_safe']={}
    else: del cmd['tau_deadline']
    assert a.invoke(p)['actuation_executed'] is False
    assert a._client.sent==[]


def test_old_telemetry_and_wrong_transaction_are_not_execution():
    a=adapter(); before=status(); p=payload()['layer5_output']['result']['layer6_package']
    st=status('other',time.monotonic())
    assert not a._confirmed(st,before,p,'cycle-1')
    st=status('cycle-1',time.monotonic()-0.01); st['telemetry_age_s']['ATTITUDE']=20.
    assert not a._confirmed(st,before,p,'cycle-1')


def load_prepare():
    spec=importlib.util.spec_from_file_location('native_prepare_test',ROOT/'scripts/prepare_all.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_refresh_preserves_only_three_twin_fmus_and_native_builds(tmp_path):
    m=load_prepare(); twins=tmp_path/'twins'; climate=tmp_path/'climate'
    native=tmp_path/'layer6'; (native/'FlightStack/build').mkdir(parents=True)
    for root,paths in ((twins,['fmus/MESA_NAV.fmu','fmus/WingMPET.fmu','build/temp']),
                       (climate,['fmu/MicroMetTwin.fmu','build/temp'])):
        for rel in paths:
            p=root/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b'keep')
    report={'stages':[],'_total':1,'ok':True}
    rec=m.cleanup_fmu_build_caches(report,twins,climate)
    assert rec['ok'] and len(rec['preserved_final_fmus'])==3
    assert not (twins/'build').exists() and not (climate/'build').exists()
    assert (native/'FlightStack/build').is_dir()
    assert 'TDDT_PTLIB_ROOT' not in m.REQUIRED_ROOTS
    assert not hasattr(m,'build_execution_fmus')


def test_offline_route_rejects_layer6():
    a=adapter()
    with pytest.raises(ValueError): a.invoke({'learning_mode':'OFFLINE_SE'})


def test_native_execution_reaches_layer7_trigger():
    from tddt_tdoc.orchestrator import _layer7_trigger_eligibility
    result=adapter(verified=True).invoke(payload())
    gate=_layer7_trigger_eligibility(layer6_output=result,execution_evidence=result['execution_evidence'],
                                    layer7_preflight={'ok':True},request={})
    assert gate['realized_execution'] and gate['eligible']


def test_native_observation_only_has_no_execution_authority():
    from tddt_tdoc.orchestrator import _layer7_trigger_eligibility
    p=payload(False); p['request']={'online_observation_only':True}
    result=adapter().invoke(p)
    gate=_layer7_trigger_eligibility(layer6_output=result,execution_evidence=result['execution_evidence'],
                                    layer7_preflight={'ok':True},request=p['request'])
    assert gate['validation_observation'] and not gate['realized_execution']


def test_final_cannot_reuse_rejected_online_binding():
    a=adapter(); a.layer5.online_control_readiness=lambda:{'ok':False,'blockers':['retired_backend']}
    result=a.invoke(payload())
    assert not result['ok'] and not result['actuation_executed']
    assert a._client.sent==[]


def test_native_ground_boundary_uses_current_telemetry():
    from tddt_tdoc.rtmo.capability_gate import online_on_ground_boundary_evidence
    st=status(); st.update(landed_state='ON_GROUND',relative_altitude_m=0.,
                          velocity_n_m_s=0.,velocity_e_m_s=0.,velocity_d_m_s=0.)
    prep={'ok':True,'ready':True,'transport':'Layer6Client_GUI_MAVLink','current_gui_state':st}
    assert online_on_ground_boundary_evidence(prep)['supported']
    st['landed_state']='IN_AIR'
    assert not online_on_ground_boundary_evidence(prep)['supported']


def test_native_snapshot_position_retains_units_and_plant_clock():
    st=status(); st['relative_altitude_m']=5.0
    snap=Layer6OnlineAdapter._snapshot(st)
    assert snap['relative_altitude_m']==5.0
    assert snap['groups']['POSITION']['payload']['relative_alt']==5000.0
    assert snap['sim_time_s']==1.0


def test_normal_heartbeat_cadence_does_not_invalidate_fast_motion_telemetry():
    a=adapter();a.config.overrides['layers.layer6.online.telemetry_max_age_s']=.2
    st=status();st['telemetry_age_s']['HEARTBEAT']=.9
    assert a._fresh(st)
    st['telemetry_age_s']['ATTITUDE']=.3
    assert not a._fresh(st)


def test_hold_cancels_service_latch_not_only_gui_state():
    a=adapter();a._active_tx='cycle-previous'
    result=a.invoke(payload(False))
    assert result['actuation_executed'] is False
    assert a._client.cancelled[0]['transaction_id']=='cycle-previous'
    assert a._active_tx is None


def test_online_preparation_starts_native_stack_before_mavlink_and_fcu_readiness():
    events=[]
    a=adapter();client=a._client
    client.stack_responding=lambda:False
    def started(**kwargs):
        events.append('start_native_terminals')
        client._owned_native_session='mock-native-session'
    client.ensure_stack_started=started
    client.ensure_mavlink=lambda **kwargs:events.append('ensure_mavlink')
    def ready(**kwargs):
        events.append('wait_fcu_ready')
        return status()
    client.wait_fcu_ready=ready
    result=a.prepare_online_transport_session()
    assert result['ok']
    assert events==['start_native_terminals','ensure_mavlink','wait_fcu_ready']
    assert a._owns_stack and client.sent==[]  # Startup grants no ARM/flight command.


def test_running_native_stack_is_reused_without_restarting_terminals():
    a=adapter();client=a._client;events=[]
    client.stack_responding=lambda:True
    client.ensure_stack_started=lambda **kwargs:pytest.fail('running stack must not restart')
    client.ensure_mavlink=lambda **kwargs:events.append('ensure_mavlink')
    client.wait_fcu_ready=lambda **kwargs:status()
    assert a.prepare_online_transport_session()['ok']
    assert events==['ensure_mavlink'] and not a._owns_stack


def test_knowledge_binding_failure_never_launches_stack(monkeypatch):
    from tddt_tdoc import native_environment
    a=adapter();a.config.overrides['layers.layer6.online.require_bound_environment']=True
    def reject(*args,**kwargs):raise ValueError('Offline forcing identity mismatch')
    monkeypatch.setattr(native_environment,'prepare_binding',reject)
    a._client.stack_responding=lambda:pytest.fail('knowledge must be checked before startup')
    result=a.prepare_online_transport_session()
    assert not result['ok'] and 'forcing identity mismatch' in result['error']


def test_canonical_config_enables_native_stack_startup():
    from tddt_tdoc.config import TDOCConfig
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    assert cfg.get('layers.layer6.online.start_if_needed') is True
