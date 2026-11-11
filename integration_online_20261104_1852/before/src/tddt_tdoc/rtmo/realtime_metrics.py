from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    x = sorted(float(v) for v in values)
    pos = (len(x) - 1) * float(q)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return x[lo]
    return x[lo] + (x[hi] - x[lo]) * (pos - lo)


class RTMOControlLatencyTracker:
    """Thread-safe bounded tracker for *executed* Online control transactions.

    P1-4 timing semantics are intentionally narrow and auditable:
      start = Layer-3 atomic operational snapshot ready
      end   = Layer-6/GUI/MAVLink execution acknowledgement returned to TDOC

    PRE_RELEASE/HOLD/observation-only cycles are not samples and can never be used
    to claim the >=10 Hz / P99<=100 ms control objective.
    """

    def __init__(self, *, deadline_s: float = 0.1, max_samples: int = 20000,
                 min_samples_for_claim: int = 1000):
        self.deadline_s = max(float(deadline_s), 1e-9)
        self.min_samples_for_claim = max(1, int(min_samples_for_claim))
        self._samples: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_samples)))
        self._lock = threading.RLock()
        self._total = 0
        self._misses = 0

    def record(self, *, transaction_id: str, latency_s: float, phase: str | None,
               execution_status: str | None, actuation_executed: bool) -> dict[str, Any]:
        latency = max(0.0, float(latency_s))
        sample = {
            "transaction_id": str(transaction_id),
            "phase": None if phase is None else str(phase),
            "latency_s": latency,
            "deadline_s": self.deadline_s,
            "deadline_ok": bool(latency <= self.deadline_s),
            "execution_status": execution_status,
            "actuation_executed": bool(actuation_executed),
            "start_semantics": "LAYER3_ATOMIC_OPERATIONAL_SNAPSHOT_READY",
            "end_semantics": "LAYER6_NATIVE_EXECUTION_ACK_RETURNED_TO_TDOC",
        }
        with self._lock:
            self._samples.append(sample)
            self._total += 1
            if not sample["deadline_ok"]:
                self._misses += 1
        return sample

    def summary(self) -> dict[str, Any]:
        with self._lock:
            vals = [float(x["latency_s"]) for x in self._samples]
            total = int(self._total); misses = int(self._misses)
        p50 = _pct(vals, 0.50); p95 = _pct(vals, 0.95); p99 = _pct(vals, 0.99)
        enough = total >= self.min_samples_for_claim
        target = bool(enough and p99 is not None and p99 <= self.deadline_s)
        return {
            "schema": "TDOC-RTMO-control-latency-summary-v1",
            "sample_count": total,
            "retained_sample_count": len(vals),
            "deadline_s": self.deadline_s,
            "deadline_miss_count": misses,
            "deadline_miss_ratio": (misses / total if total else None),
            "p50_s": p50,
            "p95_s": p95,
            "p99_s": p99,
            "max_s": max(vals) if vals else None,
            "min_samples_for_experimental_claim": self.min_samples_for_claim,
            "experimentally_verified_p99_le_deadline": target,
            "claim_status": ("VERIFIED" if target else ("INSUFFICIENT_EXECUTED_SAMPLES" if not enough else "P99_DEADLINE_NOT_MET")),
            "measurement": "Layer3 atomic snapshot -> Layer6/GUI/MAVLink execution acknowledgement",
            "excludes": ["pre_release_bootstrap", "background_l4_refresh", "post_ack_persistence", "report_generation"],
        }
