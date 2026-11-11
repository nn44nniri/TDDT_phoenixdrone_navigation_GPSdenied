import math
from tddt_layer7.config import GPConfig, DriftConfig
from tddt_layer7.gp import SparseOnlineGP
from tddt_layer7.drift import TruncatedBayesianChangePoint
from tddt_layer7.attribution import ObjectOrientedBayesAttributor
from tddt_layer7.models import ResidualBundle, TDOCInput, ExecutionEvidence, PredictionEvidence, NetworkSnapshot, GPResult

def test_sparse_gp_bounded():
    cfg=GPConfig(max_points=4,novelty_variance=0.0)
    gp=SparseOnlineGP(cfg)
    for i in range(10): gp.observe([float(i)],float(i))
    assert len(gp.y)==4 and gp.Kinv.shape==(4,4)
    m,v=gp.predict([9.0]); assert math.isfinite(m) and v>0

def test_changepoint_is_bounded():
    d=TruncatedBayesianChangePoint(DriftConfig(max_run_length=5,anomaly_threshold=0.1,bias_threshold=0.01))
    for x in [0,0,0,3,3,3,3]: out=d.update(x,5.0,1.0)
    assert len(d.run_probs)<=6 and 0<=out.changepoint_probability<=1 and out.persistence>0

def test_bayesian_attribution_execution():
    r=ResidualBundle(execution=3.0,latency=1.0,prediction=0.1)
    inp=TDOCInput(ExecutionEvidence({'x':1}),PredictionEvidence({'x':1}),NetworkSnapshot())
    a=ObjectOrientedBayesAttributor().infer(r,{'execution':GPResult(0,1,3,2)},inp)
    assert a.posterior['Execution'] > a.posterior['Align']
    assert 0<=a.confidence<=1
