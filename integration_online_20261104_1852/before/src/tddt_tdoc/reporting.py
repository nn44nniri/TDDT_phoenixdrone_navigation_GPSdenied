from __future__ import annotations

import csv
import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from .config import TDOCConfig
from .storage import read_json_auto, iter_jsonl_auto
from .participation import aggregate_mpc_participation
from .vehicle_state import phoenix_state_from_ptlib_snapshot


STATE_FIELDS = [
    "mesa_position_n_m", "mesa_position_e_m", "mesa_position_d_m",
    "vg_n_m_s", "vg_e_m_s", "vg_d_m_s",
    "va_n_m_s", "va_e_m_s", "va_d_m_s",
    "airspeed_m_s", "flightpath_angle_rad", "air_heading_rad",
    "wind_n_m_s", "wind_e_m_s", "wind_d_m_s",
    "temperature_c", "relative_humidity_percent", "pressure_pa", "air_density_kg_m3",
]


@dataclass
class ReportArtifacts:
    output_dir: Path
    report_2d_html: Path
    report_3d_html: Path
    summary_json: Path
    path_csv: Path
    parameters_csv: Path

    def to_dict(self) -> dict[str, str]:
        return {k: str(v.resolve()) for k, v in self.__dict__.items() if isinstance(v, Path)}


