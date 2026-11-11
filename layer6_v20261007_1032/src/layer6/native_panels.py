"""Read-only evidence for the map and Console on the canonical MAVProxy master.

Window process liveness and delivery to the map are structured checks. They do
not prove that a person can see the windows; screenshots remain acceptance work.
This module never sends a MAVLink command or supplies navigation observations.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import time

SCHEMA = 'Layer6-native-panels-v1'
MASTER = 'tcp:127.0.0.1:5760'


def _alive(window):
    try:
        return bool(window.is_alive())
    except (AttributeError, RuntimeError, OSError):
        return False


class PanelEvidence:
    def __init__(self, session_id):
        self.session_id = session_id
        self.position = None
        self.heartbeat = None
        self.position_updates = 0
        self.physical_position=None
        self.physical_updates=0

    def receive(self, msg, target_system, *, now=None):
        now = time.monotonic() if now is None else now
        if not target_system or msg.get_srcSystem() != target_system:
            return
        kind = msg.get_type()
        if kind == 'SIM_STATE':
            lat=getattr(msg,'lat_int',0) or msg.lat;lon=getattr(msg,'lon_int',0) or msg.lon
            c=math.sqrt(.5);w=c*(msg.q1+msg.q3);x=c*(msg.q2+msg.q4)
            y=c*(msg.q3-msg.q1);z=c*(msg.q4-msg.q2)
            yaw=math.degrees(math.atan2(2*(w*z+x*y),1-2*(y*y+z*z)))%360.
            self.physical_position={'latitude_deg':lat*1e-7,'longitude_deg':lon*1e-7,
                'heading_deg':yaw,'altitude_msl_m':msg.alt,'received_monotonic_s':now,
                'target_system':target_system,'source':'SIM_STATE; visualization only',
                'navigation_input':False}
            self.physical_updates+=1
        elif kind == 'HEARTBEAT':
            from pymavlink import mavutil
            self.heartbeat = {'received_monotonic_s': now,
                              'target_system': target_system,
                              'target_component': msg.get_srcComponent(),
                              'mode': mavutil.mode_string_v10(msg),
                              'armed': bool(msg.base_mode & 128)}
        elif kind == 'GLOBAL_POSITION_INT':
            coordinates = [msg.lat * 1e-7, msg.lon * 1e-7, msg.hdg * .01]
            if (not all(math.isfinite(v) for v in coordinates)
                    or not -90 <= coordinates[0] <= 90 or not -180 <= coordinates[1] <= 180
                    or msg.time_boot_ms < 0):
                return
            # A repeated FCU sample must not refresh telemetry freshness.
            if self.position is not None and msg.time_boot_ms <= self.position['time_boot_ms']:
                return
            self.position_updates += 1
            self.position = {'received_monotonic_s': now, 'target_system': target_system,
                             'target_component': msg.get_srcComponent(),
                             'time_boot_ms': msg.time_boot_ms,
                             'latitude_deg': coordinates[0], 'longitude_deg': coordinates[1],
                             'heading_deg': coordinates[2]}

    def snapshot(self, map_module, console_module, *, now=None):
        now = time.monotonic() if now is None else now
        window = getattr(map_module, 'map', None)
        console = getattr(getattr(console_module, 'mpstate', None), 'console', None)
        position = self.position or {}
        actual = (getattr(map_module, 'lat_lon_heading', {}) or {}).get(position.get('target_system'))
        expected = tuple(position.get(k) for k in ('latitude_deg', 'longitude_deg', 'heading_deg'))
        ready_event = getattr(window, 'app_ready', None)
        return {'schema': SCHEMA, 'session_id': self.session_id, 'producer_pid': os.getpid(),
                'master': MASTER, 'recorded_monotonic_s': now,
                'heartbeat': self.heartbeat, 'position': self.position,
                'position_updates': self.position_updates,
                'physical_map':{'position':self.physical_position,'position_updates':self.physical_updates,
                    'ready':bool(self.physical_position and now-self.physical_position['received_monotonic_s']<1.
                        and self.physical_updates>=2 and 'PhysicsVehicle%u'%position.get('target_system',0)
                        in (getattr(map_module,'have_vehicle',{}) or {})),
                    'navigation_input':False},
                'map': {'alive': _alive(window),
                        'wx_ready': bool(ready_event and ready_event.is_set()),
                        'child_pid': getattr(getattr(window, 'child', None), 'pid', None),
                        'position_matches': bool(position and actual is not None and tuple(actual) == expected),
                        'position': actual},
                'console': {'alive': _alive(console),
                            'child_pid': getattr(getattr(console, 'child', None), 'pid', None)},
                'navigation_input': False, 'control_command_sent': False,
                'visual_acceptance_verified': False}


def validate_panel_record(record, session_id, *, producer_pid, now=None, max_age_s=2.):
    now = time.monotonic() if now is None else now
    reasons = []
    if (record.get('schema') != SCHEMA or record.get('session_id') != session_id
            or record.get('producer_pid') != producer_pid or record.get('master') != MASTER):
        reasons.append('panel_session_or_producer_mismatch')
    for label, stamp in [('record', record.get('recorded_monotonic_s')),
                         ('heartbeat', (record.get('heartbeat') or {}).get('received_monotonic_s')),
                         ('position', (record.get('position') or {}).get('received_monotonic_s'))]:
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not 0 <= now - stamp <= max_age_s:
            reasons.append('panel_' + label + '_stale_or_missing')
    mp = record.get('map') or {}; console = record.get('console') or {}
    if not (mp.get('alive') is True and mp.get('wx_ready') is True and mp.get('position_matches') is True):
        reasons.append('map_not_ready_or_position_not_delivered')
    if console.get('alive') is not True:
        reasons.append('console_not_alive')
    for name, component in [('map', mp), ('console', console)]:
        if type(component.get('child_pid')) is not int or component['child_pid'] <= 0:
            reasons.append(name + '_window_child_missing')
    if (record.get('position_updates', 0) < 2
            or (record.get('position') or {}).get('target_system') != (record.get('heartbeat') or {}).get('target_system')
            or not (record.get('heartbeat') or {}).get('mode')):
        reasons.append('panel_stream_not_live_or_incoherent')
    return {'ready': not reasons, 'reason': 'live_native_panels' if not reasons else reasons[0],
            'reasons': reasons, 'evidence': record, 'visual_acceptance_verified': False}


def init(mpstate):
    from MAVProxy.modules.lib.mp_module import MPModule
    from .native_session import check_session, write

    state_file = Path(os.environ['TDDT_NATIVE_PANELS_STATE'])
    folder, data = check_session(state_file.parent)
    session_id = os.environ['TDDT_NATIVE_SESSION_ID']
    if state_file.name != 'native_panels.json' or data['session_id'] != session_id:
        raise ValueError('Native panel observer must use its owned session directory')

    class Observer(MPModule):
        def __init__(self):
            super().__init__(mpstate, 'native_panels', 'same-session map and Console evidence')
            self.evidence = PanelEvidence(session_id)
            self.last = 0.

        def mavlink_packet(self, msg):
            self.evidence.receive(msg, self.master.target_system)

        def idle_task(self):
            now = time.monotonic()
            if now - self.last < .2:
                return
            self.last = now
            map_module = self.module('map'); console_module = self.module('console')
            physical=self.evidence.physical_position
            if map_module is not None and physical is not None:
                # Keep the physical simulator trace explicit. Sensor EKF global
                # position is an estimate and may drift in GNSS-denied flight.
                # This display path has no connection to the MESA input API.
                map_module.map_settings.showahrspos=0
                vehicle='Vehicle%u'%physical['target_system']
                map_module.update_vehicle_icon_to_loc('Physics',vehicle,'green',True,
                    (physical['latitude_deg'],physical['longitude_deg']),physical['heading_deg'])
                map_module.map.set_follow_object('Physics'+vehicle,True)
            record = self.evidence.snapshot(map_module, console_module, now=now)
            if console_module is not None:
                console = console_module.mpstate.console
                console.set_title('MAVProxy Console [' + session_id[:8] + ']')
                position = record['position'] or {}; heartbeat = record['heartbeat'] or {}
                console.set_status('NativeSession', 'Native ' + session_id[:8], row=4)
                console.set_status('NativeFCUTime', 'FCU boot %.3f s' % (position.get('time_boot_ms', 0) / 1000.), row=4)
                if physical:
                    console.set_status('NativePhysics','JSBSim %.7f %.7f | %.2fm (display only)'%(
                        physical['latitude_deg'],physical['longitude_deg'],physical['altitude_msl_m']),row=5)
            if map_module is not None and record['map']['alive']:
                from MAVProxy.modules.mavproxy_map.mp_slipmap_util import SlipInfoText
                heartbeat = record['heartbeat'] or {}; position = record['position'] or {}
                text = 'Native %s | %s | %s | FCU %.3f s\nFCU position: visualization only' % (
                    session_id[:8], heartbeat.get('mode', 'UNKNOWN'),
                    'ARMED' if heartbeat.get('armed') else 'DISARMED', position.get('time_boot_ms', 0) / 1000.)
                if physical:text+='\nGreen: JSBSim physical pose; display only, no navigation input'
                map_module.map.add_object(SlipInfoText('native_session', text))
            write(folder / 'native_panels.json', record)

    return Observer()
