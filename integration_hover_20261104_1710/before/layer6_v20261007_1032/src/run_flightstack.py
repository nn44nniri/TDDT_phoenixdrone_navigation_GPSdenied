#!/usr/bin/env python3
"""Complete Native FlightStack lifecycle with a separate JSBSim observer window.

The launcher owns all runtime components that previously lived in
run_flightstack_3term_rev1.sh.  Use --verbose or --debug when diagnosing a
terminal-launch failure.  Diagnostics are also persisted under runtime/logs.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent
HERE = SRC_ROOT.parent
RUNTIME = HERE / "runtime"
STATE = RUNTIME / "flightstack_state.json"
PIDS = RUNTIME / "pids"
LOGS = RUNTIME / "logs"
STOP_REQUEST = RUNTIME / "stop.request"
PIDS.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)
DEFAULT_DELAY = float(os.environ.get("FLIGHTSTACK_TERMINAL_DELAY", "2"))
PLACEHOLDER_PATHS = {
    "/path/to/FlightStack",
    "path/to/FlightStack",
    "/path/to/flightstack",
    "path/to/flightstack",
}
SUPPORTED_TERMINALS = (
    "gnome-terminal",
    "xfce4-terminal",
    "konsole",
    "xterm",
    "x-terminal-emulator",
)


def q(x):
    return shlex.quote(str(x))


class Diagnostics:
    def __init__(self, verbose: bool = False, debug: bool = False):
        self.verbose = bool(verbose or debug)
        self.debug = bool(debug)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = LOGS / f"run_flightstack_{stamp}.log"
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)
        self.log(f"launcher={Path(__file__).resolve()}")
        self.log(f"python={sys.executable}")
        self.log(f"cwd={Path.cwd()}")
        self.log(f"argv={sys.argv!r}")

    def log(self, message: str, *, always: bool = False, debug_only: bool = False):
        line = f"[{_dt.datetime.now().isoformat(timespec='seconds')}] {message}"
        self._fh.write(line + "\n")
        if always or (self.debug if debug_only else self.verbose):
            print(line, flush=True)

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def pick_terminal(preferred: str | None = None, diag: Diagnostics | None = None):
    candidates = (preferred,) if preferred else SUPPORTED_TERMINALS
    checked = []
    for app in candidates:
        if not app:
            continue
        path = shutil.which(app)
        checked.append((app, path))
        if path:
            if diag:
                diag.log(f"terminal selected: {app} -> {path}")
            return app
    detail = ", ".join(f"{name}={path or 'NOT_FOUND'}" for name, path in checked)
    raise RuntimeError(f"no supported terminal emulator found ({detail})")


def _term_cmd(app, title, cmd):
    """Build the terminal command using the proven legacy-shell launch semantics.

    IMPORTANT: do not use gnome-terminal --wait here.  On X11 desktop sessions
    backed by the GNOME terminal server, the client may legitimately return rc=0
    immediately after handing the request to the server.  The launcher verifies
    the *child shell* instead via its PID handshake file.
    """
    if app == "gnome-terminal":
        return [app, f"--title={title}", "--", "bash", "--noprofile", "--norc", "-c", cmd]
    if app == "xfce4-terminal":
        return [app, f"--title={title}", f"--command=bash --noprofile --norc -c {q(cmd)}"]
    if app == "konsole":
        return [app, "-p", f"tabtitle={title}", "-e", "bash", "--noprofile", "--norc", "-c", cmd]
    return [app, "-T", title, "-e", "bash", "--noprofile", "--norc", "-c", cmd]


def _select_flightstack_dir(value=None):
    local = (HERE / "FlightStack").resolve()
    cwd = (Path.cwd() / "FlightStack").resolve()
    raw = str(value).strip() if value is not None else ""
    if not raw:
        for c in (local, cwd):
            if c.exists():
                return c
        return local
    if raw in PLACEHOLDER_PATHS or ("path/to/FlightStack" in raw):
        for c in (local, cwd):
            if c.exists():
                print(f"NOTE: {raw!r} is a documentation placeholder; using installed FlightStack: {c}")
                return c
        raise FileNotFoundError(f"{raw!r} is a placeholder and no local FlightStack was found. Expected: {local}")
    return Path(raw).expanduser().resolve()


def _resolve(fs):
    fs = _select_flightstack_dir(fs)
    ap = fs / "ardupilot"
    env = fs / "phoenix_environment_adapter_v2"
    ctrl = HERE / "gui"
    js = fs / "jsbsim/build/src"
    venv = ap / "venv-ardupilot-debian13"
    req = [
        ap / "Tools/autotest/sim_vehicle.py",
        venv / "bin/activate",
        js / "JSBSim",
        env / "phoenix_env_adapter/cli.py",
        env / "phoenix_env_adapter/bound.py",
        ctrl / "scripts/run_gui.sh",
    ]
    missing = [str(x) for x in req if not x.exists()]
    if missing:
        raise FileNotFoundError("missing FlightStack components:\n  " + "\n  ".join(missing))
    return fs, ap, env, ctrl, js, venv


def _commands(fs,binding_path=None,session_dir=None):
    from layer6 import native_session as native
    if session_dir is None:raise ValueError('owned native session required')
    session_dir,data=native.check_session(session_dir)
    python=HERE.parent/'.venv/bin/python'
    commands=[]
    for i in range(1,5):
        role='terminal'+str(i)
        commands.append(f"export TDDT_NATIVE_SESSION_ID={q(data['session_id'])}; export TDDT_NATIVE_ROLE={q(role)}; "
            f"export PYTHONPATH={q(SRC_ROOT)}:${{PYTHONPATH:-}}; echo $$ > {q(PIDS/(role+'.pid'))}; "
            f"exec {q(python)} -B -m layer6.native_session --session {q(session_dir)} --terminal {q(role)}")
    return commands


def _display_diagnostics(diag: Diagnostics):
    keys = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DESKTOP_SESSION")
    diag.log("GUI/session diagnostics:")
    for key in keys:
        val = os.environ.get(key)
        shown = val if val else "<unset>"
        diag.log(f"  {key}={shown}")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        diag.log(
            "WARNING: neither DISPLAY nor WAYLAND_DISPLAY is set. A graphical terminal cannot normally be opened from this process.",
            always=True,
        )
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        diag.log(
            "WARNING: DBUS_SESSION_BUS_ADDRESS is unset. GNOME/KDE terminal clients may fail to contact the desktop session.",
            always=True,
        )


def _launch_terminal(
    app, title, cmd, diag: Diagnostics, *, expected_pidfile: Path, startup_check_s: float = 3.0
):
    """Launch one terminal and verify that its in-terminal shell actually started.

    A terminal client returning rc=0 is only a successful *handoff*.  Real startup
    success is confirmed by the shell inside the new window writing expected_pidfile.
    This avoids the false failure seen with gnome-terminal --wait while still
    detecting cases where no window/child shell was created.
    """
    argv = _term_cmd(app, title, cmd)
    diag.log(f"launch title={title!r}")
    diag.log("launch argv=" + shlex.join(argv), debug_only=True)
    diag.log(f"expected pid handshake={expected_pidfile}")
    env = os.environ.copy()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
    except Exception as exc:
        diag.log(f"ERROR spawning terminal: {type(exc).__name__}: {exc}", always=True)
        raise

    diag.log(f"terminal client pid={proc.pid}")
    deadline = time.monotonic() + max(0.2, startup_check_s)
    client_rc = None
    client_out = ""
    client_err = ""
    while time.monotonic() < deadline:
        if expected_pidfile.exists():
            try:
                child_pid = int(expected_pidfile.read_text().strip())
            except Exception:
                child_pid = None
            diag.log(f"terminal child handshake received: pid={child_pid}")
            if child_pid and _pid_alive(child_pid):
                diag.log("terminal startup verified: in-window shell is alive")
            else:
                diag.log("terminal startup verified: pid file created (child may already have advanced/exited)")
            return proc

        rc = proc.poll()
        if rc is not None and client_rc is None:
            client_rc = rc
            try:
                out, err = proc.communicate(timeout=0.5)
            except Exception:
                out, err = "", ""
            client_out, client_err = out or "", err or ""
            if client_out.strip():
                diag.log("terminal stdout: " + client_out.strip(), always=(rc != 0))
            if client_err.strip():
                diag.log("terminal stderr: " + client_err.strip(), always=(rc != 0))
            if rc != 0:
                raise RuntimeError(
                    f"terminal launcher {app!r} failed with status {rc}; see {diag.path}"
                )
            diag.log(
                f"terminal client returned rc=0 after desktop-server handoff; waiting for child PID handshake"
            )
        time.sleep(0.1)

    details = []
    if client_rc is not None:
        details.append(f"terminal-client rc={client_rc}")
    if client_err.strip():
        details.append(f"stderr={client_err.strip()!r}")
    details.append(f"PID handshake not created: {expected_pidfile}")
    raise RuntimeError(
        f"terminal request for {title!r} was not confirmed within {startup_check_s:.1f}s ("
        + "; ".join(details)
        + f"); see {diag.path}"
    )


def start(flightstack_dir=None,delay=DEFAULT_DELAY,*,verbose=False,debug=False,terminal=None,startup_check=3.0):
    from layer6 import native_session as native
    diag=Diagnostics(verbose=verbose,debug=debug);folder=None
    try:
        with native.lock():
            if STATE.exists():
                old=json.loads(STATE.read_text())
                if old.get('session_dir'):
                    existing=native.status(old['session_dir'])
                    if existing['session_ready']:return {**existing,'created':False}
                    if existing['alive']:raise RuntimeError('existing native session degraded; only its owner may stop it')
                elif any(_pid_alive(int(p.read_text())) for p in PIDS.glob('terminal*.pid')):
                    raise RuntimeError('legacy live session has no verifiable ownership; no global cleanup permitted')
            fs=_select_flightstack_dir(flightstack_dir);_resolve(fs)
            # Refuse occupied public endpoints rather than killing unrelated processes.
            import socket
            checks=[]
            try:
                for kind,port in [(socket.SOCK_STREAM,5760),(socket.SOCK_STREAM,14552),*( (socket.SOCK_DGRAM,p) for p in (14551,14660,14661))]:
                    sock=socket.socket(socket.AF_INET,kind);checks.append(sock)
                    if kind==socket.SOCK_STREAM:sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    sock.bind(('127.0.0.1',port))
                    if kind==socket.SOCK_STREAM:sock.listen(1)
            finally:
                for sock in checks:sock.close()
            folder,data=native.create(RUNTIME/'tdoc_environment_binding.json')
            saved={'schema':native.SCHEMA,'session_dir':str(folder),'session_id':data['session_id'],'flightstack_dir':str(fs),
                   'terminal':terminal,'delay':delay,'started_at':time.time(),'last_log':str(diag.path)}
            native.write(STATE,saved) # Ownership precedes terminal/component creation.
            cmds=_commands(fs,session_dir=folder);_display_diagnostics(diag);app=pick_terminal(terminal,diag)
            titles=('ArduPilot SITL','Environment Adapter','Phoenix GUI control gateway','JSBSim - Native backend observer')
            for i,(title,cmd) in enumerate(zip(titles,cmds),1):
                pidfile=PIDS/f'terminal{i}.pid';pidfile.unlink(missing_ok=True)
                print(f'Opening Terminal {i}: {title}',flush=True)
                _launch_terminal(app,title+' ['+data['session_id'][:8]+']',cmd,diag,expected_pidfile=pidfile,startup_check_s=startup_check)
                if i<len(cmds):time.sleep(delay)
            end=time.monotonic()+25
            while time.monotonic()<end:
                errors=list(folder.glob('terminal*_error.json'))
                if errors:raise RuntimeError('native terminal failed: '+str([json.loads(p.read_text()) for p in errors]))
                current=native.status(folder)
                if current['session_ready']:
                    native.write(folder/'process_ready.json',current);return {**current,'created':True}
                time.sleep(.1)
            raise RuntimeError('native component/panel readiness timeout: '+str(current.get('panels')))
    except Exception:
        if folder is not None:native.stop(folder)
        raise
    finally:diag.close()


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(verbose=False,*,close_terminals=True,session_id=None):
    from layer6 import native_session as native
    with native.lock():
        if not STATE.exists():return {'stopped':True,'reason':'no_native_session'}
        saved=json.loads(STATE.read_text())
        if not saved.get('session_dir'):raise RuntimeError('unverified legacy ownership; refusing stop')
        if session_id is not None and session_id!=saved['session_id']:raise RuntimeError('native stop ownership mismatch')
        return native.stop(saved['session_dir'])


def restart(flightstack_dir=None,delay=None,*,verbose=False,debug=False,terminal=None,startup_check=3.0,session_id=None):
    saved=json.loads(STATE.read_text()) if STATE.exists() else {}
    stop(verbose=verbose,session_id=session_id)
    return start(flightstack_dir or saved.get('flightstack_dir'),DEFAULT_DELAY if delay is None else delay,
        verbose=verbose,debug=debug,terminal=terminal or saved.get('terminal'),startup_check=startup_check)


def status(verbose=False):
    from layer6 import native_session as native
    saved=json.loads(STATE.read_text()) if STATE.exists() else {}
    payload=native.status(saved['session_dir']) if saved.get('session_dir') else {'process_ready':False,'alive':False,'flight_ready':False}
    payload.update(state=saved,display=os.environ.get('DISPLAY'),wayland_display=os.environ.get('WAYLAND_DISPLAY'),dbus_session_bus=bool(os.environ.get('DBUS_SESSION_BUS_ADDRESS')))
    if verbose:payload['terminal_candidates']={x:shutil.which(x) for x in SUPPORTED_TERMINALS}
    print(json.dumps(payload,indent=2));return payload


def main():
    p = argparse.ArgumentParser(description="TDDT Layer-6 complete FlightStack launcher")
    p.add_argument("command", nargs="?", default="start", choices=["start", "stop", "restart", "status"])
    p.add_argument("--flightstack-dir")
    p.add_argument("--delay", type=float)
    p.add_argument("--terminal", choices=SUPPORTED_TERMINALS, help="force a terminal emulator instead of auto-detection")
    p.add_argument("-v", "--verbose", action="store_true", help="print launch checks, PIDs, session variables and errors")
    p.add_argument("--debug", action="store_true", help="verbose diagnostics plus exact terminal command lines")
    p.add_argument("--startup-check", type=float, default=3.0, metavar="SECONDS", help="seconds to wait for each in-terminal PID handshake (default: 3.0)")
    a = p.parse_args()
    d = DEFAULT_DELAY if a.delay is None else a.delay
    if a.command == "start":
        start(a.flightstack_dir, d, verbose=a.verbose, debug=a.debug, terminal=a.terminal, startup_check=a.startup_check)
    elif a.command == "stop":
        stop(verbose=a.verbose or a.debug)
    elif a.command == "restart":
        restart(a.flightstack_dir, a.delay, verbose=a.verbose, debug=a.debug, terminal=a.terminal, startup_check=a.startup_check)
    else:
        status(verbose=a.verbose or a.debug)


if __name__ == "__main__":
    main()
