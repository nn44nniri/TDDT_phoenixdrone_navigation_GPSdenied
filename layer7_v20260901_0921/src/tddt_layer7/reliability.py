from __future__ import annotations
from dataclasses import dataclass
from .config import KnowledgeConfig
from .models import EvidenceType


@dataclass(slots=True)
class BetaCell:
    # FIELD-only operational posterior (L7-40).
    alpha: float = 1.0
    beta: float = 1.0
    # Capped/power-tempered non-field pseudo-counts (L7-138/139).
    sim_success: float = 0.0
    sim_failure: float = 0.0
    simulation_mass: float = 0.0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def effective_alpha(self) -> float:
        return self.alpha + self.sim_success

    @property
    def effective_beta(self) -> float:
        return self.beta + self.sim_failure

    def tempered_prior_mean(self) -> float:
        a = 1.0 + self.sim_success; b = 1.0 + self.sim_failure
        return a / (a + b)

    def effective_mean(self) -> float:
        return self.effective_alpha / (self.effective_alpha + self.effective_beta)


class BayesianReliabilityStore:
    """TEP-cell FIELD posterior plus capped/discounted non-field prior."""
    def __init__(self, cfg: KnowledgeConfig):
        self.cfg = cfg; self.cells: dict[tuple[str, str], BetaCell] = {}

    def cell(self, twin: str, cell: str) -> BetaCell:
        return self.cells.setdefault((twin, cell), BetaCell())

    def observe(self, twin: str, cell: str, success: bool, evidence_type: EvidenceType) -> float:
        c = self.cell(twin, cell)
        if evidence_type == EvidenceType.FIELD:
            if success: c.alpha += 1.0
            else: c.beta += 1.0
        elif evidence_type in (EvidenceType.SIMULATION, EvidenceType.REFERENCE, EvidenceType.HIL):
            remaining = max(0.0, self.cfg.simulation_prior_cap - c.simulation_mass)
            w = min(self.cfg.simulation_power, remaining)
            if w > 0:
                if success: c.sim_success += w
                else: c.sim_failure += w
                c.simulation_mass += w
        return c.mean

    def preview_summary(self, twin: str, cell: str, success: bool, evidence_type: EvidenceType) -> dict:
        c = self.cell(twin, cell)
        alpha, beta = c.alpha, c.beta
        sim_s, sim_f, sim_m = c.sim_success, c.sim_failure, c.simulation_mass
        if evidence_type == EvidenceType.FIELD:
            if success: alpha += 1.0
            else: beta += 1.0
        elif evidence_type in (EvidenceType.SIMULATION, EvidenceType.REFERENCE, EvidenceType.HIL):
            remaining = max(0.0, self.cfg.simulation_prior_cap - sim_m)
            w = min(self.cfg.simulation_power, remaining)
            if success: sim_s += w
            else: sim_f += w
            sim_m += w
        op = alpha/(alpha+beta); eff = (alpha+sim_s)/(alpha+beta+sim_s+sim_f)
        return {"operational_mean":op,"effective_mean":eff,
                "tempered_prior_mean":(1+sim_s)/(2+sim_s+sim_f),
                "alpha":alpha,"beta":beta,"simulation_mass":sim_m}

    def operational_update_allowed(self, evidence_type: EvidenceType) -> bool:
        return evidence_type == EvidenceType.FIELD

    def export_state(self) -> dict:
        return {f"{t}\t{c}": {"alpha":v.alpha,"beta":v.beta,"sim_success":v.sim_success,
                                "sim_failure":v.sim_failure,"simulation_mass":v.simulation_mass}
                for (t,c),v in self.cells.items()}

    def import_state(self, data: dict) -> None:
        self.cells.clear()
        for key, val in (data or {}).items():
            try:
                twin, cell = key.split("\t",1)
                self.cells[(twin,cell)] = BetaCell(**val)
            except Exception:
                continue

    def summary(self, twin: str, cell: str) -> dict:
        c = self.cell(twin, cell)
        return {"operational_mean": c.mean, "effective_mean": c.effective_mean(),
                "tempered_prior_mean": c.tempered_prior_mean(),
                "alpha": c.alpha, "beta": c.beta,
                "simulation_mass": c.simulation_mass}
