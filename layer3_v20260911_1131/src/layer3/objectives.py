from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping
import math

from .config import Layer3Config


@dataclass
class ObjectiveResult:
    objective_id: str
    horizon: str
    metric: str
    relation: str
    target: float
    actual: float | None
    unit: str
    satisfied: bool | None
    error: float | None
    achievement_pct: float | None
    weight: float
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObjectiveRegistry:
    """Registry-driven Fast/Meso/Slow objective evaluation.

    Objective definitions are data, not hard-coded control logic. Each objective
    maps one observed metric to a relation and target and returns an auditable
    actual/error/satisfaction/achievement percentage. This registry does not
    issue actuator commands; it exposes goal state to Layer-3 guidance/reporting.
    """

    def __init__(self, config: Layer3Config):
        self.config = config
        self.specs: dict[str, list[dict[str, Any]]] = {
            h: [dict(x) for x in (config.objectives.get(h) or [])]
            for h in ("fast", "meso", "slow")
        }
        self.baselines: dict[str, float] = {}

    def register(self, horizon: str, spec: Mapping[str, Any]) -> None:
        h = str(horizon).lower()
        if h not in self.specs:
            raise ValueError(f"unsupported objective horizon: {horizon}")
        self.specs[h].append(dict(spec))

    def set_baseline(self, objective_id: str, value: float) -> None:
        self.baselines[str(objective_id)] = float(value)

    @staticmethod
    def _finite(value: Any) -> float | None:
        try:
            x = float(value)
            return x if math.isfinite(x) else None
        except Exception:
            return None

    def _achievement(self, spec: Mapping[str, Any], actual: float, target: float, relation: str) -> float:
        mode = str(spec.get("achievement_mode") or "threshold").lower()
        oid = str(spec.get("id") or spec.get("metric"))
        if mode == "progress_to_target":
            baseline = self.baselines.get(oid)
            if baseline is not None and baseline > target:
                return max(0.0, min(100.0, 100.0 * (baseline - actual) / (baseline - target)))
        if relation == "bool":
            return 100.0 if actual >= 0.5 else 0.0
        if relation == "le":
            if actual <= target:
                return 100.0
            if target <= 0.0:
                return 0.0
            return max(0.0, min(100.0, 100.0 * target / max(actual, 1e-12)))
        if relation == "ge":
            if actual >= target:
                return 100.0
            if target <= 0.0:
                return 100.0 if actual >= target else 0.0
            return max(0.0, min(100.0, 100.0 * actual / target))
        if relation == "eq":
            tol = float(spec.get("tolerance", 1e-9))
            return 100.0 if abs(actual - target) <= tol else max(0.0, 100.0 * (1.0 - abs(actual-target)/max(abs(target),tol,1e-9)))
        raise ValueError(f"unsupported objective relation: {relation}")

    def evaluate(self, horizon: str, observations: Mapping[str, Any], *, provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        h = str(horizon).lower()
        if h not in self.specs:
            raise ValueError(f"unsupported objective horizon: {horizon}")
        results: dict[str, dict[str, Any]] = {}
        weighted = 0.0; wsum = 0.0; all_required = True; assessed = 0
        for spec in self.specs[h]:
            oid = str(spec.get("id") or spec.get("metric")); metric = str(spec.get("metric") or oid)
            relation = str(spec.get("relation") or "le").lower(); target = float(spec.get("target", 0.0)); unit = str(spec.get("unit") or "1"); weight = float(spec.get("weight", 1.0))
            actual = self._finite(observations.get(metric))
            if actual is None:
                result = ObjectiveResult(oid,h,metric,relation,target,None,unit,None,None,None,weight,
                                         {**dict(provenance or {}),"reason":"metric_unavailable"})
                if bool(spec.get("required", True)): all_required = False
            else:
                if relation == "le": satisfied = actual <= target
                elif relation == "ge": satisfied = actual >= target
                elif relation == "eq": satisfied = abs(actual-target) <= float(spec.get("tolerance",1e-9))
                elif relation == "bool": satisfied = actual >= 0.5
                else: raise ValueError(f"unsupported objective relation: {relation}")
                error = (actual-target) if relation == "le" else (target-actual) if relation == "ge" else abs(actual-target)
                achievement = self._achievement(spec, actual, target, relation)
                result = ObjectiveResult(oid,h,metric,relation,target,actual,unit,bool(satisfied),float(error),float(achievement),weight,
                                         {**dict(provenance or {}),"source":"ObjectiveRegistry"})
                weighted += weight * achievement; wsum += weight; assessed += 1
                if bool(spec.get("required", True)) and not satisfied: all_required = False
            results[oid] = result.to_dict()
        aggregate = (weighted/wsum) if wsum > 0 else None
        return {
            "horizon": h,
            "objectives": results,
            "achievement_pct": aggregate,
            "all_required_satisfied": bool(all_required and assessed > 0),
            "assessed_count": assessed,
            "total_count": len(self.specs[h]),
            "observations": dict(observations),
            "provenance": {"registry":"ObjectiveRegistry","actuator_command":False,**dict(provenance or {})},
        }
