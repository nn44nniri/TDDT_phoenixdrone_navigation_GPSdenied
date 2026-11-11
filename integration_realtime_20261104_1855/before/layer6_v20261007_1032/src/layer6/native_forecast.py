"""Isolated R7.03 forecast runner using the accepted real Native backend.

Freeze/thaw applies only to the private branch's exact owned ArduPlane/JSBSim
processes. No checkpoint of an active mission is claimed or manufactured.
"""
from collections import deque
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import signal
import time

from .native_replay import NativeReferenceReplay, digest, sha
from .native_session import proc, processes, write

CHANNELS = ('LOCAL_POSITION_NED', 'ATTITUDE_QUATERNION', 'SERVO_OUTPUT_RAW', 'RC_CHANNELS')


def source_time(name, row):
    msg = row['message']
    key, scale = ('time_usec', 1e6) if name == 'SERVO_OUTPUT_RAW' else ('time_boot_ms', 1000.)
    value = msg[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid native source clock')
    return value/scale


def expected_rc(wire, parameters):
    result = {}
    for axis, value, offset, scaler in (
            ('ROLL', wire['y'], 1000, 2000), ('PITCH', -wire['x'], 1000, 2000),
            ('THROTTLE', wire['z'], 0, 1000), ('YAW', wire['r'], 1000, 2000)):
        channel = int(parameters['RCMAP_'+axis])
        low, high = (parameters['RC%d_%s' % (channel, x)] for x in ('MIN', 'MAX'))
        result['chan%d_raw' % channel] = int(low+(high-low)*(value+offset)/scaler)
    return result


def select_sample(histories, lower, upper, wire, parameters, mode, offset):
    """Use actual packets, matching position/quaternion source ticks exactly."""
    selected = {}
    for name in CHANNELS:
        selected[name] = [r for r in histories[name] if lower-1e-9 <= source_time(name, r) <= upper+1e-9]
        if not selected[name]:
            return None
    local = {r['message']['time_boot_ms']: r for r in selected['LOCAL_POSITION_NED']}
    quat = {r['message']['time_boot_ms']: r for r in selected['ATTITUDE_QUATERNION']}
    common = sorted(local.keys() & quat.keys())
    if not common:
        return None
    tick = common[0]
    lrow, qrow = local[tick], quat[tick]
    rc_expected = expected_rc(wire, parameters)
    rc_rows = [r for r in selected['RC_CHANNELS']
               if all(r['message'].get(k) == v for k, v in rc_expected.items())]
    if not rc_rows:
        return None
    rcrow = rc_rows[0]
    prow = selected['SERVO_OUTPUT_RAW'][0]
    q, pos, pwm = qrow['message'], lrow['message'], prow['message']
    vector = [pos[k] for k in ('x', 'y', 'z', 'vx', 'vy', 'vz')]
    vector += [q[k] for k in ('q1', 'q2', 'q3', 'q4', 'rollspeed', 'pitchspeed', 'yawspeed')]
    pulses = [pwm['servo%d_raw' % i] for i in range(1, 5)]
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector+pulses)
            or abs(sum(v*v for v in vector[6:10])-1) > .002
            or any(not 0 < v < 65535 for v in pulses)):
        raise ValueError('invalid required physical native sample')
    return {'offset_s': offset, 'mode': mode, 'manual_control': [wire[k] for k in ('x', 'y', 'z', 'r')],
            'pwm': pulses, 'plant_state': vector, 'plant_time_s': tick/1000.,
            'rc_time_s': source_time('RC_CHANNELS', rcrow),
            'pwm_time_s': source_time('SERVO_OUTPUT_RAW', prow),
            'witness': {'LOCAL_POSITION_NED': lrow, 'ATTITUDE_QUATERNION': qrow,
                        'SERVO_OUTPUT_RAW': prow, 'RC_CHANNELS': rcrow}}


