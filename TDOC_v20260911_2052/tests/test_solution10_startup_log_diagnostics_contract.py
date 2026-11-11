"""Startup diagnostics contract after migration from PT_LIB to native Layer6."""
from pathlib import Path
from types import SimpleNamespace
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


def test_native_console_text_cannot_authorize_readiness():
    config=SimpleNamespace(get=lambda key,default=None:default)
    adapter=Layer6OnlineAdapter(config,SimpleNamespace())
    text={'mavlink_connected':True,'mode':'MANUAL','armed':False,
          'last_statustext':'Loaded defaults from ...; all systems ready'}
    assert not adapter._fresh(text)
    text['telemetry_age_s']={k:.01 for k in ('HEARTBEAT','ATTITUDE','VFR_HUD','GLOBAL_POSITION_INT')}
    assert adapter._fresh(text)
    text['last_statustext']='Skipping port (null)'
    assert adapter._fresh(text)


def test_fc_ready_cannot_override_layer5_control_gate():
    adapter=Layer6OnlineAdapter(SimpleNamespace(),SimpleNamespace(online_control_readiness=lambda:{'ok':False}))
    adapter.prepare_online_transport_session=lambda:{'ok':True,'status':'READY'}
    result=adapter.prepare_online_control_session()
    assert not result['ok'] and result['status']=='NOT_READY'


def test_workflow_declares_native_readiness_authority():
    source=(Path(__file__).parents[1]/'scripts/run_training_workflows.py').read_text()
    assert 'ONLINE_STARTUP_DIAGNOSTICS.json' in source
    assert 'diagnostic-only; readiness authority = structured Layer6/GUI/MAVLink + Layer5 + RTMO gates' in source
    assert 'control_readiness_snapshot' in source
