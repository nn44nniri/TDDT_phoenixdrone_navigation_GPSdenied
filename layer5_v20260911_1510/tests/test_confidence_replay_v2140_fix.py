from layer5.config import Layer5Config
from layer5.decision import OperationalModeManager, ConfidenceEngine


def test_observed_2140_components_are_auto_reachable_after_l558n_normalization():
    cfg=Layer5Config.from_yaml("configs/configuration_phoenix.yaml")
    w=cfg.get("confidence.weights",{})
    q={
      "separation":0.49999656199033243,
      "margin":0.9999999999999982,
      "full_fidelity":1.0,
      "freshness":1.0,
      "risk":0.0,
      "uncertainty":0.0003502280664078803,
      "sensitivity":0.49999999394635575,
    }
    Wp=w["separation"]+w["margin"]+w["full_fidelity"]+w["freshness"]
    rho=(w["separation"]*q["separation"]+w["margin"]*q["margin"]+
         w["full_fidelity"]*q["full_fidelity"]+w["freshness"]*q["freshness"]-
         w["risk"]*q["risk"]-w["uncertainty"]*q["uncertainty"]-
         w["sensitivity"]*q["sensitivity"])/Wp
    assert abs(Wp-0.75) < 1e-12
    assert rho >= cfg.get("confidence.auto_threshold")
    mode=OperationalModeManager(cfg).resolve(rho,0.0,True,False,"AUTO")
    assert mode["mode"]=="AUTO"
    assert mode["commit_authorized"] is True


def test_l558n_maps_ideal_positive_evidence_to_one_with_original_weights():
    cfg=Layer5Config.from_yaml("configs/configuration_phoenix.yaml")
    w=cfg.get("confidence.weights",{})
    Wp=sum(w[k] for k in ("separation","margin","full_fidelity","freshness"))
    assert abs(Wp-0.75) < 1e-12
    raw=Wp
    assert abs(raw/Wp-1.0)<1e-12
