from __future__ import annotations
from pathlib import Path
import queue
import threading
import time
from threading import Lock
from .models import InvocationEvent
from .utils import sha256_obj
from .storage import GzipChunkStore

class ProvenanceLog:
    """Hash-chained invocation provenance stored as bounded gzip JSONL chunks."""
    def __init__(self,path:Path, *, chunk_records:int=128, chunk_bytes:int=1_048_576):
        self.legacy_path=Path(path)
        # ``events/invocations.jsonl`` becomes ``events/invocations/chunks/*.jsonl.gz``.
        self.root=self.legacy_path.parent/self.legacy_path.stem
        self.store=GzipChunkStore(self.root,max_records=chunk_records,max_uncompressed_bytes=chunk_bytes)
        self._lock=Lock(); self._last_hash=None; self._append_count=0; self._startup_chain_ok=True
        self._async_enabled=False
        self._persist_queue:queue.Queue[dict|None]=queue.Queue(maxsize=4096)
        self._persist_thread=threading.Thread(target=self._persist_worker,name="tdoc-provenance-writer",daemon=True)
        self._persist_thread.start()
        latest=self.store.latest()
        if isinstance(latest,dict): self._last_hash=latest.get("event_hash")
        elif self.legacy_path.exists():
            # Read-only backward compatibility for an upgraded runtime.
            try:
                import json
                lines=[x for x in self.legacy_path.read_text(encoding="utf-8").splitlines() if x.strip()]
                if lines:self._last_hash=json.loads(lines[-1]).get("event_hash")
            except Exception:self._last_hash=None
        # Full chain verification is allowed once at construction/preparation time,
        # never inside the >=10 Hz control transaction.
        try:
            self._startup_chain_ok=bool(self.verify().get("ok",False))
        except Exception:
            self._startup_chain_ok=False
    def append(self,event:InvocationEvent)->InvocationEvent:
        with self._lock:
            event.previous_hash=self._last_hash
            payload=event.to_dict(); payload["event_hash"]=None
            event.event_hash=sha256_obj(payload); self._last_hash=event.event_hash
            record=event.to_dict(); async_enabled=self._async_enabled
            self._append_count += 1
        if async_enabled:
            # Bounded fail-closed queue: backpressure is preferable to dropping a
            # hash-chain record.  Normal Online capacity is orders of magnitude below
            # this bound and episode_close flushes every accepted record.
            self._persist_queue.put(record)
        else:
            self.store.append(record)
        return event

    def _persist_worker(self)->None:
        while True:
            record=self._persist_queue.get()
            try:
                if record is None:return
                self.store.append(record)
            finally:
                self._persist_queue.task_done()

    def set_async(self,enabled:bool)->None:
        with self._lock:self._async_enabled=bool(enabled)

    def flush(self,timeout_s:float=30.0)->bool:
        deadline=time.monotonic()+max(0.0,float(timeout_s))
        while self._persist_queue.unfinished_tasks and time.monotonic()<deadline:
            time.sleep(0.002)
        return self._persist_queue.unfinished_tasks==0

    def close(self,timeout_s:float=30.0)->bool:
        with self._lock:self._async_enabled=False
        ok=self.flush(timeout_s)
        self._persist_queue.put(None)
        self._persist_thread.join(max(0.0,float(timeout_s)))
        return bool(ok and not self._persist_thread.is_alive())

    def health(self)->dict:
        """O(1) in-process hash-chain health for the real-time path.

        A full disk rescan remains available through :meth:`verify` and is used at
        preparation/report boundaries.  Every append still writes the hash-chained
        record before returning; this method only avoids re-reading the whole chain
        on every fast control transaction.
        """
        with self._lock:
            return {
                "ok": bool(self._startup_chain_ok),
                "startup_full_chain_verified": bool(self._startup_chain_ok),
                "events_appended_this_process": int(self._append_count),
                "last_hash": self._last_hash,
                "verification_mode": "O1_HASH_CHAIN_HEALTH",
                "async_persistence":bool(self._async_enabled),
                "pending_persistence_records":int(self._persist_queue.unfinished_tasks),
            }

    def _records(self):
        yielded=False
        for rec in self.store.iter_records():
            yielded=True; yield rec
        if not yielded and self.legacy_path.exists():
            import json
            for line in self.legacy_path.read_text(encoding="utf-8").splitlines():
                if line.strip(): yield json.loads(line)
    def verify(self)->dict:
        prev=None; n=0
        for d in self._records():
            got=d.get("event_hash"); d2=dict(d); d2["event_hash"]=None
            if d.get("previous_hash")!=prev or sha256_obj(d2)!=got:return {"ok":False,"events":n,"failed_at":n+1}
            prev=got; n+=1
        return {"ok":True,"events":n,"last_hash":prev,"storage":"gzip-chunked"}
