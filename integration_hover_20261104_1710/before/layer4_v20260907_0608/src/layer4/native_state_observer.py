"""Causal set observer for Native controller/actuator research.

No hidden controller state is inferred from a quiet disarmed trajectory.  The
JSBSim motor lag is a different state from FCU spool and from physical RPM.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import math
import xml.etree.ElementTree as ET

from .native_operational import require, finite, vector
from .native_provider_contract import digest


def aircraft_contract(path):
    """Read the deployed physics model; never replace accepted constants."""
    raw = Path(path).read_bytes()
    xml = ET.fromstring(raw)
    lags = []
    for side in ('left', 'right'):
        node = xml.find(".//lag_filter[@name='fcs/%s-motor-pos-norm']" % side)
        require(node is not None, 'Native motor lag definition missing')
        c = float(node.findtext('c1'))
        require(finite(c) and c > 0 and float(node.findtext('clipto/min')) == 0
                and float(node.findtext('clipto/max')) == 1, 'unsupported Native motor lag')
        lags.append(c)
    return {'schema': 'Native-observer-aircraft-contract-v1',
            'aircraft_sha256': hashlib.sha256(raw).hexdigest(),
            'lag_c1_s_inverse': lags, 'lag_support': [0., 1.],
            'lag_discretization': 'JSBSim FGFilter Tustin; input changes unknown between telemetry samples',
            'RPM_observable': False}


def lag_step(interval, input_interval, coefficient, dt):
    """Exact interval image of the scalar Tustin update, then model clipping.

    The two adjacent input samples may each take any value in input_interval.
    The extrema occur at interval endpoints even when the feedback is negative.
    """
    require(vector(interval, 2) and vector(input_interval, 2)
            and interval[0] <= interval[1] and input_interval[0] <= input_interval[1],
            'invalid motor-state interval')
    require(finite(coefficient) and coefficient > 0 and finite(dt) and dt > 0,
            'invalid lag coefficient/time')
    a = coefficient * dt / (2 + coefficient * dt)
    b = (2 - coefficient * dt) / (2 + coefficient * dt)
    choices = [b*x + a*(u+v) for x in interval for u in input_interval for v in input_interval]
    return [max(0., min(1., min(choices))), max(0., min(1., max(choices)))]


class NativeStateObserver:
    """Observer inputs are timestamped observations, never a certificate flag."""
    def __init__(self, backend, aircraft, *, session_id, maximum_gap_s=.101):
        require(isinstance(session_id, str) and bool(session_id), 'observer session required')
        require(backend.get('initial_mode') in ('QHOVER', 'QSTABILIZE', 'FBWA'), 'observer mode unsupported')
        require(backend.get('armed') is False, 'current observer research scope is DISARMED')
        require(finite(maximum_gap_s) and 0 < maximum_gap_s <= .101, 'observer gap policy')
        self.backend = deepcopy(backend)
        self.aircraft = deepcopy(aircraft)
        self.session_id = session_id
        self.maximum_gap_s = maximum_gap_s
        self._history = []
        self._lag = [[0., 1.], [0., 1.]]

    def _commands(self, pwm):
        p = self.backend['parameters']
        answer = []
        for channel in (3, 4):
            low = p['SERVO%d_MIN' % channel]
            high = p['SERVO%d_MAX' % channel]
            require(finite(low) and finite(high) and high > low, 'invalid motor calibration')
            # The accepted Phoenix SITL connector uses the 1000..2000us
            # motor scale, independently of configurable FCU output clipping.
            require(low == 1000 and high == 2000, 'unsupported Phoenix connector PWM scale')
            value = (pwm[channel-1]-1000.)/1000.
            require(0 <= value <= 1, 'motor input outside Native lag support')
            answer.append(value)
        return answer

    def update(self, sample):
        row = deepcopy(sample)
        require(row['session_id'] == self.session_id, 'observer cross-session history')
        require(row['mode'] == self.backend['initial_mode'] and row['armed'] is False,
                'observer mode/armed domain changed')
        t = row['plant_time_s']
        require(finite(t) and t >= 0 and vector(row['plant_state'], 13)
                and vector(row['pwm'], 4) and vector(row['pilot'], 4), 'observer observations missing/nonfinite')
        require(abs(sum(q*q for q in row['plant_state'][6:10])-1) <= .002, 'observer quaternion invalid')
        require(row['pilot'][2] == 0, 'observer research scope forbids throttle')
        require(all(-1 <= v <= 1 for v in row['pilot']), 'observer pilot range invalid')
        imu = row.get('imu')
        if imu is not None:
            require(imu['source'] == 'MAVLink/SCALED_IMU' and imu['frame'] == 'BODY_FRD'
                    and vector(imu['specific_force_m_s2'], 3) and vector(imu['angular_rate_rad_s'], 3)
                    and finite(imu['source_time_s']) and 0 <= t-imu['source_time_s'] <= .101,
                    'observer IMU source/frame/cohort invalid')
        require(row['navigation_role'] in ('MESA_STD', 'NATIVE_EVALUATION_ONLY'), 'observer navigation provenance')
        stamps = row['source_times_s']
        require(set(stamps) == {'plant', 'pwm', 'pilot'} and all(finite(s) and 0 <= s <= t+.101 for s in stamps.values())
                and max(stamps.values())-min(stamps.values()) <= .101, 'observer source cohort invalid')
        current = self._commands(row['pwm'])
        if self._history:
            previous = self._history[-1]
            dt = t-previous['plant_time_s']
            require(0 < dt <= self.maximum_gap_s, 'observer reversed/duplicate/gapped history')
            for key in stamps:
                require(stamps[key] >= previous['source_times_s'][key], 'observer channel source time reversed')
            # A telemetry sample does not expose intermediate PWM commands.
            # Their entire legal support is retained; endpoint interpolation
            # would silently assume away a possible intervening excursion.
            for i, coefficient in enumerate(self.aircraft['lag_c1_s_inverse']):
                self._lag[i] = lag_step(self._lag[i], [0., 1.], coefficient, dt)
        row['observed_motor_command'] = current
        self._history.append(row)
        self._history = self._history[-256:]
        return self.estimate()

    def estimate(self):
        require(bool(self._history), 'observer has no observations')
        p = self.backend['parameters']
        integral = {}
        for axis in ('RLL', 'PIT', 'YAW'):
            key = 'Q_A_RAT_%s_IMAX' % axis
            value = p.get(key)
            if self.backend['initial_mode'] in ('QHOVER', 'QSTABILIZE') and finite(value) and value >= 0:
                integral[axis] = {'interval': [-value, value], 'source_parameter': key,
                                  'observed': False, 'unit': 'Native rate PID output'}
            else:
                integral[axis] = {'interval': None, 'observed': False, 'reason': 'matching bounded PID state unavailable'}
        return {'schema': 'Native-set-observer-estimate-v1', 'session_id': self.session_id,
                'source_time_s': self._history[-1]['plant_time_s'], 'history_samples': len(self._history),
                'history_sha256': digest(self._history), 'backend_sha256': digest(self.backend),
                'aircraft_sha256': self.aircraft['aircraft_sha256'],
                'plant_observation': deepcopy(self._history[-1]),
                'IMU_observed': self._history[-1].get('imu') is not None,
                'controller_integrators': integral, 'motor_lag_position_intervals': deepcopy(self._lag),
                'motor_lag_unit': 'JSBSim normalized actuator filter state, NOT RPM',
                'FCU_spool': {'observed': False, 'possible': ['SHUT_DOWN', 'GROUND_IDLE', 'SPOOLING_UP',
                                                           'THROTTLE_UNLIMITED', 'SPOOLING_DOWN']},
                'transition': {'observed': False, 'support': 'unidentified; mode alone does not reconstruct transition state'},
                'future_output_support_qualified': False, 'risk_upper_bound': None}

    def predict_pwm_support(self, plan):
        require(bool(self._history), 'observer has no observations')
        require(plan and all(c['mode'] == self.backend['initial_mode'] and c['discrete'] == []
                and vector(c['u_cmd'], 4) and c['u_cmd'][2] == 0 for c in plan), 'observer future command outside scope')
        p = self.backend['parameters']
        intervals = [[p['SERVO%d_MIN' % i], p['SERVO%d_MAX' % i]] for i in range(1, 5)]
        require(all(vector(v, 2) and v[0] < v[1] for v in intervals), 'observer PWM support invalid')
        return {'schema': 'Native-PWM-support-v1', 'intervals_us': intervals,
                'semantics': 'FCU configured output limits only; no predicted controller response guarantee',
                'qualified_for_future_decision': False}
