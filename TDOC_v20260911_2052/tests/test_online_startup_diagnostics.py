"""Regression of launcher self-kill and opt-in CLI diagnostics; no flight is started."""
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from tddt_tdoc.diagnostics import diagnostic_session,event,run_workflow
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

NATIVE=Path(__file__).resolve().parents[2]/'layer6_v20261007_1032'

def test_cleanup_matches_actual_mavproxy_but_never_its_own_launcher(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('startup_launcher',NATIVE/'src/run_flightstack.py')
    launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher,'RUNTIME',tmp_path)
    monkeypatch.setattr(launcher,'LOGS',tmp_path)
    monkeypatch.setattr(launcher,'_resolve',lambda fs:tuple(tmp_path/name for name in ('fs','ap','env','ctrl','js','venv')))
    (tmp_path/'tdoc_environment_binding.json').write_text(json.dumps({'mission_origin':dict(latitude_deg=40.,longitude_deg=-105.,altitude_msl_m=1800.,heading_deg=45.)}))
    commands=launcher._commands(tmp_path);command=commands[0]
    pattern=shlex.split(command[command.index('pkill -f '):].split(' || true')[0])[2]
    assert re.search('[m]avproxy',command)  # Reproduces the old self-kill condition.
    assert not re.search(pattern,'bash --noprofile --norc -c '+command)
    assert re.search(pattern,'/venv/bin/python /venv/bin/mavproxy.py --streamrate=-1')
    assert not re.search(pattern,'python unrelated_mavproxy_tool.py')
    assert all('exec > >(tee -a ' in c for c in commands)
    assert all('terminal'+str(i)+'_' in c for i,c in enumerate(commands,1))


def test_debug_log_captures_child_stdout_stderr_events_and_tracebacks(tmp_path,monkeypatch,capsys):
    monkeypatch.delenv('TDDT_DIAGNOSTIC_LOG',raising=False)
    path=tmp_path/'online.log'
    with diagnostic_session(SimpleNamespace(runtime_root=tmp_path,package_root=tmp_path),SimpleNamespace(debug_log=str(path),cmd='nav-online')):
        print('parent output')
        assert run_workflow([sys.executable,'-c',"import sys; print('child output'); print('child error',file=sys.stderr); sys.exit(3)"],tmp_path,enabled=True)==3
        try:raise RuntimeError('readiness failure')
        except RuntimeError:event('layer6.prepare.failed',stage='mavlink_connect',traceback=True)
    contents=path.read_text()
    assert all(s in contents for s in ['parent output','child output','child error','Traceback','readiness failure','mavlink_connect','"returncode": 3'])
    assert 'child output' in capsys.readouterr().out
    import os
    assert 'TDDT_DIAGNOSTIC_LOG' not in os.environ


def test_auto_log_path_and_no_log_without_switch(tmp_path):
    config=SimpleNamespace(runtime_root=tmp_path,package_root=tmp_path)
    with diagnostic_session(config,SimpleNamespace(debug_log=None,cmd='nav-online')):pass
    assert not (tmp_path/'logs').exists()
    with diagnostic_session(config,SimpleNamespace(debug_log='auto',cmd='nav-online')):print('automatic')
    assert len(list((tmp_path/'logs').glob('nav-online_*.log')))==1


def test_failed_mavlink_preparation_cannot_open_guidance_session():
    config=SimpleNamespace(get=lambda key,default=None:default)
    a=Layer6OnlineAdapter(config,SimpleNamespace())
    def fail(**kwargs):raise TimeoutError('no flight controller')
    a._client=SimpleNamespace(stack_responding=lambda:True,ensure_mavlink=fail)
    a._session_open=True
    result=a.prepare_online_transport_session()
    assert result['status']=='NOT_READY' and result['stage']=='mavlink_connect'
    assert result['actuation_executed'] is False and a._session_open is False

def test_cli_debug_switch_retains_force_reuse_and_records_refusal(tmp_path,monkeypatch):
    from tddt_tdoc import cli
    config=SimpleNamespace(runtime_root=tmp_path/'runtime',package_root=tmp_path)
    monkeypatch.setattr(cli.TDOCConfig,'from_yaml',lambda path:config)
    commands=[]
    class Handoff:
        def __init__(self,*args):pass
        def validate_online_start(self,*args):return {'ok':True,'manifest':{}}
    monkeypatch.setattr(cli,'OfflineKnowledgeHandoff',Handoff)
    import tddt_tdoc.diagnostics as diagnostics
    monkeypatch.setattr(diagnostics,'run_workflow',lambda command,cwd,enabled:commands.append((command,enabled)) or 0)
    path=tmp_path/'cli.log'
    assert cli.main(['nav-online','--reuse-existing-offline-knowledge','--online-episodes','1','--debug-log',str(path)])==0
    assert commands[0][1] and '--skip-offline' in commands[0][0]
    assert path.is_file()
    monkeypatch.setattr(Handoff,'validate_online_start',lambda *args:{'ok':False,'reason':'hash_mismatch'})
    assert cli.main(['nav-online','--debug-log',str(path)])==2
    assert len(commands)==1 and 'hash_mismatch' in path.read_text()
