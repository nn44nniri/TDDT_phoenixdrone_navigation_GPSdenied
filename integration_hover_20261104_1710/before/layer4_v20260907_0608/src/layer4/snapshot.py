from __future__ import annotations
from typing import Any, Mapping
from .models import AtomicSnapshot
from .hashing import sha256_obj
from .utils import finite_float

_HASH_FIELDS=("policy_version","reference_time_s","std","center_of_consistency","mismatch","weights","couplings","topology","consistency_margin","uncertainty_td","guidance","constraints","context","stability","slow_policy")

class SnapshotError(ValueError): pass

class Layer3SnapshotAdapter:
    """E4-3..E4-6 adapter for serialized Layer-3 NetworkSnapshot/StepResult."""
    def __init__(self, config): self.config=config

    def _unwrap(self, data: Mapping[str,Any]) -> dict[str,Any]:
        if "snapshot" in data and isinstance(data["snapshot"],Mapping): return dict(data["snapshot"])
        return dict(data)

    def canonical_hash(self, snap: Mapping[str,Any]) -> str:
        payload={k:snap.get(k) for k in _HASH_FIELDS}
        return sha256_obj(payload)

    def adapt(self, data: Mapping[str,Any]) -> AtomicSnapshot:
        s=self._unwrap(data)
        required=["policy_version","reference_time_s","std","topology","weights","couplings","center_of_consistency","uncertainty_td","guidance","context","provenance"]
        missing=[k for k in required if k not in s]
        if missing: raise SnapshotError(f"Layer-3 snapshot missing fields: {missing}")
        prov=dict(s.get("provenance") or {})
        if self.config.get("snapshot.require_atomic_kind",True):
            kind=prov.get("snapshot_kind")
            if kind not in (None,"atomic_layer3_network_snapshot"):
                raise SnapshotError(f"not an atomic Layer-3 snapshot: {kind}")
        if self.config.get("snapshot.require_boundary",True):
            sched=prov.get("time_scheduler") or {}
            # Layer4-details eq. (8): an invocation may be issued on any valid
            # Fast, Meso, or Slow Layer-3.3 synchronization boundary.
            boundary_keys=("fast_index","meso_index","slow_index")
            if not any(k in prov or k in sched for k in boundary_keys):
                raise SnapshotError("snapshot lacks Fast/Meso/Slow Layer-3.3 synchronization-boundary evidence")
        h=self.canonical_hash(s)
        declared=s.get("snapshot_hash") or prov.get("snapshot_hash")
        if declared and declared!=h: raise SnapshotError("declared snapshot hash does not match canonical content")
        if not declared and not self.config.get("snapshot.allow_missing_declared_hash",True):
            raise SnapshotError("snapshot has no declared hash")
        return AtomicSnapshot(
            version=str(s["policy_version"]), reference_time_s=finite_float(s["reference_time_s"]), hash=h,
            std=dict(s.get("std") or {}), center_of_consistency=dict(s.get("center_of_consistency") or {}),
            mismatch=None if s.get("mismatch") is None else finite_float(s.get("mismatch")),
            weights={str(k):finite_float(v) for k,v in (s.get("weights") or {}).items()},
            couplings={str(k):finite_float(v) for k,v in (s.get("couplings") or {}).items()},
            topology=dict(s.get("topology") or {}),
            consistency_margin=None if s.get("consistency_margin") is None else finite_float(s.get("consistency_margin")),
            uncertainty_td=dict(s.get("uncertainty_td") or {}), guidance=dict(s.get("guidance") or {}),
            constraints=dict(s.get("constraints") or {}), context=dict(s.get("context") or {}),
            stability=dict(s.get("stability") or {}), provenance=prov, slow_policy=dict(s.get("slow_policy") or {}), source_snapshot=s,
        )
