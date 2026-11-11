from __future__ import annotations

import math
from collections import Counter
from typing import Any, Mapping

import numpy as np

from .utils import mean, safe_float


def _mean_dict(records: list[Mapping[str, Any]], field: str) -> dict[str, float]:
    keys: set[str] = set()
    for r in records:
        v = r.get(field) or {}
        if isinstance(v, Mapping): keys.update(v)
    out = {}
    for k in keys:
        vals = [safe_float((r.get(field) or {}).get(k), float("nan")) for r in records]
        vals = [v for v in vals if math.isfinite(v)]
        if vals: out[k] = sum(vals) / len(vals)
    return out


def _trend(values: list[float]) -> float:
    vals = np.asarray([v for v in values if math.isfinite(v)], dtype=float)
    if vals.size < 2: return 0.0
    x = np.arange(vals.size, dtype=float)
    return float(np.polyfit(x, vals, 1)[0])


class FastToMesoAggregator:
    def aggregate(self, records: list[Mapping[str, Any]], window_index: int, fast_dt_s: float) -> dict[str, Any]:
        if not records: raise ValueError("Fast aggregate requires at least one record")
        mismatch = [safe_float(r.get("mismatch"), float("nan")) for r in records]
        mismatch = [v for v in mismatch if math.isfinite(v)]
        margins = [safe_float(r.get("consistency_margin"), float("nan")) for r in records]
        margins = [v for v in margins if math.isfinite(v)]
        weights = _mean_dict(records, "weights")
        couplings = _mean_dict(records, "couplings")
        # covariance of weight trajectory
        wkeys = sorted(weights)
        if wkeys and len(records) > 1:
            mat = np.asarray([[safe_float((r.get("weights") or {}).get(k), 0.0) for k in wkeys] for r in records], dtype=float)
            wcov = np.cov(mat, rowvar=False, bias=False)
            if wcov.ndim == 0: wcov = np.asarray([[float(wcov)]])
            wcov_out = {"order": wkeys, "matrix": wcov.tolist()}
        else:
            wcov_out = {"order": wkeys, "matrix": [[0.0 for _ in wkeys] for _ in wkeys]}

        active_count: Counter[str] = Counter()
        violation_count: Counter[str] = Counter()
        for r in records:
            graph = r.get("topology") or {}
            for k in graph.get("active_edges") or []: active_count[k] += 1
            for k, rho in (r.get("edge_normalized_residuals") or {}).items():
                if safe_float(rho, 0.0) > 1.0: violation_count[k] += 1
        n = len(records)
        edge_persistence = {k: active_count[k] / n for k in set(active_count) | set(couplings)}
        violation_persistence = {k: violation_count[k] / n for k in set(couplings) | set(violation_count)}

        context = self._crp_context(records)
        flight = self._flight_metrics(records)
        scores = self._score_aggregate(records)
        centers = [r.get("center_of_consistency") or {} for r in records]
        # Canonical state summary required by Layer-3 v3: bar_X, Sigma_X, X_end.
        flat_rows=[]
        keys=set()
        for r in records:
            row={}
            reps=(((r.get("std") or {}).get("representations") or {}))
            for twin, rep in reps.items():
                can=(rep.get("canonical_state") or {})
                for name,val in can.items():
                    if isinstance(val,(int,float)) and math.isfinite(float(val)):
                        row[f"{twin}.{name}"]=float(val)
                    elif isinstance(val,(list,tuple)):
                        for j,x in enumerate(val):
                            if isinstance(x,(int,float)) and math.isfinite(float(x)):
                                row[f"{twin}.{name}[{j}]"]=float(x)
            keys.update(row); flat_rows.append(row)
        order=sorted(k for k in keys if all(k in row for row in flat_rows))
        if order:
            X=np.asarray([[row[k] for k in order] for row in flat_rows],dtype=float)
            bar_X={k:float(v) for k,v in zip(order,np.mean(X,axis=0))}
            if len(flat_rows)>1:
                C=np.cov(X,rowvar=False,bias=False)
                if C.ndim==0: C=np.asarray([[float(C)]])
            else:
                C=np.zeros((len(order),len(order)),dtype=float)
            Sigma_X={"order":order,"matrix":C.tolist()}
            X_end={k:float(flat_rows[-1][k]) for k in order}
        else:
            bar_X={}; Sigma_X={"order":[],"matrix":[]}; X_end={}
        return {
            "window_index": window_index,
            "fast_count": n,
            "start_time_s": records[0].get("reference_time_s"),
            "end_time_s": records[-1].get("reference_time_s"),
            "duration_s": n * fast_dt_s,
            "bar_X": bar_X,
            "Sigma_X": Sigma_X,
            "X_end": X_end,
            "mean_weights": weights,
            "weight_covariance": wcov_out,
            "mean_couplings": couplings,
            "edge_persistence": edge_persistence,
            "violation_persistence": violation_persistence,
            "mean_mismatch": mean(mismatch) if mismatch else None,
            "max_mismatch": max(mismatch) if mismatch else None,
            "mismatch_integral": sum(mismatch) * fast_dt_s if mismatch else None,
            "mean_consistency_margin": mean(margins) if margins else None,
            "min_consistency_margin": min(margins) if margins else None,
            "mismatch_trend_per_tick": _trend(mismatch) if mismatch else 0.0,
            "fault_count": sum(1 for r in records if safe_float(r.get("consistency_margin"), 1.0) < 0 or r.get("gate_fault")),
            "crp_context": context,
            "flight_metrics": flight,
            "score_summary": scores,
            "end_center_of_consistency": centers[-1],
            "topology": records[-1].get("topology") or {},
            "stability": records[-1].get("stability") or {},
            "hidden_loop": records[-1].get("hidden_loop") or {},
            "provenance": {"formula": "Fast->Meso aggregation; Layer3-v3", "raw_fast_not_forwarded_to_slow": True},
        }

    @staticmethod
    def _crp_context(records: list[Mapping[str, Any]]) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        wanted = {
            "temperature_c", "relative_humidity_percent", "wind_speed_3d_m_s", "wind_direction_from_deg",
            "terrain_elevation_m", "ground_elevation_m", "terrain_slope_deg", "local_relief_m", "building_height_m",
            "magnetic_anomaly_nt", "magnetic_gradient_nt_per_m",
            "pressure_pa", "surface_pressure_pa", "air_density_kg_m3", "air_density", "crp_raster_id",
        }
        for r in records:
            std = r.get("std") or {}
            reps = std.get("representations") or {}
            climate = ((reps.get("MicroMetTwin") or {}).get("canonical_state") or {})
            ctx = std.get("context") or {}
            for source in (climate, ctx):
                for k in wanted:
                    v = safe_float(source.get(k), float("nan"))
                    if math.isfinite(v): buckets.setdefault(k, []).append(v)
        out = {k: sum(v) / len(v) for k, v in buckets.items() if v}
        if "wind_speed_3d_m_s" in out: out["wind_speed_ms"] = out["wind_speed_3d_m_s"]
        if "wind_direction_from_deg" in out:
            # Circular mean from all observed directions, not arithmetic mean.
            dirs = buckets.get("wind_direction_from_deg", [])
            if dirs:
                s = sum(math.sin(math.radians(d)) for d in dirs) / len(dirs)
                c = sum(math.cos(math.radians(d)) for d in dirs) / len(dirs)
                out["wind_direction_from_deg"] = math.degrees(math.atan2(s, c)) % 360.0
        return out

    @staticmethod
    def _flight_metrics(records: list[Mapping[str, Any]]) -> dict[str, float]:
        va=[]; clear=[]; conf=[]
        for r in records:
            reps=((r.get("std") or {}).get("representations") or {})
            wing=(reps.get("WingMPET") or {})
            can=wing.get("canonical_state") or {}; raw=wing.get("raw_state") or {}
            v=safe_float(can.get("airspeed_m_s"), float("nan"))
            if math.isfinite(v): va.append(v)
            for name in ("min_ground_clearance_m","min_ground_clearance","terrain_margin","min_obstacle_clearance_m"):
                c=safe_float(raw.get(name), float("nan"))
                if math.isfinite(c): clear.append(c); break
            q=[]
            for rep in reps.values():
                for field in ("quality","validity","reliability","freshness"):
                    x=safe_float(rep.get(field), float("nan"))
                    if math.isfinite(x): q.append(x)
            if q: conf.append(sum(q)/len(q))
        out={}
        if va: out["airspeed_m_s"]=sum(va)/len(va)
        if clear: out["min_clearance_m"]=min(clear)
        if conf: out["network_confidence"]=sum(conf)/len(conf)
        return out

    @staticmethod
    def _score_aggregate(records: list[Mapping[str, Any]]) -> dict[str, dict[str,float]]:
        result={}
        twins=set()
        for r in records:
            twins.update((((r.get("std") or {}).get("representations") or {}).keys()))
        for twin in sorted(twins):
            vals={k:[] for k in ("uncertainty","quality","validity","reliability","freshness")}
            for r in records:
                rep=(((r.get("std") or {}).get("representations") or {}).get(twin) or {})
                mapping={"uncertainty":"normalized_uncertainty","quality":"quality","validity":"validity","reliability":"reliability","freshness":"freshness"}
                for k,src in mapping.items():
                    v=safe_float(rep.get(src), float("nan"))
                    if math.isfinite(v): vals[k].append(v)
            result[twin]={k:(sum(v)/len(v) if v else 0.0) for k,v in vals.items()}
        return result



