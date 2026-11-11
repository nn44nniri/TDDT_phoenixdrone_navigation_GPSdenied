from __future__ import annotations
import math
import numpy as np
from .config import GPConfig
from .models import GPResult
from .utils import gaussian_logpdf

class SparseOnlineGP:
    """Bounded dictionary Gaussian-process discrepancy model.

    The inverse kernel matrix is maintained incrementally for O(M^2) append
    updates.  When the fixed dictionary is full a deterministic replacement
    causes an O(M^3) rebuild; this is bounded by max_points and is not on every
    tick.
    """
    def __init__(self, cfg: GPConfig):
        self.cfg = cfg
        self.X = np.empty((0, 0), dtype=float)
        self.y = np.empty((0,), dtype=float)
        self.Kinv = np.empty((0, 0), dtype=float)
        self._replace_cursor = 0

    def kernel(self, a: np.ndarray, b: np.ndarray) -> float:
        d = (a - b) / max(self.cfg.length_scale, 1e-12)
        return float(self.cfg.signal_variance * math.exp(-0.5 * float(d @ d)))

    def _ensure_dim(self, x: np.ndarray) -> None:
        if self.X.size and self.X.shape[1] != x.size:
            raise ValueError(f"feature dimension changed: {self.X.shape[1]} -> {x.size}")

    def predict(self, x) -> tuple[float, float]:
        x = np.asarray(x, dtype=float).reshape(-1)
        self._ensure_dim(x)
        kxx = self.kernel(x, x) + self.cfg.jitter
        if self.y.size == 0:
            return 0.0, max(kxx, self.cfg.min_variance)
        k = np.array([self.kernel(x, xi) for xi in self.X], dtype=float)
        alpha = self.Kinv @ self.y
        mean = float(k @ alpha)
        var = float(kxx - k @ self.Kinv @ k)
        return mean, max(var, self.cfg.min_variance)

    def _rebuild(self):
        if not self.y.size:
            self.Kinv = np.empty((0, 0), dtype=float)
            return
        m = len(self.y)
        K = np.empty((m, m), dtype=float)
        for i in range(m):
            for j in range(m):
                K[i, j] = self.kernel(self.X[i], self.X[j])
        K += np.eye(m) * (self.cfg.noise_variance + self.cfg.jitter)
        self.Kinv = np.linalg.pinv(K)

    def update(self, x, y: float) -> bool:
        x = np.asarray(x, dtype=float).reshape(-1)
        self._ensure_dim(x)
        _, pred_var = self.predict(x)
        add = (self.y.size == 0) or (pred_var >= self.cfg.novelty_variance)
        if not add:
            # Lightweight target adaptation of nearest basis point.
            if self.y.size:
                d = np.linalg.norm(self.X - x[None, :], axis=1)
                idx = int(np.argmin(d))
                self.y[idx] = 0.9 * self.y[idx] + 0.1 * float(y)
            return False

        if self.y.size < self.cfg.max_points:
            if self.y.size == 0:
                self.X = x.reshape(1, -1)
                self.y = np.array([float(y)])
                denom = self.kernel(x, x) + self.cfg.noise_variance + self.cfg.jitter
                self.Kinv = np.array([[1.0 / max(denom, self.cfg.min_variance)]])
                return True
            k = np.array([self.kernel(x, xi) for xi in self.X], dtype=float)
            c = self.kernel(x, x) + self.cfg.noise_variance + self.cfg.jitter
            v = self.Kinv @ k
            schur = max(float(c - k @ v), self.cfg.min_variance)
            top_left = self.Kinv + np.outer(v, v) / schur
            top_right = -v[:, None] / schur
            bottom = np.array([[1.0 / schur]])
            self.Kinv = np.block([[top_left, top_right], [top_right.T, bottom]])
            self.X = np.vstack([self.X, x])
            self.y = np.append(self.y, float(y))
            return True

        # Fixed-memory deterministic replacement.
        idx = self._replace_cursor % self.cfg.max_points
        self._replace_cursor += 1
        self.X[idx] = x
        self.y[idx] = float(y)
        self._rebuild()
        return True

    def observe(self, x, y: float, measurement_variance: float = 0.0) -> GPResult:
        mean, var = self.predict(x)
        total = max(var + float(measurement_variance), self.cfg.min_variance)
        anomaly = -gaussian_logpdf(float(y), mean, total)
        z = (float(y) - mean) / math.sqrt(total)
        added = self.update(x, float(y))
        return GPResult(mean=mean, variance=var, anomaly=anomaly, zscore=z, added_to_dictionary=added)

class SparseGPBank:
    def __init__(self, cfg: GPConfig, families=None):
        self.cfg = cfg
        self.models = {name: SparseOnlineGP(cfg) for name in (families or ["prediction","domain","coupling","sync","execution"])}

    def evaluate(self, feature_vector, targets: dict[str, float], update: bool = True) -> dict[str, GPResult]:
        out = {}
        # Families are created lazily so per-output discrepancy models remain
        # bounded but need not be known at construction time.
        for name in targets:
            if name not in self.models:
                self.models[name] = SparseOnlineGP(self.cfg)
        for name, model in self.models.items():
            if name not in targets:
                continue
            y = float(targets.get(name, 0.0))
            if update:
                out[name] = model.observe(feature_vector, y)
            else:
                mean, var = model.predict(feature_vector)
                z = (y - mean) / math.sqrt(max(var, self.cfg.min_variance))
                out[name] = GPResult(mean=mean, variance=var, anomaly=-gaussian_logpdf(y,mean,var), zscore=z)
        return out
