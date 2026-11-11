from __future__ import annotations
from collections import deque
from .config import Layer7Config
from .models import AttributionResult, DriftResult, TriggerDecision

class EvidenceSufficiency:
    def __init__(self, config: Layer7Config):
        self.cfg = config
        self.weights = deque(maxlen=config.edge.evidence_window)

    def add(self, quality: float, validity: float, freshness: float, credibility: float) -> float:
        w = max(0.0, float(quality)*float(validity)*float(freshness)*float(credibility))
        self.weights.append(w)
        return self.ess()

    def ess(self) -> float:
        if not self.weights:
            return 0.0
        s = sum(self.weights)
        ss = sum(w*w for w in self.weights)
        return (s*s/ss) if ss > 0 else 0.0

class TriggerEngine:
    def __init__(self, config: Layer7Config):
        self.cfg = config
        self.suff = EvidenceSufficiency(config)
        self.count = 0

    def decide(self, anomaly: float, drift: DriftResult, attr: AttributionResult,
               evidence_ok: bool, structural_error: float, maturity: float,
               governance_ok: bool, quality: float, validity: float,
               freshness: float, credibility: float) -> TriggerDecision:
        self.count += 1
        ess = self.suff.add(quality, validity, freshness, credibility)
        t = self.cfg.thresholds
        fast = bool(anomaly > t.fast_anomaly and evidence_ok)
        meso_boundary = self.count % max(self.cfg.edge.fast_per_meso, 1) == 0
        slow_boundary = self.count % max(self.cfg.edge.fast_per_meso*self.cfg.edge.meso_per_slow, 1) == 0
        meso = bool(meso_boundary and drift.persistence >= t.meso_persistence and
                    attr.confidence >= t.meso_attr_confidence and len(self.suff.weights) >= t.meso_min_evidence)
        slow = bool(slow_boundary and structural_error > t.slow_structural_error and
                    ess >= t.slow_ess_min and maturity >= t.slow_maturity_min and governance_ok)
        return TriggerDecision(fast=fast, meso=meso, slow=slow, meso_boundary=meso_boundary, slow_boundary=slow_boundary, ess=ess, maturity=float(maturity))
