import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
import pytest
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

path=Path(__file__).resolve().parents[2]/'layer6_v20261007_1032/src/layer6/body_attitude.py'
spec=importlib.util.spec_from_file_location('body_attitude_test',path)
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)


def message(pitch):
    return SimpleNamespace(q1=math.cos(pitch/2),q2=0.,q3=math.sin(pitch/2),q4=0.,
                           rollspeed=.1,pitchspeed=.2,yawspeed=.3)


def test_body_attitude_keeps_real_pitch_and_body_rates():
    for pitch in (0.,math.radians(89.),math.radians(-89.)):
        a=mod.from_mavlink_quaternion(message(pitch))
        assert a['pitch']==pytest.approx(pitch)
        assert [a[k] for k in ('roll_rate','pitch_rate','yaw_rate')]==[.1,.2,.3]
        assert a['source']=='MAVLink/ATTITUDE_QUATERNION'
        assert a['frame']=='BODY_FRD_TO_NED'


def test_invalid_quaternion_cannot_become_fresh_evidence():
    m=message(0.);m.q1=0.
    with pytest.raises(ValueError):mod.from_mavlink_quaternion(m)
    m.q1=float('nan')
    with pytest.raises(ValueError):mod.from_mavlink_quaternion(m)


def test_display_attitude_cannot_authorize_native_readiness():
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    a._option=lambda key,default=None:True if key=='require_body_attitude_quaternion' else default
    s={'mavlink_connected':True,'telemetry_age_s':{k:.01 for k in
       ('HEARTBEAT','ATTITUDE','ATTITUDE_QUATERNION','VFR_HUD','GLOBAL_POSITION_INT')},
       'attitude':{'pitch':-math.pi/2}}
    assert not a._fresh(s)
    s['attitude']=mod.from_mavlink_quaternion(message(0.))
    assert a._fresh(s)
    s['telemetry_age_s']['ATTITUDE_QUATERNION']=3.
    assert not a._fresh(s)


def test_gui_raw_vtol_display_cannot_overwrite_body_state(monkeypatch):
    import ast
    import sys
    from types import ModuleType
    package=ModuleType('layer6');package.__path__=[]
    monkeypatch.setitem(sys.modules,'layer6',package)
    monkeypatch.setitem(sys.modules,'layer6.body_attitude',mod)
    gui=path.parents[1]/'gui/phoenix_f310_gui.py'
    tree=ast.parse(gui.read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MavlinkLink')
    drain=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='drain')
    namespace={};exec(compile(ast.Module(body=[drain],type_ignores=[]),'<gui-drain>','exec'),namespace)
    body=message(0.);body.get_type=lambda:'ATTITUDE_QUATERNION'
    display=SimpleNamespace(get_type=lambda:'ATTITUDE',roll=0.,pitch=-math.pi/2,yaw=0.,
        rollspeed=.1,pitchspeed=.2,yawspeed=.3)
    msgs=iter([body,display,None]);records=[]
    link=SimpleNamespace(master=SimpleNamespace(recv_match=lambda **kw:next(msgs)),attitude={},
        _record_telemetry=lambda kind,fields:records.append((kind,fields)))
    namespace['drain'](link)
    assert link.attitude['pitch']==0.
    assert link.attitude['source']=='MAVLink/ATTITUDE_QUATERNION'
    assert records[-1][0]=='ATTITUDE' and records[-1][1]['pitch']==-math.pi/2
