from __future__ import annotations
from .config import Layer7Config
from .models import AttributionResult, ResidualBundle, RouteDecision

class RouteEngine:
    """Deterministic base Route_7; Bayesian posterior only annotates confidence."""
    def __init__(self, config: Layer7Config):
        self.cfg = config

    def route(self, r: ResidualBundle, a: AttributionResult, constraint_violations: int = 0) -> RouteDecision:
        t = self.cfg.thresholds
        rd = RouteDecision(
            layer2=(r.domain > t.domain),
            layer31=(r.alignment > t.alignment),
            layer32=((r.coupling + r.coc + r.lci) > t.coupling),
            layer33=(r.synchronization > t.synchronization),
            policy=(abs(r.decision) > t.decision or constraint_violations > 0),
            execution=(r.execution > t.execution or abs(r.latency) > t.latency),
        )
        p = a.posterior
        rd.confidence = {
            "L2": p.get("Domain", 0.0),
            "L3.1": p.get("Align", 0.0),
            "L3.2": p.get("Coupling", 0.0),
            "L3.3": p.get("Sync", 0.0),
            "POLICY": p.get("Decision", 0.0),
            "EXECUTION": p.get("Execution", 0.0),
        }
        flags = {"L2": rd.layer2, "L3.1": rd.layer31, "L3.2": rd.layer32,
                 "L3.3": rd.layer33, "POLICY": rd.policy, "EXECUTION": rd.execution}
        rd.low_confidence = [k for k,v in flags.items() if v and rd.confidence.get(k,0.0) < t.attribution_confidence]
        return rd
