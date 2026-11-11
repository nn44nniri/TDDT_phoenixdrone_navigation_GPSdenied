from tddt_layer7.config import Layer7Config
from tddt_layer7.routing import RouteEngine
from tddt_layer7.triggers import TriggerEngine
from tddt_layer7.models import ResidualBundle, AttributionResult, DriftResult

def attr(): return AttributionResult({'Domain':0.1,'Coupling':0.1,'Align':0.1,'Sync':0.1,'Execution':0.1,'Decision':0.1,'Prediction':0.1,'Unknown':0.3},0.2,'Unknown')

def test_route_independent_of_low_posterior():
    cfg=Layer7Config(); r=ResidualBundle(domain=2.0)
    rd=RouteEngine(cfg).route(r,attr())
    assert rd.layer2 and 'L2' in rd.low_confidence

def test_route_l32_uses_coc_and_lci():
    cfg=Layer7Config(); r=ResidualBundle(coupling=0.1,coc=0.7,lci=0.4)
    assert RouteEngine(cfg).route(r,attr()).layer32

def test_fast_meso_slow_boundaries():
    cfg=Layer7Config(); cfg.thresholds.fast_anomaly=0.1; cfg.thresholds.meso_persistence=0.1; cfg.thresholds.meso_attr_confidence=0.0; cfg.thresholds.slow_structural_error=0.1; cfg.thresholds.slow_ess_min=1; cfg.thresholds.slow_maturity_min=0.1
    te=TriggerEngine(cfg); d=DriftResult(0.2,0.8,True); a=AttributionResult({'Unknown':1},1,'Unknown')
    outs=[]
    for _ in range(6): outs.append(te.decide(1,d,a,True,1,1,True,1,1,1,1))
    assert outs[0].fast and outs[2].meso and outs[5].slow