def _finite(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _unwrap(v: Any) -> Any:
    return v.get("value") if isinstance(v, Mapping) and "value" in v else v


def _short(v: Any, limit: int = 320) -> str:
    if isinstance(v, (dict, list, tuple)):
        s = json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        s = str(v)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _flatten(obj: Any, prefix: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(obj, Mapping):
        # Preserve signal-envelope metadata in dedicated columns.
        if "value" in obj and any(k in obj for k in ("unit", "frame", "semantic")):
            rows.append(
                {
                    "path": prefix,
                    "value": _short(obj.get("value"), 1000),
                    "unit": obj.get("unit"),
                    "frame": obj.get("frame"),
                    "semantic": obj.get("semantic"),
                    "source": obj.get("source"),
                }
            )
            for k, v in obj.items():
                if k in {"value", "unit", "frame", "semantic", "source"}:
                    continue
                rows.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
            return rows
        for k, v in obj.items():
            rows.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return rows
    if isinstance(obj, (list, tuple)):
        # Large matrices are kept as one value to avoid producing tens of thousands of table rows.
        if len(obj) > 24 or any(isinstance(x, (list, tuple, dict)) for x in obj):
            rows.append({"path": prefix, "value": _short(obj, 3000), "unit": None, "frame": None, "semantic": None, "source": None})
        else:
            for i, v in enumerate(obj):
                rows.extend(_flatten(v, f"{prefix}[{i}]"))
        return rows
    rows.append({"path": prefix, "value": _short(obj, 1000), "unit": None, "frame": None, "semantic": None, "source": None})
    return rows


class TDOCReportGenerator:
    """Read-only TDOC report generator.

    The generator consumes TDOC runtime artifacts. It never invokes Layer 2, 3, or 4,
    so report generation cannot alter orchestration state or violate trigger ownership.
    """

    def __init__(self, config: TDOCConfig):
        self.config = config
        self.package_root = config.package_root
        self.runtime_root = config.runtime_root

    def _cycle_paths(self) -> list[Path]:
        root=self.runtime_root / "cycles"
        paths=list(root.glob("*.json.gz"))+list(root.glob("*.json"))
        return sorted(paths, key=lambda p: p.stat().st_mtime)

    def load_cycles(self, cycle: str | Path | None = None, transaction_prefix: str | None = None, transaction_ids: set[str] | list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        if cycle:
            p = Path(cycle)
            if not p.is_absolute():
                p = (self.package_root / p).resolve()
            if not p.is_file():
                raise FileNotFoundError(f"cycle file not found: {p}")
            return [read_json_auto(p)]
        paths = self._cycle_paths()
        if transaction_prefix:
            paths=[p for p in paths if p.name.startswith(str(transaction_prefix))]
        if transaction_ids is not None:
            wanted={str(x) for x in transaction_ids}
            def txname(p:Path)->str:
                n=p.name
                if n.endswith('.json.gz'):return n[:-8]
                if n.endswith('.json'):return n[:-5]
                return p.stem
            paths=[p for p in paths if txname(p) in wanted]
        if not paths:
            suffix=f" matching transaction_prefix={transaction_prefix!r}" if transaction_prefix else (" matching explicit transaction ids" if transaction_ids is not None else "")
            raise FileNotFoundError(f"no TDOC cycle JSON/JSON.GZ files under {self.runtime_root / 'cycles'}{suffix}")
        return [read_json_auto(p) for p in paths]

    @staticmethod
    def _latest_ok(cycles: list[dict[str, Any]]) -> dict[str, Any]:
        for c in reversed(cycles):
            if c.get("status") == "OK":
                return c
        return cycles[-1]

    def _invocation_events(self, transaction_ids: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not transaction_ids:
            return out
        chunk_root=self.runtime_root / "events" / "invocations" / "chunks"
        paths=sorted(chunk_root.glob("chunk_*.jsonl.gz")) if chunk_root.is_dir() else []
        legacy=self.runtime_root / "events" / "invocations.jsonl"
        if not paths and legacy.is_file(): paths=[legacy]
        for p in paths:
            try:
                records=iter_jsonl_auto(p)
                for e in records:
                    if str(e.get("transaction_id")) in transaction_ids: out.append(e)
            except Exception:
                continue
        return out

    @staticmethod
    def _layer2_summary(cycle: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for name, p in (cycle.get("layer2_packets") or {}).items():
            unc = p.get("uncertainty") or {}
            if isinstance(unc.get("by_output"), Mapping):
                u = float(np.mean([abs(float(x)) for x in unc["by_output"].values()] or [0.0]))
            elif isinstance(unc.get("std"), Mapping):
                u = float(np.mean([abs(float(x)) for x in unc["std"].values()] or [0.0]))
            else:
                u = _finite(unc.get("normalized_uncertainty"), 0.0) or 0.0
            q = p.get("quality") or {}
            v = p.get("validity") or {}
            r = p.get("reliability") or {}
            rows.append(
                {
                    "twin": name,
                    "quality": _finite(q.get("score"), 0.0) or 0.0,
                    "validity": _finite(v.get("score"), 0.0) or 0.0,
                    "reliability": _finite(r.get("posterior", r.get("prior")), 0.0) or 0.0,
                    "uncertainty": u,
                    "operation_code": p.get("operation_code") or (p.get("context") or {}).get("operation_code"),
                    "state_count": len(p.get("state") or {}),
                }
            )
        return rows

    @staticmethod
    def _layer4_candidates(cycle: Mapping[str, Any]) -> list[dict[str, Any]]:
        return list((cycle.get("layer4_result") or {}).get("candidates") or [])

    @staticmethod
    def _best_candidate(cycle: Mapping[str, Any]) -> dict[str, Any] | None:
        l4 = cycle.get("layer4_result") or {}
        front = l4.get("pareto_front") or []
        if front:
            # Prefer robust feasible, then scalar/robust score.
            return sorted(front, key=lambda c: (not bool(c.get("robust_feasible", True)), _finite(c.get("robust_score", c.get("scalar_cost")), 1e30)))[0]
        cands = l4.get("candidates") or []
        if not cands:
            return None
        return sorted(cands, key=lambda c: (not bool(c.get("feasible")), _finite(c.get("scalar_cost"), 1e30)))[0]

    def _region_definition(self) -> dict[str, Any] | None:
        boulder = self.config.path_value("TDDT_BOULDER_ROOT")
        map_root = self.config.path_value("TDDT_MAP_ROOT")
        climate = self.config.path_value("TDDT_CLIMATE_ROOT")
        choices = []
        if boulder:
            choices.append(boulder / "region" / "region_v1.geojson")
        if map_root:
            choices.extend([map_root.parent.parent / "region" / "region_v1.geojson", map_root.parent / "region" / "region_v1.geojson"])
        if climate:
            choices.append(climate / "region" / "region_v1.geojson")
        for p in choices:
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                roles = {str((f.get("properties") or {}).get("feature_role", "")).lower(): f for f in d.get("features") or []}
                if "start" in roles and "destination" in roles:
                    boundary = roles.get("region_boundary")
                    props = (boundary or {}).get("properties") or {}
                    return {
                        "path": str(p.resolve()),
                        "data": d,
                        "roles": roles,
                        "processing_crs": str(props.get("processing_crs") or "EPSG:32613"),
                    }
        return None

    @staticmethod
    def _lonlat(feature: Mapping[str, Any]) -> tuple[float, float]:
        c = ((feature.get("geometry") or {}).get("coordinates") or [None, None])
        return float(c[0]), float(c[1])

    def _project(self, lon: float, lat: float, crs: str) -> tuple[float, float]:
        from pyproj import Transformer
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
        return float(x), float(y)

    def _reference_altitude(self, cycle: Mapping[str, Any], start_lon: float, start_lat: float) -> float:
        # Prefer explicit geodetic origin from Layer 3 if it exists.
        ctx = ((cycle.get("layer3_result") or {}).get("snapshot") or {}).get("context") or {}
        geo = ctx.get("geodetic_origin") or {}
        alt = _unwrap((geo.get("altitude_msl_m") if isinstance(geo, Mapping) else None))
        if _finite(alt) is not None:
            return float(alt)
        # The distributed example is a useful fallback for a single-cycle demo.
        req = self.package_root / "examples" / "runtime_request.json"
        if req.is_file():
            try:
                d = json.loads(req.read_text(encoding="utf-8"))
                h = _unwrap(((d.get("wing_inputs") or {}).get("state_h")))
                if _finite(h) is not None:
                    return float(h)
            except Exception:
                pass
        # Last fallback: terrain + 100 m AGL.
        terrain = self._sample_terrain(start_lon, start_lat)
        return float((terrain if terrain is not None else 1700.0) + 100.0)

    def _sample_terrain(self, lon: float, lat: float) -> float | None:
        raster = self._terrain_raster()
        if not raster:
            return None
        import rasterio
        from pyproj import Transformer
        with rasterio.open(raster) as ds:
            x, y = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform(lon, lat)
            z = float(next(ds.sample([(x, y)]))[0])
            if (ds.nodata is not None and math.isclose(z, float(ds.nodata))) or not math.isfinite(z):
                return None
            return z

    def _terrain_raster(self) -> Path | None:
        map_root = self.config.path_value("TDDT_MAP_ROOT")
        boulder = self.config.path_value("TDDT_BOULDER_ROOT")
        choices = []
        if map_root:
            choices.append(map_root / "terrain_obstacles.tif")
        if boulder:
            choices.extend([boulder / "outputs" / "region_v1_map" / "terrain_obstacles.tif", boulder / "terrain_obstacles.tif"])
        for p in choices:
            if p.is_file():
                return p
        return None

    def _terrain_surface(self, region: Mapping[str, Any], grid_n: int) -> dict[str, Any] | None:
        raster = self._terrain_raster()
        if not raster:
            return None
        import rasterio
        from pyproj import Transformer
        from rasterio.windows import from_bounds
        roles = region["roles"]
        boundary = roles.get("region_boundary")
        ring = ((((boundary or {}).get("geometry") or {}).get("coordinates") or [[]])[0])
        with rasterio.open(raster) as ds:
            tr = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            projected = [tr.transform(float(lon), float(lat)) for lon, lat in ring]
            xs, ys = [p[0] for p in projected], [p[1] for p in projected]
            if not xs or not ys:
                return None
            win = from_bounds(min(xs), min(ys), max(xs), max(ys), ds.transform).round_offsets().round_lengths()
            win = win.intersection(rasterio.windows.Window(0, 0, ds.width, ds.height))
            z = ds.read(1, window=win, out_shape=(int(grid_n), int(grid_n)), resampling=rasterio.enums.Resampling.bilinear).astype(float)
            if ds.nodata is not None:
                z[np.isclose(z, float(ds.nodata))] = np.nan
            left, bottom, right, top = rasterio.windows.bounds(win, ds.transform)
            x = np.linspace(left, right, int(grid_n))
            y = np.linspace(top, bottom, int(grid_n))
            xx, yy = np.meshgrid(x, y)
            return {"x": xx, "y": yy, "z": z, "ring": projected, "crs": str(ds.crs), "raster": str(raster.resolve())}

    @staticmethod
    def _cycle_actual_point(cycle: Mapping[str, Any]) -> list[float] | None:
        p = (((cycle.get("layer2_packets") or {}).get("MESA_NAV") or {}).get("state") or {})
        vals = [p.get("position_n"), p.get("position_e"), p.get("position_d")]
        if all(_finite(v) is not None for v in vals):
            return [float(vals[0]), float(vals[1]), float(vals[2])]
        snap = ((cycle.get("layer3_result") or {}).get("snapshot") or {})
        pos = (((((snap.get("std") or {}).get("representations") or {}).get("MESA_NAV") or {}).get("canonical_state") or {}).get("position_ned_m"))
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            return [float(pos[0]), float(pos[1]), float(pos[2])]
        return None


    @staticmethod
    def _actual_cross_layer_state(cycle: Mapping[str, Any]) -> dict[str, Any]:
        snap = ((cycle.get("layer3_result") or {}).get("snapshot") or {})
        reps = ((snap.get("std") or {}).get("representations") or {})
        mesa = ((reps.get("MESA_NAV") or {}).get("canonical_state") or {})
        wing = ((reps.get("WingMPET") or {}).get("canonical_state") or {})
        climate = ((reps.get("MicroMetTwin") or {}).get("canonical_state") or {})
        p = mesa.get("position_ned_m") or [None, None, None]
        vg = mesa.get("ground_velocity_ned_m_s") or [None, None, None]
        va = wing.get("air_velocity_ned_m_s") or [None, None, None]
        w = climate.get("wind_ned_m_s") or [None, None, None]
        def get3(v, i):
            return float(v[i]) if isinstance(v, (list, tuple)) and len(v) > i and _finite(v[i]) is not None else None
        return {
            "mesa_position_n_m": get3(p,0), "mesa_position_e_m": get3(p,1), "mesa_position_d_m": get3(p,2),
            "vg_n_m_s": get3(vg,0), "vg_e_m_s": get3(vg,1), "vg_d_m_s": get3(vg,2),
            "va_n_m_s": get3(va,0), "va_e_m_s": get3(va,1), "va_d_m_s": get3(va,2),
            "airspeed_m_s": _finite(wing.get("airspeed_m_s")),
            "flightpath_angle_rad": _finite(wing.get("flightpath_angle_rad")),
            "air_heading_rad": _finite(wing.get("air_heading_rad")),
            "wind_n_m_s": get3(w,0), "wind_e_m_s": get3(w,1), "wind_d_m_s": get3(w,2),
            "temperature_c": _finite(climate.get("temperature_c")),
            "relative_humidity_percent": _finite(climate.get("relative_humidity_percent")),
            "pressure_pa": _finite(climate.get("pressure_pa")),
            "air_density_kg_m3": _finite(climate.get("air_density_kg_m3")),
        }

    @staticmethod
    def _nested_numeric(obj: Any, keys: tuple[str, ...]) -> float | None:
        wanted={k.lower() for k in keys}
        stack=[obj]; seen=0
        while stack and seen < 4000:
            cur=stack.pop(); seen+=1
            if isinstance(cur, Mapping):
                for k,v in cur.items():
                    if str(k).lower() in wanted:
                        n=_finite(_unwrap(v))
                        if n is not None:return float(n)
                    if isinstance(v,(Mapping,list,tuple)):stack.append(v)
            elif isinstance(cur,(list,tuple)):
                stack.extend(x for x in cur if isinstance(x,(Mapping,list,tuple)))
        return None

    def _cycle_layer6_position(self, cycle: Mapping[str, Any]) -> dict[str, Any] | None:
        d6=((cycle.get("downstream_layers") or {}).get("6") or {})
        out=d6.get("output") if isinstance(d6,Mapping) else None
        ev=((out or {}).get("execution_evidence") if isinstance(out,Mapping) else None) or (d6.get("execution_evidence") if isinstance(d6,Mapping) else None)
        if not isinstance(ev,Mapping):return None
        snap=ev.get("io_snapshot") or {}
        if not isinstance(snap,Mapping):return None
        lat=self._nested_numeric(snap,("latitude_deg","lat_deg","latitude","lat"))
        lon=self._nested_numeric(snap,("longitude_deg","lon_deg","longitude","lon","lng"))
        if lat is not None and abs(lat)>90:lat/=1e7
        if lon is not None and abs(lon)>180:lon/=1e7
        if lat is None or lon is None or abs(lat)>90 or abs(lon)>180:return None
        alt=self._nested_numeric(snap,("altitude_msl_m","alt_msl_m","altitude_m","altitude","alt"))
        if alt is not None and abs(alt)>100000:alt/=1000.0
        sim_t=self._nested_numeric(snap,("simulation_time_s","sim_time_s","time_s"))
        return {"latitude_deg":lat,"longitude_deg":lon,"altitude_msl_m":alt,"simulation_time_s":sim_t,
                "layer6_status":d6.get("status"),"evidence_source":ev.get("source"),"validation_only":bool(ev.get("validation_only"))}

    @staticmethod
    def _state_row(v: Iterable[Any]) -> dict[str, Any]:
        vals = list(v)
        return {name: (float(vals[i]) if i < len(vals) and _finite(vals[i]) is not None else None) for i, name in enumerate(STATE_FIELDS)}

    def build_path_rows(self, cycles: list[dict[str, Any]], latest: Mapping[str, Any], *, start_xy: tuple[float, float], base_alt: float) -> list[dict[str, Any]]:
        sx, sy = start_xy
        rows: list[dict[str, Any]] = []
        # Actual/observed MESA-NAV positions across TDOC cycles.
        for i, c in enumerate(cycles):
            p = self._cycle_actual_point(c)
            if not p:
                continue
            state = self._actual_cross_layer_state(c)
            rows.append({
                "source": "TDOC_actual_MESA_NAV",
                "index": i,
                "transaction_id": c.get("transaction_id"),
                **state,
                "east_m": p[1], "north_m": p[0], "down_m": p[2],
                "x_projected_m": sx + p[1], "y_projected_m": sy + p[0], "altitude_msl_m": base_alt - p[2],
            })
        # Live ONLINE_JSBSIM position from Layer6/PT_LIB, when available.  This is
        # distinct from the pre-execution Layer2/MESA-NAV state and is the preferred
        # actual online trajectory in route reports.
        region=self._region_definition()
        crs=(region or {}).get("processing_crs")
        if crs:
            for i,c in enumerate(cycles):
                pos=self._cycle_layer6_position(c)
                if not pos:continue
                x,y=self._project(pos["longitude_deg"],pos["latitude_deg"],crs)
                alt=pos.get("altitude_msl_m")
                rows.append({"source":"Layer6_PT_LIB_position","index":i,"transaction_id":c.get("transaction_id"),
                             "longitude_deg":pos["longitude_deg"],"latitude_deg":pos["latitude_deg"],
                             "x_projected_m":x,"y_projected_m":y,"altitude_msl_m":float(alt) if alt is not None else base_alt,
                             "simulation_time_s":pos.get("simulation_time_s"),"layer6_status":pos.get("layer6_status"),
                             "evidence_source":pos.get("evidence_source"),"validation_only":pos.get("validation_only")})
        # Reuse the complete offline mission reference as a reference-only overlay for
        # online reports.  It is never labeled as executed JSBSim motion.
        ref_csv=self.package_root/"reports"/"offline"/"mission"/"ROUTE_HISTORY.csv"
        if ref_csv.is_file():
            try:
                import pandas as pd
                rdf=pd.read_csv(ref_csv)
                for i,r in rdf.iterrows():
                    x=_finite(r.get("reference_x_m")); y=_finite(r.get("reference_y_m")); z=_finite(r.get("reference_z_msl_m"))
                    if x is None or y is None:continue
                    rows.append({"source":"Offline_reference_route","index":int(i),"transaction_id":None,
                                 "x_projected_m":float(x),"y_projected_m":float(y),"altitude_msl_m":float(z) if z is not None else base_alt})
            except Exception:
                pass

        # The best current Layer-4 candidate is shown as a forecast, never mislabeled as executed motion.
        best = self._best_candidate(latest)
        if best:
            traj = best.get("predicted_trajectory") or best.get("trajectory") or []
            for i, state in enumerate(traj):
                s = self._state_row(state)
                row = {
                    "source": "Layer4_best_candidate_forecast",
                    "candidate_id": best.get("candidate_id"),
                    "scenario_id": best.get("scenario_id"),
                    "index": i,
                    **s,
                }
                n = s.get("mesa_position_n_m") or 0.0
                e = s.get("mesa_position_e_m") or 0.0
                d = s.get("mesa_position_d_m") or 0.0
                row.update({"east_m": e, "north_m": n, "down_m": d, "x_projected_m": sx + e, "y_projected_m": sy + n, "altitude_msl_m": base_alt - d})
                rows.append(row)
        return rows

    def parameter_rows(self, cycle: Mapping[str, Any]) -> list[dict[str, Any]]:
        downstream = cycle.get("downstream_layers") or cycle.get("future_layers") or {}
        blocks = [
            ("Layer2", cycle.get("layer2_packets") or {}),
            ("Layer3", cycle.get("layer3_result") or {}),
            ("Layer4", cycle.get("layer4_result") or {}),
            ("Layer5", downstream.get("5") or {}),
            ("Layer6", downstream.get("6") or {}),
            ("Layer7", downstream.get("7") or {}),
            ("Knowledge", (cycle.get("provenance") or {}).get("knowledge_ledger") or {}),
            ("TDOC", {"layer_status": cycle.get("layer_status"), "conformance": cycle.get("conformance"), "provenance": cycle.get("provenance")}),
        ]
        rows: list[dict[str, Any]] = []
        for layer, obj in blocks:
            for r in _flatten(obj):
                rows.append({"layer": layer, **r})
        return rows

    @staticmethod
    def _table_html(rows: list[dict[str, Any]], title: str, max_rows: int | None = None) -> str:
        rows = rows[:max_rows] if max_rows else rows
        if not rows:
            return f"<h3>{html.escape(title)}</h3><p>No data.</p>"
        cols = list(rows[0].keys())
        th = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
        body = []
        for r in rows:
            body.append("<tr>" + "".join(f"<td>{html.escape(_short(r.get(c), 1200))}</td>" for c in cols) + "</tr>")
        return f"<h3>{html.escape(title)}</h3><div class='scroll'><table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"

    def write_2d(self, cycles: list[dict[str, Any]], latest: dict[str, Any], events: list[dict[str, Any]], output: Path, parameter_rows: list[dict[str, Any]]) -> None:
        """Decision-oriented 2-D runtime dashboard.

        The former report emphasized a single latest snapshot and large raw tables.
        This view makes mission progress, vehicle state, actuator feedback, Layer-4
        feasibility, Layer-5 authorization and timing visible on one common cycle axis.
        Raw detail remains available in collapsed audit sections.
        """
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        def l5_result(c):
            d5=((c.get('downstream_layers') or {}).get('5') or {})
            env=d5.get('output') if isinstance(d5,dict) else {}
            env=env if isinstance(env,dict) else {}
            return env.get('result') if isinstance(env.get('result'),dict) else env
        def vehicle(c):
            d5=((c.get('downstream_layers') or {}).get('5') or {})
            env=d5.get('output') if isinstance(d5,dict) else {}
            if isinstance(env,dict) and isinstance(env.get('decision_vehicle_state'),dict): return env['decision_vehicle_state']
            d6=((c.get('downstream_layers') or {}).get('6') or {}); o6=d6.get('output') if isinstance(d6,dict) else {}
            ev=(o6 or {}).get('execution_evidence') if isinstance(o6,dict) else None; snap=(ev or {}).get('io_snapshot') if isinstance(ev,dict) else None
            return phoenix_state_from_ptlib_snapshot(snap,l5_result(c),o6) if isinstance(snap,dict) else {}
        def n(v): return _finite(v,None)
        def hv(lat1,lon1,lat2,lon2):
            if None in (lat1,lon1,lat2,lon2): return None
            R=6371008.8; a1=math.radians(lat1); a2=math.radians(lat2); da=a2-a1; dl=math.radians(lon2-lon1)
            h=math.sin(da/2)**2+math.cos(a1)*math.cos(a2)*math.sin(dl/2)**2
            return 2*R*math.asin(min(1.0,math.sqrt(max(0.0,h))))

        region=self._region_definition(); dlat=dlon=None
        if region:
            try: dlon,dlat=self._lonlat(region['roles']['destination'])
            except Exception: pass
        rows=[]; first_goal=None
        for i,c in enumerate(cycles,1):
            vs=vehicle(c); att=vs.get('attitude') or {}; spd=vs.get('speed') or {}; pos=vs.get('position') or {}; acts=vs.get('actuators') or {}
            le=acts.get('left_elevon') or {}; re=acts.get('right_elevon') or {}; lm=acts.get('left_motor') or {}; rm=acts.get('right_motor') or {}
            lat=n(pos.get('lat_deg')); lon=n(pos.get('lon_deg')); gd=hv(lat,lon,dlat,dlon)
            if first_goal is None and gd is not None:first_goal=gd
            prog=(first_goal-gd) if first_goal is not None and gd is not None else None
            l4=c.get('layer4_result') or {}; cands=l4.get('candidates') or []; l5=l5_result(c) or {}; dec=l5.get('decision') or {}; app=dec.get('approval') or {}
            d6=((c.get('downstream_layers') or {}).get('6') or {}); o6=d6.get('output') if isinstance(d6,dict) else {}; d7=((c.get('downstream_layers') or {}).get('7') or {})
            timings={}
            for lid in ('4','5','6','7'):
                dobj=((c.get('downstream_layers') or {}).get(lid) or {})
                oo=dobj.get('output') if isinstance(dobj,dict) else None
                tt=(oo.get('orchestration_timing') or {}) if isinstance(oo,dict) else {}
                timings[lid]=n(tt.get('elapsed_s'))
            rows.append({'cycle':i,'tx':c.get('transaction_id'),'status':c.get('status'),'goal_distance_m':gd,'progress_m':prog,
                'roll_deg':n(att.get('roll_deg')),'pitch_deg':n(att.get('pitch_deg')),'yaw_deg':n(att.get('yaw_deg')),
                'airspeed_m_s':n(spd.get('airspeed_m_s')),'groundspeed_m_s':n(spd.get('groundspeed_m_s')),'climb_m_s':n(spd.get('climb_m_s')),
                'left_elevon_deg':n(le.get('estimated_angle_deg')),'right_elevon_deg':n(re.get('estimated_angle_deg')),'left_elevon_pwm':n(le.get('pwm_us')),'right_elevon_pwm':n(re.get('pwm_us')),
                'left_motor_rpm':n(lm.get('estimated_speed_rpm')),'right_motor_rpm':n(rm.get('estimated_speed_rpm')),'left_motor_pwm':n(lm.get('pwm_us')),'right_motor_pwm':n(rm.get('pwm_us')),
                'motion_state':(vs.get('motion') or {}).get('state'),'l4_candidates':len(cands),'l4_feasible':sum(x.get('feasible') is True for x in cands),'l4_verified':len(l4.get('verified_candidates') or []),
                'l5_mode':l5.get('operational_mode'),'l5_reason':app.get('reason') or (l5.get('fallback') or {}).get('reason'),'l5_final':1 if app.get('final_gate') else 0,
                'l6_status':d6.get('status'),'l6_actuation':1 if isinstance(o6,dict) and o6.get('actuation_executed') else 0,'l7_status':d7.get('status'),
                't4':timings['4'],'t5':timings['5'],'t6':timings['6'],'t7':timings['7']})
        x=[r['cycle'] for r in rows]
        fig=make_subplots(rows=8,cols=1,vertical_spacing=0.035,subplot_titles=(
            'Mission progress — live PT_LIB position (zero remains zero when no translation occurs)',
            'Vehicle attitude relative to horizon/body axes [deg]','Vehicle speed [m/s]',
            'Elevon state — PWM feedback mapped to estimated angle [deg]','Motor state — PWM feedback mapped to estimated rotor speed [rpm]',
            'Layer 4 candidate availability / verification','Layer 5 authorization and Layer 6 physical execution','Per-layer orchestration latency [s]'))
        for key,name in [('goal_distance_m','goal distance [m]'),('progress_m','progress [m]')]:fig.add_trace(go.Scatter(x=x,y=[r[key] for r in rows],mode='lines',name=name,connectgaps=False),row=1,col=1)
        for key,name in [('roll_deg','roll'),('pitch_deg','pitch'),('yaw_deg','yaw')]:fig.add_trace(go.Scatter(x=x,y=[r[key] for r in rows],mode='lines',name=name,connectgaps=False),row=2,col=1)
        for key,name in [('airspeed_m_s','airspeed'),('groundspeed_m_s','groundspeed'),('climb_m_s','climb')]:fig.add_trace(go.Scatter(x=x,y=[r[key] for r in rows],mode='lines',name=name,connectgaps=False),row=3,col=1)
        for key,name,pwm in [('left_elevon_deg','left elevon est deg','left_elevon_pwm'),('right_elevon_deg','right elevon est deg','right_elevon_pwm')]:
            fig.add_trace(go.Scatter(x=x,y=[r[key] for r in rows],customdata=[[r[pwm],r['l5_mode'],r['l5_reason']] for r in rows],mode='lines',name=name,connectgaps=False,hovertemplate='%{y:.2f} deg<br>PWM=%{customdata[0]}<br>L5=%{customdata[1]} %{customdata[2]}<extra></extra>'),row=4,col=1)
        for key,name,pwm in [('left_motor_rpm','left motor est rpm','left_motor_pwm'),('right_motor_rpm','right motor est rpm','right_motor_pwm')]:
            fig.add_trace(go.Scatter(x=x,y=[r[key] for r in rows],customdata=[[r[pwm],r['motion_state']] for r in rows],mode='lines',name=name,connectgaps=False,hovertemplate='%{y:.0f} rpm<br>PWM=%{customdata[0]}<br>motion=%{customdata[1]}<extra></extra>'),row=5,col=1)
        for key,name in [('l4_candidates','candidates'),('l4_feasible','feasible'),('l4_verified','verified')]:fig.add_trace(go.Scatter(x=x,y=[r[key] for r in rows],mode='lines',name=name),row=6,col=1)
        fig.add_trace(go.Scatter(x=x,y=[r['l5_final'] for r in rows],customdata=[[r['l5_mode'],r['l5_reason']] for r in rows],mode='lines',name='Layer5 FINAL',hovertemplate='FINAL=%{y}<br>%{customdata[0]} %{customdata[1]}<extra></extra>'),row=7,col=1)
        fig.add_trace(go.Scatter(x=x,y=[r['l6_actuation'] for r in rows],mode='lines',name='Layer6 actuation executed'),row=7,col=1)
        for key,name in [('t4','L4'),('t5','L5'),('t6','L6'),('t7','L7')]:fig.add_trace(go.Scatter(x=x,y=[r[key] for r in rows],mode='lines',name=f'{name} elapsed',connectgaps=False),row=8,col=1)
        for rr in range(1,9):fig.update_xaxes(title_text='TDOC cycle',row=rr,col=1)
        fig.update_layout(height=2500,title=f"TDOC decision-oriented 2D dashboard | cycles={len(rows)} | latest={latest.get('transaction_id')} | status={latest.get('status')}",hovermode='x unified',legend={'orientation':'h'})

        last=rows[-1] if rows else {}; latest_vs=vehicle(latest) if latest else {}; mpc_summary=aggregate_mpc_participation(cycles)
        blocked=bool(last.get('l5_mode')=='HOLD' and not last.get('l6_actuation'))
        cause='Layer5 HOLD / no authorized actuation' if blocked and last.get('motion_state')=='NO_ACTUATION_STATIONARY' else (last.get('motion_state') or 'unknown')
        cards=[('TDOC',latest.get('status')),('Motion',last.get('motion_state')),('Progress',f"{last.get('progress_m'):.2f} m" if isinstance(last.get('progress_m'),(int,float)) else 'N/A'),('Goal remaining',f"{last.get('goal_distance_m'):.2f} m" if isinstance(last.get('goal_distance_m'),(int,float)) else 'N/A'),('Layer5',f"{last.get('l5_mode')} — {last.get('l5_reason') or ''}"),('Layer6',f"{last.get('l6_status')} / actuation={bool(last.get('l6_actuation'))}"),('Why progress?',cause)]
        card_html=''.join(f"<div class='card'><b>{html.escape(str(k))}</b><span>{html.escape(str(v))}</span></div>" for k,v in cards)
        event_rows=[]
        for e in events[-500:]:
            det=e.get('details') or {}; event_rows.append({'id':e.get('invocation_id'),'source':e.get('source'),'destination':e.get('destination'),'type':e.get('event_type'),'status':e.get('status'),'reason':e.get('reason'),'elapsed_s':det.get('elapsed_s'),'deadline_ok':det.get('deadline_ok')})
        latest_act=[]
        a=(latest_vs.get('actuators') or {})
        for name in ('left_elevon','right_elevon','left_motor','right_motor'):
            latest_act.append({'actuator':name,**(a.get(name) or {})})
        mpc_rows=[{'scope':'Layer4','metric':k,'value':v} for k,v in (mpc_summary.get('layer4') or {}).items() if k!='interpretation'] + [{'scope':'Layer5','metric':k,'value':v} for k,v in (mpc_summary.get('layer5') or {}).items() if k!='interpretation']
        css="""body{font-family:Arial,sans-serif;margin:24px;color:#222}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.card{border:1px solid #bbb;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:7px}.card span{font-size:14px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #ccc;padding:5px;vertical-align:top;text-align:left}th{background:#eee}.scroll{max-height:560px;overflow:auto}.note{padding:12px;border:1px solid #aaa;background:#f7f7f7}details{margin:12px 0}summary{cursor:pointer;font-weight:bold}"""
        plot=fig.to_html(full_html=False,include_plotlyjs='inline'); full_json=html.escape(json.dumps(latest,ensure_ascii=False,indent=2,default=str))
        text=f"""<!doctype html><html><head><meta charset='utf-8'><title>TDOC decision dashboard</title><style>{css}</style></head><body>
        <h1>TDOC — Decision / Vehicle / Actuator Dashboard</h1><div class='cards'>{card_html}</div>
        <div class='note'><b>Actuator semantics:</b> SERVO_OUTPUT_RAW PWM is live ArduPilot feedback. Elevon angle and motor RPM are estimates derived from PWM plus Layer5 Phoenix limits unless a direct physical angle/RPM sensor is present; estimates are never presented as measured values. A zero progress trace is preserved when live position does not move and is annotated with the Layer5/Layer6 authorization state rather than fabricated.</div>{plot}
        {self._table_html(latest_act,'Latest Phoenix actuator state / feedback')}
        {self._table_html(mpc_rows,'Layer4 / Layer5 MPC participation and authorization')}
        <details><summary>Invocation provenance (last 500)</summary>{self._table_html(event_rows,'TDOC invocation provenance')}</details>
        <details><summary>Complete flattened parameter inventory</summary>{self._table_html(parameter_rows,'All parameters')}</details>
        <details><summary>Raw latest transaction JSON</summary><pre><code>{full_json}</code></pre></details></body></html>"""
        output.write_text(text,encoding='utf-8')

    def write_3d(self, cycles: list[dict[str, Any]], latest: dict[str, Any], output: Path, path_rows: list[dict[str, Any]], *, terrain_grid_n: int = 90) -> dict[str, Any]:
        import plotly.graph_objects as go

        region = self._region_definition()
        if not region:
            raise FileNotFoundError("region_v1.geojson is required for the 3D report")
        roles = region["roles"]
        slon, slat = self._lonlat(roles["start"])
        dlon, dlat = self._lonlat(roles["destination"])
        sx, sy = self._project(slon, slat, region["processing_crs"])
        dx, dy = self._project(dlon, dlat, region["processing_crs"])
        base_alt = self._reference_altitude(latest, slon, slat)
        surface = self._terrain_surface(region, terrain_grid_n)

        fig = go.Figure()
        if surface:
            fig.add_trace(go.Surface(x=surface["x"], y=surface["y"], z=surface["z"], opacity=0.72, showscale=False, name="terrain_obstacles.tif"))
            if surface.get("ring"):
                rz = float(np.nanmax(surface["z"])) + 8.0
                fig.add_trace(go.Scatter3d(x=[p[0] for p in surface["ring"]], y=[p[1] for p in surface["ring"]], z=[rz] * len(surface["ring"]), mode="lines", name="region boundary"))

        mesa_actual = [r for r in path_rows if r.get("source") == "TDOC_actual_MESA_NAV"]
        online_actual = [r for r in path_rows if r.get("source") == "Layer6_PT_LIB_position"]
        actual = online_actual if online_actual else mesa_actual
        reference = [r for r in path_rows if r.get("source") == "Offline_reference_route"]
        forecast = [r for r in path_rows if r.get("source") == "Layer4_best_candidate_forecast"]
        if reference:
            fig.add_trace(go.Scatter3d(x=[r["x_projected_m"] for r in reference],y=[r["y_projected_m"] for r in reference],z=[r["altitude_msl_m"] for r in reference],
                                       mode="lines",line={"dash":"dash"},name="Offline/SARG mission reference (reference only)"))
        if online_actual:
            fig.add_trace(go.Scatter3d(x=[r["x_projected_m"] for r in online_actual],y=[r["y_projected_m"] for r in online_actual],z=[r["altitude_msl_m"] for r in online_actual],
                                       mode="lines+markers",name="ONLINE actual PT_LIB / ArduPilot / JSBSim position",
                                       customdata=[[r.get("transaction_id"),r.get("latitude_deg"),r.get("longitude_deg"),r.get("simulation_time_s"),r.get("layer6_status")] for r in online_actual],
                                       hovertemplate="tx=%{customdata[0]}<br>lat/lon=%{customdata[1]:.7f}/%{customdata[2]:.7f}<br>sim_time=%{customdata[3]} s<br>L6=%{customdata[4]}<extra>Layer6 live</extra>"))
        elif mesa_actual:
            fig.add_trace(go.Scatter3d(
                x=[r["x_projected_m"] for r in mesa_actual], y=[r["y_projected_m"] for r in mesa_actual], z=[r["altitude_msl_m"] for r in mesa_actual],
                mode="lines+markers", name="TDOC observed MESA-NAV path (no Layer6 geographic evidence)",
                customdata=[[r.get("transaction_id"), r.get("north_m"), r.get("east_m"), r.get("down_m"),r.get("airspeed_m_s")] for r in mesa_actual],
                hovertemplate="tx=%{customdata[0]}<br>N/E/D=%{customdata[1]}/%{customdata[2]}/%{customdata[3]} m<br>airspeed=%{customdata[4]} m/s<extra>Layer2 actual/canonical</extra>"
            ))
        if forecast:
            custom = []
            for r in forecast:
                custom.append([
                    r.get("candidate_id"), r.get("index"),
                    r.get("vg_n_m_s"), r.get("vg_e_m_s"), r.get("vg_d_m_s"),
                    r.get("va_n_m_s"), r.get("va_e_m_s"), r.get("va_d_m_s"), r.get("airspeed_m_s"),
                    r.get("wind_n_m_s"), r.get("wind_e_m_s"), r.get("wind_d_m_s"),
                    r.get("temperature_c"), r.get("relative_humidity_percent"), r.get("pressure_pa"), r.get("air_density_kg_m3"),
                ])
            fig.add_trace(go.Scatter3d(
                x=[r["x_projected_m"] for r in forecast], y=[r["y_projected_m"] for r in forecast], z=[r["altitude_msl_m"] for r in forecast],
                mode="lines+markers", name="Layer-4 best candidate forecast",
                marker={"size": 5, "color": [r.get("airspeed_m_s") or 0.0 for r in forecast], "colorscale": "Viridis", "colorbar": {"title": "airspeed m/s"}},
                customdata=custom,
                hovertemplate=("candidate=%{customdata[0]} step=%{customdata[1]}<br>"
                               "MESA Vg N/E/D=%{customdata[2]:.2f}/%{customdata[3]:.2f}/%{customdata[4]:.2f} m/s<br>"
                               "Wing Va N/E/D=%{customdata[5]:.2f}/%{customdata[6]:.2f}/%{customdata[7]:.2f} m/s; Va=%{customdata[8]:.2f}<br>"
                               "Climate W N/E/D=%{customdata[9]:.2f}/%{customdata[10]:.2f}/%{customdata[11]:.2f} m/s<br>"
                               "T=%{customdata[12]:.2f} C RH=%{customdata[13]:.2f}% p=%{customdata[14]:.1f} Pa rho=%{customdata[15]:.3f}<extra>Layer 2/4 forecast</extra>")
            ))
            stride = max(1, len(forecast) // 10)
            wind = forecast[::stride]
            fig.add_trace(go.Cone(
                x=[r["x_projected_m"] for r in wind], y=[r["y_projected_m"] for r in wind], z=[r["altitude_msl_m"] for r in wind],
                u=[r.get("wind_e_m_s") or 0.0 for r in wind], v=[r.get("wind_n_m_s") or 0.0 for r in wind], w=[-(r.get("wind_d_m_s") or 0.0) for r in wind],
                sizemode="absolute", sizeref=8, showscale=False, name="MicroMetTwin wind forecast"
            ))

        # Geographic mission endpoints from the authoritative region definition.
        dest_terrain = self._sample_terrain(dlon, dlat)
        dest_alt = max(base_alt, (dest_terrain + 100.0) if dest_terrain is not None else base_alt)
        fig.add_trace(go.Scatter3d(x=[sx, dx], y=[sy, dy], z=[base_alt, dest_alt], mode="markers+text", text=["ORIGIN", "DESTINATION"], name="region_v1 mission endpoints"))

        # Current target from the TDOC snapshot can differ from the full region destination.
        ctx = (((latest.get("layer3_result") or {}).get("snapshot") or {}).get("context") or {})
        target = _unwrap(ctx.get("target_enu_m"))
        if isinstance(target, (list, tuple)) and len(target) >= 3:
            tx, ty, tz = float(target[0]), float(target[1]), float(target[2])
            fig.add_trace(go.Scatter3d(x=[sx + tx], y=[sy + ty], z=[tz], mode="markers+text", text=["CURRENT TDOC TARGET"], name="current target_enu_m"))

        # A straight reference only; never presented as executed or planned trajectory.
        last = forecast[-1] if forecast else (actual[-1] if actual else None)
        if last:
            fig.add_trace(go.Scatter3d(x=[last["x_projected_m"], dx], y=[last["y_projected_m"], dy], z=[last["altitude_msl_m"], dest_alt], mode="lines", line={"dash":"dot"}, name="line-of-sight reference to destination"))

        fig.update_layout(
            title=f"TDOC 3D cross-layer route report | tx={latest.get('transaction_id')} | live L6 points={len(online_actual)} | MESA points={len(mesa_actual)} | reference points={len(reference)} | L4 forecast={len(forecast)}",
            scene={"xaxis_title":"Easting (m)", "yaxis_title":"Northing (m)", "zaxis_title":"MSL altitude (m)", "aspectmode":"data"},
            margin={"l":0,"r":0,"b":0,"t":70},
        )
        output.write_text(fig.to_html(full_html=True, include_plotlyjs="inline"), encoding="utf-8")
        # Prefer deployment provenance paths recorded by the executed Twin over any
        # temporary validation/mount path used to render this HTML.
        climate_prov = ((((latest.get("layer2_packets") or {}).get("MicroMetTwin") or {}).get("provenance") or {}))
        declared_raster = climate_prov.get("terrain_source")
        declared_region = None
        if declared_raster:
            try:
                rp = Path(str(declared_raster))
                parts = list(rp.parts)
                if "outputs" in parts:
                    idx = parts.index("outputs")
                    declared_region = str(Path(*parts[:idx]) / "region" / "region_v1.geojson")
            except Exception:
                declared_region = None
        return {
            "region": declared_region or region["path"], "processing_crs": region["processing_crs"], "raster": declared_raster or (surface.get("raster") if surface else None),
            "origin_wgs84": [slon, slat], "destination_wgs84": [dlon, dlat], "origin_projected_m": [sx, sy], "destination_projected_m": [dx, dy],
            "reference_altitude_msl_m": base_alt, "actual_path_points": len(actual), "layer6_live_path_points":len(online_actual),
            "mesa_path_points":len(mesa_actual),"offline_reference_points":len(reference),"layer4_forecast_points": len(forecast),
            "route_semantics": "ONLINE prefers live Layer6/PT_LIB geographic positions across all selected cycles; MESA-NAV is fallback actual state; offline/SARG route and destination LOS are reference-only overlays",
        }

    def build_summary(self, cycles: list[dict[str, Any]], latest: dict[str, Any], path_meta: Mapping[str, Any], parameter_rows: list[dict[str, Any]]) -> dict[str, Any]:
        l4 = latest.get("layer4_result") or {}
        snap = (latest.get("layer3_result") or {}).get("snapshot") or {}
        return {
            "schema": "TDOC-cross-layer-report-v1",
            "transaction_id": latest.get("transaction_id"),
            "status": latest.get("status"),
            "cycles_in_history": len(cycles),
            "layer2": {
                "twins": self._layer2_summary(latest),
                "executed_twins": sorted((latest.get("layer2_packets") or {}).keys()),
                "scope_note": "TDOC executes the configured production operation for each required Twin; this does not mean every operation code exported by each Layer-2 library is invoked on every cycle.",
            },
            "layer3": {
                "snapshot_version": snap.get("policy_version"),
                "weights": snap.get("weights"), "couplings": snap.get("couplings"), "active_edges": ((snap.get("topology") or {}).get("active_edges")),
                "mismatch": snap.get("mismatch"), "consistency_margin": snap.get("consistency_margin"), "stability": snap.get("stability"),
                "tdoc_health": snap.get("tdoc_health"),
                "scope_note": "Layer3Engine.step is invoked with TDOC-owned Layer-2 packets, exercising Layer 3.1 alignment, Layer 3.2 network/CoC and Layer 3.3 scheduling/snapshot generation.",
            },
            "layer4": {
                "candidate_count": len(l4.get("candidates") or []), "pareto_count": len(l4.get("pareto_front") or []), "verified_count": len(l4.get("verified_candidates") or []),
                "budget": l4.get("budget"), "conformance": l4.get("conformance"), "actuator_command": l4.get("actuator_command"), "network_commit": l4.get("network_commit"),
                "scope_note": "Layer4 prediction/what-if/optimization runs under TDOC ownership. Full-fidelity verification is also TDOC-owned and is attempted only through an evidence-validated Phoenix action bridge; with the shipped unvalidated 5D→4D mapping/mixer it remains fail-closed rather than fabricating actuator commands.",
            },
            "mpc_participation": aggregate_mpc_participation(cycles),
            "tdoc": {"conformance": latest.get("conformance"), "provenance": latest.get("provenance"), "layer_status": latest.get("layer_status")},
            "parameters_flattened": len(parameter_rows),
            "route_3d": dict(path_meta),
        }

    def generate(self, *, cycle: str | Path | None = None, transaction_prefix: str | None = None, transaction_ids: set[str] | list[str] | tuple[str, ...] | None = None, output_dir: str | Path | None = None, terrain_grid_n: int = 90) -> tuple[dict[str, Any], ReportArtifacts]:
        cycles = self.load_cycles(cycle,transaction_prefix=transaction_prefix,transaction_ids=transaction_ids)
        latest = self._latest_ok(cycles)
        txids = {str(c.get("transaction_id")) for c in cycles if c.get("transaction_id")}
        events = self._invocation_events(txids)

        raw_out = output_dir or self.config.get("reporting.output_dir", "reports/visualization")
        out = Path(raw_out)
        if not out.is_absolute():
            out = (self.package_root / out).resolve()
        d2, d3, data_dir = out / "2d", out / "3d", out / "data"
        for p in (d2, d3, data_dir):
            p.mkdir(parents=True, exist_ok=True)
        art = ReportArtifacts(out, d2 / "tdoc_layers_2d.html", d3 / "tdoc_route_3d.html", data_dir / "report_summary.json", data_dir / "path_timeseries.csv", data_dir / "all_parameters.csv")

        region = self._region_definition()
        if not region:
            raise FileNotFoundError("3D reporting requires region_v1.geojson through TDDT_BOULDER_ROOT/TDDT_MAP_ROOT")
        slon, slat = self._lonlat(region["roles"]["start"])
        sx, sy = self._project(slon, slat, region["processing_crs"])
        base_alt = self._reference_altitude(latest, slon, slat)
        path_rows = self.build_path_rows(cycles, latest, start_xy=(sx, sy), base_alt=base_alt)
        parameter_rows = self.parameter_rows(latest)

        self.write_2d(cycles, latest, events, art.report_2d_html, parameter_rows)
        path_meta = self.write_3d(cycles, latest, art.report_3d_html, path_rows, terrain_grid_n=terrain_grid_n)
        summary = self.build_summary(cycles, latest, path_meta, parameter_rows)
        art.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

        if path_rows:
            keys = sorted({str(k) for r in path_rows for k in r})
            with art.path_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader(); w.writerows(path_rows)
        if parameter_rows:
            keys = ["layer", "path", "value", "unit", "frame", "semantic", "source"]
            with art.parameters_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader(); w.writerows(parameter_rows)
        return summary, art
