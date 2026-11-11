from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import json
from typing import Any, Dict


@dataclass(slots=True)
class GPConfig:
    max_points: int = 32
    length_scale: float = 1.0
    signal_variance: float = 1.0
    noise_variance: float = 0.05
    jitter: float = 1e-8
    novelty_variance: float = 0.2
    min_variance: float = 1e-9


@dataclass(slots=True)
class DriftConfig:
    max_run_length: int = 32
    hazard: float = 0.02
    prior_mean: float = 0.0
    prior_precision: float = 1.0
    observation_variance: float = 1.0
    persistence_lambda: float = 0.2
    anomaly_threshold: float = 3.0
    bias_threshold: float = 0.1


@dataclass(slots=True)
class ThresholdConfig:
    domain: float = 1.0
    alignment: float = 0.5
    coupling: float = 1.0
    synchronization: float = 0.5
    decision: float = 0.5
    execution: float = 0.25
    latency: float = 0.25
    attribution_confidence: float = 0.45
    fast_anomaly: float = 2.0
    meso_persistence: float = 0.45
    meso_attr_confidence: float = 0.35
    meso_min_evidence: int = 3
    slow_structural_error: float = 0.75
    slow_ess_min: float = 3.0
    slow_maturity_min: float = 0.6
    catastrophic: float = 4.0
    topology_chatter: float = 0.5
    oscillation: float = 0.6
    stability_margin: float = 1e-3
    shadow_min_improvement: float = -1e-9
    rollback_degradation: float = 0.25
    max_uncertainty: float = 10.0


@dataclass(slots=True)
class RateConfig:
    fast_state: float = 0.5
    meso_relation: float = 0.1
    slow_structural: float = 0.02
    weight: float = 0.05
    coupling: float = 0.05
    epsilon: float = 0.02
    omega: float = 0.01
    policy: float = 0.02
    constraints: float = 0.02
    oscillation_decay: float = 0.5


@dataclass(slots=True)
class EdgeConfig:
    fast_period_s: float = 1.0
    fast_per_meso: int = 3
    meso_per_slow: int = 2
    max_cycle_candidates: int = 8
    max_edges: int = 64
    topology_dwell_ticks: int = 2
    shadow_window: int = 24
    post_commit_monitor_ticks: int = 6
    evidence_window: int = 64
    oscillation_window: int = 8
    kstore_max_records: int = 256
    kstore_segment_records: int = 64
    kstore_max_segments: int = 8
    setd_max_packages: int = 32
    freshness_max_s: float = 2.0
    require_semantic_metadata: bool = True
    require_ptlib_correlation: bool = True


@dataclass(slots=True)
class KnowledgeConfig:
    simulation_prior_cap: float = 10.0
    simulation_power: float = 0.2
    epsilon_quantile: float = 0.95
    require_context_for_persistent: bool = True


@dataclass(slots=True)
class Layer7Config:
    gp: GPConfig = field(default_factory=GPConfig)
    drift: DriftConfig = field(default_factory=DriftConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    rates: RateConfig = field(default_factory=RateConfig)
    edge: EdgeConfig = field(default_factory=EdgeConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    fail_closed_structural: bool = True
    require_field_for_persistent: bool = True
    tdoc_managed: bool = True
    direct_actuation_allowed: bool = False

    def __post_init__(self) -> None:
        # These are architecture invariants in Formalism_layer7_v2, not normal
        # tuning switches.  Refuse configurations that weaken them.
        if self.direct_actuation_allowed:
            raise ValueError("Layer 7 direct actuation is forbidden")
        if not self.tdoc_managed:
            raise ValueError("Layer 7 must remain TDOC-managed")
        if not self.require_field_for_persistent:
            raise ValueError("persistent operational adaptation requires FIELD evidence")
        if not self.fail_closed_structural:
            raise ValueError("structural adaptation must fail closed")
        r = self.rates
        if not (r.fast_state >= r.meso_relation >= r.slow_structural >= 0.0):
            raise ValueError("adaptation-rate hierarchy must satisfy fast >= meso >= slow >= 0")
        if r.weight > r.meso_relation or r.coupling > r.meso_relation:
            raise ValueError("W/K rates may not exceed meso_relation")
        for name in ("epsilon", "omega", "policy", "constraints"):
            if getattr(r, name) > r.slow_structural:
                raise ValueError(f"{name} rate may not exceed slow_structural")
        if self.edge.kstore_max_segments < 1 or self.edge.setd_max_packages < 1:
            raise ValueError("persistent KStore budgets must be positive")

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Layer7Config":
        return cls(
            gp=GPConfig(**d.get("gp", {})),
            drift=DriftConfig(**d.get("drift", {})),
            thresholds=ThresholdConfig(**d.get("thresholds", {})),
            rates=RateConfig(**d.get("rates", {})),
            edge=EdgeConfig(**d.get("edge", {})),
            knowledge=KnowledgeConfig(**d.get("knowledge", {})),
            fail_closed_structural=bool(d.get("fail_closed_structural", True)),
            require_field_for_persistent=bool(d.get("require_field_for_persistent", True)),
            tdoc_managed=bool(d.get("tdoc_managed", True)),
            direct_actuation_allowed=bool(d.get("direct_actuation_allowed", False)),
        )


def _resolve_config_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.exists():
        return candidate
    if not candidate.is_absolute():
        project_root = Path(__file__).resolve().parents[2]
        fallback = project_root / candidate
        if fallback.exists():
            return fallback
    raise FileNotFoundError(f"Layer 7 config not found: {path}")


def load_config(path: str | Path | None = None) -> Layer7Config:
    if path is None:
        return Layer7Config()
    resolved = _resolve_config_path(path)
    data = json.loads(resolved.read_text(encoding="utf-8"))
    return Layer7Config.from_dict(data)
