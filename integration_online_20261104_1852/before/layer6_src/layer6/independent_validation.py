"""Visible Native branches and an independent observation rollout for R7.04.

Backend setup/sealing is shared; reference execution never calls the provider,
forecast(), output_row() or select_sample(). All evaluation samples are decoded
independently later from recorded raw MAVLink frames.
"""
from copy import deepcopy
import json
import os
import time

from .native_forecast import NativeForecastRunner
from .native_replay import ObservationFeed, sha
from .native_session import write


class VisibleNativeRunner(NativeForecastRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gui_feed = None

    def record(self, master, message):
        super().record(master, message)
        if self.gui_feed is not None and (message.get_srcSystem(), message.get_srcComponent()) == (self.link.target_system, self.link.target_component):
            self.gui_feed.publish(message.get_msgbuf())

    def start(self, *, show_map=True):
        if not show_map:
            raise ValueError('v9 native validation requires GUI and map together')
        super().start(show_map=True)
        gui_port, ipc_port = 21000+10*self.instance, 22000+10*self.instance
        self.gui_feed = ObservationFeed(gui_port)
        env = dict(os.environ, TDDT_NATIVE_SESSION_ID=self.session_id, PYTHONNOUSERSITE='1',
                   PYTHONDONTWRITEBYTECODE='1', TMPDIR='/tmp')
        self.launch('phoenix_gui', [self.root/'FlightStack/ardupilot/venv-ardupilot-debian13/bin/python',
            '-B', '-m', 'layer6.reference_gui', '--layer6-root', self.root, '--port', gui_port,
            '--ipc-port', ipc_port, '--output', self.output/'GUI_OBSERVATION.json', '--session-id', self.session_id], env)
        def ready():
            path = self.output/'GUI_OBSERVATION.json'
            if not path.exists():
                return False
            gui = json.loads(path.read_text())
            return (gui['session_id'] == self.session_id and gui['connected'] and gui['visible']
                    and gui['platform'] not in ('offscreen', 'minimal') and gui['armed'] is False
                    and 0 <= self.source_time-gui['source_fcu_boot_s'] <= 1.)
        self.pump(ready, timeout=15)
        return self

    def stop(self):
        try:
            super().stop()
        finally:
            if self.gui_feed is not None:
                self.gui_feed.drain()
                if self.output.exists():
                    write(self.output/'GUI_FEED.json', {'telemetry_packets': self.gui_feed.forwarded,
                          'discarded_return_packets': self.gui_feed.discarded, 'forwarded_control_packets': 0})
                self.gui_feed.close()
                self.gui_feed = None


def _stamp(name, row):
    payload = row['message']
    return payload['time_usec']/1e6 if name == 'SERVO_OUTPUT_RAW' else payload['time_boot_ms']/1000.


class IndependentObservationRunner(VisibleNativeRunner):
    def forecast(self, request):
        raise RuntimeError('independent observation cannot call forecast/provider')

    def observe_plan(self, pilot_plan, dt, commit, candidate_hash):
        self.check_sealed_reference()
        self.candidate_consumed = True
        reference = self.reference
        low, high = reference['plant_start_bounds_s']
        mode = reference['backend']['initial_mode']
        if any(r['u_cmd'][2] != 0 or r['discrete'] or r['mode'] not in (None, mode) for r in pilot_plan):
            raise ValueError('independent reference only supports DISARMED zero-throttle constant mode')
        # Independent implementation of the production GUI gain/clamp/round
        # mapping. It is checked against the FCU raw RC readback by the validator.
        def wire(command):
            a,e,t,r = command['u_cmd']
            def axis(v,g): return int(round(1000*max(-1., min(1., v*g))))
            return {'x': axis(e,self.gains['max_pitch']), 'y': axis(a,self.gains['max_roll']),
                    'z': int(round(1000*t)), 'r': axis(r,self.gains['max_yaw'])}
        def send(values):
            self.link.send_manual(values['x'],values['y'],values['z'],values['r'])
            sends.append({'wire': dict(values), 'source_before_send_s': self.source_time,
                          'sent_monotonic_s': time.monotonic()})
        targets = sorted(set([commit]+[(i+1)*dt for i in range(len(pilot_plan))]))
        index = 0
        current = wire(pilot_plan[0])
        sends = []
        changes = [{'plan_index': 0, 'source_before_send_s': self.source_time}]
        send(current)
        self._thaw()
        next_send = time.monotonic()+1/30
        end = time.monotonic()+len(pilot_plan)*dt+5
        stamps = []
        for offset in targets:
            completed = False
            while time.monotonic() < end:
                if self.link.armed is not False or any(p.poll() is not None for p in self.children):
                    raise RuntimeError('independent native branch unavailable')
                if time.monotonic()-max(self.last_source_received,self._resume_monotonic) > 1:
                    raise TimeoutError('independent native clock stalled')
                if time.monotonic() >= next_send:
                    send(current)
                    next_send = time.monotonic()+1/30
                self.link.master.recv_match(blocking=True, timeout=.005)
                found = {name: [r for r in self.histories[name]
                                if low+offset-1e-9 <= _stamp(name,r) <= high+offset+1e-9]
                         for name in self.histories}
                ticks = {r['message']['time_boot_ms'] for r in found['LOCAL_POSITION_NED']} & {
                         r['message']['time_boot_ms'] for r in found['ATTITUDE_QUATERNION']}
                if ticks and all(found.values()):
                    completed = True
                    stamps.append({'offset_s': offset, 'joint_tick_ms': min(ticks)})
                    break
                if self.source_time > high+offset+.1:
                    raise TimeoutError('independent native observation grid unavailable')
            if not completed:
                raise TimeoutError('independent native observation deadline')
            if abs(offset-(index+1)*dt) < 1e-9 and index+1 < len(pilot_plan):
                index += 1
                current = wire(pilot_plan[index])
                send(current)
                changes.append({'plan_index': index, 'source_before_send_s': self.source_time})
                next_send = time.monotonic()+1/30
        self._freeze()
        self.trace.flush()
        result = {'schema': 'TDDT-independent-native-observation-v1',
                  'branch_session_id': self.session_id, 'candidate_hash': candidate_hash,
                  'pilot_plan': deepcopy(pilot_plan), 'prediction_dt_s': dt, 'commit_segment_s': commit,
                  'mode': mode, 'reference': reference, 'trace_sha256': sha(self.output/'telemetry.jsonl'),
                  'sample_grid_witnesses': stamps, 'sends': sends, 'changes': changes,
                  'provider_or_forecast_called': False, 'mission_state_advanced': False}
        write(self.output/'INDEPENDENT_OBSERVATION.json', result)
        self.stop()
        return result
