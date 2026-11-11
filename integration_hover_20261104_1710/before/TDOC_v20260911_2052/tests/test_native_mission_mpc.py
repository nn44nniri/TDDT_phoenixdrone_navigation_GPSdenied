"""Real Layer5 MPC/safety gates feed the explicit process-local SITL assembly.

The existing Layer4 fixture is a unit-test input, never Native flight evidence.
Only live-status/forcing inputs are simulated; solver, PSF, RTA, governance,
SETD storage and the Layer5 assembler are the actual implementations.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from layer5.config import Layer5Config
from layer5.engine import Layer5Engine
from layer5.knowledge import KStore
from layer6.status import project_status
from layer6.telemetry_source import native_source_contract
from test_r5_01_telemetry_receivers import wire_message
from test_r5_04_gui_status import make_link
from tddt_tdoc import TDOCConfig
from tddt_tdoc.native_mission import NativeMissionBinding, IDENTITY_KEYS
from tddt_tdoc.utils import sha256_obj

ROOT=Path(__file__).parents[1]
L5=ROOT.parent/'layer5_v20260911_1510'
ORIGINAL=json.loads((L5/'examples/layer4_result_verified_TEST_FIXTURE.json').read_text())


def setup_case(tmp_path,monkeypatch,*,phase='vertical_takeoff',armed=False,commit=True):
    store=tmp_path/'kstore'
    monkeypatch.setenv('TDDT_KSTORE_ROOT',str(store))
    config=yaml.safe_load((L5/'configs/configuration_phoenix.yaml').read_text())
    config.setdefault('testing',{})['allow_fixture_verification']=True
    config['confidence']['auto_threshold']=0.
    config['confidence']['auto_risk_max']=.2
    config['knowledge']['directory']=str(store)
    c=Layer5Config(config,L5/'configs/configuration_phoenix.yaml')
    source=deepcopy(ORIGINAL)
    for candidate in source['candidates']:candidate['tdoc_phase']=phase
    offline=Layer5Engine(c).run(source,'OFFLINE_SE').to_dict()
    assert offline['gates']['FINAL'],offline['gates']
    if commit:
        assert KStore(c).finalize({'source':'MPC unit test prior'})['committed']
    tdoc=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    tdoc.data['layers']['layer6']['online']['execution_profile']='closed_loop_sitl'
    def prior():
        value=KStore(c).load_setd()
        return {'ok':value.get('ok') is True,'handoff_hash':sha256_obj(value.get('knowledge'))}
    env={k:'unit-'+k for k in IDENTITY_KEYS}
    env.update(ok=True,handoff_hash=prior()['handoff_hash'],plant_epoch_s=0.)
    sender=native_source_contract()
    def status():
        rows=[wire_message('HEARTBEAT',changes={'autopilot':3,'type':1,'custom_mode':18,'base_mode':128 if armed else 0}),
            wire_message('SCALED_IMU',changes={'time_boot_ms':1000}),
            wire_message('ATTITUDE_QUATERNION',changes={'time_boot_ms':1000}),
            wire_message('ATTITUDE',changes={'time_boot_ms':1000}),
            wire_message('GLOBAL_POSITION_INT',changes={'time_boot_ms':1000}),
            wire_message('VFR_HUD'),wire_message('SYS_STATUS'),
            wire_message('EKF_STATUS_REPORT',changes={'flags':1|2|4|16|32}),
            wire_message('SERVO_OUTPUT_RAW'),wire_message('EXTENDED_SYS_STATE',changes={'landed_state':1})]
        link=make_link(sender,rows);p=project_status(link)
        return {**p,'schema':'Layer6-GUI-status-v1','schema_version':1,'ok':True,
            'mavlink_connected':True,'target_system':1,'target_component':1,
            'monotonic_domain_id':p['domain'],'telemetry_metadata':p['metadata'],
            'telemetry_age_s':p['ages'],'throttle':0.,'environment_contract':deepcopy(env),
            'status_quality':{'native_session_id':'b'*32}}
    binding=NativeMissionBinding(tdoc,status_source=status,prior_validator=prior,
        environment_validator=lambda st:st['environment_contract']==env)
    engine=Layer5Engine(c)
    engine.native_mission_binding=binding
    engine.native_status_source=status
    return engine,source,offline,binding,c,status


@pytest.mark.parametrize('phase,armed,mode,actions',[
    ('vertical_takeoff',False,'QHOVER',['ARM']),
    ('hover',True,'QHOVER',[]),('cruise',True,'FBWA',[])])
def test_real_mpc_first_move_reaches_layer5_assembly_without_replacement(tmp_path,monkeypatch,phase,armed,mode,actions):
    engine,source,offline,binding,_,_=setup_case(tmp_path,monkeypatch,phase=phase,armed=armed)
    assert binding.operational_readiness()['ok']
    result=engine.run(source,'ONLINE_JSBSIM',human_approval=True,requested_mode='AUTO').to_dict()
    assert result['gates']['FINAL'],result['gates']
    chosen=result['decision']['candidate_id']
    actual=result['gates']['solver'][chosen]['corrected_actions'][0]
    package=result['layer6_package']
    assert package['u_cmd']==result['control_action']['first_move']==actual==offline['control_action']['first_move']
    assert package['execution_intent']=={'requested_pilot_mode':mode,'discrete':actions}
    assert result['gates']['psf']['gate'] and result['gates']['rta']['source']=='advanced'
    assert result['gates']['governance']['gate'] and result['knowledge']['setd']['ok']
    assert result['knowledge']['online_persistent_write'] is False
    assert result['environment_feedback']['executed'] is False
    # A unit fixture may exercise the real assembler but cannot register a
    # Native execution decision for the live transport consumer.
    with pytest.raises(ValueError):
        binding.bind_result(result,layer4_result=source,request={'stage':phase,'transaction_id':'unit'})


@pytest.mark.parametrize('fault',['lifecycle','risk','integrity','tdoc_boundary','psf','not_ready'])
def test_real_gate_rejection_never_emits_native_package(tmp_path,monkeypatch,fault):
    engine,source,_,_,_,_=setup_case(tmp_path,monkeypatch,commit=fault!='lifecycle')
    if fault=='risk':
        for c in source['candidates']:c['violation_probability']=.99
    elif fault=='integrity':source['snapshot_hash']='wrong'
    elif fault=='tdoc_boundary':source['actuator_command']=True
    elif fault=='psf':
        engine.config.data.setdefault('safety',{})['max_command_norm']=.01
    elif fault=='not_ready':
        engine.native_mission_binding.config.data['layers']['layer6']['online']['execution_profile']='qualification'
    result=engine.run(source,'ONLINE_JSBSIM',human_approval=True,requested_mode='AUTO').to_dict()
    assert not result['gates']['FINAL'],result['gates']
    assert result['layer6_package'] is None and result['control_action'] is None
    assert result['environment_feedback']['executed'] is False
    if fault=='psf':assert 'command_norm_limit' in result['gates']['psf']['reasons']


def test_json_flags_without_process_local_binding_cannot_enter_assembler(tmp_path,monkeypatch):
    engine,source,_,_,_,_=setup_case(tmp_path,monkeypatch)
    del engine.native_mission_binding
    source.update(native_controller_certified=True,flight_authorized=True)
    result=engine.run(source,'ONLINE_JSBSIM').to_dict()
    assert not result['gates']['FINAL'] and result['layer6_package'] is None
