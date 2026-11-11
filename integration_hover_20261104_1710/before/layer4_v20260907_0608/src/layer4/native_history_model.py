"""Research alternative B: causal history predictor and independent error audit.

Finite observed errors are NOT an error bound for all admitted hidden states.
This prototype deliberately cannot issue operational qualification or invent a
risk upper bound. The independent audit makes that remaining gap measurable.
"""
from copy import deepcopy
import math

from .native_prediction_validation import RawTrace,residuals,validation_policy,quaternion_distance
from .native_operational import require,finite
from .native_provider_contract import digest


class NativeHistoryEnvelopeModel:
    def __init__(self,history,backend,*,qualification=None):
        require(len(history)>=3,'three causal Native history samples required')
        stamps=[r['plant_time_s'] for r in history]
        require(all(finite(t) for t in stamps) and all(b>a for a,b in zip(stamps,stamps[1:])),
                'history source clock missing/reversed/duplicated')
        require(max(b-a for a,b in zip(stamps,stamps[1:]))<=.101,'Native history gap')
        require(backend['armed'] is False and backend['initial_mode']=='QHOVER','history model research scope')
        for r in history:
            require(len(r['plant_state'])==13 and len(r['pwm'])==4 and
                    all(finite(x) for x in r['plant_state']+r['pwm']),'invalid history observations')
            quaternion_distance(r['plant_state'][6:10],r['plant_state'][6:10])
        self.history=deepcopy(history);self.backend=deepcopy(backend)
        self.qualification=qualification

    def predict_reference(self,plan,dt,commit):
        require(0<commit<=dt and len(plan)*dt<=.6+1e-9,'history model research horizon')
        require(all(c['mode']=='QHOVER' and c['discrete']==[] and c['u_cmd'][2]==0 and
                    max(abs(c['u_cmd'][i]) for i in (0,1,3))<=.2 for c in plan),
                'history model command outside declared research domain')
        base=self.history[-1];samples=[]
        for offset in sorted(set([commit]+[(i+1)*dt for i in range(len(plan))])):
            state=list(base['plant_state'])
            for i in range(3):state[i]+=offset*state[i+3]
            # Rates/quaternion/PWM are held observations. This does not claim a
            # direct pilot->PWM mapping or reconstruct integrators/spool/RPM.
            samples.append({'offset_s':offset,'plant_state':state,'pwm':list(base['pwm']),
                            'manual_control':None})
        return {'schema':'Native-history-research-prediction-v1','samples':samples,
            'history_sha256':digest(self.history),'backend_sha256':digest(self.backend),
            'uncertainty':{'kind':'unqualified','proposed_error_budget':validation_policy()['limits'],
                'latent_state_support':'unbounded/unidentified','risk_upper_bound':None},
            'future_rollouts_covered':False,'operational_prediction_qualified':False,
            'navigation_role':'Native evaluation only; not MESA state','flight_authorized':False}

    def verify_qualification(self,request,model,identity):
        # A few fitting traces cannot establish hidden-state coverage, an IID
        # population, a deterministic invariant, or a chance-risk guarantee.
        # An opaque qualification dictionary is not a substitute for that proof.
        raise ValueError('Native history model: latent-state support and future error/risk coverage not qualified')


def audit_independent_history_prediction(model,prediction,observation,trace_path):
    trace=RawTrace(trace_path);ref=observation['reference']
    require(observation['provider_or_forecast_called'] is False,'observer must not call history predictor')
    require(observation['backend_sha256']==digest(model.backend),'independent Native backend differs')
    require(prediction['history_sha256']==digest(model.history),'history prediction changed')
    limits=validation_policy()['limits'];rows=[]
    low,high=ref['plant_start_bounds_s'];plan=observation['pilot_plan'];dt=observation['prediction_dt_s']
    require(len(prediction['samples'])==len(observation['sample_grid_witnesses']), 'independent horizon coverage')
    for sample in prediction['samples']:
        offset=sample['offset_s'];index=min(len(plan)-1,max(0,int(math.ceil((offset-1e-9)/dt))-1))
        actual=trace.point(low+offset,high+offset,plan[index],ref['backend'])
        compare=deepcopy(sample);compare['manual_control']=actual['manual_control']
        metrics=residuals(compare,actual)
        rows.append({'offset_s':offset,'metrics':metrics,
            'within_predeclared_budget':all(metrics[k]<=limits[k] for k in metrics),
            'manual_control':'checked independently from RC; not a history-model output'})
    return {'schema':'Native-history-independent-audit-v1','status':
        'OBSERVED_ERRORS_WITHIN_RESEARCH_BUDGET' if all(r['within_predeclared_budget'] for r in rows) else 'REJECTED',
        'residuals':rows,'policy':validation_policy(),'independent_trace_sha256':trace.sha256,
        'future_model_qualified':False,'risk_upper_bound':None,
        'reason':'empirical finite residuals do not establish latent-state or future distribution coverage'}
