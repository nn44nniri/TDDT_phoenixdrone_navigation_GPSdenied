"""R7.02 cold-start native reference branches; no mission checkpoint or predictor.

Each branch has private files, ports and exact owned-process cleanup. Only
DISARMED, zero-throttle experiments are supported by this revision. Unobserved
controller state is never restored from GUI telemetry or invented zeros.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

from .native_session import proc, processes, write

REVISION = 'phoenix-isolated-native-reference-r7.02-v1'
MODES = {'QSTABILIZE', 'QHOVER', 'QLOITER', 'FBWA'}
BOOT_DERIVED = {
    'STAT_BOOTCNT': 'AP_Stats/AP_Stats.cpp: bootcount set_and_save(bootcount+1)',
    'ARSPD_DEVID': 'AP_Airspeed: native sensor discovery assigns device identity',
    'BARO1_GND_PRESS': 'AP_Baro/AP_Baro.cpp: boot calibration updates internal ground_pressure',
    'BARO2_GND_PRESS': 'AP_Baro/AP_Baro.cpp: boot calibration updates internal ground_pressure',
    'COMPASS_DEC': 'AP_Compass/AP_Compass.cpp: AUTODEC updates declination for the observed location',
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def json_observation(value):
    """Keep MAVLink's explicit unavailable NaN without inventing numeric data."""
    if isinstance(value, float) and not math.isfinite(value):
        return {'nonfinite_MAVLink_value': repr(value)}
    if isinstance(value, dict):
        return {k: json_observation(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_observation(v) for v in value]
    if isinstance(value, bytes):
        return {'raw_bytes_hex': value.hex()}
    return value


def parameters(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        name, raw = line.split()
        value = float(raw)
        if name in result or not math.isfinite(value):
            raise ValueError('invalid reference parameter: ' + name)
        result[name] = value
    if not result:
        raise ValueError('empty parameter reference')
    return result


def compare_parameters(reference, found, wind):
    """Exact tuning/semantics; explicit boot-derived observations, never a clone.

    The complete actual map, including calibration, is bound to this reference
    trace. The historical startup snapshot is not a restorable FCU checkpoint.
    """
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
           for values in (reference, found, wind) for v in values.values()):
        raise ValueError('nonfinite or nonnumeric reference parameter map')
    expected = {**reference, **wind}
    mismatch = {}
    boot = {}
    for k, v in expected.items():
        actual = found.get(k)
        if actual is None or not math.isfinite(actual):
            mismatch[k] = {'expected': v, 'actual': actual}
            continue
        if abs(actual - struct.unpack('f', struct.pack('f', v))[0]) <= 1e-6*max(1, abs(v)):
            continue
        admissible = (
            (k == 'STAT_BOOTCNT' and actual == v + 1)
            or (k == 'ARSPD_DEVID' and reference.get('ARSPD_TYPE') == 100 and actual > 0 and actual == int(actual))
            or (k in ('BARO1_GND_PRESS', 'BARO2_GND_PRESS') and 0 < actual < 200000)
            or (k == 'COMPASS_DEC' and reference.get('COMPASS_AUTODEC') == 1 and abs(actual) <= math.pi))
        if admissible:
            boot[k] = {'historical': v, 'observed': actual, 'source': BOOT_DERIVED[k],
                       'mission_state_equivalence_claimed': False}
        else:
            mismatch[k] = {'expected': v, 'actual': actual}
    return {'mismatches': mismatch, 'missing': sorted(set(expected)-set(found)),
            'extra': sorted(set(found)-set(expected)), 'boot_derived_observations': boot,
            'wind_override_from_bound_forcing': wind,
            'reference_parameters_sha256': digest(reference), 'actual_parameters_sha256': digest(found),
            'comparison': 'float32 MAVLink representation; relative tolerance 1e-6; enumerated native boot observations',
            'parameter_count': len(found), 'restored_mission_checkpoint': False}


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {'candidate_id', 'mode', 'u_cmd', 'duration_s'}:
        raise ValueError('reference candidate fields')
    if not isinstance(plan['candidate_id'], str) or not 1 <= len(plan['candidate_id']) <= 128:
        raise ValueError('candidate identity')
    if plan['mode'] not in MODES:
        raise ValueError('reference mode')
    from layer4.native_provider_contract import pilot_to_manual
    pilot_to_manual(plan['u_cmd'], {'max_pitch': 1., 'max_roll': 1., 'max_yaw': 1.})
    if plan['u_cmd'][2] != 0:
        raise ValueError('R7.02 branch requires zero throttle and DISARMED')
    dt = plan['duration_s']
    if isinstance(dt, bool) or not isinstance(dt, (int, float)) or not math.isfinite(dt) or not .1 <= dt <= 5:
        raise ValueError('reference duration must be .1..5 source seconds')
    return json.loads(json.dumps(plan, allow_nan=False))


def ports(instance):
    if isinstance(instance, bool) or not isinstance(instance, int) or not 20 <= instance <= 80:
        raise ValueError('isolated reference instance must be 20..80')
    return {'fcu_tcp': 5760 + 10*instance, 'serial1_tcp': 5762 + 10*instance,
            'serial2_tcp': 5763 + 10*instance, 'control_tcp': 5505 + 10*instance,
            'fdm_udp': 5504 + 10*instance, 'rates_udp': 5506 + 10*instance,
            'rc_udp': 5501 + 10*instance, 'irlock_udp': 9005 + 10*instance,
            'map_udp': 20000 + 10*instance}


def map_observation_current(observation, session_id, source_time, now):
    """Viewer freshness never comes from repeatedly repainting an old packet."""
    try:
        received = observation['received_monotonic_s']
        source = observation['source_fcu_boot_s']
        return (observation['session_id'] == session_id and observation['map_alive'] is True
                and observation['wx_ready'] is True and observation['position_matches_map'] is True
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                        for v in (received, source, source_time, now))
                and 0 <= now-received <= 1 and 0 <= source_time-source <= 1)
    except (KeyError, TypeError):
        return False


