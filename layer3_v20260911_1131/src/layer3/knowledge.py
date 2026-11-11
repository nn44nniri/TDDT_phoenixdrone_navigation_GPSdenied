from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping

import numpy as np

from .config import Layer3Config
from .utils import clamp, safe_float


def _coerce(value: str) -> Any:
    text = (value or "").strip()
    if text == "":
        return None
    try:
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        return float(text)
    except Exception:
        return text


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: _coerce(v) for k, v in row.items()} for row in csv.DictReader(f)]


class CRPRepository:
    """Canonical CRP matcher for C_k=(mu_k,Sigma_k,n_k,q_k).

    Selection always returns the nearest formalism-compatible candidate, while
    a separate chi-square-style acceptance gate prevents an out-of-distribution
    observation from being mislabeled as a statistically supported match.
    """

    def __init__(self, config: Layer3Config):
        self.config = config
        path = config.crp_root / "crp_runtime_statistics.json"
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.feature_order: list[str] = list(self.data["feature_order"])
        self.clusters: list[dict[str, Any]] = list(self.data["clusters"])
        self.catalog_path = config.crp_root / "crp_cluster_catalog.csv"
        self.assignments_path = config.crp_root / "crp_cell_assignments.csv.gz"
        self.cluster_map_path = config.crp_root / "crp_cluster_map.tif"
        self.metadata_path = config.crp_root / "metadata.json"
        self.catalog_rows = _read_csv(self.catalog_path)
        self.catalog_by_id = {int(r["cluster_id"]): r for r in self.catalog_rows if r.get("cluster_id") is not None}
        self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))

    @staticmethod
    def _normalize_context(context: Mapping[str, Any]) -> dict[str, float]:
        c: dict[str, float] = {}
        for k, v in context.items():
            if isinstance(v, (dict, list, tuple)):
                continue
            fv = safe_float(v, float("nan"))
            if math.isfinite(fv):
                c[str(k)] = fv
        direction = c.get("wind_direction_from_deg", c.get("wind_direction_deg", float("nan")))
        if math.isfinite(direction):
            rad = math.radians(direction)
            c["wind_direction_sin"] = math.sin(rad)
            c["wind_direction_cos"] = math.cos(rad)
        if not math.isfinite(c.get("wind_speed_ms", float("nan"))):
            c["wind_speed_ms"] = c.get("wind_speed_3d_m_s", float("nan"))
        if not math.isfinite(c.get("terrain_elevation_m", float("nan"))):
            c["terrain_elevation_m"] = c.get("ground_elevation_m", float("nan"))
        return {k: v for k, v in c.items() if math.isfinite(v)}

    @staticmethod
    def _chi_square_threshold(df: int, probability: float) -> float:
        # Wilson-Hilferty approximation avoids adding scipy solely for a gate.
        df = max(1, int(df))
        p = min(max(float(probability), 0.500001), 0.999999)
        z = NormalDist().inv_cdf(p)
        a = 1.0 - 2.0 / (9.0 * df) + z * math.sqrt(2.0 / (9.0 * df))
        return float(df * max(a, 1e-9) ** 3)

    def match(self, context: Mapping[str, Any]) -> dict[str, Any]:
        c = self._normalize_context(context)
        available = [f for f in self.feature_order if f in c]
        if not available:
            return {"selected": False, "matched": False, "in_distribution": False,
                    "reason": "no_canonical_crp_features", "crp_id": None, "features_used": []}
        idx = [self.feature_order.index(f) for f in available]
        x = np.asarray([c[f] for f in available], dtype=float)
        best: tuple[float, float, dict[str, Any]] | None = None
        for cluster in self.clusters:
            mu = np.asarray(cluster["centroid"], dtype=float)[idx]
            cov_all = np.asarray(cluster.get("covariance_regularized") or cluster["covariance"], dtype=float)
            cov = cov_all[np.ix_(idx, idx)] + np.eye(len(idx)) * float(self.config.network.crp_regularizer)
            d = x - mu
            try:
                mahal = float(d.T @ np.linalg.solve(cov, d))
            except np.linalg.LinAlgError:
                mahal = float(d.T @ np.linalg.pinv(cov) @ d)
            q = clamp(float(cluster.get("quality_score", 0.0)), 0.0, 1.0)
            score = mahal + self.config.network.crp_quality_penalty * (1.0 - q)
            if best is None or score < best[0]:
                best = (score, mahal, cluster)
        assert best is not None
        score, mahal, cluster = best
        q = float(cluster.get("quality_score", 0.0))
        threshold = self._chi_square_threshold(len(available), self.config.network.crp_acceptance_probability)
        enough_features = len(available) >= int(self.config.network.crp_min_features)
        quality_ok = q >= float(self.config.network.crp_min_quality)
        spatial = context.get("crp_raster_id")
        try:
            spatial_id = int(float(spatial)) if spatial is not None else None
        except Exception:
            spatial_id = None
        selected_id = int(cluster["cluster_id"])
        spatial_agrees = spatial_id is None or spatial_id == selected_id
        spatial_ok = spatial_agrees or not bool(self.config.network.crp_require_spatial_prior_agreement)
        accepted = bool(math.isfinite(mahal) and mahal <= threshold and enough_features and quality_ok and spatial_ok)
        reason = "accepted" if accepted else "out_of_distribution"
        if not enough_features: reason = "insufficient_features"
        elif not quality_ok: reason = "cluster_quality_below_minimum"
        elif not spatial_ok: reason = "spatial_prior_disagreement"
        return {
            "selected": True,
            "matched": accepted,
            "in_distribution": accepted,
            "out_of_distribution": not accepted,
            "reason": reason,
            "crp_id": selected_id,
            "nearest_crp_id": selected_id,
            "crp_code": cluster.get("cluster_code"),
            "score": score,
            "mahalanobis_squared": mahal,
            "acceptance_threshold": threshold,
            "acceptance_probability": float(self.config.network.crp_acceptance_probability),
            "degrees_of_freedom": len(available),
            "quality_score": q,
            "sample_count": int(cluster.get("sample_count", 0)),
            "features_used": available,
            "feature_coverage": len(available) / max(len(self.feature_order), 1),
            "context_vector": {f: c[f] for f in available},
            "spatial_prior_crp_id": spatial_id,
            "spatial_prior_agrees": spatial_agrees,
            "canonical_definition": "C_k=(mu_k,Sigma_k,n_k,q_k)",
            "selection_formula": "argmin[d2 + lambda_q*(1-q)]",
            "quality_components": cluster.get("quality_components") or {},
            "catalog_record": self.catalog_by_id.get(selected_id),
            "dataset_metadata": {
                "schema": self.data.get("schema"),
                "feature_notes": self.data.get("feature_notes"),
                "covariance_definition": self.data.get("covariance_definition"),
                "quality_score_definition": self.data.get("quality_score_definition"),
                "cluster_count": len(self.clusters),
                "metadata_schema": self.metadata.get("schema"),
            },
            "data_sources": {
                "statistics": str(self.config.crp_root / "crp_runtime_statistics.json"),
                "catalog": str(self.catalog_path),
                "assignments": str(self.assignments_path),
                "cluster_map": str(self.cluster_map_path),
                "metadata": str(self.metadata_path),
            },
        }


