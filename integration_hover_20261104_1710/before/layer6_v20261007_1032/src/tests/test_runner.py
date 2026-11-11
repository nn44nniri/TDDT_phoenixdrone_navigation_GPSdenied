from pathlib import Path
import run_flightstack as r

def test_module_exports_restart(): assert callable(r.restart)
def test_migrated_commands_present():
 from layer6 import native_session
 s=Path(r.__file__).read_text()+Path(native_session.__file__).read_text()
 for x in ['arduplane','jsbsim:phoenix_v3','phoenix_env_adapter.cli',"'prepare'","'runtime'",'--binding','14551','14552','phoenix_f310_gui.py']:
  assert x in s

def test_placeholder_falls_back_to_local(monkeypatch,tmp_path):
    monkeypatch.setattr(r,'HERE',tmp_path)
    expected=(tmp_path/'FlightStack').resolve(); expected.mkdir()
    assert r._select_flightstack_dir('/path/to/FlightStack') == expected

def test_default_prefers_package_local(monkeypatch,tmp_path):
    monkeypatch.setattr(r,'HERE',tmp_path)
    expected=(tmp_path/'FlightStack').resolve(); expected.mkdir()
    assert r._select_flightstack_dir() == expected

def test_cli_has_verbose_and_debug_switches():
    s=Path(r.__file__).read_text()
    assert '"-v", "--verbose"' in s
    assert '"--debug"' in s
    assert 'LOGS = RUNTIME / "logs"' in s

def test_gnome_terminal_matches_working_legacy_launch():
    cmd=r._term_cmd('gnome-terminal','T','echo ok')
    assert '--wait' not in cmd
    assert 'setsid' not in cmd
    assert cmd[:2] == ['gnome-terminal','--title=T']
    assert cmd[2:6] == ['--','bash','--noprofile','--norc']

def test_status_contains_desktop_diagnostics(capsys):
    r.status(verbose=True)
    out=capsys.readouterr().out
    assert 'terminal_candidates' in out
    assert 'display' in out

def test_launcher_uses_pid_handshake():
    s=Path(r.__file__).read_text()
    assert 'expected_pidfile' in s
    assert 'terminal child handshake received' in s
    assert 'PID handshake not created' in s
