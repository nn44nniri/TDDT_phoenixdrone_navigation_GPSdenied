from __future__ import annotations

from typing import Any

from .config import Layer3Config


class TimeScheduler:
    """Explicit Fast/Meso/Slow clock coordinator for Layer 3.3.

    The scheduler owns only clock/boundary state.  It does not execute domain
    models and it does not mutate network policy outside a synchronization
    barrier.  This keeps the formal time policy T={dt,dt_m,dT} explicit.
    """

    def __init__(self, config: Layer3Config):
        self.fast_dt_s = float(config.time.fast_dt_s)
        self.meso_fast_steps = max(1, int(config.time.meso_fast_steps))
        self.slow_meso_windows = max(1, int(config.time.slow_meso_windows))
        self.fast_index = 0
        self.meso_index = 0
        self.slow_index = 0

    @property
    def meso_dt_s(self) -> float:
        return self.fast_dt_s * self.meso_fast_steps

    @property
    def slow_dt_s(self) -> float:
        return self.meso_dt_s * self.slow_meso_windows

    def fast_tick(self, reference_time_s: float) -> dict[str, Any]:
        self.fast_index += 1
        return {
            "event": "FAST_TICK",
            "fast_index": self.fast_index,
            "reference_time_s": float(reference_time_s),
            "fast_dt_s": self.fast_dt_s,
            "meso_due": self.fast_index % self.meso_fast_steps == 0,
        }

    def meso_boundary(self) -> dict[str, Any]:
        self.meso_index += 1
        return {
            "event": "MESO_BOUNDARY",
            "meso_index": self.meso_index,
            "meso_dt_s": self.meso_dt_s,
            "slow_due": self.meso_index % self.slow_meso_windows == 0,
        }

    def slow_boundary(self) -> dict[str, Any]:
        self.slow_index += 1
        return {
            "event": "SLOW_BOUNDARY",
            "slow_index": self.slow_index,
            "slow_dt_s": self.slow_dt_s,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "fast_index": self.fast_index,
            "meso_index": self.meso_index,
            "slow_index": self.slow_index,
            "fast_dt_s": self.fast_dt_s,
            "meso_dt_s": self.meso_dt_s,
            "slow_dt_s": self.slow_dt_s,
            "meso_fast_steps": self.meso_fast_steps,
            "slow_meso_windows": self.slow_meso_windows,
        }
