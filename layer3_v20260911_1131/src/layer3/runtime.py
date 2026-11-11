from __future__ import annotations
from pathlib import Path

import copy
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping

from .boulder_runtime import BoulderRuntimeAdapter
from .config import Layer3Config
from .contracts import SignalContractRegistry
from .engine import Layer3Engine
from .utils import safe_float


@dataclass
class RuntimeRequest:
    reference_time_s: float
    regime_id: int | None
    stage: str = "cruise"
    mesa_operation: int = 5
    mesa_inputs: dict[str, Any] = field(default_factory=lambda: {
        "dt": {"value":0.01,"unit":"s","frame":"SCALAR","semantic":"dt","source":"RuntimeRequest.default"}
    })
    wing_operation: int = 4
    wing_inputs: dict[str, Any] = field(default_factory=lambda: {
        "state_x":{"value":0.0,"unit":"m","frame":"ENU","semantic":"state_x","source":"RuntimeRequest.default"},
        "state_y":{"value":0.0,"unit":"m","frame":"ENU","semantic":"state_y","source":"RuntimeRequest.default"},
        "state_h":{"value":1800.0,"unit":"m","frame":"ENU","semantic":"state_h","source":"RuntimeRequest.default"},
        "air_velocity_x":{"value":13.0,"unit":"m/s","frame":"ENU","semantic":"air_velocity_x","source":"RuntimeRequest.default"},
        "air_velocity_y":{"value":0.5,"unit":"m/s","frame":"ENU","semantic":"air_velocity_y","source":"RuntimeRequest.default"},
        "air_velocity_h":{"value":0.0,"unit":"m/s","frame":"ENU","semantic":"air_velocity_h","source":"RuntimeRequest.default"},
    })
    climate: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    prediction: dict[str, Any] = field(default_factory=dict)
    action: dict[str, Any] = field(default_factory=dict)
    safety_gates: dict[str, bool] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    risks: dict[str, Any] = field(default_factory=dict)
    objectives: dict[str, Any] = field(default_factory=dict)
    mesa_anchor: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RuntimeRequest":
        return cls(**dict(data))


