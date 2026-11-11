#!/usr/bin/env python3
"""Observe the existing Native session; never send flight commands."""
import argparse
import contextlib
import ctypes
import io
import json
from pathlib import Path
import re
import subprocess
import time

from layer6 import Layer6Client
from layer6.native_session import session_health
from layer6.status import validate_observation_status
from online_reports.provenance import file_hash, write_json


def capture(health, status, folder):
    folder.mkdir(parents=True, exist_ok=True)
    sid = health['session_id']
    windows = subprocess.check_output(['xwininfo', '-root', '-tree'], text=True)
    (folder / 'windows.txt').write_text(windows)
    matches = re.findall(r'^\s+(0x[0-9a-f]+) "([^"]*)"', windows, re.M)
    roles = {'ardupilot': 'ArduPilot SITL [' + sid[:8] + ']',
             'environment': 'Environment Adapter [' + sid[:8] + ']',
             'jsbsim': 'JSBSim - Native backend observer [' + sid[:8] + ']',
             'gui': 'TDDT Layer-6 Digital Pilot', 'map': 'Map',
             'console': 'MAVProxy Console [' + sid[:8] + ']'}
    manifest = {'native_session_id': sid, 'captured_utc_s': time.time(),
                'health': health, 'GUI_observation_before_capture': status, 'windows': {}}
    x = ctypes.CDLL('libX11.so.6')
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x.XOpenDisplay(None)
    try:
        for role, title in roles.items():
            found = [(wid, name) for wid, name in matches
                     if name == title or role == 'gui' and name.startswith(title)]
            if len(found) != 1:
                manifest['windows'][role] = {'ok': False, 'reason': 'missing_or_ambiguous_window'}
                continue
            wid, name = found[0]
            tree = subprocess.check_output(['xwininfo', '-id', wid, '-tree'], text=True)
            parent = re.search(r'Parent window id: (0x[0-9a-f]+)', tree)
            if display and parent:
                x.XRaiseWindow(display, int(parent[1], 16))
                x.XSync(display, 0)
            target = folder / (role + '.png')
            done = subprocess.run(['import', '-window', wid, str(target)],
                                  capture_output=True, timeout=5)
            manifest['windows'][role] = {'ok': done.returncode == 0, 'window_id': wid,
                'title': name, 'image': str(target.resolve()),
                'sha256': file_hash(target) if target.exists() else None}
    finally:
        if display:
            x.XCloseDisplay(display)
    manifest['all_six_captured'] = len(manifest['windows']) == 6 and all(
        w['ok'] for w in manifest['windows'].values())
    write_json(folder / 'manifest.json', manifest)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--stop-file', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    seen = set()
    last_periodic = 0.
    with Layer6Client() as client, (args.output / 'native_observations.jsonl').open('a', buffering=1) as log:
        while not args.stop_file.exists():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    state = client.native_session_status()
                if not state.get('session_ready'):
                    time.sleep(.5)
                    continue
                sid = state['session_id']
                health = session_health(sid)
                panel = json.loads((Path(health['session_dir']) / 'native_panels.json').read_text())
                heartbeat = panel.get('heartbeat') or {}
                log.write(json.dumps({'wall_time_s': time.time(), 'health': health,
                    'panel_stream': panel, 'observation_source': 'same-session MAVProxy publication'}) + '\n')
                key = (sid, heartbeat.get('mode'), heartbeat.get('armed'))
                periodic = heartbeat.get('armed') and time.monotonic() - last_periodic > 60.
                if heartbeat and health['ok'] and (key not in seen or periodic):
                    status = client.gui_status(timeout=.5)
                    if (not status or not validate_observation_status(status)['ok']
                            or status.get('mode') != heartbeat.get('mode')
                            or status.get('armed') is not heartbeat.get('armed')):
                        continue
                    label = ('armed' if status['armed'] else 'disarmed') + '_' + str(status['mode'])
                    capture(health, status, args.output / (label + '_' + sid[:8] + '_' + str(time.time_ns())))
                    seen.add(key)
                    last_periodic = time.monotonic()
            except Exception as exc:
                log.write(json.dumps({'wall_time_s': time.time(), 'observer_error': repr(exc)}) + '\n')
            time.sleep(.5)


if __name__ == '__main__':
    main()
