from __future__ import annotations
import math
import numpy as np
from .config import DriftConfig
from .models import DriftResult
from .utils import gaussian_logpdf

class TruncatedBayesianChangePoint:
    """Bounded-memory Bayesian online changepoint detector.

    Uses a Gaussian mean model with known observation variance and truncates
    the run-length posterior to max_run_length.
    """
    def __init__(self, cfg: DriftConfig):
        self.cfg = cfg
        self.run_probs = np.array([1.0], dtype=float)
        self.means = np.array([cfg.prior_mean], dtype=float)
        self.precisions = np.array([cfg.prior_precision], dtype=float)
        self.persistence = 0.0

    def update(self, x: float, anomaly: float, gp_mean: float) -> DriftResult:
        x = float(x)
        obs_var = max(self.cfg.observation_variance, 1e-9)
        n = len(self.run_probs)
        pred = np.empty(n, dtype=float)
        for r in range(n):
            pred_var = obs_var + 1.0 / max(self.precisions[r], 1e-12)
            pred[r] = math.exp(gaussian_logpdf(x, self.means[r], pred_var))
        hazard = min(max(self.cfg.hazard, 1e-9), 1-1e-9)
        cp_mass = float(np.sum(self.run_probs * pred * hazard))
        growth = self.run_probs * pred * (1.0 - hazard)
        new_probs = np.concatenate([[cp_mass], growth])[: self.cfg.max_run_length + 1]
        s = float(new_probs.sum()) or 1.0
        new_probs /= s

        # Posterior sufficient statistics corresponding to run lengths.
        new_means = [self.cfg.prior_mean]
        new_precs = [self.cfg.prior_precision]
        for r in range(min(n, self.cfg.max_run_length)):
            old_prec = self.precisions[r]
            new_prec = old_prec + 1.0 / obs_var
            new_mean = (old_prec * self.means[r] + x / obs_var) / new_prec
            new_means.append(new_mean)
            new_precs.append(new_prec)
        self.run_probs = new_probs
        self.means = np.asarray(new_means[:len(new_probs)], dtype=float)
        self.precisions = np.asarray(new_precs[:len(new_probs)], dtype=float)

        cp = float(self.run_probs[0])
        drift_evidence = bool(anomaly > self.cfg.anomaly_threshold and abs(gp_mean) > self.cfg.bias_threshold)
        d = 1.0 if drift_evidence else 0.0
        lam = self.cfg.persistence_lambda
        self.persistence = (1.0 - lam) * self.persistence + lam * d
        return DriftResult(changepoint_probability=cp, persistence=float(self.persistence), drift_evidence=drift_evidence)
