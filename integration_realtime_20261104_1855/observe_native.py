"""Read-only Native telemetry and real X11 window evidence for this transaction."""
import ctypes
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from layer6 import Layer6Client
from layer6.native_session import session_health

OUT=Path(__file__).parent/'evidence'
OUT.mkdir(exist_ok=True)


def capture(health, label, status):
    sid=health['session_id'];folder=OUT/(label+'_'+sid[:8]);folder.mkdir(exist_ok=True)
    windows=subprocess.check_output(['xwininfo','-root','-tree'],text=True)
    folder.joinpath('windows.txt').write_text(windows)
    matches=re.findall(r'^\s+(0x[0-9a-f]+) "([^"]*)"',windows,re.M)
    roles={'ardupilot':'ArduPilot SITL ['+sid[:8]+']',
           'environment':'Environment Adapter ['+sid[:8]+']',
           'jsbsim':'JSBSim - Native backend observer ['+sid[:8]+']',
           'gui':'TDDT Layer-6 Digital Pilot', 'map':'Map',
           'console':'MAVProxy Console ['+sid[:8]+']'}
    manifest={'native_session_id':sid,'captured_utc_s':time.time(),'health':health,
              'GUI_observation_before_capture':status,'windows':{}}
    x=ctypes.CDLL('libX11.so.6');x.XOpenDisplay.restype=ctypes.c_void_p
    x.XRaiseWindow.argtypes=[ctypes.c_void_p,ctypes.c_ulong];x.XSync.argtypes=[ctypes.c_void_p,ctypes.c_int]
    x.XCloseDisplay.argtypes=[ctypes.c_void_p]
    display=x.XOpenDisplay(None)
    try:
        for role in ('gui','ardupilot','jsbsim','environment','map','console'):
            title=roles[role]
            found=[(wid,name) for wid,name in matches if name==title or (role=='gui' and name.startswith(title))]
            if len(found)!=1:
                manifest['windows'][role]={'ok':False,'reason':'missing_or_ambiguous_window'};continue
            wid,name=found[0]
            tree=subprocess.check_output(['xwininfo','-id',wid,'-tree'],text=True)
            parent=re.search(r'Parent window id: (0x[0-9a-f]+)',tree)
            if display and parent:
                x.XRaiseWindow(display,int(parent[1],16));x.XSync(display,0)
            image=folder/(role+'.png')
            run=subprocess.run(['import','-window',wid,str(image)],capture_output=True,timeout=5)
            manifest['windows'][role]={'ok':run.returncode==0,'window_id':wid,'title':name,
                'image':str(image.resolve()),'sha256':hashlib.sha256(image.read_bytes()).hexdigest() if image.exists() else None,
                'window_info':tree}
    finally:
        if display:x.XCloseDisplay(display)
    manifest['all_six_captured']=len(manifest['windows'])==6 and all(v['ok'] for v in manifest['windows'].values())
    folder.joinpath('manifest.json').write_text(json.dumps(manifest,indent=2))


seen=set();last_session=None
end=time.monotonic()+14400
with Layer6Client() as client, (OUT/'native_observations.jsonl').open('a',buffering=1) as log:
    while time.monotonic()<end and not OUT.joinpath('observer.stop').exists():
        try:
            session=client.native_session_status();sid=session.get('session_id')
            if session.get('session_ready'):
                health=session_health(sid)
                # Continuous observation uses MAVProxy's already-published stream.
                # Only a mode/ARM transition requests a GUI snapshot for capture.
                panel=json.loads((Path(health['session_dir'])/'native_panels.json').read_text())
                heartbeat=panel.get('heartbeat') or {}
                log.write(json.dumps({'wall_time_s':time.time(),'health':health,'panel_stream':panel,
                    'observation_source':'same-session MAVProxy publication'})+'\n')
                if heartbeat and health['ok']:
                    key=(sid,heartbeat.get('mode'),heartbeat.get('armed'))
                    if key not in seen:
                        status=client.gui_status(timeout=.5)
                        if (not status or not status.get("mavlink_connected")
                            or not status.get("mode") or status.get("armed") is None
                            or status.get("mode")!=heartbeat.get("mode")
                            or status.get("armed") is not heartbeat.get("armed")):continue
                        from layer6.status import validate_observation_status
                        if not validate_observation_status(status)["ok"]:continue
                        capture(health,('armed' if status.get('armed') else 'disarmed')+'_'+str(status.get('mode')),status)
                        seen.add(key)
        except Exception as exc:
            log.write(json.dumps({'wall_time_s':time.time(),'observer_error':str(exc)})+'\n')
        time.sleep(.5)
