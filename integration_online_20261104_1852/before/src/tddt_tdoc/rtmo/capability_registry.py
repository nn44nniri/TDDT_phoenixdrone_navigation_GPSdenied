from __future__ import annotations

from pathlib import Path
from typing import Any
import copy, gzip, json, os, tempfile, time

from .phase_manager import PHASE_ORDER

SCHEMA = "TDOC-RTMO-live-capability-registry-v1"
PASSIVE_PHASES = {"on_ground", "touchdown"}


def _empty_phase(phase: str) -> dict[str, Any]:
    return {
        "semantic_role": "passive_boundary" if phase in PASSIVE_PHASES else "controlled_phase",
        "l4_feasible_seen": False,
        "l4_verified": False,
        "l4_verified_cycles": [],
        "l4_verified_reference_times": [],
        "l4_snapshot_versions": [],
        "l4_snapshot_hashes": [],
        "l4_model_hashes": [],
        "l4_parameter_hashes": [],
        "effective_climate_epochs": [],
        "effective_climate_fingerprints": [],
        "l5_final": False,
        "l5_final_cycles": [],
        "l5_decision_ids": [],
        "joint_control_proof_cycles": [],
        "boundary_state_supported": False,
        "boundary_evidence": {},
        "pre_release_verification": {},
    }


def _append_unique(seq: list, value: Any) -> None:
    if value is not None and value not in seq:
        seq.append(value)


