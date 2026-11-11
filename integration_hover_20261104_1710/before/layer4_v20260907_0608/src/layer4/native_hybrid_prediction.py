"""Dynamic Native research predictor with explicit unresolved outer-bound status.

The physics dependency must run the pinned deployed model. Finite ensembles are
kept distinct from a reachability tube: their hull is not a guaranteed bound.
"""
from copy import deepcopy
import itertools
import math
import numpy as np

from .native_operational import require, finite, vector
from .native_provider_contract import digest, DOMAIN


class NativeControllerResponse:
    """Fitted command/rate/PWM response; hold-out data must stay independent."""
    def __init__(self, backend, coefficients, fit_trace_sha256):
        require(isinstance(coefficients, list) and len(coefficients) == 4
                and all(vector(row, 12) for row in coefficients), 'Native response coefficients invalid')
        require(fit_trace_sha256 and all(isinstance(s, str) and len(s) == 64 for s in fit_trace_sha256),
                'Native response fitting lineage missing')
        self.backend = deepcopy(backend)
        self.coefficients = deepcopy(coefficients)
        self.fit_trace_sha256 = list(fit_trace_sha256)
        self.identification = None

    @staticmethod
    def features(pwm, pilot, rates):
        require(vector(pwm, 4) and vector(pilot, 4) and vector(rates, 3), 'response features invalid')
        return [1.] + [(v-1500.)/500. for v in pwm] + list(pilot) + list(rates)

    @classmethod
    def fit(cls, backend, transitions, fit_trace_sha256):
        require(len(transitions) >= 24, 'insufficient Native controller fitting transitions')
        x = np.array([cls.features(r['pwm'], r['pilot'], r['rates']) for r in transitions])
        require(all(vector(r['next_pwm'], 4) for r in transitions), 'response targets invalid')
        # Targets are PWM changes at the registered 50ms sampling cadence.
        require(all(finite(r['dt_s']) and abs(r['dt_s']-.05) <= .002 for r in transitions),
                'response fitting cadence outside declared domain')
        y = np.array([[b-a for a, b in zip(r['pwm'], r['next_pwm'])] for r in transitions])
        coef = np.linalg.solve(x.T @ x + 1e-6*np.eye(12), x.T @ y).T
        require(np.all(np.isfinite(coef)), 'Native response fit nonfinite')
        fitted = cls(backend, coef.tolist(), fit_trace_sha256)
        fitted.identification = {'samples': len(transitions), 'features': 12,
            'design_rank': int(np.linalg.matrix_rank(x)),
            'condition_number': float(np.linalg.cond(x.T@x+1e-6*np.eye(12))),
            'hidden_state_identified': False, 'future_support_qualified': False}
        return fitted

    def step(self, pwm, pilot, rates, dt):
        require(finite(dt) and 0 < dt <= .05, 'Native response integration step')
        f = self.features(pwm, pilot, rates)
        p = self.backend['parameters']
        out = [sum(a*b for a, b in zip(row, f)) for row in self.coefficients]
        return [max(p['SERVO%d_MIN' % (i+1)], min(p['SERVO%d_MAX' % (i+1)],
                    pwm[i] + out[i]*dt/.05)) for i in range(4)]


