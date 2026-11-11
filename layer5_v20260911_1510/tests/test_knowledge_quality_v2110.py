from __future__ import annotations
from types import SimpleNamespace
from layer5.policy import PolicyAdapter

class Cfg:
    def get(self,key,default=None):
        vals={
          'objectives.guidance_weights':{'mission':1.0,'risk':1.0},
          'policy.decision_weight_min':0.0,'policy.decision_weight_max':10.0,
          'policy.meso_gain':1.0,'policy.slow_gain':1.0,
          'governance.policy_version':'L5-policy-v1',
          'policy.default_constraints':{},'policy.parameters':{},
          'policy.allowed_runtime_keys':[],'policy.allowed_online_update_keys':[],
          'policy.online_delta_max':0.1,'policy.safe_parameter_bounds':{}
        }
        return vals.get(key,default)

def _setd(control_valid):
    return {'ok':True,'knowledge':{'knowledge':{
      'decision_weights':{'mission':0.9,'risk':0.1},
      'knowledge_quality':{'safety_knowledge_valid':True,'control_knowledge_valid':control_valid}
    }}}

def test_hold_only_setd_cannot_seed_control_weights():
    p=PolicyAdapter(Cfg()).adapt({},setd_prior=_setd(False))
    assert p['setd_safety_knowledge_valid'] is True
    assert p['setd_control_knowledge_valid'] is False
    assert p['setd_prior_used'] is False
    assert abs(p['decision_weights']['mission']-.5)<1e-9

def test_control_valid_setd_may_seed_control_weights():
    p=PolicyAdapter(Cfg()).adapt({},setd_prior=_setd(True))
    assert p['setd_control_knowledge_valid'] is True
    assert p['setd_prior_used'] is True
    assert p['decision_weights']['mission'] > p['decision_weights']['risk']
