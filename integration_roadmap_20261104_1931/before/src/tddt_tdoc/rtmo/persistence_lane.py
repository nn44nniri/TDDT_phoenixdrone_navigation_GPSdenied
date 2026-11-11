from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from ..storage import write_json_gz


class OnlinePersistenceLane:
    """Bounded nonblocking persistence lane for Online cycles only.

    It moves per-layer audit archives and full-cycle gzip writes out of the control
    transaction.  Online cycles are deliberately *not* appended to the generic
    knowledge ledger: persistent online learning is owned exclusively by Layer 7.
    The queue is bounded; overflow is explicit and never silently converted into a
    control-success claim.
    """

    def __init__(self, *, knowledge, layer_archive, runtime_root: Path,
                 max_queue: int = 256):
        self.knowledge = knowledge
        self.layer_archive = layer_archive
        self.runtime_root = Path(runtime_root)
        self._q: queue.Queue = queue.Queue(maxsize=max(1, int(max_queue)))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._stats = {
            "schema": "TDOC-RTMO-online-persistence-lane-v2",
            "enqueued": 0, "persisted": 0, "dropped": 0, "failed": 0,
            "max_queue": self._q.maxsize, "last_error": None,
            "persistent_learning_authority": "Layer7",
            "general_knowledge_writes": 0,
        }
        self._thread = threading.Thread(target=self._worker, name="tdoc-online-persistence", daemon=True)
        self._thread.start()

    def submit(self, cycle: dict[str, Any]) -> dict[str, Any]:
        # Caller transfers ownership of a freshly materialized cycle dictionary.
        # Avoid a second deep copy of large Layer4 trajectories in the post-ack path.
        item = cycle
        try:
            self._q.put_nowait(item)
            with self._lock:
                self._stats["enqueued"] += 1
            return {"accepted": True, "nonblocking": True, "queue_depth": self._q.qsize(), **self.status()}
        except queue.Full:
            with self._lock:
                self._stats["dropped"] += 1
            return {"accepted": False, "nonblocking": True, "reason": "bounded_persistence_queue_full", "queue_depth": self._q.qsize(), **self.status()}

    def _persist(self, cycle: dict[str, Any]) -> None:
        tx = str(cycle.get("transaction_id") or "unknown")
        t0 = time.perf_counter()
        s0 = time.perf_counter(); layer_storage = self.layer_archive.record_cycle(cycle); st = time.perf_counter() - s0
        prov = cycle.setdefault("provenance", {})
        prov["online_knowledge_persistence"] = {
            "schema": "TDOC-online-knowledge-authority-v1",
            "general_knowledge_write": False,
            "persistent_learning_authority": "Layer7",
            "reason": "online_cycle_is_audit_evidence_not_new_generic_knowledge",
        }
        prov["layer_runtime_storage"] = layer_storage
        prov["post_ack_persistence_timing_s"] = {
            "runtime_storage": st,
            "total_before_cycle_snapshot": time.perf_counter() - t0,
            "control_hot_path": False,
        }
        outdir = self.runtime_root / "cycles"; outdir.mkdir(parents=True, exist_ok=True)
        w0 = time.perf_counter(); write_json_gz(outdir / f"{tx}.json.gz", cycle, indent=2)
        prov["post_ack_persistence_timing_s"]["cycle_snapshot"] = time.perf_counter() - w0
        prov["post_ack_persistence_timing_s"]["total"] = time.perf_counter() - t0

    def _worker(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._persist(item)
                with self._lock:
                    self._stats["persisted"] += 1
            except Exception as exc:
                with self._lock:
                    self._stats["failed"] += 1
                    self._stats["last_error"] = repr(exc)
            finally:
                self._q.task_done()

    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._stats)
        out.update({"queue_depth": self._q.qsize(), "unfinished_tasks": int(self._q.unfinished_tasks), "worker_alive": self._thread.is_alive()})
        return out

    def flush(self, timeout_s: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._q.unfinished_tasks == 0

    def close(self, timeout_s: float = 30.0) -> dict[str, Any]:
        flushed = self.flush(timeout_s)
        self._stop.set()
        self._thread.join(max(0.0, float(timeout_s)))
        return {"flushed": flushed, **self.status()}