class LiveCapabilityRegistry:
    """Runtime authority for phase capability evidence.

    Reports are downstream audit products.  This registry is written directly from
    the in-memory Layer4/Layer5 results at the TDOC transaction boundary and is the
    authority consumed by RTMO readiness.  No CSV/HTML report is parsed to create a
    capability decision during normal operation.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, Any] | None = None

    def reset(self, *, run_id: str, episode_id: str, mission_version: str = "RTMO-11PHASE-v2") -> dict[str, Any]:
        self._data = {
            "schema": SCHEMA,
            "authority": "TDOC runtime transaction results; reports are audit-only",
            "run_id": str(run_id),
            "episode_id": str(episode_id),
            "mission_version": str(mission_version),
            "created_unix_s": time.time(),
            "updated_unix_s": time.time(),
            "phases": {p: _empty_phase(p) for p in PHASE_ORDER},
        }
        self._persist()
        return copy.deepcopy(self._data)

    def load(self) -> dict[str, Any] | None:
        if self._data is not None:
            return copy.deepcopy(self._data)
        if not self.path.is_file():
            return None
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as f:
                obj = json.load(f)
            if not isinstance(obj, dict) or obj.get("schema") != SCHEMA:
                return None
            self._data = obj
            return copy.deepcopy(obj)
        except Exception:
            return None

    def record_cycle(
        self,
        *,
        phase: str,
        cycle_id: int,
        reference_time_s: float,
        layer4_result: dict[str, Any] | None,
        layer5_result: dict[str, Any] | None,
        control_effect_applied: bool,
        boundary_evidence: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._data is None:
            loaded = self.load()
            if loaded is None:
                raise RuntimeError("live capability registry must be reset before record_cycle")
            self._data = loaded
        if phase not in PHASE_ORDER:
            raise ValueError(f"non-canonical mission phase cannot enter capability registry: {phase!r}")
        rec = self._data["phases"][phase]
        l4 = dict(layer4_result or {})
        l5 = dict(layer5_result or {})
        verified = bool(l4.get("verified_candidates"))
        feasible_seen = any(bool(c.get("feasible")) for c in (l4.get("candidates") or []))
        if feasible_seen:
            rec["l4_feasible_seen"] = True
        gates = dict(l5.get("gates") or {})
        decision = dict(l5.get("decision") or {})
        final = bool(gates.get("FINAL") is True or ((decision.get("approval") or {}).get("final_gate") is True))
        if verified:
            rec["l4_verified"] = True
            _append_unique(rec["l4_verified_cycles"], int(cycle_id))
            _append_unique(rec["l4_verified_reference_times"], float(reference_time_s))
            _append_unique(rec["l4_snapshot_versions"], l4.get("snapshot_version"))
            _append_unique(rec["l4_snapshot_hashes"], l4.get("snapshot_hash"))
            prov = dict(l4.get("provenance") or {})
            _append_unique(rec["l4_model_hashes"], prov.get("model_hash"))
            _append_unique(rec["l4_parameter_hashes"], prov.get("parameter_hash"))
            ctx=dict(context or {})
            align=dict(ctx.get("offline_climate_time_alignment") or ctx.get("online_climate_time_alignment") or {})
            _append_unique(rec["effective_climate_epochs"], ctx.get("effective_climate_epoch") or align.get("effective_climate_epoch"))
            _append_unique(rec["effective_climate_fingerprints"], ctx.get("effective_climate_fingerprint") or align.get("forcing_file_sha256"))
        if final:
            rec["l5_final"] = True
            _append_unique(rec["l5_final_cycles"], int(cycle_id))
            _append_unique(rec["l5_decision_ids"], decision.get("decision_id"))
        if verified and final and bool(control_effect_applied):
            _append_unique(rec["joint_control_proof_cycles"], int(cycle_id))
        if boundary_evidence is not None and phase in PASSIVE_PHASES:
            supported = bool(boundary_evidence.get("supported"))
            rec["boundary_state_supported"] = bool(rec.get("boundary_state_supported") or supported)
            rec["boundary_evidence"] = copy.deepcopy(boundary_evidence)
        self._data["updated_unix_s"] = time.time()
        self._persist()
        return copy.deepcopy(rec)

    def record_boundary_state(self, *, phase: str, boundary_evidence: dict[str, Any]) -> dict[str, Any]:
        """Replace passive-boundary authority with the latest operational evidence.

        Unlike ``record_cycle`` (which accumulates historical proof), this method is
        intentionally *current-state authoritative*: a fresh Online observation may
        either grant or revoke a passive-boundary capability.  It never writes L4/L5
        proof and therefore cannot manufacture controlled-phase capability.
        """
        if self._data is None:
            loaded = self.load()
            if loaded is None:
                raise RuntimeError("live capability registry must be reset before record_boundary_state")
            self._data = loaded
        if phase not in PASSIVE_PHASES:
            raise ValueError(f"current boundary-state evidence is only valid for passive phases: {phase!r}")
        rec = self._data["phases"][phase]
        rec["boundary_state_supported"] = bool(boundary_evidence.get("supported"))
        rec["boundary_evidence"] = copy.deepcopy(boundary_evidence)
        self._data["updated_unix_s"] = time.time()
        self._persist()
        return copy.deepcopy(rec)

    def record_pre_release_verification(self, *, phase: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """Persist audit evidence for the latest non-actuating PRE_RELEASE_VERIFY.

        This field is intentionally *not* folded into ``joint_control_proof_cycles``
        or the durable ``capability`` calculation.  RuntimeRelease may consume the
        same evidence transaction directly, while durable capability is earned only
        after a real control effect is observed by ``record_cycle``.
        """
        if self._data is None:
            loaded = self.load()
            if loaded is None:
                raise RuntimeError("live capability registry must be reset before record_pre_release_verification")
            self._data = loaded
        if phase not in PHASE_ORDER or phase in PASSIVE_PHASES:
            raise ValueError(f"pre-release verification is only valid for controlled mission phases: {phase!r}")
        rec = self._data["phases"][phase]
        rec["pre_release_verification"] = copy.deepcopy(dict(evidence or {}))
        self._data["updated_unix_s"] = time.time()
        self._persist()
        return copy.deepcopy(rec)

    def snapshot(self) -> dict[str, Any]:
        obj = self.load()
        if obj is None:
            raise RuntimeError("live capability registry unavailable")
        return obj

    def _persist(self) -> None:
        assert self._data is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        os.close(fd)
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                json.dump(self._data, f, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            try:
                if os.path.exists(tmp): os.unlink(tmp)
            except Exception:
                pass


def load_live_capability_registry(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return LiveCapabilityRegistry(path).load()