class NativeForecastRunner(NativeReferenceReplay):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.histories = {k: deque(maxlen=2048) for k in CHANNELS}
        self._reference = None
        self._frozen = []
        self._next_send = 0.
        self._wire = None
        self.application = []
        self._resume_monotonic = 0.

    @property
    def reference(self):
        return deepcopy(self._reference)

    def _observe_forecast(self, master, message):
        name = message.get_type()
        if name in self.histories and (message.get_srcSystem(), message.get_srcComponent()) == (self.link.target_system, self.link.target_component):
            self.histories[name].append(deepcopy(self.latest[name]))

    def _send(self):
        wire = self._wire
        if time.monotonic() >= self._next_send:
            before = self.source_time
            self.link.send_manual(wire['x'], wire['y'], wire['z'], wire['r'])
            self.application.append({'wire': dict(wire), 'received_source_before_send_s': before,
                                     'sent_monotonic_s': time.monotonic()})
            self._next_send = time.monotonic()+1/30

    def _tick(self):
        if any(p.poll() is not None for p in self.children) or self.link.armed is not False:
            raise RuntimeError('native forecast branch unavailable or not DISARMED')
        if time.monotonic()-max(self.last_source_received, self._resume_monotonic) > 1:
            raise TimeoutError('native forecast source clock stalled')
        self._send()
        self.link.master.recv_match(blocking=True, timeout=.005)

    def _signal_owned(self, row, sig):
        current = proc(row['pid'])
        if not current or any(current[k] != row[k] for k in ('pid', 'uid', 'start_ticks', 'session_id')):
            raise RuntimeError('native forecast ownership/PID-birth changed')
        os.kill(row['pid'], sig)

    def _freeze(self):
        rows = processes(self.session_id)
        # Stop controller first; JSBSim remains lockstep and receives no new
        # controller frames. Its separate process session is owned explicitly.
        native = sorted((r for r in rows if r['role'] in ('arduplane', 'JSBSim')),
                        key=lambda r: r['role'] != 'arduplane')
        if {r['role'] for r in native} != {'arduplane', 'JSBSim'}:
            raise RuntimeError('complete owned native process pair required')
        for row in native:
            self._signal_owned(row, signal.SIGSTOP)
            self._frozen.append(row)
        end = time.monotonic()+.5
        while not all(self._is_stopped(row) for row in self._frozen):
            if time.monotonic() > end:
                raise RuntimeError('owned native pair did not stop')
            time.sleep(.001)
        for _ in range(4096):
            if self.link.master.recv_match(blocking=False) is None:
                break
        else:
            raise RuntimeError('native forecast pre-freeze receive queue unbounded')

    def _thaw(self):
        for row in reversed(self._frozen):
            self._signal_owned(row, signal.SIGCONT)
        self._frozen = []
        # Resume grace is a watchdog anchor, never a new telemetry receipt.
        self._resume_monotonic = time.monotonic()

    @staticmethod
    def _is_stopped(row):
        try:
            return (Path('/proc')/str(row['pid'])/'stat').read_text().rsplit(')', 1)[1].split()[0] in ('T', 't')
        except OSError:
            return False

    def prepare_reference(self, mode):
        from pymavlink import mavutil
        from layer4.native_provider_contract import MODES, INITIAL_FIELDS, unknown_state
        from layer4.native_reference_provider import observed_channel
        from .timebase import monotonic_domain
        if mode not in MODES or not self.running or self.candidate_consumed or self._reference is not None:
            raise ValueError('one new running isolated reference per candidate required')
        self.verify_backend()
        master = self.link.master
        master.message_hooks.append(self._observe_forecast)
        master.mav.command_long_send(self.link.target_system, self.link.target_component,
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED, 50000, 0, 0, 0, 0, 0)
        master.mav.command_long_send(self.link.target_system, self.link.target_component,
            mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE, 0,
            mavutil.mavlink.MAVLINK_MSG_ID_GPS_GLOBAL_ORIGIN, 0, 0, 0, 0, 0, 0)
        if not self.link.set_mode(mode):
            raise ValueError('unsupported reference mode')
        self._wire = {'x': 0, 'y': 0, 'z': 0, 'r': 0, 'buttons': 0}
        self.parameters = json.loads((self.output/'PARAMETERS.json').read_text())
        end = time.monotonic()+15
        sample = None
        while time.monotonic() < end:
            self._tick()
            if self.link.mode != mode or 'GPS_GLOBAL_ORIGIN' not in self.latest:
                continue
            sample = select_sample(self.histories, max(0., self.source_time-.05), self.source_time+.001,
                                   self._wire, self.parameters, mode, 0.)
            if sample is not None:
                break
        if sample is None:
            raise TimeoutError('no source-matched position/quaternion/RC/mixer reference')
        self._freeze()
        # Select the latest available joint timestamp after draining every
        # already-written packet from the stopped private branch.
        sample = select_sample(self.histories, max(0., self.source_time-.05), self.source_time+.001,
                               self._wire, self.parameters, mode, 0.)
        if sample is None:
            raise RuntimeError('frozen native reference sample unavailable')
        self._initial_sample = sample
        stamps = [sample['plant_time_s'], sample['rc_time_s'], sample['pwm_time_s'], self.source_time]
        lower = min(stamps)
        # One declared 20-Hz telemetry period of acquisition uncertainty. This
        # is not an execution duration, first-application time or R7.04 tolerance.
        upper = max(stamps)+.05
        if upper-lower > .101:
            raise ValueError('frozen native sample cohort too wide')
        self._anchor = lower
        self.trace.flush()
        initial_artifact = sha(self.output/'telemetry.jsonl')
        initial = {k: unknown_state(k, 'no mission controller checkpoint; isolated reference only') for k in INITIAL_FIELDS}
        initial['attitude_body'] = observed_channel('attitude_body', sample['plant_state'][6:], sample['plant_time_s'], initial_artifact)
        initial['previous_pilot'] = observed_channel('previous_pilot', [0., 0., 0., 0.], sample['rc_time_s'], initial_artifact)
        binding, files = self.verify_backend()
        if sha(Path(__file__)) != sha(self.root/'src/layer6/native_forecast.py'):
            raise ValueError('deployed native forecast module differs from model source')
        source_paths = [self.root/'src/layer6/native_replay.py', self.root/'src/layer6/native_forecast.py',
                        self.root/'src/layer6/mavlink_link.py', self.root/'src/gui/phoenix_f310_gui.py',
                        self.root/'FlightStack/ardupilot/ArduPlane/GCS_MAVLink_Plane.cpp',
                        self.root/'FlightStack/ardupilot/libraries/GCS_MAVLink/GCS_Common.cpp']
        sources = {str(p.relative_to(self.root)): sha(p) for p in source_paths}
        backend = {'arduplane_sha256': self.expected['arduplane'], 'jsbsim_sha256': self.expected['jsbsim'],
                   'aircraft_sha256': self.expected['aircraft'], 'defaults_sha256': self.expected['defaults'],
                   'gui_config_sha256': self.expected['gui_config'], 'execution_path_sha256': digest(sources),
                   'parameter_snapshot_sha256': digest(self.parameters), 'parameters': self.parameters,
                   'gui_gains': self.gains, 'initial_mode': mode, 'armed': False}
        self.origin = deepcopy(self.latest['GPS_GLOBAL_ORIGIN'])
        clock = {'source': 'FCU_BOOT; original MAVLink ticks', 'branch_session_id': self.session_id,
                 'source_bounds_s': [lower, upper], 'position_origin': self.origin,
                 'no_JSBSim_to_FCU_clock_equality_assumed': True}
        self._reference = {'schema': 'TDDT-native-sealed-reference-v1', 'branch_session_id': self.session_id,
            'scope': 'PAUSED_ISOLATED_COLD_BOOT; NOT_A_MISSION_CHECKPOINT', 'backend': backend,
            'context': {'model_id': binding['model_id'], 'binding_hash': binding['binding_hash'],
                        'handoff_hash': binding['handoff_hash'], 'forcing_sha256': binding['forcing_file_sha256'],
                        'native_session_id': self.mission_session_id, 'telemetry_session_id': self.session_id},
            'initial_state': initial, 'plant_start_bounds_s': [lower, upper],
            'monotonic_domain_id': monotonic_domain(), 'clock_mapping_sha256': digest(clock),
            'coordinate_origin': self.origin, 'initial_sample': sample,
            'frozen_processes': self._frozen, 'execution_sources': sources, 'clock_mapping': clock}
        write(self.output/'SEALED_REFERENCE.json', self._reference)
        return self.reference

    def check_sealed_reference(self):
        if not self.running or not self._frozen or self.candidate_consumed:
            raise ValueError('native sealed reference no longer available')
        if self._reference != json.loads((self.output/'SEALED_REFERENCE.json').read_text()):
            raise ValueError('native sealed reference artifact changed')
        if self.parameters != json.loads((self.output/'PARAMETERS.json').read_text()):
            raise ValueError('native sealed parameter map changed')
        self.verify_backend()
        for row in self._frozen:
            current = proc(row['pid'])
            if not current or any(current[k] != row[k] for k in ('pid', 'uid', 'start_ticks', 'session_id')):
                raise ValueError('native frozen branch identity changed')
            if not self._is_stopped(row):
                raise ValueError('native sealed reference was resumed outside prediction')

    def forecast(self, request):
        from layer4.native_provider_contract import pilot_to_manual
        self.check_sealed_reference()
        self.candidate_consumed = True
        t = request['timing']
        dt, commit = t['prediction_dt_s'], t['commit_segment_s']
        points = sorted(set([commit]+[(i+1)*dt for i in range(len(request['pilot_plan']))]))
        samples = [deepcopy(self._initial_sample)]
        applications = []
        self.application = []
        self._next_send = 0.
        index = 0
        self._wire = pilot_to_manual(request['pilot_plan'][0]['u_cmd'], self.gains)
        self._send()  # Queue onto the private TCP link while only this FCU is paused.
        applications.append({'plan_index': 0, 'source_before_send_s': self.source_time,
                             'sent_monotonic_s': self.application[-1]['sent_monotonic_s'],
                             'timing': 'queued on private stream; actual RC receipt is witnessed, never invented'})
        self._thaw()
        end = time.monotonic()+len(request['pilot_plan'])*dt+5
        lower, upper = self._reference['plant_start_bounds_s']
        for offset in points:
            sample = None
            while time.monotonic() < end:
                self._tick()
                sample = select_sample(self.histories, lower+offset, upper+offset,
                    self._wire, self.parameters, request['backend']['initial_mode'], offset)
                if sample is not None:
                    break
                if self.source_time > upper+offset+.1:
                    raise TimeoutError('missing source-matched native forecast point; no extrapolation')
            if sample is None:
                raise TimeoutError('native forecast horizon deadline')
            samples.append(sample)
            # End-point samples are the left limit of the held command. Only
            # after collecting that point is the next counterfactual input sent.
            at_grid = math.isclose(offset, (index+1)*dt, abs_tol=1e-9)
            if at_grid and index+1 < len(request['pilot_plan']):
                index += 1
                self._wire = pilot_to_manual(request['pilot_plan'][index]['u_cmd'], self.gains)
                self._next_send = 0.
                self._send()
                applications.append({'plan_index': index, 'source_before_send_s': self.source_time,
                                     'sent_monotonic_s': self.application[-1]['sent_monotonic_s'],
                                     'timing': 'after previous grid sample; native source-time uncertainty retained'})
        self._freeze()
        self.trace.flush()
        artifact = sha(self.output/'telemetry.jsonl')
        horizon = [samples[0]]+[next(s for s in samples if math.isclose(s['offset_s'], (i+1)*dt, abs_tol=1e-9))
                                 for i in range(len(request['pilot_plan']))]
        result = {'trace_sha256': artifact, 'branch_session_id': self.session_id,
                  'horizon_samples': horizon,
                  'first_move_sample': next(s for s in samples if s['offset_s'] == commit),
                  'source_time_witnesses': [s['witness'] for s in samples],
                  'plan_application': {'changes': applications, 'all_sends': self.application,
                                       'target_hz': 30, 'measured_hz_qualified': False},
                  'coordinate_origin': self.origin}
        write(self.output/'NATIVE_FORECAST_TRACE.json', result)
        self.stop()
        return result

    def stop(self):
        if self._frozen:
            self._thaw()
        super().stop()