class NativeHybridPredictor:
    def __init__(self, observer, physics, response, *, current_backend):
        require(callable(current_backend), 'trusted current Native backend reader required')
        self.observer = observer
        self.physics = physics
        self.response = response
        self.backend_reader = current_backend

    def current_backend(self):
        value = self.backend_reader()
        require(value == self.observer.backend == self.response.backend, 'Native model backend changed')
        return deepcopy(value)

    def predict_reference(self, plan, dt, commit, environment):
        backend = self.current_backend()
        require(len(plan) in range(1, 11) and finite(dt) and finite(commit)
                and .1 <= commit <= dt and dt*len(plan) <= .6+1e-9, 'hybrid research timing scope')
        require(all(abs(round(t/.05)*.05-t) <= 1e-9 for t in (dt, commit)),
                'hybrid research timing must lie on registered 50ms controller grid')
        require(all(c['domain'] == DOMAIN and c['mode'] == backend['initial_mode']
                and c['discrete'] == [] and vector(c['u_cmd'], 4) and c['u_cmd'][2] == 0
                and all(-1 <= v <= 1 for v in c['u_cmd']) for c in plan), 'hybrid command scope')
        estimate = self.observer.estimate()
        row = estimate['plant_observation']
        # Nominal controller response reacts to the candidate; no fixed
        # quaternion/rate/PWM trajectory is returned by this model.
        offsets = sorted(set([commit] + [(i+1)*dt for i in range(len(plan))]))
        h = .05
        schedule = []
        pwm = list(row['pwm'])
        rates = list(row['plant_state'][10:13])
        for k in range(round(len(plan)*dt/h)):
            command = plan[min(len(plan)-1, int((k*h+1e-9)/dt))]['u_cmd']
            pwm = self.response.step(pwm, command, rates, h)
            schedule.append({'offset_s': (k+1)*h, 'pwm_us': pwm})
            # Replay the complete growing input prefix in the private physics
            # process, then feed predicted rates into the next controller step.
            # This preserves all prior physics steps without treating RunIC as
            # a restorable intermediate controller/contact checkpoint.
            partial = self.physics.rollout(row['plant_state'], schedule, [(k+1)*h], environment)
            require(len(partial) == 1 and vector(partial[0]['plant_state'], 13),
                    'Native physics feedback step incomplete/nonfinite')
            rates = list(partial[0]['plant_state'][10:13])
        nominal = self.physics.rollout(row['plant_state'], schedule, offsets, environment)
        require(len(nominal) == len(offsets) and all(vector(r['plant_state'], 13) for r in nominal),
                'Native physics trajectory incomplete/nonfinite')
        support = self.observer.predict_pwm_support(plan)
        # Endpoint experiments expose sensitivity. Nonlinear physics extrema
        # can be interior: this finite hull is explicitly NOT an outer bound.
        trajectories = []
        for values in itertools.product(*support['intervals_us']):
            branch = [{'offset_s': r['offset_s'], 'pwm_us': list(values)} for r in schedule]
            trajectories.append(self.physics.rollout(row['plant_state'], branch, offsets, environment))
        samples = []
        for index, (offset, point) in enumerate(zip(offsets, nominal)):
            center = point['plant_state']
            members = [r[index]['plant_state'] for r in trajectories] + [center]
            command_index = min(len(schedule)-1, max(0, round(offset/h)-1))
            samples.append({'offset_s': offset, 'plant_state': center,
                'pwm': schedule[command_index]['pwm_us'], 'manual_control': None,
                'sensitivity_hull_lower': [min(v[i] for v in members) for i in range(13)],
                'sensitivity_hull_upper': [max(v[i] for v in members) for i in range(13)]})
        return {'schema': 'Native-hybrid-research-prediction-v1', 'samples': samples,
                'observer': estimate, 'controller_response_sha256': digest(self.response.coefficients),
                'fit_trace_sha256': self.response.fit_trace_sha256,
                'controller_identification': self.response.identification,
                'physics_identity': self.physics.identity(), 'backend_sha256': digest(backend),
                'prediction_schedule': schedule, 'environment_sha256': digest(environment),
                'uncertainty': {'kind': 'unqualified', 'finite_hull_is_outer_bound': False,
                    'hidden_motor_lag_initialization_restored': False, 'risk_upper_bound': None,
                    'reason': 'validated nonlinear reachability/error envelope and controller hidden-state coverage absent'},
                'future_rollouts_covered': False, 'operational_prediction_qualified': False}

    def verify_qualification(self, request, model, identity):
        raise ValueError('Native hybrid prediction: independent future outer-bound/hidden-state/risk qualification not established')

    def predict(self, request):
        # Do not return a reference/sensitivity hull to an operational issuer.
        self.verify_qualification(request, {}, {})
