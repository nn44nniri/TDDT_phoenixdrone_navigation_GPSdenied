from __future__ import annotations

import copy
import json
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from .config import Layer3Config
from .utils import append_jsonl, write_json


class TwinRegistry:
    """Registry-driven N-Twin contract catalog.

    The configured three-Twin profile is only the default population. Additional
    twins can be registered at runtime or declared under runtime.twin_registry.
    """
    REQUIRED_PACKET_FIELDS = {"state", "uncertainty", "quality", "validity", "reliability", "timestamp", "provenance"}

    def __init__(self, config: Layer3Config):
        self.config = config
        defaults = {
            "MESA_NAV": {"contract_path": str(config.twins_root / "resources/tddt_layer2/MESA_NAV/twin_contract.json"),
                         "adapter": "navigation_ned", "required": True},
            "WingMPET": {"contract_path": str(config.twins_root / "resources/tddt_layer2/WingMPET/twin_contract.json"),
                         "adapter": "flight_ned", "required": True},
            "MicroMetTwin": {"contract_path": str(config.climate_root / "resources/tddt_layer2/twin_contract.json"),
                             "adapter": "climate_enu", "required": True},
        }
        declared = dict(config.runtime.get("twin_registry") or {})
        merged = {**defaults, **declared}
        self.twins: dict[str, dict[str, Any]] = {}
        for twin, spec in merged.items():
            self.register(twin, **dict(spec))

    def register(self, twin_id: str, *, contract_path: str | None = None, contract: Mapping[str, Any] | None = None,
                 adapter: str = "generic", required: bool = False, coc_observations: list[dict[str, Any]] | None = None,
                 metadata: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        path = Path(contract_path).expanduser().resolve() if contract_path else None
        payload = dict(contract or {})
        if not payload and path is not None and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        entry = {
            "twin_id": str(twin_id), "contract_path": str(path) if path else None,
            "contract": payload, "adapter": str(adapter), "required": bool(required),
            "coc_observations": list(coc_observations or []), "metadata": dict(metadata or {}),
            "health": "UNKNOWN", "last_timestamp": None, "packet_valid": False,
        }
        self.twins[str(twin_id)] = entry
        return entry

    def adapter_for(self, twin_id: str) -> str:
        return str((self.twins.get(twin_id) or {}).get("adapter", "generic"))

    def coc_observations_for(self, twin_id: str) -> list[dict[str, Any]]:
        own = list((self.twins.get(twin_id) or {}).get("coc_observations") or [])
        configured = list((self.config.network.coc_observations or {}).get(twin_id) or [])
        return own or configured

    def validate_packet(self, twin_id: str, packet: Mapping[str, Any]) -> tuple[bool, list[str]]:
        if twin_id not in self.twins and bool(self.config.runtime.get("allow_dynamic_twin_registration", True)):
            prov = dict(packet.get("provenance") or {})
            self.register(twin_id, adapter=str(prov.get("layer3_adapter", "generic")), required=False,
                          coc_observations=list(prov.get("coc_observations") or []), metadata={"auto_registered": True})
        missing = sorted(self.REQUIRED_PACKET_FIELDS - set(packet))
        pid = str(packet.get("twin_id") or twin_id)
        if pid != twin_id: missing.append(f"twin_id_mismatch:{pid}")
        ok = not missing
        entry = self.twins.setdefault(twin_id, {"twin_id": twin_id, "adapter": "generic", "required": False})
        entry["packet_valid"] = ok; entry["health"] = "HEALTHY" if ok else "INVALID_PACKET"
        entry["last_timestamp"] = packet.get("timestamp"); entry["last_errors"] = list(missing)
        return ok, missing

    def validate_all(self, packets: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]]) -> dict[str, Any]:
        iterable = packets.values() if isinstance(packets, Mapping) else packets
        details: dict[str, Any] = {}; seen: set[str] = set()
        for p in iterable:
            twin = str(p.get("twin_id") or "UNKNOWN"); seen.add(twin)
            ok, errors = self.validate_packet(twin, p); details[twin] = {"ok": ok, "errors": errors}
        for expected, entry in self.twins.items():
            if entry.get("required") and expected not in seen:
                entry["health"] = "MISSING_PACKET"; entry["packet_valid"] = False
                details[expected] = {"ok": False, "errors": ["missing_packet"]}
        return {"ok": bool(details) and all(v["ok"] for v in details.values()), "details": details,
                "registered_twins": sorted(self.twins), "active_twins": sorted(seen)}

    def snapshot(self) -> dict[str, Any]:
        out = {}
        for k,v in self.twins.items():
            c = v.get("contract") or {}
            out[k] = {kk:vv for kk,vv in v.items() if kk != "contract"} | {
                "model": c.get("model"), "fmi": c.get("fmi"), "layer": c.get("layer")}
        return out


