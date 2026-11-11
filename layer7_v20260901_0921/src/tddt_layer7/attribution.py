from __future__ import annotations
import math
from collections import defaultdict
from .models import AttributionResult, GPResult, ResidualBundle, TDOCInput
from .utils import gaussian_logpdf, stable_softmax
from .graph import cycle_attribution


class ObjectOrientedBayesAttributor:
    """Small factorized OOBN suitable for Edge execution.

    The top-level cause posterior is inferred from structured residual evidence.
    Twin/edge sub-posteriors are derived from supplied or locally computable
    residuals; they are not treated as authoritative route gates.
    """
    BASE_CAUSES = ["Domain", "Coupling", "Align", "Sync", "Execution", "Decision", "Prediction", "Unknown"]

    def __init__(self):
        self.priors = {k: 1.0 / len(self.BASE_CAUSES) for k in self.BASE_CAUSES}
        self.templates = {
            "Domain": {"domain": 1.0},
            "Coupling": {"coupling": 1.0, "coc": 0.8, "lci": 0.7},
            "Align": {"alignment": 1.0},
            "Sync": {"sync": 1.0, "latency": 0.5},
            "Execution": {"execution": 1.0, "latency": 0.6},
            "Decision": {"decision": 1.0},
            "Prediction": {"prediction": 1.0, "domain": 0.3},
            "Unknown": {},
        }

    @staticmethod
    def _squash(x: float) -> float:
        return 1.0 - math.exp(-abs(float(x)))

    @staticmethod
    def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
        if not scores: return {}
        ex = {k: math.exp(min(abs(float(v)), 50.0)) for k, v in scores.items()}
        s = sum(ex.values()) or 1.0
        return {k: v / s for k, v in ex.items()}

    @staticmethod
    def _score_keys_for_twin(twin: str, maps: list[dict[str, float]]) -> float:
        score = 0.0
        prefixes = (twin, f"{twin}:", f"{twin}.", f"{twin}/")
        for m in maps:
            for k, v in m.items():
                if str(k).startswith(prefixes): score += abs(float(v))
        return score

    def _twin_scores(self, residuals: ResidualBundle, inp: TDOCInput) -> dict[str, float]:
        if inp.hints.twin_error_scores:
            return dict(inp.hints.twin_error_scores)
        twins = set(inp.network.weights) | set(inp.network.twin_validity) | set(inp.hints.theta_residual)
        maps = [residuals.observation_error_map, residuals.model_error_map, residuals.state_residual]
        scores = {t: self._score_keys_for_twin(t, maps) for t in twins}
        for t, vec in inp.hints.theta_residual.items():
            try: scores[t] = scores.get(t, 0.0) + math.sqrt(sum(float(x) ** 2 for x in vec))
            except Exception: pass
        if not scores and len(twins) == 1:
            scores[next(iter(twins))] = residuals.domain
        return scores

    def _edge_scores(self, residuals: ResidualBundle, inp: TDOCInput) -> dict[str, float]:
        if inp.hints.edge_error_scores:
            base = dict(inp.hints.edge_error_scores)
        else:
            base = {}
            edge_names = {f"{a}->{b}" for a, b in inp.network.topology} | set(inp.network.couplings)
            for e in edge_names:
                if e in residuals.coupling_error_map: base[e] = abs(float(residuals.coupling_error_map[e]))
                elif e in inp.hints.coupling_error: base[e] = abs(float(inp.hints.coupling_error[e]))
        cyc = cycle_attribution(inp.network.topology, inp.network.hidden_loop_lci)
        for e, v in cyc.items(): base[e] = base.get(e, 0.0) + abs(float(v))
        return base

    def infer(self, residuals: ResidualBundle, gp: dict[str, GPResult], inp: TDOCInput) -> AttributionResult:
        feat = {
            "domain": self._squash(residuals.domain), "coupling": self._squash(residuals.coupling),
            "coc": self._squash(residuals.coc), "lci": self._squash(residuals.lci),
            "alignment": self._squash(residuals.alignment), "sync": self._squash(residuals.synchronization),
            "execution": self._squash(residuals.execution), "latency": self._squash(residuals.latency),
            "decision": self._squash(residuals.decision), "prediction": self._squash(residuals.prediction),
        }
        if gp:
            feat["prediction"] = max(feat["prediction"], self._squash(gp.get("prediction", GPResult(0,1,0,0)).anomaly / 5.0))
            feat["coupling"] = max(feat["coupling"], self._squash(gp.get("coupling", GPResult(0,1,0,0)).anomaly / 5.0))
            feat["execution"] = max(feat["execution"], self._squash(gp.get("execution", GPResult(0,1,0,0)).anomaly / 5.0))
        logp = {}; var = 0.18
        for cause in self.BASE_CAUSES:
            lp = math.log(max(self.priors[cause], 1e-12)); template = self.templates[cause]
            if cause == "Unknown":
                lp += gaussian_logpdf(max(feat.values() or [0.0]), 0.1, 0.25)
            else:
                for key, expected in template.items(): lp += gaussian_logpdf(feat.get(key, 0.0), expected, var)
                contradiction = max([v for k, v in feat.items() if k not in set(template)] or [0.0])
                lp += gaussian_logpdf(contradiction, 0.15, 0.5)
            logp[cause] = lp
        posterior = stable_softmax(logp); vals = list(posterior.values())
        entropy = -sum(p * math.log(max(p, 1e-12)) for p in vals) if len(vals) > 1 else 0.0
        confidence = 1.0 - entropy / math.log(len(vals)) if len(vals) > 1 else 1.0
        dominant = max(posterior, key=posterior.get)
        twin_post = self._normalize_scores(self._twin_scores(residuals, inp))
        edge_post = self._normalize_scores(self._edge_scores(residuals, inp))
        return AttributionResult(posterior=posterior, confidence=confidence, dominant_cause=dominant,
                                 twin_posterior=twin_post, edge_posterior=edge_post)