class MesoToSlowAggregator:
    def aggregate(self, events: list[Mapping[str, Any]], period_index: int) -> dict[str, Any]:
        if not events: raise ValueError("Slow aggregate requires meso events")
        aggs=[e.get("aggregate") or {} for e in events]
        mean_weights=_mean_dict(aggs,"mean_weights")
        mean_couplings=_mean_dict(aggs,"mean_couplings")
        mismatches=[safe_float(a.get("mean_mismatch"),0.0) for a in aggs]
        worst=[safe_float(a.get("max_mismatch"),0.0) for a in aggs]
        certified=[bool((a.get("stability") or {}).get("stable")) for a in aggs if (a.get("stability") or {}).get("certified")]
        lci=[safe_float((a.get("hidden_loop") or {}).get("lci_proxy"),0.0) for a in aggs]
        crp=Counter()
        sarg=[]; risks=[]; costs=[]
        for e in events:
            cm=e.get("crp_match") or {}
            if cm.get("crp_id") is not None: crp[str(cm["crp_id"])]+=1
            sd=e.get("sarg_deviation") or {}
            sarg.append(safe_float(sd.get("score"),0.0))
            g=e.get("guidance") or {}
            risks.append(safe_float(g.get("risk_signal"),0.0))
            costs.append(safe_float((e.get("sarg_reference") or {}).get("pattern",{}).get("objective"),0.0))
        return {
            "period_index":period_index,
            "meso_count":len(events),
            "mean_weights":mean_weights,
            "mean_couplings":mean_couplings,
            "topology":aggs[-1].get("topology") or {},
            "mean_mismatch":mean(mismatches),
            "worst_mismatch":max(worst),
            "stable_fraction":(sum(certified)/len(certified) if certified else 0.0),
            "stability_assessed_fraction":len(certified)/len(events),
            "lci_summary":{"mean_proxy":mean(lci),"max_proxy":max(lci) if lci else 0.0},
            "risk":mean(risks),
            "cost":mean(costs),
            "crp_histogram":dict(crp),
            "sarg_deviation":mean(sarg),
            "provenance":{"formula":"Meso->Slow aggregate; Layer3-v3"},
        }
