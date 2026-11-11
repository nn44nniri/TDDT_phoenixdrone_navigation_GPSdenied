"""The FCU window must preserve child output and lifetime; no FCU/GUI is run."""
import importlib.util
from pathlib import Path
import subprocess
import sys

NATIVE=Path(__file__).resolve().parents[2]/'layer6_v20261007_1032/src'
sys.path.insert(0,str(NATIVE))
spec=importlib.util.spec_from_file_location('fcu_terminal_test',NATIVE/'launch_fcu_terminal.py')
fcu=importlib.util.module_from_spec(spec);spec.loader.exec_module(fcu)


def test_fcu_output_and_exit_status_are_captured(tmp_path,monkeypatch):
    script=tmp_path/'fake FCU.sh';script.write_text("#!/bin/sh\necho simulated-output\necho simulated-error >&2\nexit 7\n")
    stop=tmp_path/'stop.request';stop.touch()
    monkeypatch.setattr(fcu.runner,'STOP_REQUEST',stop)
    log=tmp_path/'fcu.log';pid=tmp_path/'fcu.pid'
    cp=subprocess.run(['bash','--noprofile','--norc','-c',fcu.build_command(script,log,pid)],capture_output=True,text=True,timeout=5)
    assert cp.returncode==7 and pid.is_file()
    assert all(s in log.read_text() for s in ('simulated-output','simulated-error','ArduPlane exited with status 7'))


def test_fcu_hook_uses_native_visible_terminal_and_handshake(tmp_path,monkeypatch):
    calls=[]
    script=tmp_path/'ritw';script.write_text('echo test\n')
    monkeypatch.setattr(fcu.runner,'LOGS',tmp_path)
    monkeypatch.setattr(fcu.runner,'PIDS',tmp_path)
    monkeypatch.setattr(fcu.runner,'pick_terminal',lambda **kw:'gnome-terminal')
    monkeypatch.setattr(fcu.runner,'Diagnostics',lambda:type('D',(),{'close':lambda self:None})())
    monkeypatch.setattr(fcu.runner,'_launch_terminal',lambda *args,**kw:calls.append((args,kw)))
    assert fcu.main([str(script)])==0
    args,kw=calls[0]
    assert args[0]=='gnome-terminal' and args[1]=='ArduPlane + JSBSim'
    assert 'tee -a' in args[2] and kw['expected_pidfile'].name=='terminal_fcu.pid'
    assert '-iconic' not in args[2]


def test_launcher_selects_fcu_hook_without_changing_controller():
    source=(NATIVE/'run_flightstack.py').read_text()
    assert 'export SITL_RITW_TERMINAL=' in source and 'launch_fcu_terminal.py' in source
    assert 'export SITL_RITW_MINIMIZE=0' in source
