from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import numpy as np

from .config import Layer3Config
from .models import AlignedTwinState, SharedTransDomainState
from .state_authority import bind_state_authority, StateAuthorityError, NAV_PROTECTED
from .tdoc_adapter import TDOCAdapter
from .utils import clamp, finite, parse_iso, safe_float


@dataclass
class TimeAlignmentResult:
    source_time_s: float
    age_s: float
    future_skew_s: float = 0.0
    warning: str | None = None
    local_source_time_s: float | None = None
    mission_override_applied: bool = False


class TimeSynchronizer:
    def __init__(self, config: Layer3Config, tdoc: TDOCAdapter):
        self.config = config
        self.tdoc = tdoc
        self.epoch_text = config.time.mission_epoch_utc
        self.epoch = parse_iso(config.time.mission_epoch_utc) if config.time.mission_epoch_utc else None

    def to_elapsed(self, timestamp: Any) -> float:
        if isinstance(timestamp, (int, float)):
            return float(timestamp)
        if isinstance(timestamp, str):
            if self.epoch_text is None:
                raise ValueError("mission_epoch_utc is required to align common/UTC timestamps with FMI elapsed time")
            text = timestamp.strip()
            # Some domain datasets emit an ISO local/common timestamp without an
            # offset. Bind it explicitly to the configured mission epoch offset
            # before handing it to TDOC; never let the host local timezone decide.
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is None and self.epoch is not None:
                    dt = dt.replace(tzinfo=self.epoch.tzinfo)
                    text = dt.isoformat()
            except Exception:
                pass
            value, _ = self.tdoc.common_time_to_elapsed(text, epoch=self.epoch_text)
            return float(value)
        raise TypeError(f"unsupported timestamp type: {type(timestamp).__name__}")

    def align(self, packet: Mapping[str, Any], reference_time_s: float, *, context: Mapping[str, Any] | None = None) -> TimeAlignmentResult:
        local_src: float | None = None
        projection=(context or {}).get('native_climate_time_projection')
        if projection is not None and packet.get('twin_id')=='MicroMetTwin':
            from tddt_tdoc.native_time import validate_native_time
            p=validate_native_time(projection)
            if (packet.get('timestamp')!=p['dataset_timestamp'] or packet.get('native_climate_time_projection')!=p
                or packet.get('timestamp_reference')!=p['time_reference']):
                raise ValueError('native MicroMet packet time/binding mismatch')
            # Alignment uses the observed plant domain of reference_time_s.
            # The dataset timestamp is retained, without pretending it is UTC.
            local_src=p['plant_time_s']
        else:
            try:
                local_src = self.to_elapsed(packet.get("timestamp", reference_time_s))
            except Exception:
                local_src = None

        # TDOC full-fidelity replay runs isolated FMUs whose local clocks restart
        # at zero.  The explicit context below is the trusted time-origin bridge
        # from that local verification epoch to mission elapsed time.  Consume it
        # here in Sublayer 3.1 rather than relying on a caller to rewrite packet
        # timestamps before alignment.  The override is simulation-only and must
        # agree with this Layer-3 transaction's reference time within the normal
        # bounded future-skew tolerance; otherwise we ignore it and fall back to
        # the packet timestamp so freshness remains fail-closed.
        ctx = dict(context or {})
        override_raw = ctx.get("tdoc_full_fidelity_reference_time_s")
        if projection is not None and packet.get('twin_id')=='MicroMetTwin' and override_raw is not None:
            raise ValueError('native observed climate clock cannot use a replay clock override')
        max_future = max(0.0, float(getattr(self.config.time, "max_future_skew_s", 0.0)))
        if override_raw is not None:
            try:
                override = float(override_raw)
                if not math.isfinite(override):
                    raise ValueError("non_finite_full_fidelity_reference_time")
                mismatch = abs(override - float(reference_time_s))
                if mismatch <= max(max_future, 1e-9):
                    delta = float(reference_time_s) - override
                    future_skew = max(0.0, -delta)
                    return TimeAlignmentResult(
                        override, max(0.0, delta), future_skew_s=future_skew,
                        local_source_time_s=local_src, mission_override_applied=True,
                    )
            except Exception:
                pass

        try:
            src = local_src if local_src is not None else self.to_elapsed(packet.get("timestamp", reference_time_s))
            delta = float(reference_time_s) - src
            future_skew = max(0.0, -delta)
            if future_skew > max_future:
                return TimeAlignmentResult(src, 0.0, future_skew_s=future_skew,
                    warning=f"timestamp_in_future:{future_skew:.9f}s>max_future_skew_s:{max_future:.9f}s",
                    local_source_time_s=local_src)
            warning = None
            if override_raw is not None:
                warning = "tdoc_full_fidelity_reference_time_mismatch_or_invalid"
            return TimeAlignmentResult(src, max(0.0, delta), future_skew_s=future_skew,
                warning=warning, local_source_time_s=local_src)
        except Exception as exc:
            return TimeAlignmentResult(reference_time_s, 0.0, future_skew_s=0.0, warning=f"timestamp_unaligned:{exc}",
                local_source_time_s=local_src)


