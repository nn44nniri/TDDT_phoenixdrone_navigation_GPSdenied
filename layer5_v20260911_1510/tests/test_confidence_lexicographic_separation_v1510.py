from layer5.config import Layer5Config
from layer5.decision import ConfidenceEngine
from layer5.models import Layer5Candidate


def _candidate(cid, normalized, logical_first_move=None):
    candidate=Layer5Candidate(
        candidate_id=cid, raw={}, snapshot_version='v', snapshot_hash='h',
        actions=[[0.0,0.0,0.0,0.0]], objective_vector=dict(normalized),
        normalized_objectives=dict(normalized), network_trajectory=[{'mu':1.0}],
        uncertainty=[], feasible=True, violation_probability=0.0,
        verified=True, verification={'executed':True,'verified':True}, provenance={}
    )
    if logical_first_move is not None:
        candidate.logical_actions=[list(logical_first_move)]
    return candidate


def test_confidence_separation_uses_first_lexicographic_discriminating_level():
    cfg=Layer5Config.from_yaml('configs/configuration_phoenix.yaml')
    eng=ConfidenceEngine(cfg)
    # Mirrors the shape seen in the Online regression: C0005 wins the first
    # lexicographic level (consistency), while lower-priority time/sensitivity
    # objectives make an unweighted scalar sum look artificially close.
    selected=_candidate('C0005',{
        'mission':0.0,'time':1.0,'energy':0.0,'risk':0.0,'uncertainty':0.0,
        'consistency':0.0,'margin':0.0,'control_effort':0.0,'sensitivity':1.0,
    })
    runner=_candidate('C0002',{
        'mission':1.0,'time':0.0,'energy':1.0,'risk':0.0,'uncertainty':0.0,
        'consistency':1.0,'margin':0.0,'control_effort':0.0,'sensitivity':0.0,
    })
    sep,meta=eng._decision_separation([selected,runner])
    assert sep > 0.99
    assert meta['level'] == 1
    assert 'consistency' in meta['objectives']
    # Old cross-priority scalarization would have been |3-2|/(3+2)=0.2.
    old=abs(3.0-2.0)/(3.0+2.0)
    assert old == 0.2


def test_confidence_separation_is_zero_for_true_lexicographic_tie():
    cfg=Layer5Config.from_yaml('configs/configuration_phoenix.yaml')
    eng=ConfidenceEngine(cfg)
    a=_candidate('A',{'mission':0.1,'time':0.2,'energy':0.3,'risk':0.0,'uncertainty':0.0,'consistency':0.1,'margin':0.0,'control_effort':0.0,'sensitivity':0.0},[0.0,0.1,0.7,0.0])
    b=_candidate('B',dict(a.normalized_objectives),[0.0,-0.1,0.7,0.0])
    sep,meta=eng._decision_separation([a,b])
    assert sep == 0.0
    assert meta['reason'] == 'lexicographic_tie'


def test_confidence_treats_identical_first_moves_as_one_actionable_decision_class():
    cfg=Layer5Config.from_yaml('configs/configuration_phoenix.yaml')
    eng=ConfidenceEngine(cfg)
    objectives={'mission':0.0,'time':0.0,'energy':0.0,'risk':0.0,'uncertainty':0.0,'consistency':0.0,'margin':0.0,'control_effort':0.0,'sensitivity':0.0}
    move=[0.01,0.06,0.756,0.0]
    a=_candidate('scenario-A',objectives,move)
    b=_candidate('scenario-B',objectives,move)
    sep,meta=eng._decision_separation([a,b])
    assert sep == 1.0
    assert meta['reason'] == 'decision_equivalent_first_move'
    assert meta['only_first_mpc_move'] is True


def test_confidence_uses_verified_same_direction_first_move_effect_when_objectives_nearly_tie():
    cfg=Layer5Config.from_yaml('configs/configuration_phoenix.yaml')
    eng=ConfidenceEngine(cfg)
    a=_candidate('C0003',{
        'mission':0.0,'time':0.0,'energy':0.0,'risk':0.0,'uncertainty':0.0,
        'consistency':0.999981,'margin':0.0,'control_effort':0.0,'sensitivity':0.0,
    })
    b=_candidate('C0004',{
        'mission':0.0,'time':0.0,'energy':0.0,'risk':0.0,'uncertainty':0.0,
        'consistency':1.0,'margin':0.0,'control_effort':0.0,'sensitivity':0.0,
    })
    a.solver={'corrected_actions':[[-0.037567947784319135,0.24150943396226407,0.7854296699694494,0.0]]}
    b.solver={'corrected_actions':[[-0.027643353253142374,0.24150943396226407,0.7869887030884644,0.0]]}
    sep,meta=eng._decision_separation([a,b])
    expected=abs(b.solver['corrected_actions'][0][0]/a.solver['corrected_actions'][0][0])
    assert abs(sep-expected)<1e-12
    assert meta['objective_separation']<1e-3
    assert meta['first_move_effect']['manual_threshold_used'] is False
    assert meta['first_move_effect']['verified_pair_required'] is True


def test_confidence_uses_the_same_policy_weights_and_epsilon_as_selector():
    cfg=Layer5Config.from_yaml('configs/configuration_phoenix.yaml')
    eng=ConfidenceEngine(cfg)
    selected=_candidate('selected',{
        'mission':0.0,'time':0.0,'energy':0.0,'risk':0.0,'uncertainty':0.0,
        'consistency':0.999995,'margin':0.0,'control_effort':0.0,'sensitivity':0.0,
    })
    runner=_candidate('runner',{
        'mission':1.0,'time':0.0,'energy':0.0,'risk':0.0,'uncertainty':0.0,
        'consistency':1.0,'margin':0.0,'control_effort':0.0,'sensitivity':0.0,
    })
    policy_weights={k:1.0/9.0 for k in (
        'mission','time','energy','risk','uncertainty','consistency','margin',
        'control_effort','sensitivity')}
    sep,meta=eng._decision_separation([selected,runner],policy_weights)
    # With the selector's 1/9 policy weight, the level-1 gap is below epsilon;
    # mission is the first level that actually discriminates the candidates.
    assert meta['level']==2
    assert meta['objectives']==['mission']
    assert sep>0.99
