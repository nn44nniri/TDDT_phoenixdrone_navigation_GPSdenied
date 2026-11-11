from layer5.config import Layer5Config
from layer5.engine import Layer5Engine
from layer5.governance import GovernanceManager

def cfg():
    return Layer5Config.from_yaml("configs/configuration_phoenix.yaml")

def test_preflight_confirms_auto_reachable_without_relaxing_threshold():
    r=Layer5Engine(cfg()).preflight(strict_external=False)
    c=r["confidence_reachability"]
    assert c["reward_weight_sum"] == 0.75
    assert c["normalization_valid"] is True
    assert c["max_possible_confidence"] == 1.0
    assert c["auto_threshold"] == 0.8
    assert c["auto_reachable"] is True
    assert c["reason"] is None
    assert c["threshold_relaxed"] is False

def test_governance_false_explains_supervised_missing_human_approval():
    g=GovernanceManager(cfg()).authorize(
        {"mode":"SUPERVISED","commit_authorized":False,"human_required":True,"human_approval":False},
        "C1","v1","h1",0.56,{})
    assert g["gate"] is False
    assert "mode_commit_not_authorized" in g["reasons"]
    assert "resolved_mode_SUPERVISED" in g["reasons"]
    assert "human_approval_required" in g["reasons"]