class STDAlignment:
    """Sublayer 3.1: temporal, unit, frame and semantic alignment."""

    def __init__(self, config: Layer3Config, tdoc: TDOCAdapter | None = None, registry: Any | None = None):
        self.config = config
        self.tdoc = tdoc or TDOCAdapter(config)
        self.registry = registry
        self.time = TimeSynchronizer(config, self.tdoc)

    @staticmethod
    def _score(container: Any, *names: str, default: float = 0.0) -> float:
        if isinstance(container, Mapping):
            for name in names:
                if name in container and finite(container[name]):
                    return clamp(float(container[name]), 0.0, 1.0)
        if finite(container):
            return clamp(float(container), 0.0, 1.0)
        return default

    @staticmethod
    def _reliability(packet: Mapping[str, Any]) -> float:
        r = packet.get("reliability", {})
        if isinstance(r, Mapping):
            for key in ("posterior", "prior", "score"):
                if finite(r.get(key)):
                    return clamp(float(r[key]), 0.0, 1.0)
        return 0.0

    @staticmethod
    def _uncertainty_maps(packet: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
        u = packet.get("uncertainty", {})
        if not isinstance(u, Mapping):
            return {}, {}
        std = u.get("by_output") or u.get("std") or {}
        var = u.get("variance_by_output") or u.get("variance") or {}
        std = {str(k): abs(float(v)) for k, v in std.items() if finite(v)} if isinstance(std, Mapping) else {}
        var = {str(k): abs(float(v)) for k, v in var.items() if finite(v)} if isinstance(var, Mapping) else {}
        if not std and var:
            std = {k: math.sqrt(v) for k, v in var.items()}
        return std, var

    @staticmethod
    def _normalized_uncertainty(state: Mapping[str, Any], std: Mapping[str, float]) -> float:
        ratios: list[float] = []
        for key, sigma in std.items():
            if key in state and finite(state[key]):
                scale = max(1.0, abs(float(state[key])))
                ratios.append(abs(float(sigma)) / scale)
        if not ratios:
            ratios = [abs(float(v)) for v in std.values() if finite(v)]
        if not ratios:
            return 0.5
        rms = math.sqrt(sum(r * r for r in ratios) / len(ratios))
        return clamp(rms / (1.0 + rms), 0.0, 1.0)

    @staticmethod
    def _wing_air_velocity_ned(state: Mapping[str, Any]) -> list[float] | None:
        # Direct components, if a future Wing operation emits them.
        direct_sets = [
            ("air_velocity_n", "air_velocity_e", "air_velocity_d"),
            ("va_n", "va_e", "va_d"),
        ]
        for names in direct_sets:
            if all(finite(state.get(n)) for n in names):
                return [float(state[n]) for n in names]
        if all(finite(state.get(n)) for n in ("airspeed_norm", "flightpath_angle", "air_heading")):
            va = float(state["airspeed_norm"])
            gamma = float(state["flightpath_angle"])
            psi = float(state["air_heading"])
            horizontal = va * math.cos(gamma)
            return [horizontal * math.cos(psi), horizontal * math.sin(psi), -va * math.sin(gamma)]
        return None

    @staticmethod
    def _mesa_ground_velocity_ned(state: Mapping[str, Any]) -> list[float] | None:
        for names in [
            ("velocity_n", "velocity_e", "velocity_d"),
            ("ground_velocity_n", "ground_velocity_e", "ground_velocity_d"),
            ("vg_n", "vg_e", "vg_d"),
        ]:
            if all(finite(state.get(n)) for n in names):
                return [float(state[n]) for n in names]
        return None

    def _validate_source_envelopes(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        records: dict[str, Any] = {}
        envs = packet.get("signal_envelopes")
        if not isinstance(envs, Mapping):
            return {"available": False, "validated": 0, "reason": "no_signal_envelopes"}
        for name, env in envs.items():
            if not isinstance(env, Mapping) or "value" not in env:
                if self.config.alignment.strict_units or self.config.alignment.strict_frames or self.config.alignment.strict_semantics:
                    raise ValueError(f"{packet.get('twin_id')} output {name} lacks a complete TDOC envelope")
                continue
            src = self.tdoc.validate_envelope(env, external=False)
            _, out = self.tdoc.normalize_envelope(src, target_unit=str(src.get("unit") or "1"),
                target_frame=str(src.get("frame") or "SCALAR"), target_semantic=str(src.get("semantic") or name),
                context=packet.get("context") or {}, external=False)
            records[str(name)] = out.get("provenance", {}).get("tdoc", {})
        return {"available": True, "validated": len(records), "records": records}

    def _canonicalize(self, twin_id: str, state: Mapping[str, Any], std_map: Mapping[str, float] | None = None,
                      packet: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        canonical: dict[str, Any] = {"raw": dict(state)}
        conv: dict[str, Any] = {}
        adapter = self.registry.adapter_for(twin_id) if self.registry is not None else {
            "MESA_NAV":"navigation_ned","WingMPET":"flight_ned","MicroMetTwin":"climate_enu"}.get(twin_id,"generic")
        if adapter == "navigation_ned":
            v = self._mesa_ground_velocity_ned(state)
            if v is not None: canonical["ground_velocity_ned_m_s"] = v
            if all(finite(state.get(n)) for n in ("position_n", "position_e", "position_d")):
                canonical["position_ned_m"] = [float(state["position_n"]), float(state["position_e"]), float(state["position_d"])]
            for key in ("heading", "course", "yaw"):
                if finite(state.get(key)): canonical["heading_rad"] = float(state[key]); break
        elif adapter == "flight_ned":
            v = self._wing_air_velocity_ned(state)
            if v is not None:
                canonical["air_velocity_ned_m_s"] = v
                canonical["airspeed_m_s"] = float(np.linalg.norm(np.asarray(v, dtype=float)))
            if finite(state.get("airspeed_norm")): canonical["airspeed_m_s"] = float(state["airspeed_norm"])
            if finite(state.get("air_heading")): canonical["air_heading_rad"] = float(state["air_heading"])
            if finite(state.get("flightpath_angle")): canonical["flightpath_angle_rad"] = float(state["flightpath_angle"])
        elif adapter == "climate_enu":
            sm = dict(std_map or {}); cov_enu = None
            if all(n in sm for n in ("wind_u_3dw", "wind_v_3dw", "diagnostic_vertical_wind")):
                cov_enu = [[sm["wind_u_3dw"]**2,0.,0.],[0.,sm["wind_v_3dw"]**2,0.],[0.,0.,sm["diagnostic_vertical_wind"]**2]]
            wind, record = self.tdoc.climate_wind_to_mesa_ned(state, covariance_enu=cov_enu)
            conv["wind_frame"] = record
            if wind is not None:
                canonical["wind_ned_m_s"] = wind; canonical["wind_speed_3d_m_s"] = float(np.linalg.norm(np.asarray(wind,dtype=float)))
                if record.get("covariance_ned") is not None: canonical["wind_covariance_ned"] = record["covariance_ned"]
            elif finite(state.get("wind_speed_terrain")): canonical["wind_speed_3d_m_s"] = float(state["wind_speed_terrain"])
            if finite(state.get("temperature_local")):
                canonical["temperature_c"] = float(state["temperature_local"])
                val,_,rec=self.tdoc.convert_scalar(float(state["temperature_local"]),"degC","K",source_frame="SCALAR",target_frame="SCALAR",source_semantic="temperature",target_semantic="temperature")
                canonical["temperature_k"]=float(val); conv["temperature"]=rec
            if finite(state.get("relative_humidity_local")):
                canonical["relative_humidity_percent"]=float(state["relative_humidity_local"]); canonical["relative_humidity_fraction"]=float(state["relative_humidity_local"])/100.
            if finite(state.get("wind_direction_terrain_deg")):
                canonical["wind_direction_from_deg"]=float(state["wind_direction_terrain_deg"])
                val,_,rec=self.tdoc.convert_scalar(float(state["wind_direction_terrain_deg"]),"deg","rad",source_frame="SCALAR",target_frame="SCALAR",source_semantic="wind_direction",target_semantic="wind_direction")
                canonical["wind_direction_from_rad"]=float(val); conv["wind_direction"]=rec
            if finite(state.get("surface_pressure_pa")):
                p=float(state["surface_pressure_pa"]); canonical["pressure_pa"]=p; canonical["log_pressure_ratio"]=self.tdoc.pressure_to_log_ratio(p)
                conv["pressure_semantics"]={"physical":"Pa","log_ratio_reference_pa":101325.0,"adapter":"TDOC.pressure_to_log_ratio"}
            if finite(state.get("air_density")): canonical["air_density_kg_m3"]=float(state["air_density"])

        # Generic N-Twin path: a producer may publish already-canonical signals
        # without requiring a code change in Layer 3.
        pkt = dict(packet or {})
        supplied = pkt.get("canonical_state")
        generic = pkt.get("shared_state")
        for supplied_state in (supplied, generic):
            if isinstance(supplied_state, Mapping):
                if twin_id == "MESA_NAV" and NAV_PROTECTED.intersection(supplied_state):
                    raise StateAuthorityError("MESA_navigation_authority_cannot_be_shadowed")
                if twin_id == "MESA_NAV" and any(t in str(k).lower() for k in supplied_state for t in ("gps", "gnss", "ground_truth", "validation_truth", "sim_truth")):
                    raise StateAuthorityError("gps_truth_cannot_override_navigation_authority")
                canonical.update(dict(supplied_state))
        return canonical, conv

    def align_packet(self, packet: Mapping[str, Any], reference_time_s: float, *, context: Mapping[str, Any] | None = None) -> AlignedTwinState:
        twin_id = str(packet.get("twin_id") or "UNKNOWN")
        state = dict(packet.get("state") or {})
        ta = self.time.align(packet, reference_time_s, context=context)
        lam = float(self.config.time.freshness_lambda_per_s.get(twin_id, 0.001))
        future_time_violation = bool(ta.future_skew_s > max(0.0, float(getattr(self.config.time, "max_future_skew_s", 0.0))))
        invalid_time = bool(ta.warning and ta.warning.startswith("timestamp_unaligned:")) or "timestamp" not in packet
        freshness = 0.0 if (future_time_violation or invalid_time) else math.exp(-max(0.0, lam) * max(0.0, ta.age_s))
        q = self._score(packet.get("quality", {}), "score", "quality", default=0.0)
        v = self._score(packet.get("validity", {}), "score", "validity", default=0.0)
        r = self._reliability(packet)
        std_map, var_map = self._uncertainty_maps(packet)
        u = self._normalized_uncertainty(state, std_map)
        source_contract_validation = self._validate_source_envelopes(packet)
        canonical, conversion = self._canonicalize(twin_id, state, std_map, packet)
        conversion["source_contract_validation"] = source_contract_validation
        qmin = self.config.threshold(self.config.alignment.min_quality, twin_id)
        vmin = self.config.threshold(self.config.alignment.min_validity, twin_id)
        fmin = self.config.threshold(self.config.alignment.min_freshness, twin_id)
        gate = int((not future_time_violation) and (not invalid_time) and v >= vmin and freshness >= fmin and q >= qmin)
        covariance = {
            "std_by_output": std_map,
            "variance_by_output": var_map,
            "normalized_uncertainty": u,
        }
        prov = dict(packet.get("provenance") or {})
        prov.update({
            "layer3_alignment": "sample-and-hold temporal projection + explicit TDOC conversion",
            "time_alignment_warning": ta.warning,
            "future_skew_s": ta.future_skew_s,
            "future_time_violation": future_time_violation,
            "invalid_or_missing_timestamp": invalid_time,
            "conversion": conversion,
        })
        if ta.mission_override_applied:
            prov["tdoc_full_fidelity_time_origin_alignment"] = {
                "schema": "TDOC-L3-FF-TimeOrigin-v1",
                "simulation_only": True,
                "not_field_evidence": True,
                "local_fmi_timestamp_s": ta.local_source_time_s,
                "mission_reference_time_s": ta.source_time_s,
            }
        return AlignedTwinState(
            twin_id=twin_id,
            source_time_s=ta.source_time_s,
            reference_time_s=reference_time_s,
            age_s=ta.age_s,
            freshness=freshness,
            gate=gate,
            quality=q,
            validity=v,
            reliability=r,
            normalized_uncertainty=u,
            raw_state=state,
            canonical_state=canonical,
            covariance=covariance,
            context=dict(packet.get("context") or {}),
            provenance=prov,
        )

    def build_std(
        self,
        packets: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]],
        reference_time_s: float,
        *,
        context: Mapping[str, Any] | None = None,
        constraints: Mapping[str, Any] | None = None,
        risks: Mapping[str, Any] | None = None,
        objectives: Mapping[str, Any] | None = None,
        control_status: Mapping[str, Any] | None = None,
    ) -> SharedTransDomainState:
        seq = list(packets.values()) if isinstance(packets, Mapping) else list(packets)
        reps = {str(p.get("twin_id")): self.align_packet(p, reference_time_s, context=context) for p in seq}
        state_authority = None
        ctx = dict(context or {})
        if "layer3_state_authority_v1" in ctx:
            state_authority = bind_state_authority(
                ctx['layer3_state_authority_v1'], reference_time_s=reference_time_s,
                max_age_s=float(self.config.time.fast_dt_s),
                max_future_skew_s=float(self.config.time.max_future_skew_s))
        ctrl = dict(control_status or {})
        ctrl['state_authority'] = state_authority or {
            'schema': 'TDDT-L3-StateAuthority-v1', 'status': 'NOT_PROVIDED',
            'controller_prediction_eligible': False, 'native_controller_certified': False,
            'flight_authorized': False,
        }
        return SharedTransDomainState(
            reference_time_s=reference_time_s,
            context=dict(context or {}),
            meta={"active_twin_count": len(reps), "accepted_twins": [k for k, v in reps.items() if v.gate]},
            representations=reps,
            constraints=dict(constraints or {}),
            risks=dict(risks or {}),
            objectives=dict(objectives or {}),
            control_status=ctrl,
            provenance={"formula": "3.1-1..3.1-9", "alignment_policy": "no cross-domain averaging"},
        )
