from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Iterable
from .config import Layer5Config
from .models import Layer5Candidate

@dataclass
class RegistryRecord:
    candidate_id: str
    snapshot_version: str
    snapshot_hash: str
    verified: bool
    feasible: bool
    provenance: dict[str, Any]

class CandidateRegistry:
    """Bounded, version-consistent candidate registry (Formalism §3 / L5-11..12).

    The registry never generates candidates or a new Pareto set.  It only owns the
    bounded set received from Layer 4 for the current invocation and rejects duplicate
    or malformed IDs so later audit records can be reconstructed deterministically.
    """
    def __init__(self, config: Layer5Config):
        self.config = config
        self._items: list[Layer5Candidate] = []
        self._records: dict[str, RegistryRecord] = {}
        self._errors: list[str] = []

    def load(self, candidates: Iterable[Layer5Candidate]) -> list[Layer5Candidate]:
        self._items.clear(); self._records.clear(); self._errors.clear()
        max_n = int(self.config.get("edge.candidates_max", 12))
        for c in list(candidates)[:max_n]:
            cid = str(c.candidate_id or "")
            if not cid:
                self._errors.append("empty_candidate_id"); continue
            if cid in self._records:
                self._errors.append(f"duplicate_candidate_id:{cid}"); continue
            self._items.append(c)
            self._records[cid] = RegistryRecord(
                cid, str(c.snapshot_version), str(c.snapshot_hash), bool(c.verified),
                bool(c.feasible), dict(c.provenance or {}))
        return list(self._items)

    def items(self) -> list[Layer5Candidate]: return list(self._items)
    def record(self, candidate_id: str) -> RegistryRecord | None: return self._records.get(candidate_id)
    def status(self) -> dict[str, Any]:
        return {"count": len(self._items), "max": int(self.config.get("edge.candidates_max",12)),
                "errors": list(self._errors), "ok": not self._errors,
                "candidate_ids": [c.candidate_id for c in self._items],
                "pareto_generation": False}