class Layer3FMURuntime:
    """External-FMU runtime with two-phase Layer-3.2 execution and strict TDOC boundaries."""

    def __init__(self, config: Layer3Config, engine: Layer3Engine | None = None):
        self.config = config
        self.engine = engine or Layer3Engine(config)
        self.dependencies = self.engine.dependencies
        self.tdoc = self.engine.tdoc
        for p in (config.twins_root / "python", config.climate_root / "python"):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        from twins_fmi import DomainTwin  # type: ignore
        from estimate_climate import estimate  # type: ignore
        from tddt_layer2 import ClimateDomainTwin  # type: ignore
        self._estimate_climate = estimate
        cfg_yaml = config.twins_root / "config" / "phoenix_drone_v3.yaml"
        self.mesa = DomainTwin(config.twins_root / "fmus" / "MESA_NAV.fmu", config=cfg_yaml, tep_root=config.twins_root / "resources" / "tddt_layer2" / "MESA_NAV")
        self.wing = DomainTwin(config.twins_root / "fmus" / "WingMPET.fmu", config=cfg_yaml, tep_root=config.twins_root / "resources" / "tddt_layer2" / "WingMPET")
        self.climate_twin = ClimateDomainTwin(config.climate_root / "resources" / "tddt_layer2")
        self.contracts = SignalContractRegistry(config, self.tdoc)
        self.boulder = BoulderRuntimeAdapter(config) if bool(config.runtime.get("use_boulder_runtime_sampling", True)) else None

    @staticmethod
    def _packet_dict(packet: Any) -> dict[str, Any]:
        return packet.to_dict() if hasattr(packet, "to_dict") else dict(packet)

    @staticmethod
    def _source_envelope(packet: Mapping[str, Any], signal: str, *, fallback_value: Any = None, fallback_unit: str = "1",
                         fallback_frame: str = "SCALAR", fallback_semantic: str | None = None,
                         source: str = "Layer3-derived") -> dict[str, Any]:
        envs = packet.get("signal_envelopes") or {}
        if isinstance(envs, Mapping) and isinstance(envs.get(signal), Mapping):
            return copy.deepcopy(dict(envs[signal]))
        return {"value": fallback_value, "unit": fallback_unit, "frame": fallback_frame,
                "semantic": fallback_semantic or signal, "uncertainty": None, "source": source,
                "provenance": {"origin": source, "signal": signal, "contract": "Layer3 explicit derived envelope"}}

    @staticmethod
    def _semantic_map(*pairs: tuple[str, str, str]) -> dict[str, dict[str, Any]]:
        return {dst: {"allow_relabel": True,
                      "provenance": {"adapter": phi, "source_signal": src, "target_signal": dst,
                                     "authorized_by": "Layer3 coupling operator"}}
                for dst, src, phi in pairs}

    def invoke_dependency(self, dependency: str, relative_py: str, symbol: str, *args: Any, **kwargs: Any) -> Any:
        return self.dependencies.invoke(dependency, relative_py, symbol, *args, **kwargs)

    def capability_report(self) -> dict[str, Any]:
        return self.dependencies.capability_report()

    def _normalize_inputs(self, twin: str, domain_twin: Any, operation: int, inputs: Mapping[str, Any], context: Mapping[str, Any], *,
                          trusted_internal: bool = False, semantic_mapping: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        norm = self.contracts.normalize_operation_inputs(domain_twin, int(operation), inputs, context=dict(context or {}),
                                                         trusted_internal=trusted_internal, semantic_mapping=semantic_mapping)
        return norm.values, {"twin": twin, "operation": int(operation), "records": norm.records,
                             "envelopes": norm.envelopes, "trusted_internal": bool(trusted_internal)}

    def _evaluate_domain_packet(self, twin_id: str, domain_twin: Any, operation: int, *, context: Mapping[str, Any],
                                inputs: Mapping[str, Any], context_id: str | None = None, stage_id: str | None = None) -> dict[str, Any]:
        kw: dict[str, Any] = {"context": dict(context or {}), "inputs": dict(inputs or {})}
        if context_id is not None: kw["context_id"] = context_id
        if stage_id is not None: kw["stage_id"] = stage_id
        pd = self._packet_dict(domain_twin.evaluate_packet(int(operation), **kw))
        # Isolated full-fidelity verification sessions restart the FMI local clock at
        # zero.  TDOC may provide an explicit trusted reference-time override so
        # Layer 3 freshness is evaluated in the mission epoch rather than against
        # the isolated FMU's local epoch.  This is simulation/verification time
        # alignment only; normal ONLINE operational packets never set this key.
        ts_override = (context or {}).get("tdoc_full_fidelity_reference_time_s")
        if ts_override is not None:
            try:
                ts_override = float(ts_override)
                if math.isfinite(ts_override):
                    local_ts = pd.get("timestamp")
                    pd["timestamp"] = ts_override
                    prov = dict(pd.get("provenance") or {})
                    prov["tdoc_isolated_fmi_time_alignment"] = {
                        "schema": "TDOC-FF-IsolatedFMITimeAlignment-v1",
                        "simulation_only": True,
                        "not_field_evidence": True,
                        "local_fmi_timestamp_s": local_ts,
                        "mission_reference_time_s": ts_override,
                    }
                    pd["provenance"] = prov
            except Exception:
                pass
        pd["signal_envelopes"] = self.contracts.output_envelopes(domain_twin, int(operation), pd, twin_id=twin_id)
        return pd

    def _climate_args(self, request: RuntimeRequest, climate_override: Mapping[str, Any] | None = None) -> SimpleNamespace:
        c, _ = self.contracts.normalize_climate_request(request.climate,
            context={**request.context, "reference_time_s": request.reference_time_s}, trusted_internal=False)
        if climate_override:
            ov, _ = self.contracts.normalize_climate_request(climate_override,
                context={**request.context, "reference_time_s": request.reference_time_s}, trusted_internal=True)
            c.update(ov)
        if not c:
            raise ValueError("RuntimeRequest.climate is required for direct MicroMetTwin execution")
        projection=request.context.get('native_climate_time_projection')
        if projection is not None:
            from tddt_tdoc.native_time import validate_native_time
            bound=validate_native_time(projection)
            if Path(c.get('input') or self._active_climate_forcing()).resolve()!=Path(bound['forcing_file']).resolve():
                raise ValueError('Layer3 climate input differs from native accepted forcing')
        agl = c.get("elevation_ground_level", c.get("altitude_agl_m")); msl = c.get("elevation_sea_level", c.get("altitude_msl_m"))
        if agl is None and msl is None:
            raise ValueError("climate requires elevation_ground_level/altitude_agl_m or elevation_sea_level/altitude_msl_m")
        time_text = str(c["time"])
        try:
            dt = datetime.fromisoformat(time_text.replace("Z", "+00:00"))
            if dt.tzinfo is not None and not request.context.get('native_climate_time_projection'):
                time_text = dt.replace(tzinfo=None).isoformat(timespec="seconds")
        except Exception:
            pass
        return SimpleNamespace(
            fmu=str(c.get("fmu") or (self.config.climate_root / "fmu" / "MicroMetTwin.fmu")),
            input=str(c.get("input") or self._active_climate_forcing()),
            time=time_text, latitude=float(c["latitude"]), longitude=float(c["longitude"]),
            elevation_sea_level=None if msl is None else float(msl), elevation_ground_level=None if agl is None else float(agl),
            dem=str(c.get("dem") or (self.config.map_root / "terrain_obstacles.tif")), dem_dir=c.get("dem_dir"),
            terrain_policy=str(c.get("terrain_policy", "require-dem")), curvature_radius_m=float(c.get("curvature_radius_m", 1000.)),
            ground_elevation_m=c.get("ground_elevation_m"), slope_deg=float(c.get("slope_deg", 0.)), aspect_deg=float(c.get("aspect_deg", 0.)),
            curvature=float(c.get("curvature", 0.)), barnes_filter_km=float(c.get("barnes_filter_km", 20.)),
            wind_3dw_dx_m=float(c.get("wind_3dw_dx_m", 100.)), wind_3dw_dy_m=float(c.get("wind_3dw_dy_m", 100.)),
            wind_3dw_dz_m=float(c.get("wind_3dw_dz_m", 10.)), wind_profile_power=float(c.get("wind_profile_power", .143)),
            with_metadata=bool(c.get("with_metadata", (self.config.climate_root / "docs" / "fmi_semantic_schema.json").exists())),
            tddt_layer2=False, context_id=str(c.get("context_id", request.context.get("context_id", "runtime"))),
            stage_id=str(request.stage), operation_code=str(c.get("operation_code", "climate_estimate")), evidence_log=None)

    def _active_climate_forcing(self) -> Path:
        import os, json
        raw=os.environ.get("TDDT_CLIMATE_FORCING_FILE", "").strip()
        if raw:
            p=Path(raw).expanduser().resolve()
            if p.is_file(): return p
        cache=os.environ.get("TDDT_CLIMATE_INTERNET_DATASETS_ROOT", "").strip()
        if cache:
            ds=Path(cache).expanduser().resolve(); mp=ds/"tdoc_active_forcing.json"
            if mp.is_file():
                try:
                    r=str((json.loads(mp.read_text()) or {}).get("file") or "").strip(); q=Path(r); p=(q if q.is_absolute() else ds/q).resolve()
                    if p.is_file(): return p
                except Exception: pass
            q=ds/"openmeteo_24h.csv"
            if q.is_file(): return q
        p=(self.config.climate_root/"climate_dataset"/"openmeteo_24h.csv").resolve()
        if not p.is_file(): raise RuntimeError(f"active climate forcing unavailable: {p}")
        return p

    @staticmethod
    def _hour(text: str) -> int:
        try: return datetime.fromisoformat(str(text).replace("Z", "+00:00")).hour
        except Exception: return 12

    def _boulder_context(self, args: SimpleNamespace) -> dict[str, Any]:
        return {} if self.boulder is None else self.boulder.sample(args.longitude, args.latitude, hour=self._hour(args.time))

    @staticmethod
    def _merge_packet(base: Mapping[str, Any], update: Mapping[str, Any], label: str) -> dict[str, Any]:
        out = copy.deepcopy(dict(base)); upd = dict(update); out["twin_id"] = str(upd.get("twin_id") or out.get("twin_id"))
        state = dict(out.get("state") or {}); state.update(dict(upd.get("state") or {})); out["state"] = state
        for fld in ("uncertainty", "quality", "validity", "reliability", "context"):
            a = dict(out.get(fld) or {}) if isinstance(out.get(fld), Mapping) else {}; b = upd.get(fld)
            if isinstance(b, Mapping): a.update(dict(b)); out[fld] = a
            elif b is not None: out[fld] = b
        if upd.get("timestamp") is not None: out["timestamp"] = upd["timestamp"]
        se = dict(out.get("signal_envelopes") or {}); use = upd.get("signal_envelopes")
        if isinstance(use, Mapping): se.update(dict(use)); out["signal_envelopes"] = se
        prov = dict(out.get("provenance") or {}); prov.setdefault("layer3_coupled_updates", [])
        prov["layer3_coupled_updates"] = list(prov["layer3_coupled_updates"]) + [label]
        prov.setdefault("coupled_packet_sources", []); prov["coupled_packet_sources"] = list(prov["coupled_packet_sources"]) + [dict(upd.get("provenance") or {})]
        out["provenance"] = prov
        return out

    @staticmethod
    def _tep_match_record(match: Any) -> dict[str, Any] | str:
        """Serialize diagnostic TEP association without non-finite JSON values.

        An unmatched nearest-neighbour query may legitimately report an infinite
        distance internally.  Across a TDOC boundary that means "no finite
        distance", represented as null; Infinity is not a valid contract value and
        would make Layer4's canonical hash fail before any safety gate is evaluated.
        """
        raw = dict(match.__dict__) if hasattr(match, "__dict__") else match
        if not isinstance(raw, Mapping):
            return str(raw)
        out = dict(raw)
        for key, value in list(out.items()):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    if not math.isfinite(float(value)):
                        out[key] = None
                except (TypeError, ValueError):
                    pass
        return out

    @staticmethod
    def _delta(base: Mapping[str, Any], updated: Mapping[str, Any]) -> dict[str, Any]:
        a = dict(base.get("state") or {}); b = dict(updated.get("state") or {}); common = []; sq = 0.0
        for k in sorted(set(a) & set(b)):
            if isinstance(a[k], (int, float)) and isinstance(b[k], (int, float)) and math.isfinite(float(a[k])) and math.isfinite(float(b[k])):
                d = float(b[k]) - float(a[k]); common.append({"name": k, "delta": d}); sq += d*d
        return {"delta_norm": math.sqrt(sq), "changed_outputs": [x for x in common if abs(x["delta"]) > 1e-12], "common_numeric_count": len(common)}

    @staticmethod
    def _edge_plan(plan: Mapping[str, Any], key: str) -> dict[str, Any]:
        g = ((plan.get("graph") or {}).get("edges") or {}).get(key, {})
        eff = ((plan.get("coupling_effects") or {}).get("edges") or {}).get(key, {})
        det = ((plan.get("coupling_details") or {}).get("edges") or {}).get(key, {})
        return {"graph": g, "effect": eff, "detail": det}

    def _allowed(self, plan: Mapping[str, Any], key: str, *kinds: str, require_all: bool = True) -> tuple[bool, float, dict[str, Any]]:
        p = self._edge_plan(plan, key); g = p["graph"]; eff = p["effect"]; det = p["detail"]; by = dict(det.get("K_by_type") or {})
        threshold = float(det.get("execution_threshold", 1e-9)); kvals = [float(by.get(k, 0.0)) for k in kinds] if kinds else [float((plan.get("couplings") or {}).get(key, 0.0))]
        typed_ready = all(v > threshold for v in kvals) if require_all else any(v > threshold for v in kvals)
        kval = (min(kvals) if require_all else max(kvals)) if kvals else 0.0
        allowed = bool(g.get("active_effective")) and bool(eff.get("delay_ready", int(g.get("delay_ticks", 0)) == 0)) and typed_ready
        return allowed, kval, {**p, "required_types": list(kinds), "required_all": bool(require_all), "effective_execution_K": kval}

    @staticmethod
    def _delayed_raw(plan: Mapping[str, Any], key: str, current: Mapping[str, Any]) -> dict[str, Any]:
        eff = Layer3FMURuntime._edge_plan(plan, key)["effect"]; types = eff.get("types") or {}
        for kind in ("Data", "State"):
            rec = types.get(kind) or {}
            if isinstance(rec.get("raw_payload"), Mapping): return dict(rec["raw_payload"])
            if isinstance(rec.get("raw_state"), Mapping): return dict(rec["raw_state"])
        return dict(current.get("state") or {})

    def _normalize_context_vector(self, item: Any, *, target_unit: str, target_frame: str, target_semantic: str,
                                  context: Mapping[str, Any]) -> tuple[list[float], dict[str, Any]]:
        if not isinstance(item, Mapping) or "value" not in item:
            if bool(self.config.runtime.get("require_contract_normalization", True)):
                raise ValueError(f"context {target_semantic} requires a TDOC envelope {{value,unit,frame,semantic}}")
            env = {"value": item, "unit": target_unit, "frame": target_frame, "semantic": target_semantic, "source": "legacy_context"}
        else:
            env = self.tdoc.validate_envelope(item, external=True)
        raw = env.get("value")
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise ValueError(f"{target_semantic} must contain a 3-vector")
        su = str(env.get("unit") or target_unit); vec = [float(v) for v in raw]; cov = env.get("covariance") if isinstance(env.get("covariance"), (list, tuple)) else None
        if su != target_unit:
            f = lambda x: tuple(float(self.tdoc.convert(v, su, target_unit)) for v in x)
            if cov is not None: cov = [list(r) for r in self.tdoc.av.propagate_covariance_numeric(f, vec, cov)]
            vec = list(f(vec))
        sem = self.tdoc.semantic_check(str(env.get("semantic")), target_semantic, provenance=env.get("provenance"), allow_relabel=False)
        if self.config.alignment.strict_semantics and not sem.get("valid"):
            raise ValueError(f"context semantic mismatch {env.get('semantic')}->{target_semantic}")
        out, cov_out, rec = self.tdoc.transform_vector(vec, str(env.get("frame")), target_frame, covariance=cov, context=context)
        return out, {"unit": target_unit, "frame": target_frame, "semantic": target_semantic, "covariance": cov_out, "tdoc": rec}

    def _origin(self, request: RuntimeRequest) -> dict[str, float] | None:
        raw = request.context.get("geodetic_origin") or request.context.get("origin_wgs84") or request.context.get("mesa_geodetic_origin")
        if not isinstance(raw, Mapping): return None
        def field(*names: str, target_unit: str, target_frame: str, target_semantic: str):
            item = next((raw[n] for n in names if n in raw), None)
            if item is None: return None
            if isinstance(item, Mapping) and "value" in item:
                val, _ = self.tdoc.normalize_envelope(item, target_unit=target_unit, target_frame=target_frame,
                    target_semantic=target_semantic, context=request.context, external=True)
                return float(val)
            if bool(self.config.runtime.get("require_contract_normalization", True)):
                raise ValueError(f"geodetic_origin.{target_semantic} requires a TDOC envelope")
            return float(item)
        lat = field("latitude", "lat", target_unit="deg", target_frame="WGS84", target_semantic="latitude")
        lon = field("longitude", "lon", target_unit="deg", target_frame="WGS84", target_semantic="longitude")
        h = field("altitude_msl_m", "h_m", "altitude", target_unit="m", target_frame="MSL", target_semantic="altitude_msl_m")
        if lat is None or lon is None or h is None: return None
        return {"latitude": lat, "longitude": lon, "altitude_msl_m": h}

    def _mesa_query(self, request: RuntimeRequest, mesa_packet: Mapping[str, Any], K: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        origin = self._origin(request); ms = dict(mesa_packet.get("state") or {})
        if origin is None: return None, {"geodetic_origin_available": False, "geodetic_origin_inferred": False}
        if not all(k in ms for k in ("position_n", "position_e", "position_d")):
            return None, {"geodetic_origin_available": True, "reason": "MESA position NED unavailable"}
        unc = mesa_packet.get("uncertainty") or {}; std = unc.get("std") or unc.get("by_output") or {}
        cov_ned = None
        if isinstance(std, Mapping) and all(k in std for k in ("position_n", "position_e", "position_d")):
            sig = [safe_float(std[k], 0.0) for k in ("position_n", "position_e", "position_d")]; cov_ned = [[sig[i]*sig[i] if i == j else 0.0 for j in range(3)] for i in range(3)]
        wgs, rec = self.tdoc.local_ned_to_wgs84([ms["position_n"], ms["position_e"], ms["position_d"]],
            origin_lat_deg=origin["latitude"], origin_lon_deg=origin["longitude"], origin_h_m=origin["altitude_msl_m"], covariance_ned=cov_ned)
        base, _ = self.contracts.normalize_climate_request(request.climate,
            context={**request.context, "reference_time_s": request.reference_time_s}, trusted_internal=False)
        alpha = max(0.0, min(1.0, float(K)))
        lat0 = float(base.get("latitude", wgs[0])); lon0 = float(base.get("longitude", wgs[1])); msl0 = float(base.get("elevation_sea_level", base.get("altitude_msl_m", wgs[2])))
        lat = lat0 + alpha * (wgs[0] - lat0); lon = lon0 + alpha * (wgs[1] - lon0); msl = msl0 + alpha * (wgs[2] - msl0)
        time_text = str(base.get("time"))
        projection=request.context.get('native_climate_time_projection')
        if projection is not None:
            from tddt_tdoc.native_time import validate_native_time
            time_text=validate_native_time(projection)['dataset_timestamp']
        else:
            try:
                common_text, _ = self.tdoc.elapsed_to_common_time(float(mesa_packet.get("timestamp", request.reference_time_s)), epoch=self.config.time.mission_epoch_utc)
                dt = datetime.fromisoformat(str(common_text).replace("Z", "+00:00")); time_text = dt.replace(tzinfo=None).isoformat(timespec="seconds") if dt.tzinfo is not None else dt.isoformat(timespec="seconds")
            except Exception:
                pass
        q = {**base, "latitude": lat, "longitude": lon, "elevation_sea_level": msl, "time": time_text}
        return q, {"geodetic_origin_available": True, "geodetic_origin_inferred": False, "tdoc": rec,
                   "mesa_wgs84": [wgs[0], wgs[1], wgs[2]], "K_data": alpha,
                   "climate_contract_time_binding": "native_accepted_dataset_clock" if projection is not None else "mission_epoch_naive_iso"}

    def collect_base_packets(self, request: RuntimeRequest) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
        ctx = {**request.context, "stage": request.stage, "regime_id": request.regime_id, "reference_time_s": request.reference_time_s}
        mi, mnorm = self._normalize_inputs("MESA_NAV", self.mesa, request.mesa_operation, request.mesa_inputs, ctx, trusted_internal=False)
        wi, wnorm = self._normalize_inputs("WingMPET", self.wing, request.wing_operation, request.wing_inputs, ctx, trusted_internal=False)
        mesa = self._evaluate_domain_packet("MESA_NAV", self.mesa, int(request.mesa_operation), context=ctx,
            context_id=str(request.context.get("context_id", "runtime")), stage_id=str(request.stage), inputs=mi)
        wing = self._evaluate_domain_packet("WingMPET", self.wing, int(request.wing_operation), context=ctx,
            context_id=str(request.context.get("context_id", "runtime")), stage_id=str(request.stage), inputs=wi)
        args = self._climate_args(request); boulder = self._boulder_context(args); cres = self._estimate_climate(args); ctep = self.climate_twin.match_tep(cres)
        climate = dict(self.climate_twin.packet_from_estimate(cres, context_id=str(request.context.get("context_id", "runtime")),
            stage_id=str(request.stage), operation_code="climate_estimate", data_age_s=0.0))
        if request.context.get('native_climate_time_projection') is not None:
            from tddt_tdoc.native_time import stamp_climate_time
            climate=stamp_climate_time(climate,request.context)
        climate["signal_envelopes"] = self.contracts.climate_output_envelopes(climate)
        creq = cres.get("request") or {}; rctx = dict(ctx)
        rctx.update({"ground_elevation_m": creq.get("ground_elevation_m"), "terrain_elevation_m": creq.get("ground_elevation_m"),
                     "terrain_slope_deg": creq.get("slope_deg"), "terrain_aspect_deg": creq.get("aspect_deg"),
                     "terrain_curvature": creq.get("curvature_scaled"), "latitude": args.latitude, "longitude": args.longitude,
                     "altitude_agl_m": creq.get("altitude_agl_m"), "altitude_msl_m": creq.get("altitude_msl_m")})
        rctx.update(boulder.get("context") or {}); rctx.update({k:v for k,v in request.context.items() if v is not None})
        meta = {"input_normalization": {"MESA_NAV": mnorm, "WingMPET": wnorm}, "normalized_wing_inputs": wi,
                "boulder": boulder, "climate_tep_match": self._tep_match_record(ctep)}
        return {"MESA_NAV": mesa, "WingMPET": wing, "MicroMetTwin": climate}, rctx, meta

    def _external_anchor_value(self, operation: int, name: str, spec: Mapping[str, Any], default: Any, context: Mapping[str, Any]) -> Any:
        if name not in spec: return default
        vals, _ = self._normalize_inputs("MESA_NAV", self.mesa, int(operation), {name: spec[name]}, context, trusted_internal=False)
        return vals[name]

    def _evaluate_environmental_anchor_candidates(self, request: RuntimeRequest, climate_packet: Mapping[str, Any], context: Mapping[str, Any], *, K: float = 1.0) -> dict[str, Any]:
        spec, state = dict(request.mesa_anchor or {}), dict(climate_packet.get("state") or {}); unc = climate_packet.get("uncertainty") or {}; stdmap = unc.get("std") or unc.get("by_output") or {}; out: dict[str, Any] = {}
        wind = spec.get("wind")
        if isinstance(wind, Mapping) and bool(wind.get("enabled", True)):
            if "terrain_flow_anchor_residual" not in wind:
                out["wind"] = {"offered": False, "reason": "terrain_flow_anchor_residual_required"}
            else:
                if "measurement_variance" in wind: raw_var = float(self._external_anchor_value(11, "measurement_variance", wind, 1.0, context))
                else:
                    su, sv = safe_float(stdmap.get("wind_u_3dw"), 1.0), safe_float(stdmap.get("wind_v_3dw"), 1.0); raw_var = max(1e-6, .5*(su*su + sv*sv))
                base_var = max(raw_var, 1e-6) / (max(float(K), 1e-6)**2)
                for k in ("map_variance", "interpolation_variance", "frame_variance", "time_variance", "model_variance"):
                    if k in wind:
                        _, env = self.tdoc.normalize_envelope(wind[k], target_unit="1", target_frame="SCALAR", target_semantic=k,
                            context=context, external=True, allow_semantic_relabel=True,
                            semantic_provenance={"adapter":"Layer3 anchor uncertainty budget", "signal":k})
                        base_var += max(0.0, float(env["value"]))
                residual = self._external_anchor_value(11, "terrain_flow_anchor_residual", wind, None, context)
                jac_n = self._external_anchor_value(11, "jacobian_n", wind, 0.0, context); jac_e = self._external_anchor_value(11, "jacobian_e", wind, 0.0, context); nis_gate = self._external_anchor_value(11, "nis_gate", wind, 9.21, context)
                vals, norm = self._normalize_inputs("MESA_NAV", self.mesa, 11,
                    {"terrain_flow_anchor_residual": float(residual), "jacobian_n": float(jac_n), "jacobian_e": float(jac_e),
                     "measurement_variance": float(base_var), "nis_gate": float(nis_gate)}, context, trusted_internal=True)
                pd = self._evaluate_domain_packet("MESA_NAV", self.mesa, 11, context={"anchor_source":"MicroMetTwin", "coupling_strength":float(K)}, inputs=vals)
                accepted = safe_float((pd.get("state") or {}).get("terrain_flow_update_valid"), 0.0) >= .5
                out["wind"] = {"offered": True, "accepted_by_mesa": accepted, "packet": pd, "normalization": norm,
                               "state_injected_by_layer3": False, "K": float(K)}
        pressure = spec.get("pressure")
        if isinstance(pressure, Mapping) and bool(pressure.get("enabled", True)):
            p_abs = state.get("surface_pressure_pa")
            if p_abs is None:
                out["pressure"] = {"offered": False, "reason": "surface_pressure_missing"}
            else:
                lp_item = pressure.get("log_pressure_prior")
                if lp_item is None:
                    lp = float(self.tdoc.pressure_to_log_ratio(float(p_abs), 1.0)); lp_source = "derived_from_MicroMet_surface_pressure_via_TDOC_p_ref_1Pa"
                else:
                    lp_vals, _ = self._normalize_inputs("MESA_NAV", self.mesa, 10, {"log_pressure_prior": lp_item}, context, trusted_internal=False); lp = float(lp_vals["log_pressure_prior"]); lp_source = "external_explicit_contract"
                lsh_item = pressure.get("log_scale_height")
                if lsh_item is None:
                    lsh = math.log(8434.5); lsh_source = "default_ln_8434.5m"
                else:
                    lsh_vals, _ = self._normalize_inputs("MESA_NAV", self.mesa, 10, {"log_scale_height": lsh_item}, context, trusted_internal=False); lsh = float(lsh_vals["log_scale_height"]); lsh_source = "external_explicit_contract"
                pts_item = pressure.get("pressure_time_scale")
                if pts_item is None:
                    pts = 30.0; pts_source = "phoenix_navigation_pressure_time_scale_s_default"
                else:
                    pts_vals, _ = self._normalize_inputs("MESA_NAV", self.mesa, 10, {"pressure_time_scale": pts_item}, context, trusted_internal=False); pts = float(pts_vals["pressure_time_scale"]); pts_source = "external_explicit_contract"
                sp = safe_float(stdmap.get("surface_pressure_pa"), 50.0)
                pvar = float(self._external_anchor_value(10, "pressure_variance", pressure, sp*sp, context)) if "pressure_variance" in pressure else sp*sp
                var = max(pvar, 1e-6) / (max(float(K), 1e-6)**2)
                pgn = self._external_anchor_value(10, "pressure_grad_n", pressure, 0.0, context); pge = self._external_anchor_value(10, "pressure_grad_e", pressure, 0.0, context); pnis = self._external_anchor_value(10, "nis_gate", pressure, 9.21, context)
                pin = {"absolute_pressure": float(p_abs), "log_pressure_prior": lp, "log_scale_height": lsh,
                       "pressure_grad_n": float(pgn), "pressure_grad_e": float(pge), "pressure_variance": max(float(var), 1e-6),
                       "nis_gate": float(pnis), "pressure_time_scale": pts}
                vals, norm = self._normalize_inputs("MESA_NAV", self.mesa, 10, pin, context, trusted_internal=True)
                pd = self._evaluate_domain_packet("MESA_NAV", self.mesa, 10, context={"anchor_source":"MicroMetTwin", "coupling_strength":float(K)}, inputs=vals)
                accepted = safe_float((pd.get("state") or {}).get("pressure_anchor_valid"), 0.0) >= .5
                out["pressure"] = {"offered": True, "accepted_by_mesa": accepted, "packet": pd, "normalization": norm,
                    "semantic_adapter":"TDOC pressure_to_log_ratio(p_ref=1Pa)", "contract_override":"resources/tdoc_contract_overrides.json",
                    "log_pressure_source":lp_source, "log_scale_height_source":lsh_source, "pressure_time_scale_source":pts_source,
                    "state_injected_by_layer3":False, "K":float(K)}
        return out

    def execute_couplings(self, request: RuntimeRequest, base_packets: Mapping[str, Mapping[str, Any]], context: Mapping[str, Any],
                          meta: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
        packets = {k: copy.deepcopy(dict(v)) for k,v in base_packets.items()}
        out = {"enabled": bool(self.config.runtime.get("enable_runtime_couplings", True)), "state_overwrite": False, "operations": {},
               "execution_order": [], "counterfactuals": {}, "input_normalization": dict(meta.get("input_normalization") or {}),
               "boulder": meta.get("boulder"), "climate_tep_match": meta.get("climate_tep_match")}
        anchors: dict[str, Any] = {}; rctx = dict(context)
        if not out["enabled"]: return packets, rctx, anchors, out

        ok, K, p = self._allowed(plan, "MESA_NAV->MicroMetTwin", "Data")
        if ok:
            src_raw = self._delayed_raw(plan, "MESA_NAV->MicroMetTwin", packets["MESA_NAV"]); mesa_src = {**packets["MESA_NAV"], "state": src_raw}
            query, qprov = self._mesa_query(request, mesa_src, K)
            if query is not None:
                args = self._climate_args(request, query); boulder = self._boulder_context(args); cres = self._estimate_climate(args); ctep = self.climate_twin.match_tep(cres)
                newc = dict(self.climate_twin.packet_from_estimate(cres, context_id=str(request.context.get("context_id","runtime")), stage_id=str(request.stage), operation_code="mesa_coupled_climate_query", data_age_s=0.0))
                if request.context.get('native_climate_time_projection') is not None:
                    from tddt_tdoc.native_time import stamp_climate_time
                    newc=stamp_climate_time(newc,request.context)
                newc["signal_envelopes"] = self.contracts.climate_output_envelopes(newc)
                delta = self._delta(packets["MicroMetTwin"], newc); packets["MicroMetTwin"] = self._merge_packet(packets["MicroMetTwin"], newc, "MESA_NAV->MicroMetTwin:query")
                out["operations"]["MESA_NAV->MicroMetTwin:query"] = {"executed":True,"a_ij":1,"K_by_type":p["detail"].get("K_by_type"),"delay_ticks":p["graph"].get("delay_ticks",0),"safety_gate":p["graph"].get("safety_gate",1),"query":query,"provenance":qprov,"packet":newc,"geodetic_origin_inferred":False}
                out["counterfactuals"]["MESA_NAV->MicroMetTwin"] = {**delta,"actual_fmu_evaluation":True,"destination":"MicroMetTwin"}; out["execution_order"].append("MESA_NAV->MicroMetTwin:query"); out["boulder"] = boulder; out["climate_tep_match"] = self._tep_match_record(ctep)
                creq = cres.get("request") or {}; rctx.update({"latitude":args.latitude,"longitude":args.longitude,"altitude_agl_m":creq.get("altitude_agl_m"),"altitude_msl_m":creq.get("altitude_msl_m")}); rctx.update(boulder.get("context") or {})
            else:
                out["operations"]["MESA_NAV->MicroMetTwin:query"] = {"executed":False,"reason":"geodetic_origin_or_position_unavailable","K":K,**qprov,"geodetic_origin_inferred":False}
        else:
            out["operations"]["MESA_NAV->MicroMetTwin:query"] = {"executed":False,"reason":"edge_inactive_delay_or_K","K":K,"edge":p["graph"],"geodetic_origin_inferred":False}

        okcw, Kcw, pcw = self._allowed(plan, "MicroMetTwin->WingMPET", "Data", "Model"); cs = dict(packets["MicroMetTwin"].get("state") or {}); ms = dict(packets["MESA_NAV"].get("state") or {}); ws = dict(packets["WingMPET"].get("state") or {}); wing_direct = copy.deepcopy(packets["WingMPET"])
        if okcw and all(k in cs for k in ("temperature_local","surface_pressure_pa","air_density","relative_humidity_local")):
            altitude_value = rctx.get("altitude_msl_m"); altitude_value = rctx.get("terrain_elevation_m") if altitude_value is None else altitude_value; altitude_value = 0.0 if altitude_value is None else altitude_value
            op5 = {
                "altitude":{"value":float(altitude_value),"unit":"m","frame":"SCALAR","semantic":"altitude","source":"Layer3 resolved altitude","provenance":{"origin":"runtime_context"}},
                "external_temperature":self._source_envelope(packets["MicroMetTwin"],"temperature_local",fallback_value=cs["temperature_local"],fallback_unit="degC",fallback_semantic="temperature_local",source="MicroMetTwin"),
                "external_pressure":self._source_envelope(packets["MicroMetTwin"],"surface_pressure_pa",fallback_value=cs["surface_pressure_pa"],fallback_unit="Pa",fallback_semantic="surface_pressure_pa",source="MicroMetTwin"),
                "external_density":self._source_envelope(packets["MicroMetTwin"],"air_density",fallback_value=cs["air_density"],fallback_unit="kg/m3",fallback_semantic="air_density",source="MicroMetTwin"),
                "relative_humidity":self._source_envelope(packets["MicroMetTwin"],"relative_humidity_local",fallback_value=cs["relative_humidity_local"],fallback_unit="%",fallback_semantic="relative_humidity_local",source="MicroMetTwin"),
                "airspeed":self._source_envelope(packets["WingMPET"],"airspeed_norm",fallback_value=ws.get("airspeed_norm",0.),fallback_unit="m/s",fallback_semantic="airspeed_norm",source="WingMPET")}
            smap = self._semantic_map(("external_temperature","temperature_local","Phi_Climate_Data_to_Wing_temperature"),("external_pressure","surface_pressure_pa","Phi_Climate_Data_to_Wing_pressure"),("external_density","air_density","Phi_Climate_Data_to_Wing_density"),("relative_humidity","relative_humidity_local","Phi_Climate_Data_to_Wing_humidity"),("airspeed","airspeed_norm","Phi_Wing_State_to_Wing_airdata"))
            vals, rec = self._normalize_inputs("WingMPET", self.wing, 5, op5, rctx, trusted_internal=True, semantic_mapping=smap)
            pd = self._evaluate_domain_packet("WingMPET", self.wing, 5, context={"coupling":"Climate->WingMPET","K":Kcw}, inputs=vals); packets["WingMPET"] = self._merge_packet(packets["WingMPET"], pd, "Climate->WingMPET:op5")
            out["operations"]["Climate->WingMPET:op5"] = {"executed":True,"a_ij":1,"K":Kcw,"K_by_type":pcw["detail"].get("K_by_type"),"normalization":rec,"packet":pd}; out["execution_order"].append("Climate->WingMPET:op5")

        okmw, Kmw, pmw = self._allowed(plan, "MESA_NAV->WingMPET", "State")
        if okcw and okmw and all(k in cs for k in ("wind_u_3dw","wind_v_3dw","diagnostic_vertical_wind")) and all(k in ms for k in ("velocity_n","velocity_e","velocity_d")):
            names = (("11","du_dx"),("12","du_dy"),("13","du_dz"),("21","dv_dx"),("22","dv_dy"),("23","dv_dz"),("31","dw_dx"),("32","dw_dy"),("33","dw_dz"))
            op6 = {
                "wind_x":self._source_envelope(packets["MicroMetTwin"],"wind_u_3dw",fallback_value=cs["wind_u_3dw"],fallback_unit="m/s",fallback_frame="ENU",source="MicroMetTwin"),
                "wind_y":self._source_envelope(packets["MicroMetTwin"],"wind_v_3dw",fallback_value=cs["wind_v_3dw"],fallback_unit="m/s",fallback_frame="ENU",source="MicroMetTwin"),
                "wind_h":self._source_envelope(packets["MicroMetTwin"],"diagnostic_vertical_wind",fallback_value=cs["diagnostic_vertical_wind"],fallback_unit="m/s",fallback_frame="ENU",source="MicroMetTwin"),
                "ground_velocity_x":self._source_envelope(packets["MESA_NAV"],"velocity_n",fallback_value=ms["velocity_n"],fallback_unit="m/s",fallback_frame="NED",source="MESA_NAV"),
                "ground_velocity_y":self._source_envelope(packets["MESA_NAV"],"velocity_e",fallback_value=ms["velocity_e"],fallback_unit="m/s",fallback_frame="NED",source="MESA_NAV"),
                "ground_velocity_h":self._source_envelope(packets["MESA_NAV"],"velocity_d",fallback_value=ms["velocity_d"],fallback_unit="m/s",fallback_frame="NED",source="MESA_NAV"),
                "wind_time_derivative_x":self._source_envelope(packets["MicroMetTwin"],"du_dt",fallback_value=cs.get("du_dt",0.),fallback_unit="m/s2",fallback_frame="ENU",fallback_semantic="du_dt",source="MicroMetTwin/Layer3-zero-default"),
                "wind_time_derivative_y":self._source_envelope(packets["MicroMetTwin"],"dv_dt",fallback_value=cs.get("dv_dt",0.),fallback_unit="m/s2",fallback_frame="ENU",fallback_semantic="dv_dt",source="MicroMetTwin/Layer3-zero-default"),
                "wind_time_derivative_h":self._source_envelope(packets["MicroMetTwin"],"dw_dt",fallback_value=cs.get("dw_dt",0.),fallback_unit="m/s2",fallback_frame="ENU",fallback_semantic="dw_dt",source="MicroMetTwin/Layer3-zero-default")}
            for ij, src in names:
                op6[f"wind_jacobian_{ij}"] = self._source_envelope(packets["MicroMetTwin"],src,fallback_value=cs.get(src,0.),fallback_unit="1/s",fallback_frame="SCALAR",fallback_semantic=src,source="MicroMetTwin/Layer3-zero-default")
            smap: dict[str,dict[str,Any]] = {}
            for dst, src in (("wind_x","wind_u_3dw"),("wind_y","wind_v_3dw"),("wind_h","diagnostic_vertical_wind"),("ground_velocity_x","velocity_n"),("ground_velocity_y","velocity_e"),("ground_velocity_h","velocity_d"),("wind_time_derivative_x","du_dt"),("wind_time_derivative_y","dv_dt"),("wind_time_derivative_h","dw_dt")):
                smap.update(self._semantic_map((dst,src,"Phi_State_Data_to_Wing_wind_mechanics")))
            for ij, src in names: smap.update(self._semantic_map((f"wind_jacobian_{ij}",src,"Phi_Climate_Model_to_Wing_wind_gradient")))
            vals, rec = self._normalize_inputs("WingMPET", self.wing, 6, op6, rctx, trusted_internal=True, semantic_mapping=smap); keff = min(Kcw,Kmw)
            pd = self._evaluate_domain_packet("WingMPET", self.wing, 6, context={"coupling":"Climate+MESA->WingMPET","K":keff}, inputs=vals); packets["WingMPET"] = self._merge_packet(packets["WingMPET"], pd, "Climate+MESA->WingMPET:op6")
            out["operations"]["Climate+MESA->WingMPET:op6"] = {"executed":True,"a_ij":1,"K":keff,"K_climate_by_type":pcw["detail"].get("K_by_type"),"K_mesa_by_type":pmw["detail"].get("K_by_type"),"delay_ticks":max(int(pcw["graph"].get("delay_ticks",0)),int(pmw["graph"].get("delay_ticks",0))),"safety_gate":int(bool(pcw["graph"].get("safety_gate",1)) and bool(pmw["graph"].get("safety_gate",1))),"normalization":rec,"packet":pd}; out["execution_order"].append("Climate+MESA->WingMPET:op6")

        target_item = request.context.get("target_enu_m"); target = None; target_rec = None
        if target_item is not None:
            target, target_rec = self._normalize_context_vector(target_item, target_unit="m", target_frame="ENU", target_semantic="target_position", context=rctx)
        if okmw and isinstance(target,(list,tuple)) and len(target)==3 and all(k in ms for k in ("position_n","position_e","position_d")):
            wing_norm_envs = (((meta.get("input_normalization") or {}).get("WingMPET") or {}).get("envelopes") or {})
            op7 = {
                "dt":{"value":self.config.time.fast_dt_s,"unit":"s","frame":"SCALAR","semantic":"dt","source":"Layer3 scheduler"},
                "x":self._source_envelope(packets["MESA_NAV"],"position_n",fallback_value=ms["position_n"],fallback_unit="m",fallback_frame="NED",source="MESA_NAV"),
                "y":self._source_envelope(packets["MESA_NAV"],"position_e",fallback_value=ms["position_e"],fallback_unit="m",fallback_frame="NED",source="MESA_NAV"),
                "h":self._source_envelope(packets["MESA_NAV"],"position_d",fallback_value=ms["position_d"],fallback_unit="m",fallback_frame="NED",source="MESA_NAV"),
                "air_velocity_x":copy.deepcopy(wing_norm_envs.get("air_velocity_x") or {"value":0.,"unit":"m/s","frame":"ENU","semantic":"air_velocity_x","source":"Layer3 default"}),
                "air_velocity_y":copy.deepcopy(wing_norm_envs.get("air_velocity_y") or {"value":0.,"unit":"m/s","frame":"ENU","semantic":"air_velocity_y","source":"Layer3 default"}),
                "air_velocity_h":copy.deepcopy(wing_norm_envs.get("air_velocity_h") or {"value":0.,"unit":"m/s","frame":"ENU","semantic":"air_velocity_h","source":"Layer3 default"}),
                "wind_x":self._source_envelope(packets["MicroMetTwin"],"wind_u_3dw",fallback_value=cs.get("wind_u_3dw",0.) if okcw else 0.,fallback_unit="m/s",fallback_frame="ENU",source="MicroMetTwin") if okcw else {"value":0.,"unit":"m/s","frame":"ENU","semantic":"wind_x","source":"Layer3 edge-off zero"},
                "wind_y":self._source_envelope(packets["MicroMetTwin"],"wind_v_3dw",fallback_value=cs.get("wind_v_3dw",0.) if okcw else 0.,fallback_unit="m/s",fallback_frame="ENU",source="MicroMetTwin") if okcw else {"value":0.,"unit":"m/s","frame":"ENU","semantic":"wind_y","source":"Layer3 edge-off zero"},
                "wind_h":self._source_envelope(packets["MicroMetTwin"],"diagnostic_vertical_wind",fallback_value=cs.get("diagnostic_vertical_wind",0.) if okcw else 0.,fallback_unit="m/s",fallback_frame="ENU",source="MicroMetTwin") if okcw else {"value":0.,"unit":"m/s","frame":"ENU","semantic":"wind_h","source":"Layer3 edge-off zero"},
                "target_x":{"value":target[0],"unit":"m","frame":"ENU","semantic":"target_x","source":"Layer3 normalized target"},
                "target_y":{"value":target[1],"unit":"m","frame":"ENU","semantic":"target_y","source":"Layer3 normalized target"},
                "target_h":{"value":target[2],"unit":"m","frame":"ENU","semantic":"target_h","source":"Layer3 normalized target"}}
            smap: dict[str,dict[str,Any]] = {}
            for dst, src in (("x","position_n"),("y","position_e"),("h","position_d"),("wind_x","wind_u_3dw"),("wind_y","wind_v_3dw"),("wind_h","diagnostic_vertical_wind")):
                smap.update(self._semantic_map((dst,src,"Phi_MESA_Climate_State_to_Wing_path")))
            vals, rec = self._normalize_inputs("WingMPET", self.wing, 7, op7, rctx, trusted_internal=True, semantic_mapping=smap)
            pd = self._evaluate_domain_packet("WingMPET", self.wing, 7, context={"coupling":"MESA->WingMPET","K":Kmw}, inputs=vals); packets["WingMPET"] = self._merge_packet(packets["WingMPET"], pd, "MESA->WingMPET:op7")
            out["operations"]["MESA+Climate->WingMPET:op7"] = {"executed":True,"a_ij":1,"K":Kmw,"K_by_type":pmw["detail"].get("K_by_type"),"delay_ticks":pmw["graph"].get("delay_ticks",0),"safety_gate":pmw["graph"].get("safety_gate",1),"tdoc_target":target_rec,"normalization":rec,"packet":pd}; out["execution_order"].append("MESA+Climate->WingMPET:op7")
        if packets["WingMPET"] != wing_direct:
            out["counterfactuals"]["MicroMetTwin->WingMPET"] = {**self._delta(wing_direct,packets["WingMPET"]),"actual_fmu_evaluation":True,"destination":"WingMPET"}

        okcm, Kcm, pcm = self._allowed(plan, "MicroMetTwin->MESA_NAV", "Data", "Model"); mesa_direct = copy.deepcopy(packets["MESA_NAV"])
        if okcm:
            anchors = self._evaluate_environmental_anchor_candidates(request, packets["MicroMetTwin"], rctx, K=Kcm)
            for name in ("wind","pressure"):
                a = anchors.get(name) or {}
                if a.get("offered") and a.get("accepted_by_mesa") and isinstance(a.get("packet"), Mapping):
                    packets["MESA_NAV"] = self._merge_packet(packets["MESA_NAV"], a["packet"], f"Climate->MESA_NAV:{name}_anchor")
            out["operations"]["Climate->MESA_NAV:anchors"] = {"executed":bool(anchors),"a_ij":1,"K":Kcm,"K_by_type":pcm["detail"].get("K_by_type"),"delay_ticks":pcm["graph"].get("delay_ticks",0),"safety_gate":pcm["graph"].get("safety_gate",1),"results":anchors,"state_overwrite":False}; out["execution_order"].append("Climate->MESA_NAV:anchors")
            out["counterfactuals"]["MicroMetTwin->MESA_NAV"] = {**self._delta(mesa_direct,packets["MESA_NAV"]),"actual_fmu_evaluation":bool(anchors),"destination":"MESA_NAV"}
        else:
            anchors = {"wind":{"offered":False,"reason":"edge_inactive_delay_or_K"},"pressure":{"offered":False,"reason":"edge_inactive_delay_or_K"}}

        okwm, Kwm, pwm = self._allowed(plan, "WingMPET->MESA_NAV", "Model", "Error")
        if okwm:
            ws = dict(packets["WingMPET"].get("state") or {}); evidence = {k:ws.get(k) for k in ("airspeed_norm","feasible","terrain_margin","obstacle_margin","energy","power","objective","cost_total") if k in ws}
            ctxm = dict(packets["MESA_NAV"].get("context") or {}); ctxm["layer3_model_error_input"] = {"source":"WingMPET","K_by_type":pwm["detail"].get("K_by_type"),"evidence":evidence,"quality":packets["WingMPET"].get("quality"),"validity":packets["WingMPET"].get("validity")}; packets["MESA_NAV"]["context"] = ctxm
            out["operations"]["WingMPET->MESA_NAV:model_error"] = {"executed":True,"a_ij":1,"K":Kwm,"K_by_type":pwm["detail"].get("K_by_type"),"delay_ticks":pwm["graph"].get("delay_ticks",0),"safety_gate":pwm["graph"].get("safety_gate",1),"destination_packet_updated":True,"state_overwrite":False,"fmi_input_invented":False,"wing_state":evidence}; out["execution_order"].append("WingMPET->MESA_NAV:model_error")

        out["counterfactual_states"] = {t:{"direct":dict(base_packets[t].get("state") or {}),"observed":dict(packets[t].get("state") or {})} for t in packets if t in base_packets}
        return packets, rctx, anchors, out

    def step(self, request: RuntimeRequest):
        base_packets, runtime_context, meta = self.collect_base_packets(request)
        plan = self.engine.plan_network(base_packets, reference_time_s=request.reference_time_s, context=runtime_context,
            safety_gates=request.safety_gates, constraints=request.constraints, risks=request.risks, objectives=request.objectives)
        packets, runtime_context, anchors, couplings = self.execute_couplings(request, base_packets, runtime_context, meta, plan)
        return self.engine.step(packets, reference_time_s=request.reference_time_s, context=runtime_context, regime_id=request.regime_id,
            stage=request.stage, prediction=request.prediction, action=request.action, safety_gates=request.safety_gates,
            constraints=request.constraints, risks=request.risks, objectives=request.objectives, anchor_results=anchors,
            runtime_couplings=couplings, network_plan=plan)

    def close(self) -> None:
        self.mesa.close(); self.wing.close()

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): self.close()