@contextmanager
def reserve_ports(instance):
    """Refuse occupied endpoints. No reuse and no cleanup of another owner."""
    import fcntl
    endpoints = ports(instance)
    # A process-lifetime instance lock also closes the gap between bind probes
    # and ArduPilot binding its ports. Other applications can still race us;
    # their failure is observed as a startup failure, never killed.
    with open('/tmp/tddt-native-replay-%s-%s.lock' % (os.getuid(), instance), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = []
        try:
            for name, port in endpoints.items():
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if name.endswith('udp') else socket.SOCK_STREAM)
                held.append(s)
                s.bind(('127.0.0.1', port))
            for s in held:
                s.close()
            yield
        finally:
            for s in held:
                s.close()


class ObservationFeed:
    """FCU bytes go to the map; every byte returning from the map is discarded."""
    def __init__(self, port):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.setblocking(False)
        self.target = ('127.0.0.1', port)
        self.forwarded = 0
        self.discarded = 0

    def publish(self, raw):
        self.socket.sendto(raw, self.target)
        self.forwarded += 1
        self.drain()

    def drain(self):
        for _ in range(256):
            try:
                self.socket.recvfrom(65536)
                self.discarded += 1
            except BlockingIOError:
                break

    def close(self):
        self.socket.close()


class NativeReferenceReplay:
    def __init__(self, *, layer6_root, output, binding_file, parameter_file,
                 backend_hashes, mission_session_id, instance=42):
        self.root = Path(layer6_root).resolve(strict=True)
        self.output = Path(output).absolute()
        # Output is fresh and cannot be the live runtime or a source/input tree.
        if self.output.exists() or self.output.is_symlink():
            raise ValueError('reference output must be new')
        allowed = (self.root.parent / 'TDOC_v20260911_2052/reports', Path('/tmp'))
        resolved = self.output.resolve()
        if not any(resolved.is_relative_to(p.resolve()) for p in allowed):
            raise ValueError('reference output must be private reports or /tmp')
        if not isinstance(mission_session_id, str) or len(mission_session_id) != 32:
            raise ValueError('explicit mission session identity required')
        self.session_id = uuid.uuid4().hex
        if self.session_id == mission_session_id:
            raise ValueError('reference branch cannot be the mission')
        self.mission_session_id = mission_session_id
        self.binding_file = Path(binding_file).resolve(strict=True)
        self.parameter_file = Path(parameter_file).resolve(strict=True)
        for p in (self.binding_file, self.parameter_file):
            if p.is_relative_to(resolved):
                raise ValueError('output overlaps reference input')
        self.expected = dict(backend_hashes)
        self.instance = instance
        self.endpoints = ports(instance)
        self.children = []
        self.link = None
        self.feed = None
        self.trace = None
        self.latest = {}
        self.counts = {}
        self.heartbeats = []
        self.position = None
        self.source_time = None
        self.last_source_received = None
        self.running = False
        self.candidate_consumed = False

    def verify_backend(self):
        from .telemetry_source import native_source_contract
        from .binding import load_context
        source = native_source_contract()
        if not source['deployment_verified']:
            raise ValueError('unqualified native source deployment')
        binding, cfg = load_context(self.binding_file)
        if cfg.path_value('TDDT_LAYER6_ROOT', required=True).resolve() != self.root:
            raise ValueError('reference Layer6 root differs from authoritative binding')
        fs = self.root / 'FlightStack'
        files = {'arduplane': fs / 'ardupilot/build/sitl/bin/arduplane',
                 'jsbsim': fs / 'jsbsim/build/src/JSBSim',
                 'aircraft': fs / 'ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml',
                 'reset': fs / 'ardupilot/Tools/autotest/aircraft/phoenix_v3/reset.xml',
                 'defaults': fs / 'ardupilot/Tools/autotest/default_params/phoenix_v3.parm',
                 'gui_config': self.root / 'gui/config/f310_phoenix.yaml',
                 'parameters': self.parameter_file}
        actual = {k: sha(p) for k, p in files.items()}
        if actual != self.expected:
            raise ValueError('reference backend identity mismatch')
        return binding, files

    def launch(self, role, argv, env):
        log = (self.output / (role + '.log')).open('w')
        try:
            child = subprocess.Popen(list(map(str, argv)), cwd=self.output,
                                     env=dict(env, TDDT_NATIVE_ROLE=role),
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        self.children.append(child)
        write(self.output / (role + '_process.json'), {'pid': child.pid, 'role': role,
              'argv': list(map(str, argv)), 'session_id': self.session_id})
        return child

    def record(self, master, message):
        if (message.get_srcSystem(), message.get_srcComponent()) != (self.link.target_system, self.link.target_component):
            return
        kind = message.get_type()
        if kind == 'BAD_DATA':
            raise ValueError('invalid native MAVLink packet')
        now = time.monotonic()
        row = {'received_monotonic_s': now, 'source_system': message.get_srcSystem(),
               'source_component': message.get_srcComponent(), 'message': json_observation(message.to_dict()),
               'raw_MAVLink_hex': bytes(message.get_msgbuf()).hex()}
        self.trace.write(json.dumps(row, allow_nan=False) + '\n')
        self.latest[kind] = row
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if self.feed is not None:
            self.feed.publish(message.get_msgbuf())
        if kind == 'HEARTBEAT':
            armed = bool(message.base_mode & 128)
            self.heartbeats.append({'received_monotonic_s': now, 'armed': armed,
                                    'custom_mode': message.custom_mode})
            self.link._update_heartbeat(message)
            if armed:
                raise RuntimeError('reference branch unexpectedly ARMED; stopping owned branch')
        if kind == 'ATTITUDE':
            if not all(math.isfinite(getattr(message, k)) for k in
                       ('roll', 'pitch', 'yaw', 'rollspeed', 'pitchspeed', 'yawspeed')):
                raise ValueError('nonfinite required native attitude/rates')
            source = message.time_boot_ms / 1000.
            if self.source_time is not None and source < self.source_time:
                raise RuntimeError('reference FCU clock moved backwards')
            self.source_time = source
            self.last_source_received = now
        if kind == 'GLOBAL_POSITION_INT':
            self.position = row
        if kind == 'SERVO_OUTPUT_RAW' and message.port == 0:
            if message.servo3_raw > 1000 or message.servo4_raw > 1000:
                raise RuntimeError('unexpected motor output in DISARMED zero-throttle reference')

    def pump(self, predicate, timeout=10.):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(p.poll() is not None for p in self.children):
                raise RuntimeError('owned native component exited')
            self.link.master.recv_match(blocking=True, timeout=.05)
            if predicate():
                return
        raise TimeoutError('reference evidence deadline')

    def collect_parameters(self, timeout=35.):
        master = self.link.master
        found = {}
        indexes = set()
        count = None
        target = (self.link.target_system, self.link.target_component)
        def receive(_, msg):
            nonlocal count
            if msg.get_type() != 'PARAM_VALUE' or (msg.get_srcSystem(), msg.get_srcComponent()) != target:
                return
            name = msg.param_id
            if isinstance(name, bytes):
                name = name.decode('ascii')
            name = name.rstrip('\0')
            if not math.isfinite(msg.param_value):
                raise ValueError('nonfinite native parameter')
            if count is not None and count != msg.param_count:
                raise ValueError('native parameter count changed during snapshot')
            count = msg.param_count
            found[name] = float(msg.param_value)
            indexes.add(msg.param_index)
        master.message_hooks.append(receive)
        try:
            master.mav.param_request_list_send(*target)
            end = time.monotonic() + timeout
            retry_at = time.monotonic() + 3
            while time.monotonic() < end:
                master.recv_match(blocking=True, timeout=.05)
                if count is not None and len(indexes) == count and len(found) == count:
                    return found
                if count is not None and time.monotonic() >= retry_at:
                    for index in sorted(set(range(count)) - indexes)[:100]:
                        master.mav.param_request_read_send(*target, b'', index)
                    retry_at = time.monotonic() + 2
            raise TimeoutError('incomplete native parameter snapshot')
        finally:
            master.message_hooks.remove(receive)

    def start(self, *, show_map=True):
        import shutil
        import yaml
        binding, files = self.verify_backend()
        reference = parameters(self.parameter_file)
        self.output.mkdir(parents=True, exist_ok=False)
        self.running = True
        fs = self.root / 'FlightStack'
        ap = fs / 'ardupilot'
        private = self.output / 'autotest'
        shutil.copytree(files['aircraft'].parent, private / 'aircraft/phoenix_v3')
        if (sha(private/'aircraft/phoenix_v3/phoenix_v3.xml') != self.expected['aircraft']
                or sha(private/'aircraft/phoenix_v3/reset.xml') != self.expected['reset']):
            raise ValueError('private reference model copy mismatch')
        shutil.copy2(self.parameter_file, self.output / 'reference.parm')
        shutil.copy2(self.binding_file, self.output / 'binding.json')
        write(self.output / 'session.json', {'schema': REVISION, 'session_id': self.session_id,
              'mission_session_id': self.mission_session_id, 'uid': os.getuid(),
              'model_id': binding['model_id'], 'backend': self.expected,
              'ports': self.endpoints, 'instance': self.instance, 'binding_hash': binding['binding_hash'],
              'cold_boot_reference': True, 'restored_mission_checkpoint': False,
              'internal_state': 'native boot; not a known mission controller state',
              'uncertainty': {'kind': 'unbounded', 'reason': 'controller/mixer/spool/actuator internal mission state unavailable'},
              'flight_authorized': False})
        env = dict(os.environ, TDDT_NATIVE_SESSION_ID=self.session_id,
                   PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', TMPDIR='/tmp',
                   PATH=str(fs / 'jsbsim/build/src') + os.pathsep + os.environ['PATH'])
        self.trace = (self.output / 'telemetry.jsonl').open('w', buffering=1)
        origin = binding['mission_origin']
        home = ','.join(str(origin[k]) for k in ('latitude_deg', 'longitude_deg', 'terrain_elevation_msl_m', 'heading_deg'))
        self.launch('arduplane', [files['arduplane'], '--model', 'jsbsim:phoenix_v3', '--home', home,
                    '--speedup', '1', '-I', self.instance, '--autotest-dir', private,
                    '--defaults', self.output / 'reference.parm'], env)
        # Wait only on this branch's endpoint. Never connect to a mission port.
        end = time.monotonic() + 10
        while True:
            try:
                probe = socket.create_connection(('127.0.0.1', self.endpoints['fcu_tcp']), timeout=.1)
                probe.close()
                break
            except OSError:
                if time.monotonic() > end:
                    raise TimeoutError('reference FCU TCP startup')
                time.sleep(.1)
        from .mavlink_link import MavlinkLink
        gui = yaml.safe_load(files['gui_config'].read_text())
        self.gains = {k: gui['controller'][k] for k in ('max_pitch', 'max_roll', 'max_yaw')}
        self.link = MavlinkLink(gui, override_connection='tcp:127.0.0.1:%s' % self.endpoints['fcu_tcp'])
        self.link.connect()
        if self.link.armed is not False:
            raise RuntimeError('reference startup must be observed DISARMED')
        self.link.master.message_hooks.append(self.record)
        from pymavlink import mavutil
        self.link.master.mav.command_long_send(self.link.target_system, self.link.target_component,
                    mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                    mavutil.mavlink.MAVLINK_MSG_ID_RC_CHANNELS, 50000, 0, 0, 0, 0, 0)
        if show_map:
            self.feed = ObservationFeed(self.endpoints['map_udp'])
            python = ap / 'venv-ardupilot-debian13/bin/python'
            self.launch('map', [python, '-B', '-m', 'layer6.replay_map', '--port', self.endpoints['map_udp'],
                               '--output', self.output / 'map', '--session-id', self.session_id,
                               '--mavproxy', ap / 'venv-ardupilot-debian13/bin/mavproxy.py'], env)
        self.pump(lambda: self.source_time is not None and self.source_time >= 8 and self.position is not None, timeout=25)
        # New binding uses unchanged accepted forcing. Sample and independently
        # GET the six wind parameters at this branch's observed source clock.
        adapter_root = fs / 'phoenix_environment_adapter_v2'
        if str(adapter_root) not in sys.path:
            sys.path.insert(0, str(adapter_root))
        from phoenix_env_adapter.bound import BoundEnvironment
        from phoenix_env_adapter.native_forcing import apply_wind
        if not Path(sys.modules['phoenix_env_adapter.bound'].__file__).resolve().is_relative_to(adapter_root):
            raise ValueError('Environment Adapter outside the explicit native backend')
        from .binding import wind_parameter_plan
        provider = BoundEnvironment(self.binding_file)
        pos = self.position['message']
        state = provider.sample(self.source_time, latitude=pos['lat']/1e7, longitude=pos['lon']/1e7,
                                altitude_msl_m=pos['alt']/1000., source='isolated MAVLink ATTITUDE.time_boot_ms')
        state['application'] = apply_wind(self.link.master, state)
        provider.check_context()
        write(self.output / 'ENVIRONMENT_REFERENCE.json', state)
        wind = wind_parameter_plan(state['model_outputs'])
        found = self.collect_parameters()
        comparison = compare_parameters(reference, found, wind)
        write(self.output / 'PARAMETER_COMPARISON.json', comparison)
        write(self.output / 'PARAMETERS.json', found)
        if comparison['mismatches'] or comparison['missing'] or comparison['extra']:
            raise ValueError('reference parameters do not match native snapshot')
        if show_map:
            def ready():
                p = self.output / 'map/observation.json'
                if not p.exists():
                    return False
                m = json.loads(p.read_text())
                return map_observation_current(m, self.session_id, self.source_time, time.monotonic())
            self.pump(ready, timeout=15)
        return self

    def run_candidate(self, plan):
        plan = validate_plan(plan)
        if self.candidate_consumed:
            raise RuntimeError('one candidate per cold reference branch; start a new branch')
        if not self.running or self.link is None or self.link.armed is not False:
            raise RuntimeError('DISARMED reference branch required')
        self.candidate_consumed = True
        self.verify_backend()
        from layer4.native_provider_contract import pilot_to_manual
        wire = pilot_to_manual(plan['u_cmd'], self.gains)
        if not self.link.set_mode(plan['mode']):
            raise ValueError('native reference mode unavailable')
        self.pump(lambda: self.link.mode == plan['mode'], timeout=5)
        start = self.source_time
        before = {k: v for k, v in self.latest.items() if k in ('ATTITUDE', 'ATTITUDE_QUATERNION', 'SERVO_OUTPUT_RAW', 'GLOBAL_POSITION_INT')}
        sends = []
        next_send = time.monotonic()
        end = time.monotonic() + plan['duration_s'] + 5
        while self.source_time - start < plan['duration_s']:
            if time.monotonic() > end or time.monotonic() - self.last_source_received > 1:
                raise TimeoutError('native reference source clock stalled')
            if any(p.poll() is not None for p in self.children):
                raise RuntimeError('native reference component exited')
            if time.monotonic() >= next_send:
                self.link.send_manual(wire['x'], wire['y'], wire['z'], wire['r'])
                sends.append({'monotonic_s': time.monotonic(), 'last_observed_fcu_boot_s': self.source_time, 'wire': wire})
                next_send = time.monotonic() + 1/30
            self.link.master.recv_match(blocking=True, timeout=.01)
        stop = self.source_time
        actual = json.loads((self.output/'PARAMETERS.json').read_text())
        rc = self.latest['RC_CHANNELS']['message']
        expected_rc = {}
        for axis, value, offset, scaler in (
                ('ROLL', wire['y'], 1000, 2000), ('PITCH', -wire['x'], 1000, 2000),
                ('THROTTLE', wire['z'], 0, 1000), ('YAW', wire['r'], 1000, 2000)):
            channel = int(actual['RCMAP_'+axis])
            low, high = (actual['RC%d_%s' % (channel, suffix)] for suffix in ('MIN', 'MAX'))
            expected_rc['chan%d_raw' % channel] = int(low + (high-low)*(value+offset)/scaler)
        if rc['time_boot_ms']/1000. <= start or any(rc[k] != v for k, v in expected_rc.items()):
            raise RuntimeError('post-command FCU RC readback does not match pilot mapping')
        for key in ('SERVO_OUTPUT_RAW', 'ATTITUDE', 'GLOBAL_POSITION_INT'):
            if self.latest[key]['received_monotonic_s'] <= sends[0]['monotonic_s']:
                raise RuntimeError('missing actual post-command plant/mixer evidence')
        # End output by stopping this branch, never by pretending GUI cancellation
        # released the FCU override. No further candidate reuses its internal state.
        self.trace.flush()
        result = {'schema': REVISION, 'candidate': plan, 'candidate_sha256': digest(plan),
                  'branch_session_id': self.session_id, 'mission_session_id': self.mission_session_id,
                  'initial_reference_sha256': digest({'backend': self.expected, 'binding': sha(self.binding_file),
                                                      'state': before, 'parameters': sha(self.output/'PARAMETERS.json')}),
                  'start_fcu_boot_s': start, 'stop_fcu_boot_s': stop, 'source_clock': 'FCU_BOOT',
                  'send_clock_semantics': 'last observed source time is a bound, not exact FCU application time',
                  'before': before, 'after': self.latest, 'sends': sends,
                  'expected_FCU_RC_channels': expected_rc, 'FCU_pilot_mapping_readback_verified': True,
                  'uncertainty': {'kind': 'unbounded', 'reason': 'cold native boot is not restored mission controller/spool state'},
                  'prediction_created': False, 'certificate_created': False, 'flight_authorized': False,
                  'mission_plant_advanced': False, 'map_control_forwarded': False}
        write(self.output / 'CANDIDATE_RESULT.json', result)
        return result

    def stop(self):
        if not self.running:
            return
        # Use same-UID, UUID and PID birth checks, including JSBSim's separate
        # process session and the wx child. Do not signal shared process groups.
        for sig, timeout in ((signal.SIGTERM, 3), (signal.SIGKILL, 1)):
            for row in processes(self.session_id):
                current = proc(row['pid'])
                if current and all(current[k] == row[k] for k in ('pid', 'uid', 'start_ticks', 'session_id')):
                    try:
                        os.kill(row['pid'], sig)
                    except ProcessLookupError:
                        pass
            end = time.monotonic() + timeout
            while processes(self.session_id) and time.monotonic() < end:
                time.sleep(.05)
        for p in self.children:
            p.wait(timeout=2)
        if self.link is not None and self.link.master is not None:
            self.link.master.close()
        if self.trace is not None:
            self.trace.close()
        feed = None
        if self.feed is not None:
            self.feed.drain()
            feed = {'telemetry_packets': self.feed.forwarded, 'discarded_map_packets': self.feed.discarded,
                    'forwarded_map_control_packets': 0}
            self.feed.close()
        remaining = processes(self.session_id)
        write(self.output / 'STOP_RESULT.json', {'session_id': self.session_id, 'stopped': not remaining,
              'remaining': remaining, 'heartbeats': self.heartbeats, 'message_counts': self.counts,
              'telemetry_sha256': sha(self.output/'telemetry.jsonl') if (self.output/'telemetry.jsonl').is_file() else None,
              'map_feed': feed})
        self.running = False
        if remaining:
            raise RuntimeError('owned reference processes remained after stop')