class SARGRepository:
    """Canonical three-key SARG runtime consumer using the documented API."""

    def __init__(self, config: Layer3Config):
        self.config = config
        self.patterns: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.records: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.stage_to_phase: dict[str, int] = {}
        self.phase_to_stage: dict[int, str] = {}
        self._load(config.sarg_root / "sarg_patterns.csv", self.patterns, remember_stages=True)
        self._load(config.sarg_root / "sarg_records.csv", self.records, remember_stages=False)
        self.scenarios = _read_csv(config.sarg_root / "scenarios.csv")
        self.stage_summary = _read_csv(config.sarg_root / "sarg_stage_summary.csv")
        self.scenario_contexts = _read_csv(config.sarg_root / "scenario_climate_contexts.csv")
        self.status_codes = _read_csv(config.sarg_root / "status_codes.csv")
        self.lookup_index = _read_csv(config.sarg_root / "sarg_lookup_index.csv")
        self.fmi_execution_trace = _read_csv(config.sarg_root / "fmi_execution_trace.csv")
        self.crs_rejections = _read_csv(config.sarg_root / "crs_rejections.csv")
        self.metadata = json.loads((config.sarg_root / "metadata.json").read_text(encoding="utf-8"))
        self.schema = json.loads((config.sarg_root / "schema.json").read_text(encoding="utf-8"))
        self.region_context = json.loads((config.sarg_root / "region_context.json").read_text(encoding="utf-8"))
        self.region_geojson = json.loads((config.sarg_root / "region_v1.geojson").read_text(encoding="utf-8"))
        self.sample_count = self._gzip_csv_count(config.sarg_root / "sarg_samples.csv.gz")
        self.status_lookup = {(str(r.get("group")), int(r.get("code"))): str(r.get("name"))
                              for r in self.status_codes if r.get("code") is not None}
        if str(config.sarg_package_root) not in sys.path:
            sys.path.insert(0, str(config.sarg_package_root))
        from sarg_builder import PRIMARY_KEY, SARGSelector  # type: ignore
        self.primary_key = tuple(PRIMARY_KEY)
        if self.primary_key != ("regime_id", "phase_id", "crp_id"):
            raise RuntimeError(f"Unexpected SARG PRIMARY_KEY: {self.primary_key!r}")
        self.selector = SARGSelector(config.sarg_root)
        if len(self.lookup_index) != len(self.records):
            raise RuntimeError("SARG lookup-index cardinality differs from sarg_records.csv")
        if len(self.records) != len(set(self.records)):
            raise RuntimeError("Duplicate SARG primary keys detected")

    @staticmethod
    def _gzip_csv_count(path: Path) -> int:
        if not path.is_file():
            return 0
        with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f))

    def _scenario(self, scenario_id: int | None) -> dict[str, Any] | None:
        if scenario_id is None:
            return None
        return next((r for r in self.scenarios if int(r.get("scenario_id", -1)) == int(scenario_id)), None)

    def _fmi_trace_for(self, model_prefix: str | None = None) -> list[dict[str, Any]]:
        if not model_prefix:
            return list(self.fmi_execution_trace)
        prefix = str(model_prefix).lower()
        return [r for r in self.fmi_execution_trace if prefix in str(r).lower()]

    def _load(self, path: Path, target: dict[tuple[int, int, int], dict[str, Any]], remember_stages: bool) -> None:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                parsed = {k: _coerce(v) for k, v in row.items()}
                key = (int(parsed["regime_id"]), int(parsed["phase_id"]), int(parsed["crp_id"]))
                if key in target:
                    raise RuntimeError(f"Duplicate SARG key {key}")
                target[key] = parsed
                if remember_stages:
                    stage = str(parsed.get("mission_stage") or "").strip()
                    if stage:
                        self.stage_to_phase[stage] = int(parsed["phase_id"])
                        self.phase_to_stage[int(parsed["phase_id"])] = stage

    # TDOC mission-driver phases are more granular than the six canonical SARG
    # reference phases.  SARG remains reference/prior only; this table only selects
    # the nearest canonical prior and never replaces realized Twin state.
    MISSION_STAGE_ALIASES = {
        "on_ground": "takeoff_transition",
        "vertical_takeoff": "takeoff_transition",
        "hover": "takeoff_transition",
        "transition_forward": "takeoff_transition",
        "climb": "climb",
        "cruise": "cruise",
        "turn": "turn",
        "approach": "approach",
        "transition_back": "approach",
        "vertical_landing": "approach",
        "landing": "approach",
        "touchdown": "approach",
        "emergency_return": "emergency_return",
    }

    def canonical_sarg_stage(self, stage_or_phase: str | int) -> str | int:
        if isinstance(stage_or_phase, int):
            return stage_or_phase
        text=str(stage_or_phase).strip()
        if text.isdigit():
            return text
        return self.MISSION_STAGE_ALIASES.get(text, text)

    def resolve_phase(self, stage_or_phase: str | int) -> int:
        if isinstance(stage_or_phase, int):
            return stage_or_phase
        text = str(self.canonical_sarg_stage(stage_or_phase)).strip()
        if text.isdigit():
            return int(text)
        if text not in self.stage_to_phase:
            raise KeyError(f"Unknown SARG mission stage: {stage_or_phase!r} (canonical={text!r})")
        return self.stage_to_phase[text]

    def _stage_summary(self, scenario_id: int | None, stage: str | None) -> dict[str, Any] | None:
        for row in self.stage_summary:
            if stage and str(row.get("mission_stage")) != stage:
                continue
            if scenario_id is not None and int(row.get("scenario_id", -1)) != int(scenario_id):
                continue
            return row
        return None

    def _route_summary(self, scenario_id: int | None, crp_id: int, phase_id: int) -> dict[str, Any] | None:
        if scenario_id is None:
            return None
        path = self.config.sarg_root / "routes" / f"S{int(scenario_id):02d}_C{int(crp_id):03d}.csv"
        if not path.is_file():
            return {"available": False, "path": str(path)}
        rows = _read_csv(path)
        phase_rows = [r for r in rows if int(r.get("phase_id", -1)) == int(phase_id)]
        if not phase_rows:
            return {"available": True, "path": str(path), "point_count": len(rows), "phase_point_count": 0}
        def val(row: Mapping[str, Any], key: str) -> float | None:
            v = safe_float(row.get(key), float("nan")); return v if math.isfinite(v) else None
        return {
            "available": True, "path": str(path), "point_count": len(rows), "phase_point_count": len(phase_rows),
            "phase_id": int(phase_id),
            "start": {k: val(phase_rows[0], k) for k in ("lon_deg","lat_deg","z_msl_m","time_s","airspeed_ms","clearance_m")},
            "end": {k: val(phase_rows[-1], k) for k in ("lon_deg","lat_deg","z_msl_m","time_s","airspeed_ms","clearance_m")},
            "flight_valid_fraction": sum(1.0 for r in phase_rows if safe_float(r.get("flight_valid"),0.0)>=0.5)/len(phase_rows),
            "navigation_valid_fraction": sum(1.0 for r in phase_rows if safe_float(r.get("navigation_valid"),0.0)>=0.5)/len(phase_rows),
            "role": "pointwise SARG trajectory reference",
        }

    @staticmethod
    def _record_reference_admission(record: Mapping[str, Any], status_lookup: Mapping[tuple[str, int], str]) -> dict[str, Any]:
        """Fail-closed admission for an SARG record used as flight/navigation guidance.

        SARG remains a diagnostic/reference dataset.  A row may still be returned
        for provenance when it is not admissible, but it must not become a primary
        flight seed unless both WingMPET flight evidence and MESA navigation evidence
        are valid.
        """
        def ival(name: str, default: int = -999) -> int:
            try:
                return int(float(record.get(name, default)))
            except (TypeError, ValueError):
                return default

        def fval(name: str, default: float = 0.0) -> float:
            try:
                return float(record.get(name, default))
            except (TypeError, ValueError):
                return default

        record_status = ival("record_status")
        wing_status = ival("wing_status")
        mesa_status = ival("mesa_status")
        flight_ok = fval("flight_ok") >= 0.5
        nav_ok = fval("nav_ok") >= 0.5
        phase_used = fval("phase_used", 1.0) >= 0.5
        blockers: list[str] = []
        # VALID / VALID_LOW_CONF are the only canonical records that prove both
        # flight and navigation validity. FLIGHT_ONLY/NAV_ONLY remain diagnostics.
        if record_status not in (0, 1):
            blockers.append(f"record_status:{status_lookup.get(('record', record_status), record_status)}")
        if wing_status != 0:
            blockers.append(f"wing_status:{status_lookup.get(('wing', wing_status), wing_status)}")
        if mesa_status != 0:
            blockers.append(f"mesa_status:{status_lookup.get(('mesa', mesa_status), mesa_status)}")
        if not flight_ok:
            blockers.append("flight_ok:false")
        if not nav_ok:
            blockers.append("nav_ok:false")
        if not phase_used:
            blockers.append("phase_used:false")
        return {
            "eligible": not blockers,
            "blockers": blockers,
            "record_status": record_status,
            "wing_status": wing_status,
            "mesa_status": mesa_status,
            "flight_ok": flight_ok,
            "nav_ok": nav_ok,
            "phase_used": phase_used,
            "policy": "VALID_OR_VALID_LOW_CONF_WITH_WING_MESA_OK",
        }

    def select(self, regime_id: int | None, stage_or_phase: str | int, crp_id: int, *, reference_admissible: bool = True) -> dict[str, Any]:
        phase_id = self.resolve_phase(stage_or_phase)
        if regime_id is None:
            candidates = self.selector.select(regime_id=None, phase_id=phase_id, crp_id=int(crp_id))
            return {
                "selected": False, "reason": "regime_required_by_canonical_selector",
                "phase_id": phase_id, "crp_id": int(crp_id), "candidates": candidates,
                "candidate_count": len(candidates), "selector_api": "sarg_builder.SARGSelector.select",
                "primary_key": list(self.primary_key),
            }
        key = (int(regime_id), phase_id, int(crp_id))
        try:
            official = self.selector.select(regime_id=key[0], phase_id=key[1], crp_id=key[2])
        except KeyError:
            return {"selected": False, "reason": "key_not_found", "key": list(key), "primary_key": list(self.primary_key)}
        pattern = self.patterns.get(key) or {}
        record = dict(official)
        record_admission = self._record_reference_admission(record, self.status_lookup)
        context_admissible = bool(reference_admissible)
        effective_admissible = context_admissible and bool(record_admission["eligible"])
        scenario_id = int(record.get("scenario_id")) if record.get("scenario_id") is not None else None
        stage = str(pattern.get("mission_stage") or self.phase_to_stage.get(phase_id) or "")
        scenario_context = next((r for r in self.scenario_contexts
                                 if int(r.get("regime_id", -1)) == key[0] and int(r.get("crp_id", -1)) == key[2]), None)
        return {
            "selected": True,
            # ``selected`` means the exact canonical record exists and is retained
            # for diagnostics/provenance.  It is not equivalent to guidance authority.
            "reference_admissible": effective_admissible,
            "primary_flight_seed_eligible": effective_admissible,
            "reference_mode": "accepted" if effective_admissible else "diagnostic_only",
            "reference_admission": {
                "context_admissible": context_admissible,
                "record_eligible": bool(record_admission["eligible"]),
                "effective_admissible": effective_admissible,
                "blockers": list(record_admission["blockers"]),
                "policy": record_admission["policy"],
                "no_implicit_cross_regime_fallback": True,
            },
            "requested_mission_stage": (str(stage_or_phase) if not isinstance(stage_or_phase, int) else None),
            "canonical_sarg_stage": stage,
            "key": list(key),
            "selector": "Select_SARG(regime_id, phase_id, crp_id)",
            "selector_api": "sarg_builder.SARGSelector.select",
            "primary_key": list(self.primary_key),
            "pattern": pattern,
            "record": record,
            "scenario": self._scenario(scenario_id),
            "scenario_climate_context": scenario_context,
            "stage_summary": self._stage_summary(scenario_id, stage),
            "route_reference": self._route_summary(scenario_id, key[2], phase_id),
            "region_context": self.region_context,
            "region_geojson_source": str(self.config.sarg_root / "region_v1.geojson"),
            "fmi_execution_trace": list(self.fmi_execution_trace),
            "crs_rejections": list(self.crs_rejections),
            "status": {
                "record": self.status_lookup.get(("record", int(record.get("record_status", -999)))),
                "wing": self.status_lookup.get(("wing", int(record.get("wing_status", -999)))),
                "mesa": self.status_lookup.get(("mesa", int(record.get("mesa_status", -999)))),
            },
            "dataset_contract": {
                "record_count": len(self.records),
                "lookup_count": len(self.lookup_index),
                "metadata_record_count": self.metadata.get("record_count"),
                "scenario_count": len(self.scenarios),
                "stage_summary_count": len(self.stage_summary),
                "scenario_context_count": len(self.scenario_contexts),
                "status_code_count": len(self.status_codes),
                "fmi_trace_count": len(self.fmi_execution_trace),
                "crs_rejection_count": len(self.crs_rejections),
                "sample_count": self.sample_count,
                "fmi_operation_trace": str(self.config.sarg_root / "fmi_execution_trace.csv"),
                "metadata_fmi_operation_trace": self.metadata.get("fmi_operation_trace"),
                "authority_order": ["metadata.json","schema.json","sarg_records.csv","sarg_lookup_index.csv","routes/*.csv","region_v1.geojson"],
            },
        }

    @staticmethod
    def deviation(meso: Mapping[str, Any], selected: Mapping[str, Any]) -> dict[str, Any]:
        if not selected.get("selected"):
            return {"available": False, "score": 0.0, "components": {}}
        p = selected.get("pattern") or {}
        r = selected.get("record") or {}
        metrics = meso.get("flight_metrics") or {}
        ctx = meso.get("crp_context") or {}
        components: dict[str, float] = {}

        def rel(name: str, cur: Any, ref: Any, floor: float = 1.0) -> None:
            c, q = safe_float(cur, float("nan")), safe_float(ref, float("nan"))
            if math.isfinite(c) and math.isfinite(q):
                components[name] = abs(c - q) / max(abs(q), floor)

        rel("airspeed_relative", metrics.get("airspeed_m_s"), p.get("airspeed_ref_m_s"))
        cur_clear = safe_float(metrics.get("min_clearance_m"), float("nan"))
        ref_clear = safe_float(p.get("min_clearance_m"), float("nan"))
        if math.isfinite(cur_clear) and math.isfinite(ref_clear):
            components["clearance_deficit_relative"] = max(0.0, ref_clear-cur_clear) / max(abs(ref_clear), 1.0)
        cur_conf = safe_float(metrics.get("network_confidence"), float("nan"))
        ref_conf = safe_float(p.get("pattern_confidence"), safe_float(r.get("record_confidence"), float("nan")))
        if math.isfinite(cur_conf) and math.isfinite(ref_conf):
            components["confidence_deficit"] = max(0.0, ref_conf-cur_conf)

        rel("temperature_relative", ctx.get("temperature_c"), p.get("crp_temperature_mean_c"), 10.0)
        cur_rh, ref_rh = safe_float(ctx.get("relative_humidity_percent"), float("nan")), safe_float(p.get("crp_humidity_mean_pct"), float("nan"))
        if math.isfinite(cur_rh) and math.isfinite(ref_rh):
            components["humidity_fraction_difference"] = abs(cur_rh-ref_rh)/100.0
        rel("wind_speed_relative", ctx.get("wind_speed_ms", ctx.get("wind_speed_3d_m_s")), p.get("crp_wind_speed_mean_ms"))
        cur_dir, ref_dir = safe_float(ctx.get("wind_direction_from_deg"), float("nan")), safe_float(p.get("crp_wind_direction_mean_deg"), float("nan"))
        if math.isfinite(cur_dir) and math.isfinite(ref_dir):
            delta = abs((cur_dir-ref_dir+180.0) % 360.0 - 180.0)
            components["wind_direction_halfturn"] = delta/180.0
        rel("pressure_relative", ctx.get("surface_pressure_pa", ctx.get("pressure_pa")), p.get("crp_pressure_mean_pa"), 1000.0)
        rel("air_density_relative", ctx.get("air_density_kg_m3", ctx.get("air_density")), p.get("crp_air_density_mean_kg_m3"), 0.1)

        if safe_float(r.get("flight_ok"), 1.0) < 0.5:
            components["reference_flight_status_penalty"] = 1.0
        if safe_float(r.get("nav_ok"), 1.0) < 0.5:
            components["reference_navigation_status_penalty"] = 1.0
        score = float(math.sqrt(sum(v*v for v in components.values()) / max(len(components), 1))) if components else 0.0
        return {
            "available": bool(components), "score": score, "components": components,
            "reference_sarg_id": p.get("sarg_id"), "reference_admissible": bool(selected.get("reference_admissible", True)),
            "evidence_type": "simulation/reference", "component_count": len(components),
        }


class CRPHistogram:
    @staticmethod
    def from_events(events: list[Mapping[str, Any]]) -> dict[str, int]:
        c = Counter()
        for e in events:
            crp = (e.get("crp_match") or {}).get("crp_id")
            if crp is not None:
                c[str(crp)] += 1
        return dict(c)
