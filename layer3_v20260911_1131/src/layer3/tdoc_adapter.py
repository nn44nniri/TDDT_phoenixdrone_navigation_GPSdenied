from __future__ import annotations

import math
import sys
from typing import Any, Mapping, Sequence

from .config import Layer3Config
from .utils import write_json


class TDOCAdapter:
    """Strict contract-driven gateway to TDOC 20260824_0914.

    The Layer-3 boundary never silently changes unit/frame/reference/semantics.
    Specialized aviation transformations are delegated to TDOC and every
    transformation is recorded in the TDOC health log.
    """

    DATA_ROLES = {"current_state", "prior", "reference", "truth"}

    def __init__(self, config: Layer3Config):
        self.config = config
        src = config.tdoc_root / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from tdoc_unit_converter import UnitConverter, convert  # type: ignore
        from tdoc_unit_converter import adapters as av  # type: ignore
        self.UnitConverter = UnitConverter
        self.convert = convert
        self.av = av
        self.converter = UnitConverter()
        self.health = {"ok": True, "conversions": 0, "failures": 0, "last_error": None, "events": []}

    @property
    def strict_units(self) -> bool:
        return bool(self.config.alignment.strict_units)

    @property
    def strict_frames(self) -> bool:
        return bool(self.config.alignment.strict_frames)

    @property
    def strict_semantics(self) -> bool:
        return bool(self.config.alignment.strict_semantics)

    def _event(self, rec: Mapping[str, Any], ok: bool = True) -> dict[str, Any]:
        event = dict(rec); event["ok"] = bool(ok)
        self.health["conversions"] = int(self.health.get("conversions", 0)) + int(ok)
        self.health["failures"] = int(self.health.get("failures", 0)) + int(not ok)
        self.health["ok"] = int(self.health["failures"]) == 0
        if not ok:
            self.health["last_error"] = event.get("error") or event.get("reason")
        events = list(self.health.get("events") or []); events.append(event)
        self.health["events"] = events[-100:]
        return event

    def flush_health(self) -> None:
        try:
            write_json(self.config.resolve(self.config.paths.tdoc_health), self.health)
        except Exception:
            pass

    def enu_to_ned(self, vector: Sequence[float]) -> list[float]:
        return list(self.av.enu_to_ned(tuple(float(x) for x in vector)))

    def ned_to_enu(self, vector: Sequence[float]) -> list[float]:
        return list(self.av.ned_to_enu(tuple(float(x) for x in vector)))

    def frd_to_flu(self, vector: Sequence[float]) -> list[float]:
        return list(self.av.frd_to_flu(tuple(float(x) for x in vector)))

    def flu_to_frd(self, vector: Sequence[float]) -> list[float]:
        return list(self.av.flu_to_frd(tuple(float(x) for x in vector)))

    def covariance_enu_ned(self, matrix: Sequence[Sequence[float]]) -> list[list[float]]:
        return [list(row) for row in self.av.ned_enu_covariance(tuple(tuple(float(x) for x in r) for r in matrix))]

    def covariance_frd_flu(self, matrix: Sequence[Sequence[float]]) -> list[list[float]]:
        return [list(row) for row in self.av.frd_flu_covariance(tuple(tuple(float(x) for x in r) for r in matrix))]

    def to_kelvin(self, value_c: float) -> float:
        return float(self.convert(float(value_c), "degC", "K"))

    def deg_to_rad(self, value_deg: float) -> float:
        return float(self.convert(float(value_deg), "deg", "rad"))

    def common_time_to_elapsed(self, common_time: Any, *, epoch: Any) -> tuple[float, dict[str, Any]]:
        rec = {"kind": "time", "source_scale": "UTC", "target_scale": "mission_elapsed", "epoch": str(epoch)}
        try:
            value = float(self.av.common_time_to_elapsed(common_time, epoch=epoch)); rec["adapter"] = "tdoc.adapters.common_time_to_elapsed"
            return value, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def elapsed_to_common_time(self, elapsed_s: float, *, epoch: Any) -> tuple[str, dict[str, Any]]:
        rec = {"kind": "time", "source_scale": "mission_elapsed", "target_scale": "UTC", "epoch": str(epoch)}
        try:
            value = self.av.elapsed_to_common_time(float(elapsed_s), epoch=epoch)
            text = value.isoformat() if hasattr(value, "isoformat") else str(value)
            rec["adapter"] = "tdoc.adapters.elapsed_to_common_time"
            return text, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def validate_envelope(self, envelope: Mapping[str, Any], *, external: bool = True) -> dict[str, Any]:
        src = dict(envelope)
        if "value" not in src:
            raise ValueError("TDOC envelope requires value")
        missing = []
        if self.strict_units and not src.get("unit"): missing.append("unit")
        if self.strict_frames and not src.get("frame"): missing.append("frame")
        if self.strict_semantics and not src.get("semantic"): missing.append("semantic")
        if missing:
            raise ValueError("TDOC strict envelope missing: " + ", ".join(missing))
        src.setdefault("uncertainty", None); src.setdefault("timestamp", None)
        src.setdefault("source", "external_runtime_request" if external else "layer3_internal")
        prov = src.get("provenance")
        if prov is None:
            src["provenance"] = {"origin": src["source"], "metadata_completion": "Layer3 TDOC gateway"}
        elif not isinstance(prov, Mapping):
            raise ValueError("provenance must be a mapping when supplied")
        return src

    @staticmethod
    def _role_token(value: Any) -> str | None:
        if value is None: return None
        t = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {"current": "current_state", "state": "current_state", "prior_state": "prior", "ref": "reference", "ground_truth": "truth"}
        return aliases.get(t, t)

    def semantic_check(self, source_role: str | None, target_role: str | None, *, provenance: Mapping[str, Any] | None = None,
                       allow_relabel: bool = False, source_signal: str | None = None, target_signal: str | None = None) -> dict[str, Any]:
        if not source_role or not target_role or source_role == target_role:
            return {"valid": True, "source_role": source_role, "target_role": target_role}
        sr, tr = self._role_token(source_role), self._role_token(target_role)
        if sr in self.DATA_ROLES and tr in self.DATA_ROLES:
            result = self.av.validate_data_semantics(source_role=sr, target_role=tr, allow_relabel=bool(allow_relabel), provenance=dict(provenance or {}))
            return {"valid": bool(result), "source_role": sr, "target_role": tr, "adapter": "tdoc.adapters.validate_data_semantics"}
        valid = bool(allow_relabel and provenance)
        return {"valid": valid, "source_role": source_role, "target_role": target_role,
                "source_signal": source_signal, "target_signal": target_signal,
                "adapter": "explicit_signal_semantic_mapping" if valid else None}

    @staticmethod
    def _frame_token(frame: str | None) -> str:
        text = str(frame or "SCALAR").upper()
        if text == "SCALAR" or text.startswith("SCALAR_"): return "SCALAR"
        if "LOCAL_ENU" in text or text == "ENU": return "ENU"
        if text == "NED" or "_NED" in text: return "NED"
        if text == "FRD" or "_FRD" in text: return "FRD"
        if text == "FLU" or "_FLU" in text: return "FLU"
        if "WGS84" in text: return "WGS84"
        if "ECEF" in text: return "ECEF"
        if "MSL" in text: return "MSL"
        if "AGL" in text: return "AGL"
        if text in {"MAP", "BODY", "CONTEXT_DEPENDENT"}: return text
        return text

    def _resolve_context_frame(self, token: str, which: str, ctx: Mapping[str, Any]) -> str:
        if token not in {"CONTEXT_DEPENDENT", "BODY", "MAP"}: return token
        if token == "BODY":
            keys = (f"{which}_body_frame", "body_frame", f"{which}_frame_resolved", "context_frame")
        elif token == "MAP":
            keys = (f"{which}_map_frame", "map_frame", f"{which}_frame_resolved", "context_frame")
        else:
            keys = (f"{which}_frame_resolved", f"resolved_{which}_frame", "context_frame")
        for k in keys:
            if ctx.get(k): return self._frame_token(str(ctx[k]))
        raise ValueError(f"{token} {which} frame requires an explicit resolver in context")

    def transform_vector(self, vector: Sequence[float], source_frame: str, target_frame: str, *, covariance: Sequence[Sequence[float]] | None = None,
                         context: Mapping[str, Any] | None = None) -> tuple[list[float], list[list[float]] | None, dict[str, Any]]:
        ctx = dict(context or {})
        sf0, tf0 = self._frame_token(source_frame), self._frame_token(target_frame)
        sf = self._resolve_context_frame(sf0, "source", ctx) if sf0 in {"CONTEXT_DEPENDENT","BODY","MAP"} and sf0 != tf0 else sf0
        tf = self._resolve_context_frame(tf0, "target", ctx) if tf0 in {"CONTEXT_DEPENDENT","BODY","MAP"} and sf0 != tf0 else tf0
        v = [float(x) for x in vector]; P = None if covariance is None else [[float(x) for x in row] for row in covariance]
        rec: dict[str, Any] = {"kind": "frame", "source_frame": sf0, "target_frame": tf0, "resolved_source_frame": sf, "resolved_target_frame": tf}
        try:
            if sf == tf:
                out, cov = v, P; rec["adapter"] = "identity_same_frame"
            elif {sf, tf} == {"ENU", "NED"}:
                out = self.enu_to_ned(v) if sf == "ENU" else self.ned_to_enu(v)
                cov = self.covariance_enu_ned(P) if P is not None else None; rec["adapter"] = "tdoc.adapters.enu_to_ned/ned_to_enu"
            elif {sf, tf} == {"FRD", "FLU"}:
                out = self.frd_to_flu(v) if sf == "FRD" else self.flu_to_frd(v)
                cov = self.covariance_frd_flu(P) if P is not None else None; rec["adapter"] = "tdoc.adapters.frd_to_flu/flu_to_frd"
            elif sf == "WGS84" and tf == "ECEF":
                f = lambda x: self.av.wgs84_to_ecef(math.radians(x[0]), math.radians(x[1]), x[2])
                out = list(f(v)); cov = None if P is None else [list(r) for r in self.av.propagate_covariance_numeric(f, v, P)]
                rec["adapter"] = "tdoc.adapters.wgs84_to_ecef+propagate_covariance_numeric"
            elif sf == "ECEF" and tf == "WGS84":
                def f(x):
                    lat, lon, h = self.av.ecef_to_wgs84(*x); return (math.degrees(lat), math.degrees(lon), h)
                out = list(f(v)); cov = None if P is None else [list(r) for r in self.av.propagate_covariance_numeric(f, v, P)]
                rec["adapter"] = "tdoc.adapters.ecef_to_wgs84+propagate_covariance_numeric"
            else:
                raise ValueError(f"unsupported vector frame conversion {sf}->{tf}")
            return out, cov, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def transform_crs(self, point: Sequence[float], *, source_crs: str, target_crs: str,
                      covariance: Sequence[Sequence[float]] | None = None) -> tuple[list[float], list[list[float]] | None, dict[str, Any]]:
        rec = {"kind": "crs", "source_crs": source_crs, "target_crs": target_crs, "adapter": "tdoc.adapters.transform_crs"}
        try:
            p = [float(x) for x in point]
            def f(x):
                return self.av.transform_crs(x[0], x[1], x[2] if len(x) > 2 else None, source_crs=source_crs, target_crs=target_crs)
            out = list(f(p)); cov = None if covariance is None else [list(r) for r in self.av.propagate_covariance_numeric(f, p, covariance)]
            return out, cov, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def local_ned_to_wgs84(self, ned_m: Sequence[float], *, origin_lat_deg: float, origin_lon_deg: float, origin_h_m: float,
                           covariance_ned: Sequence[Sequence[float]] | None = None) -> tuple[list[float], dict[str, Any]]:
        rec = {"kind": "frame", "source_frame": "NED", "target_frame": "WGS84", "origin_lat_deg": float(origin_lat_deg),
               "origin_lon_deg": float(origin_lon_deg), "origin_h_m": float(origin_h_m)}
        try:
            def f(x):
                enu = self.ned_to_enu(x)
                ecef = self.av.local_enu_to_ecef(tuple(enu), origin_lat_rad=math.radians(float(origin_lat_deg)),
                                                 origin_lon_rad=math.radians(float(origin_lon_deg)), origin_h_m=float(origin_h_m))
                lat, lon, h = self.av.ecef_to_wgs84(*ecef)
                return (math.degrees(lat), math.degrees(lon), float(h))
            out = list(f(tuple(float(v) for v in ned_m)))
            cov = None if covariance_ned is None else [list(r) for r in self.av.propagate_covariance_numeric(f, ned_m, covariance_ned)]
            rec["adapter"] = "tdoc.adapters.ned_to_enu+local_enu_to_ecef+ecef_to_wgs84"; rec["covariance_wgs84"] = cov
            return out, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def pressure_to_log_ratio(self, p_pa: float, p_ref: float = 101325.0) -> float:
        return float(self.av.pressure_to_log_ratio(float(p_pa), p_ref=float(p_ref)))

    def log_ratio_to_pressure(self, value: float, p_ref: float = 101325.0) -> float:
        return float(self.av.log_ratio_to_pressure(float(value), p_ref=float(p_ref)))

    def msl_to_agl(self, h_msl: float, terrain_msl: float) -> float:
        return float(self.av.msl_to_agl(float(h_msl), float(terrain_msl)))

    def agl_to_msl(self, h_agl: float, terrain_msl: float) -> float:
        return float(self.av.agl_to_msl(float(h_agl), float(terrain_msl)))

    def transform_vertical(self, value_m: float, source_reference: str, target_reference: str, *, context: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
        src, dst = str(source_reference).upper(), str(target_reference).upper(); ctx = dict(context)
        rec = {"kind": "vertical_reference", "source_reference": src, "target_reference": dst}
        try:
            x = float(value_m)
            if src == dst: out = x; rec["adapter"] = "identity_same_vertical_reference"
            elif {src, dst} == {"MSL", "AGL"}:
                terrain = ctx.get("terrain_msl_m", ctx.get("ground_elevation_m"))
                if terrain is None: raise ValueError("terrain_msl_m required for MSL<->AGL")
                out = self.msl_to_agl(x, float(terrain)) if src == "MSL" else self.agl_to_msl(x, float(terrain)); rec["adapter"] = "tdoc.adapters.msl_to_agl/agl_to_msl"
            elif {src, dst} == {"ELLIPSOIDAL", "ORTHOMETRIC"}:
                N = ctx.get("geoid_undulation_m")
                if N is None: raise ValueError("geoid_undulation_m required for ellipsoidal<->orthometric")
                out = float(self.av.ellipsoidal_to_orthometric(x, float(N))) if src == "ELLIPSOIDAL" else float(self.av.orthometric_to_ellipsoidal(x, float(N))); rec["adapter"] = "tdoc.adapters.ellipsoidal_to_orthometric/orthometric_to_ellipsoidal"
            elif src == "NED_DOWN" and dst in {"ALTITUDE", "MSL"}:
                origin = ctx.get("origin_altitude_m", ctx.get("origin_msl_m"));
                if origin is None: raise ValueError("origin_altitude_m required for NED Down->altitude")
                out = float(self.av.ned_down_to_altitude(x, float(origin))); rec["adapter"] = "tdoc.adapters.ned_down_to_altitude"
            elif dst == "NED_DOWN" and src in {"ALTITUDE", "MSL"}:
                origin = ctx.get("origin_altitude_m", ctx.get("origin_msl_m"));
                if origin is None: raise ValueError("origin_altitude_m required for altitude->NED Down")
                out = float(self.av.altitude_to_ned_down(x, float(origin))); rec["adapter"] = "tdoc.adapters.altitude_to_ned_down"
            else: raise ValueError(f"unsupported vertical reference conversion {src}->{dst}")
            return out, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def transform_heading(self, value_rad: float, *, source_convention: str, target_convention: str, context: Mapping[str, Any] | None = None) -> tuple[float, dict[str, Any]]:
        ctx = dict(context or {}); src, dst = source_convention.upper(), target_convention.upper(); rec = {"kind":"heading", "source_convention":src, "target_convention":dst}
        try:
            x = float(value_rad)
            if src == "TRUE_NAV": true_nav = x
            elif src == "TRUE_ENU": true_nav = float(self.av.heading_enu_to_nav(x))
            elif src == "MAGNETIC_NAV": true_nav = float(self.av.magnetic_to_true(x, float(ctx["magnetic_variation_rad"])))
            elif src == "GRID_NAV": true_nav = float(self.av.grid_to_true(x, grid_convergence_rad=float(ctx["grid_convergence_rad"])))
            else: raise ValueError(f"unsupported heading convention {src}")
            if dst == "TRUE_NAV": out = true_nav
            elif dst == "TRUE_ENU": out = float(self.av.heading_nav_to_enu(true_nav))
            elif dst == "MAGNETIC_NAV": out = float(self.av.true_to_magnetic(true_nav, float(ctx["magnetic_variation_rad"])))
            elif dst == "GRID_NAV": out = float(self.av.true_to_grid(true_nav, grid_convergence_rad=float(ctx["grid_convergence_rad"])))
            else: raise ValueError(f"unsupported heading convention {dst}")
            rec["adapter"] = "tdoc.adapters HeadingConventionAdapter"
            return out, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def wind_from_to_vector(self, speed_m_s: float, from_rad: float, *, vertical_m_s: float = 0.0, target_frame: str = "ENU",
                            covariance_enu: Sequence[Sequence[float]] | None = None) -> tuple[list[float], list[list[float]] | None, dict[str, Any]]:
        rec = {"kind":"wind_convention", "source":"meteorological_FROM_north_clockwise", "target_frame":target_frame}
        try:
            enu = list(self.av.wind_from_to_vector(float(speed_m_s), float(from_rad), float(vertical_m_s)))
            out, cov, frame_rec = self.transform_vector(enu, "ENU", target_frame, covariance=covariance_enu)
            rec["adapter"] = "tdoc.adapters.wind_from_to_vector + " + str(frame_rec.get("adapter")); rec["frame_record"] = frame_rec
            return out, cov, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def transform_airdata(self, value: float, *, source_type: str, target_type: str, context: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
        src, dst = source_type.upper(), target_type.upper(); ctx = dict(context); rec = {"kind":"air_data", "source_type":src, "target_type":dst}
        try:
            x = float(value)
            if src == "TAS": tas = x
            elif src == "MACH": tas = float(self.av.mach_to_tas(x, speed_of_sound=ctx.get("speed_of_sound_m_s"), temperature_k=ctx.get("temperature_k")))
            elif src == "CAS": tas = float(self.av.cas_to_tas(x, static_pressure_pa=ctx.get("static_pressure_pa"), temperature_k=ctx.get("temperature_k"), density=ctx.get("density_kg_m3")))
            elif src == "EAS": tas = float(self.av.eas_to_tas(x, float(ctx["density_kg_m3"])))
            elif src == "IAS":
                cas = float(self.av.ias_to_cas(x, calibration=ctx.get("ias_calibration"))); tas = float(self.av.cas_to_tas(cas, static_pressure_pa=ctx.get("static_pressure_pa"), temperature_k=ctx.get("temperature_k"), density=ctx.get("density_kg_m3")))
            else: raise ValueError(f"unsupported air-data source {src}")
            if dst == "TAS": out = tas
            elif dst == "MACH": out = float(self.av.tas_to_mach(tas, speed_of_sound=ctx.get("speed_of_sound_m_s"), temperature_k=ctx.get("temperature_k")))
            elif dst == "CAS": out = float(self.av.tas_to_cas(tas, static_pressure_pa=ctx.get("static_pressure_pa"), temperature_k=ctx.get("temperature_k"), density=ctx.get("density_kg_m3")))
            elif dst == "EAS": out = float(self.av.tas_to_eas(tas, float(ctx["density_kg_m3"])))
            elif dst == "IAS":
                cas = float(self.av.tas_to_cas(tas, static_pressure_pa=ctx.get("static_pressure_pa"), temperature_k=ctx.get("temperature_k"), density=ctx.get("density_kg_m3"))); out = float(self.av.cas_to_ias(cas, calibration=ctx.get("ias_calibration")))
            else: raise ValueError(f"unsupported air-data target {dst}")
            rec["adapter"] = "tdoc.adapters AirDataAdapter"
            return out, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def transform_quaternion(self, quaternion: Sequence[float], *, source_metadata: Mapping[str, Any], target_metadata: Mapping[str, Any],
                             covariance: Sequence[Sequence[float]] | None = None) -> tuple[list[float], list[list[float]] | None, dict[str, Any]]:
        rec = {"kind":"quaternion", "source_metadata":dict(source_metadata), "target_metadata":dict(target_metadata)}
        try:
            q = list(self.av.convert_quaternion(tuple(float(v) for v in quaternion), source_metadata=dict(source_metadata), target_metadata=dict(target_metadata)))
            cov = None if covariance is None else [list(r) for r in self.av.quaternion_covariance(covariance, quaternion, source_metadata=dict(source_metadata), target_metadata=dict(target_metadata))]
            rec["adapter"] = "tdoc.adapters.convert_quaternion/quaternion_covariance"
            return q, cov, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def transform_time(self, value: Any, *, source_reference: str, target_reference: str, context: Mapping[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        ctx = dict(context or {}); src, dst = source_reference.upper(), target_reference.upper(); rec = {"kind":"time_scale", "source_reference":src, "target_reference":dst}
        try:
            if src == "UTC": utc = value
            elif src == "TAI": utc = self.av.tai_seconds_to_utc(float(value))
            elif src == "GPS_SECONDS":
                week, tow = divmod(float(value), 604800.0); utc = self.av.gps_week_tow_to_utc(int(week), tow)
            elif src in {"MISSION_ELAPSED", "FMI_TIME"}: utc = self.av.elapsed_to_common_time(float(value), epoch=ctx.get("epoch"))
            else: raise ValueError(f"unsupported time source {src}")
            if dst == "UTC": out = utc.isoformat() if hasattr(utc, "isoformat") else str(utc)
            elif dst == "TAI": out = float(self.av.utc_to_tai_seconds(utc))
            elif dst == "GPS_SECONDS": out = float(self.av.utc_to_gps_seconds(utc))
            elif dst == "GPS_WEEK_TOW": out = tuple(self.av.utc_to_gps_week_tow(utc))
            elif dst in {"MISSION_ELAPSED", "FMI_TIME"}: out = float(self.av.common_time_to_elapsed(utc, epoch=ctx.get("epoch")))
            else: raise ValueError(f"unsupported time target {dst}")
            rec["adapter"] = "tdoc.adapters TimeBaseAdapter"
            return out, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def normalize_specialized(self, envelope: Mapping[str, Any], *, target_metadata: Mapping[str, Any], context: Mapping[str, Any] | None = None,
                              external: bool = True) -> tuple[Any, dict[str, Any]]:
        src = self.validate_envelope(envelope, external=external); tm = dict(target_metadata or {}); ctx = dict(context or {})
        kind = str(tm.get("adapter_kind") or src.get("adapter_kind") or "").lower()
        if not kind: raise ValueError("specialized TDOC route requires adapter_kind")
        rec: dict[str, Any]; value: Any; uncertainty = src.get("uncertainty"); covariance = src.get("covariance")
        if kind == "heading":
            su=str(src.get("unit") or "rad"); tu=str(tm.get("unit") or "rad")
            if uncertainty is None: x=float(self.convert(float(src["value"]),su,"rad")); u_rad=None
            else:
                cres=self.converter.convert(float(src["value"]),su,"rad",uncertainty=float(uncertainty)); x=float(cres.value); u_rad=float(cres.uncertainty)
            value, rec = self.transform_heading(x, source_convention=str(src.get("heading_convention")), target_convention=str(tm.get("heading_convention")), context=ctx)
            uncertainty=u_rad
            if tu != "rad":
                if uncertainty is None: value=float(self.convert(value,"rad",tu))
                else:
                    cres=self.converter.convert(value,"rad",tu,uncertainty=uncertainty); value=float(cres.value); uncertainty=float(cres.uncertainty)
        elif kind == "air_data":
            st, tt = str(src.get("air_data_type")), str(tm.get("air_data_type")); su = str(src.get("unit") or ("1" if st.upper()=="MACH" else "m/s")); canonical = "1" if st.upper()=="MACH" else "m/s"
            if uncertainty is None: x=float(self.convert(float(src["value"]),su,canonical)) if su!=canonical else float(src["value"]); u_can=None
            else:
                if su!=canonical:
                    cres=self.converter.convert(float(src["value"]),su,canonical,uncertainty=float(uncertainty)); x=float(cres.value); u_can=float(cres.uncertainty)
                else: x=float(src["value"]); u_can=float(uncertainty)
            value, rec = self.transform_airdata(x, source_type=st, target_type=tt, context=ctx)
            uncertainty=None if u_can is None else float(self.av.propagate_uncertainty_numeric(lambda z:self.transform_airdata(z,source_type=st,target_type=tt,context=ctx)[0],x,u_can))
            out_u = "1" if tt.upper()=="MACH" else "m/s"; tu = str(tm.get("unit") or out_u)
            if out_u != tu:
                if uncertainty is None: value=float(self.convert(value,out_u,tu))
                else:
                    cres=self.converter.convert(value,out_u,tu,uncertainty=uncertainty); value=float(cres.value); uncertainty=float(cres.uncertainty)
        elif kind == "quaternion":
            value, covariance, rec = self.transform_quaternion(src["value"], source_metadata=dict(src.get("quaternion_metadata") or {}), target_metadata=dict(tm.get("quaternion_metadata") or {}), covariance=covariance)
        elif kind == "time":
            value, rec = self.transform_time(src["value"], source_reference=str(src.get("time_reference")), target_reference=str(tm.get("time_reference")), context=ctx)
        elif kind in {"crs", "datum"}:
            source_crs = str(src.get("crs") or src.get("horizontal_datum")); target_crs = str(tm.get("crs") or tm.get("horizontal_datum")); value, covariance, rec = self.transform_crs(src["value"], source_crs=source_crs, target_crs=target_crs, covariance=covariance)
        elif kind == "vertical":
            su=str(src.get("unit") or "m"); tu=str(tm.get("unit") or "m")
            if uncertainty is None: x=float(self.convert(float(src["value"]),su,"m")); u_m=None
            else:
                cres=self.converter.convert(float(src["value"]),su,"m",uncertainty=float(uncertainty)); x=float(cres.value); u_m=float(cres.uncertainty)
            value, rec = self.transform_vertical(x, str(src.get("vertical_reference") or src.get("frame")), str(tm.get("vertical_reference") or tm.get("frame")), context=ctx); uncertainty=u_m
            if tu != "m":
                if uncertainty is None: value=float(self.convert(value,"m",tu))
                else:
                    cres=self.converter.convert(value,"m",tu,uncertainty=uncertainty); value=float(cres.value); uncertainty=float(cres.uncertainty)
        elif kind == "wind_from":
            payload = src["value"]
            if not isinstance(payload, Mapping): raise ValueError("wind_from adapter requires value={speed,direction,vertical?}")
            speed = float(self.convert(float(payload["speed"]), str(src.get("speed_unit") or "m/s"), "m/s")); direction = float(self.convert(float(payload["direction"]), str(src.get("direction_unit") or "rad"), "rad")); vertical = float(self.convert(float(payload.get("vertical",0.0)), str(src.get("vertical_unit") or "m/s"), "m/s"))
            value, covariance, rec = self.wind_from_to_vector(speed, direction, vertical_m_s=vertical, target_frame=str(tm.get("frame") or "ENU"), covariance_enu=covariance)
        elif kind == "pressure_reference":
            x = float(self.convert(float(src["value"]), str(src.get("unit") or "Pa"), "Pa")); sr, tr = str(src.get("pressure_reference")), str(tm.get("pressure_reference")); elevation = ctx.get("elevation_m",ctx.get("terrain_msl_m")); value = float(self.av.pressure_reference(x,source=sr.upper(),target=tr.upper(),elevation_m=elevation)); rec=self._event({"kind":"pressure_reference","source":sr,"target":tr,"adapter":"tdoc.adapters.pressure_reference"},True); tu=str(tm.get("unit") or "Pa"); value=float(self.convert(value,"Pa",tu)) if tu!="Pa" else value
        else: raise ValueError(f"unsupported TDOC specialized adapter_kind={kind}")
        out = dict(src); out.update({"value":value,"unit":tm.get("unit",src.get("unit")),"frame":tm.get("frame",src.get("frame")),"semantic":tm.get("semantic",src.get("semantic")),"uncertainty":uncertainty})
        if covariance is not None: out["covariance"] = covariance
        out["provenance"]={**dict(out.get("provenance") or {}),"tdoc":rec,"adapter_kind":kind,"normalization_status":"canonical"}
        return value,out

    def convert_scalar(self, value: Any, source_unit: str | None, target_unit: str | None, *, source_frame: str | None = None,
                       target_frame: str | None = None, source_semantic: str | None = None, target_semantic: str | None = None,
                       uncertainty: Any = None, context: Mapping[str, Any] | None = None, allow_semantic_relabel: bool = False,
                       semantic_provenance: Mapping[str, Any] | None = None) -> tuple[Any, Any, dict[str, Any]]:
        ctx = dict(context or {}); su, tu = source_unit or "1", target_unit or source_unit or "1"; sf, tf = self._frame_token(source_frame), self._frame_token(target_frame)
        rec: dict[str, Any] = {"kind":"scalar", "source_unit":su, "target_unit":tu, "source_frame":sf, "target_frame":tf, "source_semantic":source_semantic, "target_semantic":target_semantic}
        try:
            x, u = value, uncertainty; ss = str(source_semantic or "").lower(); ts = str(target_semantic or "").lower()
            if "log_pressure" in ts and "log_pressure" not in ss:
                p_pa = float(self.convert(float(x), su, "Pa")); p_ref = float(ctx.get("pressure_reference_pa", 101325.0))
                if u is not None: u = float(self.av.pressure_log_uncertainty(p_pa, float(self.convert(float(u), su, "Pa"))))
                x = self.av.pressure_to_log_ratio(p_pa, p_ref=p_ref); su = tu = "1"; rec["semantic_adapter"] = "tdoc.adapters.pressure_to_log_ratio"
            elif "log_pressure" in ss and "log_pressure" not in ts:
                p_ref = float(ctx.get("pressure_reference_pa", 101325.0)); x = self.av.log_ratio_to_pressure(float(x), p_ref=p_ref)
                if u is not None: u = float(self.av.log_pressure_uncertainty(float(value), float(u), p_ref=p_ref))
                su = "Pa"; rec["semantic_adapter"] = "tdoc.adapters.log_ratio_to_pressure"
            elif ss in {"qnh","qfe","qne"} and ts in {"qnh","qfe","qne"} and ss != ts:
                elevation = ctx.get("elevation_m", ctx.get("terrain_msl_m")); x = self.av.pressure_reference(float(x), source=ss.upper(), target=ts.upper(), elevation_m=elevation); rec["semantic_adapter"] = "tdoc.adapters.pressure_reference"
            if su != tu:
                if u is None: x = self.convert(x, su, tu)
                else:
                    cres = self.converter.convert(x, su, tu, uncertainty=u); x, u = cres.value, cres.uncertainty
                rec["unit_adapter"] = "tdoc.UnitConverter"
            else: rec["unit_adapter"] = "identity_same_unit"
            if sf != tf:
                if sf in {"CONTEXT_DEPENDENT","BODY","MAP"} or tf in {"CONTEXT_DEPENDENT","BODY","MAP"}:
                    sf = self._resolve_context_frame(sf, "source", ctx); tf = self._resolve_context_frame(tf, "target", ctx)
                if sf != tf:
                    if {sf, tf} == {"MSL", "AGL"}:
                        x, vrec = self.transform_vertical(float(x), sf, tf, context=ctx); rec["frame_adapter"] = vrec["adapter"]
                    else: raise ValueError(f"scalar cannot be safely transformed {sf}->{tf}; vector/specialized adapter required")
                else: rec["frame_adapter"] = "identity_resolved_same_frame"
            else: rec["frame_adapter"] = "identity_same_frame"
            sem = self.semantic_check(source_semantic, target_semantic, provenance=semantic_provenance or rec, allow_relabel=allow_semantic_relabel); rec["semantic_valid"] = sem["valid"]
            if self.strict_semantics and not sem["valid"]: raise ValueError(f"semantic mismatch {source_semantic!r}->{target_semantic!r} without explicit mapping provenance")
            return x, u, self._event(rec, True)
        except Exception as exc:
            rec["error"] = str(exc); self._event(rec, False); raise

    def normalize_envelope(self, envelope: Mapping[str, Any] | Any, *, target_unit: str, target_frame: str, target_semantic: str | None = None,
                           context: Mapping[str, Any] | None = None, external: bool = True, allow_semantic_relabel: bool = False,
                           semantic_provenance: Mapping[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        if not isinstance(envelope, Mapping) or "value" not in envelope:
            if external and bool(self.config.runtime.get("require_contract_normalization", True)):
                raise ValueError("bare cross-domain value rejected: provide {value, unit, frame, semantic}")
            src = {"value": envelope, "unit": target_unit, "frame": target_frame, "semantic": target_semantic, "source":"trusted_internal"}
        else: src = self.validate_envelope(envelope, external=external)
        value, uncertainty, rec = self.convert_scalar(src["value"], src.get("unit"), target_unit, source_frame=src.get("frame"), target_frame=target_frame,
            source_semantic=src.get("semantic"), target_semantic=target_semantic, uncertainty=src.get("uncertainty"), context=context,
            allow_semantic_relabel=allow_semantic_relabel, semantic_provenance=semantic_provenance or src.get("provenance"))
        out = dict(src); out.update({"value":value, "unit":target_unit, "frame":target_frame, "semantic":target_semantic, "uncertainty":uncertainty})
        out["provenance"] = {**dict(out.get("provenance") or {}), "tdoc":rec, "normalization_status":"canonical"}
        return value, out

    def climate_wind_to_mesa_ned(self, state: Mapping[str, Any], covariance_enu: Sequence[Sequence[float]] | None = None) -> tuple[list[float] | None, dict[str, Any]]:
        names = ("wind_u_3dw", "wind_v_3dw", "diagnostic_vertical_wind")
        if not all(name in state for name in names): return None, {"applied":False, "reason":"missing_3dw_components"}
        try:
            enu = [float(state[n]) for n in names]
            if not all(math.isfinite(x) for x in enu): raise ValueError("non-finite wind")
            ned, cov, rec = self.transform_vector(enu, "ENU", "NED", covariance=covariance_enu)
            return ned, {"applied":True, "source_frame":"ENU", "target_frame":"NED", "source_components":list(names), "covariance_ned":cov,
                         "adapter":rec.get("adapter"), "tdoc_record":rec}
        except Exception as exc:
            return None, {"applied":False, "reason":str(exc)}
