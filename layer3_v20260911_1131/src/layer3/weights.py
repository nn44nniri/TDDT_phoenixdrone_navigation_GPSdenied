from __future__ import annotations

from typing import Any, Mapping

from .config import Layer3Config
from .models import SharedTransDomainState
from .utils import clamp


class WeightManager:
    """Implements (3.2-5..6) with bounded-simplex slow-policy projection."""

    def __init__(self, config: Layer3Config):
        self.config = config

    @staticmethod
    def _bounded_simplex(values: Mapping[str, float], bounds: Mapping[str, list[float]], enabled: Mapping[str, bool]) -> dict[str, float]:
        keys = list(values)
        lo = {k: (float(bounds.get(k, [0.0, 1.0])[0]) if enabled.get(k, False) else 0.0) for k in keys}
        hi = {k: (float(bounds.get(k, [0.0, 1.0])[1]) if enabled.get(k, False) else 0.0) for k in keys}
        # Make a deterministic feasible fallback if a user-supplied policy itself
        # has infeasible bounds.
        slo, shi = sum(lo.values()), sum(hi.values())
        if slo > 1.0 + 1e-12 or shi < 1.0 - 1e-12:
            active = [k for k in keys if enabled.get(k, False)]
            return {k: (1.0 / len(active) if k in active and active else 0.0) for k in keys}
        # Euclidean projection onto {x: sum x=1, lo<=x<=hi}:
        # x_i = clip(v_i - tau, lo_i, hi_i), solve tau by bisection.
        left, right = -10.0, 10.0
        for _ in range(100):
            tau = 0.5 * (left + right)
            total = sum(clamp(float(values[k]) - tau, lo[k], hi[k]) for k in keys)
            if total > 1.0:
                left = tau
            else:
                right = tau
        tau = 0.5 * (left + right)
        x = {k: clamp(float(values[k]) - tau, lo[k], hi[k]) for k in keys}
        # Floating-point cleanup without violating bounds.
        residual = 1.0 - sum(x.values())
        if abs(residual) > 1e-12:
            for k in keys:
                room = (hi[k] - x[k]) if residual > 0 else (x[k] - lo[k])
                delta = (1 if residual > 0 else -1) * min(abs(residual), max(room, 0.0))
                x[k] += delta
                residual -= delta
                if abs(residual) <= 1e-12: break
        return x

    def compute(self, std: SharedTransDomainState, slow_policy: Mapping[str, Any] | None = None) -> tuple[dict[str, float], dict[str, Any]]:
        eps = float(self.config.network.uncertainty_epsilon)
        raw: dict[str, float] = {}
        for twin_id, rep in std.representations.items():
            if not rep.gate:
                raw[twin_id] = 0.0
                continue
            raw[twin_id] = (rep.quality * rep.validity * rep.reliability * rep.freshness) / (eps + rep.normalized_uncertainty)

        total = sum(max(0.0, v) for v in raw.values())
        if total <= eps:
            accepted = [k for k, r in std.representations.items() if r.gate]
            provisional = {k: (1.0 / len(accepted) if k in accepted and accepted else 0.0) for k in std.representations}
        else:
            provisional = {k: max(0.0, v) / total for k, v in raw.items()}

        policy_bounds = dict((slow_policy or {}).get("weight_bounds") or self.config.policy.get("weight_bounds") or {})
        bounds = {
            k: list(policy_bounds.get(k, [self.config.network.default_weight_min, self.config.network.default_weight_max]))
            for k in provisional
        }
        enabled = {k: bool(std.representations[k].gate) for k in provisional}
        weights = self._bounded_simplex(provisional, bounds, enabled)
        return weights, {"raw_eta": raw, "provisional": provisional, "projected": dict(weights), "bounds": bounds, "formula": "3.2-5/3.2-6/3.3-43"}
