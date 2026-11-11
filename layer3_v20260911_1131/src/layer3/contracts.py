from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .config import Layer3Config
from .tdoc_adapter import TDOCAdapter


@dataclass
class NormalizationResult:
    values: dict[str, Any]
    envelopes: dict[str, dict[str, Any]]
    records: list[dict[str, Any]]


class SignalContractRegistry:
    """Machine-readable signal contracts plus strict TDOC boundary normalization."""

    VECTOR_GROUPS = (
        ("state_x", "state_y", "state_h"), ("x", "y", "h"), ("target_x", "target_y", "target_h"),
        ("air_velocity_x", "air_velocity_y", "air_velocity_h"), ("ground_velocity_x", "ground_velocity_y", "ground_velocity_h"),
        ("wind_x", "wind_y", "wind_h"), ("position_n", "position_e", "position_d"), ("velocity_n", "velocity_e", "velocity_d"),
        ("ground_velocity_n", "ground_velocity_e", "ground_velocity_d"), ("windmap_n", "windmap_e", "windmap_d"),
        ("wind_n", "wind_e", "wind_d"), ("imu_gx", "imu_gy", "imu_gz"), ("imu_ax", "imu_ay", "imu_az"), ("mag_bx", "mag_by", "mag_bz"),
    )

    CLIMATE_INPUT_CONTRACT = {
        "latitude": {"unit": "deg", "frame": "WGS84", "semantic": "latitude"},
        "longitude": {"unit": "deg", "frame": "WGS84", "semantic": "longitude"},
        "elevation_sea_level": {"unit": "m", "frame": "MSL", "semantic": "elevation_sea_level"},
        "altitude_msl_m": {"unit": "m", "frame": "MSL", "semantic": "altitude_msl_m"},
        "elevation_ground_level": {"unit": "m", "frame": "AGL", "semantic": "elevation_ground_level"},
        "altitude_agl_m": {"unit": "m", "frame": "AGL", "semantic": "altitude_agl_m"},
        "ground_elevation_m": {"unit": "m", "frame": "MSL", "semantic": "ground_elevation_m"},
        "slope_deg": {"unit": "deg", "frame": "SCALAR", "semantic": "slope_deg"},
        "aspect_deg": {"unit": "deg", "frame": "SCALAR", "semantic": "aspect_deg"},
        "curvature": {"unit": "1", "frame": "SCALAR", "semantic": "curvature"},
        "curvature_radius_m": {"unit": "m", "frame": "SCALAR", "semantic": "curvature_radius_m"},
        "barnes_filter_km": {"unit": "km", "frame": "SCALAR", "semantic": "barnes_filter_km"},
        "wind_3dw_dx_m": {"unit": "m", "frame": "SCALAR", "semantic": "wind_3dw_dx_m"},
        "wind_3dw_dy_m": {"unit": "m", "frame": "SCALAR", "semantic": "wind_3dw_dy_m"},
        "wind_3dw_dz_m": {"unit": "m", "frame": "SCALAR", "semantic": "wind_3dw_dz_m"},
        "wind_profile_power": {"unit": "1", "frame": "SCALAR", "semantic": "wind_profile_power"},
    }

    def __init__(self, config: Layer3Config, tdoc: TDOCAdapter):
        self.config = config; self.tdoc = tdoc; self.static: dict[str, Any] = {}
        for twin, path in {
            "MESA_NAV": config.twins_root / "resources" / "MESA_NAV_signal_schema.json",
            "WingMPET": config.twins_root / "resources" / "WingMPET_signal_schema.json",
            "MicroMetTwin": config.climate_root / "resources" / "tddt_layer2" / "twin_contract.json",
        }.items():
            if path.exists(): self.static[twin] = json.loads(path.read_text(encoding="utf-8"))
        override_path = config.package_root / "resources" / "tdoc_contract_overrides.json"
        self.contract_overrides = {}
        if override_path.exists(): self.contract_overrides = dict((json.loads(override_path.read_text(encoding="utf-8")) or {}).get("overrides") or {})

    @staticmethod
    def _is_envelope(value: Any) -> bool:
        return isinstance(value, Mapping) and "value" in value

    def _meta(self, domain_twin: Any, operation: int, name: str, *, causality: str = "in") -> dict[str, Any]:
        fmi_name = f"op{int(operation):02d}__{causality}__{name}"
        meta = dict(domain_twin.fmu.signal_metadata(fmi_name))
        override = dict(self.contract_overrides.get(fmi_name) or {})
        if override:
            meta.update({k:v for k,v in override.items() if k != "reason"})
            meta["layer3_contract_override"] = {"reason":override.get("reason"),"source":"resources/tdoc_contract_overrides.json"}
        meta.setdefault("unit", "1"); meta.setdefault("frame", "SCALAR"); meta.setdefault("semantic", meta.get("semantic_name") or name)
        return meta

    def _wrap_trusted(self, value: Any, meta: Mapping[str, Any], name: str, context: Mapping[str, Any]) -> dict[str, Any]:
        return {"value": value, "unit": str(meta.get("unit") or "1"), "frame": str(meta.get("frame") or "SCALAR"),
                "semantic": str(meta.get("semantic") or meta.get("semantic_name") or name), "uncertainty": None,
                "timestamp": context.get("reference_time_s"), "source": "layer3_trusted_internal",
                "provenance": {"origin": "Layer3 internal coupling", "destination_contract": name}}

    def _normalize_one(self, item: Any, meta: Mapping[str, Any], name: str, *, context: Mapping[str, Any], trusted_internal: bool,
                       allow_semantic_relabel: bool = False, semantic_provenance: Mapping[str, Any] | None = None) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        external = not trusted_internal
        if not self._is_envelope(item):
            if external and bool(self.config.runtime.get("require_contract_normalization", True)):
                raise ValueError(f"{name}: bare external value rejected; provide value/unit/frame/semantic envelope")
            item = self._wrap_trusted(item, meta, name, context)
        if isinstance(item, Mapping) and (item.get("adapter_kind") or meta.get("adapter_kind")):
            value, env = self.tdoc.normalize_specialized(item, target_metadata=meta, context=context, external=external)
            return value, env, dict(env.get("provenance", {}).get("tdoc", {}))
        value, env = self.tdoc.normalize_envelope(item, target_unit=str(meta.get("unit") or "1"), target_frame=str(meta.get("frame") or "SCALAR"),
            target_semantic=str(meta.get("semantic") or meta.get("semantic_name") or name), context=context, external=external,
            allow_semantic_relabel=allow_semantic_relabel, semantic_provenance=semantic_provenance)
        return value, env, dict(env.get("provenance", {}).get("tdoc", {}))

    def normalize_operation_inputs(self, domain_twin: Any, operation: int, inputs: Mapping[str, Any], *, context: Mapping[str, Any] | None = None,
                                   trusted_internal: bool = False, semantic_mapping: Mapping[str, Mapping[str, Any]] | None = None) -> NormalizationResult:
        raw = dict(inputs or {}); ctx = dict(context or {}); allowed = set(domain_twin.fmu.operation_signature(int(operation)).get("inputs", []))
        unknown = sorted(set(raw) - allowed)
        if unknown: raise KeyError(f"Unknown inputs for operation {operation}: {unknown}")
        values: dict[str, Any] = {}; envelopes: dict[str, dict[str, Any]] = {}; records: list[dict[str, Any]] = []; consumed: set[str] = set(); mapping = dict(semantic_mapping or {})

        for group in self.VECTOR_GROUPS:
            present = [n for n in group if n in raw]
            if len(present) != 3: continue
            metas = [self._meta(domain_twin, operation, n) for n in group]
            items = [raw[n] if self._is_envelope(raw[n]) else self._wrap_trusted(raw[n], metas[i], n, ctx) if trusted_internal else raw[n] for i, n in enumerate(group)]
            if not all(self._is_envelope(x) for x in items): continue
            src_frames = [str(x.get("frame") or "") for x in items]; target_frames = [str(m.get("frame") or "SCALAR") for m in metas]
            if len(set(src_frames)) != 1 or len(set(target_frames)) != 1: continue
            vec: list[float] = []; normalized_components: list[dict[str, Any]] = []
            for name, item, meta in zip(group, items, metas):
                sm = mapping.get(name) or {}
                val, env = self.tdoc.normalize_envelope(item, target_unit=str(meta.get("unit") or "1"), target_frame=str(item.get("frame") or meta.get("frame") or "SCALAR"),
                    target_semantic=str(meta.get("semantic") or meta.get("semantic_name") or name), context=ctx, external=not trusted_internal,
                    allow_semantic_relabel=bool(sm.get("allow_relabel", False)), semantic_provenance=sm.get("provenance"))
                vec.append(float(val)); normalized_components.append(env)
            c0 = items[0].get("covariance"); covariance = c0 if isinstance(c0, (list, tuple)) and len(c0) == 3 else None
            out_vec, out_cov, frame_rec = self.tdoc.transform_vector(vec, src_frames[0], target_frames[0], covariance=covariance, context=ctx)
            records.append({"group": list(group), "tdoc": frame_rec, "complete_contract": True})
            for idx, (name, meta, env) in enumerate(zip(group, metas, normalized_components)):
                values[name] = out_vec[idx]; env = dict(env); env.update({"value": out_vec[idx], "unit": meta["unit"], "frame": meta["frame"], "semantic": meta.get("semantic") or meta.get("semantic_name") or name})
                if out_cov is not None:
                    env["covariance"] = out_cov; env["uncertainty"] = max(float(out_cov[idx][idx]), 0.0) ** 0.5
                env.setdefault("provenance", {}); env["provenance"] = {**dict(env["provenance"]), "tdoc_frame": frame_rec}; envelopes[name] = env; consumed.add(name)

        for name, item in raw.items():
            if name in consumed: continue
            meta = self._meta(domain_twin, operation, name); sm = mapping.get(name) or {}
            value, env, rec = self._normalize_one(item, meta, name, context=ctx, trusted_internal=trusted_internal,
                                                  allow_semantic_relabel=bool(sm.get("allow_relabel", False)), semantic_provenance=sm.get("provenance"))
            values[name] = value; envelopes[name] = env; records.append({"signal": name, "tdoc": rec, "complete_contract": True})
        return NormalizationResult(values=values, envelopes=envelopes, records=records)

    def normalize_climate_request(self, climate: Mapping[str, Any], *, context: Mapping[str, Any] | None = None, trusted_internal: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raw = dict(climate or {}); ctx = dict(context or {}); out: dict[str, Any] = {}; records: list[dict[str, Any]] = []
        passthrough = {"fmu","input","dem","dem_dir","terrain_policy","with_metadata","context_id","operation_code"}
        for name, item in raw.items():
            if name in passthrough: out[name] = item; continue
            if name == "time":
                projection=ctx.get('native_climate_time_projection')
                if projection is not None:
                    from tddt_tdoc.native_time import validate_native_time
                    p=validate_native_time(projection)
                    if self._is_envelope(item):
                        env=self.tdoc.validate_envelope(item,external=not trusted_internal)
                        if env.get('time_reference')!=p['time_reference'] or env.get('time_binding')!=p:
                            raise ValueError('native climate time envelope domain/binding mismatch')
                        value=env['value']
                    elif trusted_internal:value=item
                    else:raise ValueError('native climate time requires explicit bound envelope')
                    if value!=p['dataset_timestamp']:raise ValueError('native climate query timestamp changed')
                    out[name]=value;records.append({'signal':'time','native_time_projection':p,'UTC_conversion_performed':False})
                    continue
                if self._is_envelope(item):
                    env = self.tdoc.validate_envelope(item, external=not trusted_internal); src_ref = str(env.get("time_reference") or "UTC")
                    if src_ref in ('ACCEPTED_DATASET_NAIVE','OFFSET_EXPLICIT'):
                        raise ValueError('dataset clock needs native climate time projection; implicit UTC forbidden')
                    val, rec = self.tdoc.transform_time(env["value"], source_reference=src_ref, target_reference="UTC", context={"epoch": self.config.time.mission_epoch_utc})
                    out[name] = val; records.append({"signal":"time","tdoc":rec,"envelope":env})
                elif not trusted_internal and bool(self.config.runtime.get("require_contract_normalization", True)):
                    raise ValueError("climate.time requires an envelope with value/unit/frame/semantic/time_reference in strict mode")
                else: out[name] = item
                continue
            meta = self.CLIMATE_INPUT_CONTRACT.get(name)
            if meta is None: out[name] = item; continue
            value, env, rec = self._normalize_one(item, meta, name, context=ctx, trusted_internal=trusted_internal)
            out[name] = value; records.append({"signal":name,"tdoc":rec,"envelope":env})
        return out, records

    def climate_output_envelopes(self, packet: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        state = dict(packet.get("state") or {}); unc = packet.get("uncertainty") or {}; std = unc.get("std") or unc.get("by_output") or {}
        contract = self.static.get("MicroMetTwin") or {}; sci = contract.get("scientific_contract") or {}; defs = {str(x.get("name")):x for x in (sci.get("states") or []) if isinstance(x, Mapping)}
        result: dict[str, dict[str, Any]] = {}
        for name, value in state.items():
            meta = defs.get(name)
            if not meta: continue
            result[name] = {"value": value, "unit": str(meta.get("unit") or "1"), "frame": str(meta.get("frame") or "SCALAR"), "semantic": name,
                "timestamp": packet.get("timestamp"), "source": "MicroMetTwin", "uncertainty": std.get(name) if isinstance(std, Mapping) else None,
                "provenance": {"source_twin":"MicroMetTwin", "contract":"twin_contract.json/scientific_contract.states"}}
        return result

    def output_envelopes(self, domain_twin: Any, operation: int, packet: Mapping[str, Any], *, twin_id: str) -> dict[str, dict[str, Any]]:
        state = dict(packet.get("state") or {}); unc = packet.get("uncertainty") or {}; std = unc.get("std") or unc.get("by_output") or {}; result: dict[str, dict[str, Any]] = {}
        sig = set(domain_twin.fmu.operation_signature(int(operation)).get("outputs", []))
        for name, value in state.items():
            if name not in sig: continue
            try: meta = self._meta(domain_twin, operation, name, causality="out")
            except Exception: continue
            result[name] = {"value": value, "unit": str(meta.get("unit") or "1"), "frame": str(meta.get("frame") or "SCALAR"),
                "semantic": str(meta.get("semantic") or meta.get("semantic_name") or name), "timestamp": packet.get("timestamp"), "source": twin_id,
                "uncertainty": std.get(name) if isinstance(std, Mapping) else None,
                "provenance": {"source_twin": twin_id, "operation": int(operation), "contract": "FMU signal_metadata"}}
        return result
