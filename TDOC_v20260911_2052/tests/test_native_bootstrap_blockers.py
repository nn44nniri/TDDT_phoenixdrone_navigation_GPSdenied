"""Regressions for recorded native bootstrap blockers."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
from tddt_tdoc.adapters.layer4 import Layer4Adapter
NATIVE=Path(__file__).resolve().parents[2]/'layer6_v20261007_1032'
spec=importlib.util.spec_from_file_location('native_environment_test',NATIVE/'src/layer6/environment.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def test_asynchronous_wind_acknowledgements_and_expiry():
    a=module.WindAcknowledgements();first={'SIM_WIND_SPD':1.,'SIM_WIND_DIR':140.}
    assert a.request(first,{'model_timestamp':'first'},0)==first
    assert a.request({'SIM_WIND_SPD':2.,'SIM_WIND_DIR':141.},{},.2) is None
    a.observe('SIM_WIND_SPD',1.,.21);assert not a.ready(.21)
    a.observe('SIM_WIND_DIR',140.,.22);assert a.ready(.22)
    second={'SIM_WIND_SPD':2.,'SIM_WIND_DIR':141.}
    assert a.request(second,{'model_timestamp':'second'},.4)==second
    assert a.ready(.4)
    a.observe('SIM_WIND_DIR',140.,.5);assert a.pending['echoes']=={}
    assert not a.ready(1.23)
    assert a.request({}, {},1.5)==second
    a.observe('SIM_WIND_SPD',2.,1.51);a.observe('SIM_WIND_DIR',141.,1.52)
    assert a.ready(1.52) and a.confirmed['model_timestamp']=='second'
    assert not module.WindAcknowledgements().ready(1.52)

def adapter(prior_ok):
    a=Layer4Adapter.__new__(Layer4Adapter);a.config=SimpleNamespace(get=lambda key:'Layer6Client_GUI_MAVLink')
    a._ensure=lambda:None
    a.layer5_adapter=SimpleNamespace(ensure_native_control_binding=lambda:{'ok':prior_ok},layer4_action_envelope=lambda *args:pytest.fail('retired commissioning called'))
    envelopes=[];ff=[]
    a.engine=SimpleNamespace(config=SimpleNamespace(horizon=6),set_runtime_action_envelope=envelopes.append,run=lambda snapshot:SimpleNamespace(to_dict=lambda:{}))
    a._tdoc_full_fidelity=lambda result,snapshot,request:ff.append(True) or result
    a.native_workspace=SimpleNamespace(working_root='/tmp/test');a.native_root=Path('/tmp/test')
    return a,envelopes,ff

def test_native_uses_core_bounds_and_current_full_fidelity():
    a,envelopes,ff=adapter(True)
    assert a.invoke({'snapshot':{},'request':{'stage':'vertical_takeoff','learning_mode':'ONLINE_JSBSIM'}})['ok']
    assert envelopes==[None] and ff==[True]

def test_native_rejects_invalid_offline_prior():
    a,envelopes,ff=adapter(False)
    with pytest.raises(RuntimeError,match='accepted Offline model prior'):
        a.invoke({'snapshot':{},'request':{'stage':'vertical_takeoff','learning_mode':'ONLINE_JSBSIM'}})
    assert not envelopes and not ff

def test_tdoc_launcher_leaves_telemetry_rates_to_gui(tmp_path,monkeypatch):
    import json
    spec=importlib.util.spec_from_file_location('launcher_test',NATIVE/'src/run_flightstack.py')
    launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher,'RUNTIME',tmp_path)
    monkeypatch.setattr(launcher,'_resolve',lambda fs:tuple(tmp_path/name for name in ('fs','ap','env','ctrl','js','venv')))
    baseline=str(launcher._commands(tmp_path))
    assert '--streamrate=-1' not in baseline
    (tmp_path/'tdoc_environment_binding.json').write_text(json.dumps({'mission_origin':dict(latitude_deg=40.,longitude_deg=-105.,altitude_msl_m=1800.,heading_deg=45.)}))
    bound=str(launcher._commands(tmp_path))
    assert '--streamrate=-1' in bound and '--console --map' in bound


def test_gui_reapplies_rates_after_initialising_without_arming():
    import ast
    tree=ast.parse((NATIVE/'src/gui/phoenix_f310_gui.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MavlinkLink')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_update_heartbeat')
    fake=SimpleNamespace(mavlink=SimpleNamespace(MAV_MODE_FLAG_SAFETY_ARMED=128))
    namespace={'mavutil':fake}
    exec(compile(ast.Module(body=[method],type_ignores=[]),'<heartbeat>','exec'),namespace)
    requests=[]
    link=SimpleNamespace(mode='INITIALISING',master=SimpleNamespace(mode_mapping=lambda:{'MANUAL':0}),request_telemetry_intervals=lambda:requests.append(True))
    namespace['_update_heartbeat'](link,SimpleNamespace(base_mode=0,custom_mode=0))
    namespace['_update_heartbeat'](link,SimpleNamespace(base_mode=0,custom_mode=0))
    assert requests==[True] and link.armed is False