class ProvenanceManager:
    def __init__(self, config: Layer3Config):
        self.path = config.resolve(config.paths.provenance_log)

    def record(self, kind: str, payload: Mapping[str, Any]) -> None:
        append_jsonl(self.path, {"kind": kind, **dict(payload)})


class HealthManager:
    def __init__(self, config: Layer3Config):
        self.config = config
        self.mode = "NORMAL"
        self.last_safe_snapshot: dict[str, Any] | None = None

    def evaluate(self, *, registry_ok: bool, dependency_ok: bool, tdoc_ok: bool, gate_fraction: float) -> dict[str, Any]:
        score = (float(registry_ok) + float(dependency_ok) + float(tdoc_ok) + max(0.0, min(1.0, gate_fraction))) / 4.0
        if score >= float(self.config.network.health_min) and registry_ok and dependency_ok:
            self.mode = "NORMAL" if tdoc_ok else "DEGRADED"
        elif bool(self.config.runtime.get("hold_last_safe_on_validation_failure", True)):
            self.mode = "HOLD_LAST_SAFE"
        else:
            self.mode = "DEGRADED"
        return {
            "mode": self.mode, "score": score, "registry_ok": registry_ok,
            "dependency_ok": dependency_ok, "tdoc_ok": tdoc_ok, "gate_fraction": gate_fraction,
        }


class FeedbackRouter:
    """Routes diagnostic feedback to the owning layer; never mutates actuators."""
    def route(self, *, std: Mapping[str, Any], mismatch: float, stability: Mapping[str, Any], hidden_loop: Mapping[str, Any]) -> dict[str, Any]:
        destinations: list[dict[str, Any]] = []
        reps = std.get("representations") or {}
        for twin, rep in reps.items():
            if not rep.get("gate"):
                destinations.append({"type": "alignment/domain", "destination": twin, "reason": "STD_gate_rejected"})
        if mismatch > 0:
            destinations.append({"type": "trans_domain", "destination": "Layer3.2/Layer7", "metric": "J_C", "value": mismatch})
        if stability.get("certified") and not stability.get("stable"):
            destinations.append({"type": "stability", "destination": "Layer7", "reason": "unstable_certificate"})
        if float(hidden_loop.get("lci_proxy", 0.0) or 0.0) > 0.5:
            destinations.append({"type": "hidden_loop", "destination": "Layer7", "reason": "LCI_threshold"})
        return {"routes": destinations, "actuator_route": False}


class DecisionCoordinator:
    def route(self, prediction: Mapping[str, Any] | None, action: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "destination": "Layer5",
            "prediction_candidate": dict(prediction or {}),
            "action_candidate": dict(action or {}),
            "layer3_executes_actuator": False,
        }


class VersionManager:
    def __init__(self, config: Layer3Config, initial_policy: Mapping[str, Any]):
        self.config = config
        self.root = config.resolve(config.paths.versions_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.active = copy.deepcopy(dict(initial_policy))
        self.safe = copy.deepcopy(dict(initial_policy))
        self.history: list[dict[str, Any]] = []
        self._persist()

    def _persist(self) -> None:
        write_json(self.root / "active.json", self.active)
        write_json(self.root / "safe.json", self.safe)
        write_json(self.root / "history.json", self.history)

    def commit(self, policy: Mapping[str, Any], *, reason: str, safe: bool = True) -> dict[str, Any]:
        prev = copy.deepcopy(self.active)
        self.active = copy.deepcopy(dict(policy))
        if safe:
            self.safe = copy.deepcopy(self.active)
        self.history.append({"event": "commit", "reason": reason, "previous": prev.get("version"), "active": self.active.get("version")})
        self._persist()
        return self.snapshot()

    def rollback(self, *, reason: str) -> dict[str, Any]:
        prev = copy.deepcopy(self.active)
        self.active = copy.deepcopy(self.safe)
        self.history.append({"event": "rollback", "reason": reason, "previous": prev.get("version"), "active": self.active.get("version")})
        self._persist()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {"active_version": self.active.get("version"), "safe_version": self.safe.get("version"), "history_length": len(self.history)}


class AntiChatteringManager:
    def __init__(self, max_changes: int):
        self.max_changes = max(1, int(max_changes))
        self.recent = deque(maxlen=self.max_changes)

    def allow(self, topology_changes: Mapping[str, Any]) -> tuple[bool, str]:
        count = len([k for k, v in topology_changes.items() if v is not None])
        if count > self.max_changes:
            return False, f"too_many_structural_changes:{count}>{self.max_changes}"
        return True, "within_chattering_budget"
