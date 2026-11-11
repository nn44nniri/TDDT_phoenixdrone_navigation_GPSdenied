from __future__ import annotations

import csv
import hashlib
import math
import statistics
from pathlib import Path
from typing import Any, Mapping

from .storage import read_json_auto, write_json_gz
from .utils import sha256_obj


class KnowledgePolicyCompiler:
    """Compile verified OFFLINE closed-loop evidence into a read-only Online prior.

    The artifact is deliberately *not* an execution authorization.  It supplies a
    phase-local pilot-domain prior to Layer 5; Layer 4 FF, Layer 5 FINAL and the
    Layer 6/PT_LIB/ArduPilot safety chain remain mandatory.
    """

    SCHEMA = "TDOC-knowledge-policy-v1"
    ALGORITHM_VERSION = "TDOC-KPC-route-invariant-v2"
    FEATURE_ORDER = (
        "airspeed_m_s", "climb_m_s", "desired_climb_m_s", "agl_m",
        "pitch_rad", "q_rate_rad_s", "heading_error_rad",
    )
    COMMAND_ORDER = ("aileron", "elevator", "throttle", "rudder")

    @staticmethod
    def _no_guide(reason, *, status="UNAVAILABLE", **details):
        """Absence is not a zero command, a reference, or an integrity waiver."""
        return {"ok": False, "status": status, "availability": "UNAVAILABLE",
                "available": False, "reason": reason, "artifact": None, "phase_policy": None,
                "offline_prior_only": True, "actuation_authority": False, **details}

    @staticmethod
    def _guide_available(**details):
        return {"availability": "AVAILABLE", "available": True,
                "offline_prior_only": True, "actuation_authority": False, **details}

    def __init__(self, config):
        self.config = config
        rel = str(config.get("tdoc.knowledge_policy.artifact", "knowledge/policy/online_policy.json.gz"))
        self.path = (config.runtime_root / rel).resolve()
        timeline_rel = str(config.get("tdoc.knowledge_policy.offline_timeline", "reports/offline/mission/MISSION_TIMELINE.csv"))
        self.timeline_path = (config.package_root / timeline_rel).resolve()
        self.handoff_path = config.runtime_root / "knowledge" / "handoff" / "offline_latest.json.gz"

    @staticmethod
    def _file_sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _finite(row: Mapping[str, Any], key: str) -> float | None:
        try:
            value = float(row.get(key, ""))
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _true(row: Mapping[str, Any], key: str) -> bool:
        return str(row.get(key, "")).strip().lower() == "true"

    @staticmethod
    def _quantile(values: list[float], fraction: float) -> float:
        vals = sorted(float(v) for v in values)
        if not vals:
            raise ValueError("quantile requires data")
        if len(vals) == 1:
            return vals[0]
        pos = max(0.0, min(1.0, float(fraction))) * (len(vals) - 1)
        lo = int(math.floor(pos)); hi = int(math.ceil(pos))
        if lo == hi:
            return vals[lo]
        return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)

    @classmethod
    def _feature_stats(cls, rows: list[dict[str, Any]], order: list[str]) -> dict[str, Any]:
        stats = {}
        for name in order:
            values = [float(r["features"][name]) for r in rows]
            center = statistics.median(values)
            abs_dev = [abs(v - center) for v in values]
            mad = statistics.median(abs_dev)
            q05 = cls._quantile(values, 0.05); q95 = cls._quantile(values, 0.95)
            # The scale is learned from support.  The small floor is only numerical
            # conditioning for a constant feature and carries no control meaning.
            scale = max(mad * 1.4826, (q95 - q05) / 3.29, 1e-9)
            stats[name] = {
                "center": center, "scale": scale, "min": min(values), "max": max(values),
                "q05": q05, "q95": q95,
            }
        return stats

    @classmethod
    def _support_radius(cls, rows: list[dict[str, Any]], stats: Mapping[str, Any], order: list[str]) -> float:
        if len(rows) < 2:
            return 0.0
        vectors = [[(float(r["features"][k]) - float(stats[k]["center"])) /
                    float(stats[k]["scale"]) for k in order] for r in rows]
        nearest = []
        for i, vector in enumerate(vectors):
            distances = [math.sqrt(sum((a - b) ** 2 for a, b in zip(vector, other)))
                         for j, other in enumerate(vectors) if j != i]
            if distances:
                nearest.append(min(distances))
        return cls._quantile(nearest, 0.95) if nearest else 0.0

    def _reference_sources(self) -> dict[str, Any]:
        candidates: list[tuple[str, Path | None]] = []
        crp = self.config.path_value("TDDT_CRP_ROOT")
        sarg = self.config.path_value("TDDT_SARG_ROOT")
        twins = self.config.path_value("TDDT_TWINS_ROOT")
        climate = self.config.path_value("TDDT_CLIMATE_ROOT")
        candidates.extend([
            ("CRP.metadata", crp / "metadata.json" if crp else None),
            ("CRP.catalog", crp / "crp_cluster_catalog.csv" if crp else None),
            ("SARG.schema", sarg / "schema.json" if sarg else None),
            ("SARG.context", sarg / "region_context.json" if sarg else None),
            ("TEP.MESA_NAV", twins / "resources/tddt_layer2/MESA_NAV/tep/manifest.json" if twins else None),
            ("TEP.WingMPET", twins / "resources/tddt_layer2/WingMPET/tep/manifest.json" if twins else None),
            ("TEP.MicroMetTwin", climate / "resources/tddt_layer2/tep/manifest.json" if climate else None),
        ])
        out = {}
        for name, path in candidates:
            out[name] = {
                "path": str(path) if path else None,
                "present": bool(path and path.is_file()),
                "sha256": self._file_sha256(path) if path and path.is_file() else None,
            }
        return out

    def compile(self, *, handoff_path: str | Path | None = None) -> dict[str, Any]:
        hp = Path(handoff_path).resolve() if handoff_path else self.handoff_path.resolve()
        if not hp.is_file():
            return self._no_guide("offline_handoff_missing", status="REJECTED", path=str(hp))
        try:
            handoff = read_json_auto(hp)
        except Exception as exc:
            return self._no_guide("offline_handoff_unreadable", status="FAILED", error=repr(exc))
        if not isinstance(handoff, Mapping):
            return self._no_guide("offline_handoff_contract_invalid", status="REJECTED")
        finalization = handoff.get("finalization")
        if (handoff.get("schema") != "TDOC-offline-online-knowledge-handoff-v1"
                or str(handoff.get("source_learning_mode") or "").upper() != "OFFLINE_SE"
                or not isinstance(finalization, Mapping) or finalization.get("committed") is not True
                or finalization.get("ok", finalization.get("committed")) is not True
                or handoff.get("setd_integrity_valid") is False):
            return self._no_guide("offline_handoff_contract_invalid", status="REJECTED")
        if (not handoff.get("ok") or not handoff.get("handoff_hash")
                or not isinstance(handoff.get("setd"), Mapping) or not handoff["setd"].get("package_id")):
            return self._no_guide("verified_setd_handoff_required", status="REJECTED")
        check = {k: v for k, v in handoff.items() if k not in {"handoff_hash", "knowledge_id", "knowledge_version", "ok"}}
        try:
            matching_hash = sha256_obj(check) == handoff["handoff_hash"]
        except (TypeError, ValueError):
            matching_hash = False
        if not matching_hash:
            return self._no_guide("offline_handoff_hash_mismatch", status="REJECTED")
        if not self.timeline_path.is_file():
            return self._no_guide("offline_timeline_missing", path=str(self.timeline_path))

        with self.timeline_path.open("r", encoding="utf-8", newline="") as f:
            raw = list(csv.DictReader(f))
        desired_climb: list[float | None] = [None] * len(raw)
        heading_error: list[float | None] = [None] * len(raw)
        for i, row in enumerate(raw):
            j = i + 1 if i + 1 < len(raw) else i - 1
            if j < 0:
                continue
            z0 = self._finite(row, "reference_z_msl_m"); z1 = self._finite(raw[j], "reference_z_msl_m")
            t0 = self._finite(row, "reference_time_s"); t1 = self._finite(raw[j], "reference_time_s")
            if None not in (z0, z1, t0, t1) and abs(float(t1) - float(t0)) > 1e-12:
                desired_climb[i] = (float(z1) - float(z0)) / (float(t1) - float(t0))
            # Route-invariant *reference-tangent* heading error.  This deliberately
            # matches Layer5's Online ``cur -> nxt`` candidate semantics.  The old
            # reference-minus-realized vector mixed cross-track error into heading
            # and made an otherwise identical route appear outside policy support.
            # Timeline x/y are UTM E/N while runtime candidates are NED.
            ex=self._finite(raw[j],"reference_x_m"); ny=self._finite(raw[j],"reference_y_m")
            x=self._finite(row,"reference_x_m"); y=self._finite(row,"reference_y_m")
            hdg=self._finite(row,"vehicle_heading_deg")
            if None not in (ex,ny,x,y,hdg):
                de=float(ex)-float(x); dn=float(ny)-float(y)
                desired=math.atan2(de,dn) if math.hypot(de,dn)>1e-9 else math.radians(float(hdg))
                heading_error[i]=(desired-math.radians(float(hdg))+math.pi)%(2.0*math.pi)-math.pi

        accepted: dict[str, list[dict[str, Any]]] = {}
        rejected_counts: dict[str, int] = {}
        for i, row in enumerate(raw):
            phase = str(row.get("phase") or "").strip().lower()
            joint = bool(
                str(row.get("status") or "").upper() == "OK"
                and self._true(row, "l4_candidate_verified")
                and self._true(row, "l5_final_gate")
                and self._true(row, "l5_feedback_accepted")
                and self._true(row, "layer2_control_effect_applied")
                and self._true(row, "l5_control_present")
            )
            if not phase or not joint:
                rejected_counts[phase or "unknown"] = rejected_counts.get(phase or "unknown", 0) + 1
                continue
            values = {
                "airspeed_m_s": self._finite(row, "vehicle_airspeed_m_s"),
                "climb_m_s": self._finite(row, "vehicle_climb_m_s"),
                "desired_climb_m_s": desired_climb[i],
                "agl_m": self._finite(row, "agl_m"),
                "pitch_rad": None if self._finite(row, "vehicle_pitch_deg") is None else math.radians(float(row["vehicle_pitch_deg"])),
                "q_rate_rad_s": self._finite(row, "vehicle_pitch_rate_rad_s"),
                "heading_error_rad": heading_error[i],
            }
            command = [self._finite(row, f"l5_pilot_cmd_{n}") for n in range(4)]
            common=("airspeed_m_s","climb_m_s","desired_climb_m_s","agl_m","heading_error_rad")
            if (any(values[k] is None for k in common) or any(v is None for v in command)
                    or not all(lo <= v <= hi for v, lo, hi in zip(command, (-1,-1,0,-1), (1,1,1,1)))):
                rejected_counts[phase] = rejected_counts.get(phase, 0) + 1
                continue
            accepted.setdefault(phase, []).append({
                "transaction_id": str(row.get("transaction_id") or ""),
                "reference_time_s": self._finite(row, "reference_time_s"),
                "features": {k: (float(v) if v is not None else None) for k, v in values.items()},
                "pilot_command": [float(v) for v in command],
            })

        phases = {}
        for phase, rows in sorted(accepted.items()):
            # Some legacy wing-borne OFFLINE records do not expose attitude/rate.
            # Feature availability is therefore phase-local and explicit; missing
            # fields are never imputed or silently replaced with constants.
            order=[]
            constant_context={}
            for name in self.FEATURE_ORDER:
                values=[r["features"].get(name) for r in rows]
                if not all(v is not None for v in values):
                    continue
                finite_values=[float(v) for v in values]
                if min(finite_values)==max(finite_values):
                    constant_context[name]=finite_values[0]
                else:
                    order.append(name)
            for rec in rows:
                rec["features"]={name:float(rec["features"][name]) for name in order}
            stats = self._feature_stats(rows,order)
            commands = list(zip(*(r["pilot_command"] for r in rows)))
            phases[phase] = {
                "phase": phase,
                "support_count": len(rows),
                "feature_order": order,
                "command_order": list(self.COMMAND_ORDER),
                "feature_stats": stats,
                "constant_context_not_used_for_distance": constant_context,
                "support_radius_normalized": self._support_radius(rows, stats, order),
                "neighbor_count": max(1, int(math.ceil(math.sqrt(len(rows))))),
                "feedforward_median": [statistics.median(list(axis)) for axis in commands],
                "command_q05": [self._quantile(list(axis), 0.05) for axis in commands],
                "command_q95": [self._quantile(list(axis), 0.95) for axis in commands],
                "prototypes": rows,
                "confidence": 1.0 - math.exp(-float(len(rows))),
                "eligible_as_prior": True,
            }
        if not phases:
            return self._no_guide("no_joint_closed_loop_policy_rows", accepted_rows=0,
                rejected_rows_by_phase=dict(sorted(rejected_counts.items())))

        base = {
            "schema": self.SCHEMA,
            "algorithm_version": self.ALGORITHM_VERSION,
            # The policy identity is a pure function of verified evidence.  Reusing
            # the handoff timestamp avoids changing certificate identity merely
            # because the same inputs were compiled again.
            "compiled_at": handoff.get("created_at"),
            "compiler": "TDOC/KnowledgePolicyCompiler",
            "handoff_hash": handoff.get("handoff_hash"),
            "knowledge_id": handoff.get("knowledge_id"),
            "knowledge_version": handoff.get("knowledge_version"),
            "setd_package_id": (handoff.get("setd") or {}).get("package_id"),
            "source": {
                "timeline": {"path": str(self.timeline_path), "sha256": self._file_sha256(self.timeline_path), "rows": len(raw)},
                "handoff": {"path": str(hp), "content_hash": handoff.get("handoff_hash")},
                "reference_evidence": self._reference_sources(),
            },
            "selection_contract": {
                "requires": ["status=OK", "L4 candidate verified", "L5 FINAL=true", "L5 feedback accepted", "L2 control effect applied", "pilot command present"],
                "final_false_accepted": False,
                "hold_accepted": False,
                "fallback_claimed_as_success": False,
            },
            "policy_contract": {
                "kind": "phase-local evidence-weighted nearest-neighbour pilot-domain prior",
                "offline_prior_only": True,
                "trajectory_replay_required": False,
                "actuation_authority": False,
                "layer4_full_fidelity_required": True,
                "layer5_final_required": True,
                "layer6_executor_gate_required": True,
                "ardupilot_mixer_authority_preserved": True,
                "direct_jsbsim_actuation": False,
                "out_of_support": "fail_closed_to_source_bound_limited_fallback",
            },
            "feature_order": list(self.FEATURE_ORDER),
            "command_order": list(self.COMMAND_ORDER),
            "accepted_rows": sum(len(v) for v in accepted.values()),
            "rejected_rows_by_phase": dict(sorted(rejected_counts.items())),
            "phases": phases,
        }
        base["artifact_hash"] = sha256_obj(base)
        write_json_gz(self.path, base, indent=2)
        return self._guide_available(ok=True, status="COMPILED", artifact_path=str(self.path),
                artifact_hash=base["artifact_hash"], accepted_rows=base["accepted_rows"],
                phases={k: v["support_count"] for k, v in phases.items()})

    def load(self, *, handoff_hash: str | None = None) -> dict[str, Any]:
        try:
            artifact = read_json_auto(self.path)
        except FileNotFoundError:
            return self._no_guide("knowledge_policy_artifact_missing", path=str(self.path))
        except Exception as exc:
            return self._no_guide("knowledge_policy_artifact_unreadable", status="FAILED", error=repr(exc))
        if not isinstance(artifact, Mapping):
            return self._no_guide("knowledge_policy_artifact_contract_invalid", status="REJECTED")
        expected = artifact.get("artifact_hash")
        check = dict(artifact); check.pop("artifact_hash", None)
        try:
            actual = sha256_obj(check)
        except (TypeError, ValueError) as exc:
            return self._no_guide("knowledge_policy_artifact_contract_invalid", status="REJECTED", error=repr(exc))
        reasons = []
        if artifact.get("schema") != self.SCHEMA: reasons.append("schema_mismatch")
        if artifact.get("algorithm_version") != self.ALGORITHM_VERSION: reasons.append("compiler_algorithm_version_mismatch")
        if not expected or expected != actual: reasons.append("artifact_hash_mismatch")
        if handoff_hash and artifact.get("handoff_hash") != handoff_hash: reasons.append("handoff_hash_mismatch")
        source = artifact.get("source") or {}
        timeline = source.get("timeline") if isinstance(source, Mapping) else None
        if not isinstance(timeline, Mapping):
            reasons.append("source_timeline_contract_invalid")
            timeline = {}
        if not self.timeline_path.is_file(): reasons.append("source_timeline_missing")
        elif timeline.get("sha256") != self._file_sha256(self.timeline_path): reasons.append("source_timeline_hash_mismatch")
        contract = artifact.get("policy_contract") or {}
        if (not isinstance(contract, Mapping) or contract.get("offline_prior_only") is not True
                or contract.get("actuation_authority") is not False):
            reasons.append("policy_authority_contract_invalid")
        if reasons:
            return self._no_guide(reasons[0], status="REJECTED", reasons=reasons,
                                  artifact_path=str(self.path), artifact_hash=expected)
        if not isinstance(artifact.get("phases"), Mapping):
            return self._no_guide("policy_phases_contract_invalid", status="REJECTED", artifact_hash=expected)
        if not artifact["phases"]:
            return self._no_guide("phase_policy_support_missing", artifact_hash=expected)
        return self._guide_available(ok=True, status="READY", reason=None, reasons=[],
            artifact_path=str(self.path), artifact=artifact, artifact_hash=expected)

    def phase_policy(self, phase: str, *, handoff_hash: str | None = None) -> dict[str, Any]:
        loaded = self.load(handoff_hash=handoff_hash)
        if not loaded.get("ok"):
            return loaded
        artifact = loaded["artifact"]
        item = (artifact.get("phases") or {}).get(str(phase or "").strip().lower())
        if (not isinstance(item, Mapping) or item.get("eligible_as_prior") is not True
                or not item.get("prototypes")):
            return self._no_guide("phase_policy_support_missing", phase=phase,
                                  artifact_hash=artifact.get("artifact_hash"))
        if str(item.get("phase") or "") != str(phase or "").strip().lower():
            return self._no_guide("phase_policy_mismatch", phase=phase,
                                  artifact_hash=artifact.get("artifact_hash"))
        return self._guide_available(**{"ok": True, "status": "READY", "schema": "TDOC-phase-knowledge-policy-v1",
                "artifact_hash": artifact.get("artifact_hash"), "handoff_hash": artifact.get("handoff_hash"),
                "setd_package_id": artifact.get("setd_package_id"), "policy_contract": artifact.get("policy_contract"),
                "phase_policy": dict(item)})
