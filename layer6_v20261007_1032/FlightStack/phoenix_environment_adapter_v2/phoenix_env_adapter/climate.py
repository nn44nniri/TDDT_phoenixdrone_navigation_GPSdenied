from __future__ import annotations
import csv, gzip, json, math, re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

@dataclass(frozen=True)
class ClimateSample:
    temperature_c: float
    relative_humidity_pct: float
    wind_speed_m_s: float
    wind_direction_from_deg: float

    @property
    def wind_north_m_s(self) -> float:
        r = math.radians(self.wind_direction_from_deg)
        return -self.wind_speed_m_s * math.cos(r)

    @property
    def wind_east_m_s(self) -> float:
        r = math.radians(self.wind_direction_from_deg)
        return -self.wind_speed_m_s * math.sin(r)

class ClimateProvider:
    def __init__(self, climate_dir: str | Path):
        self.dir = Path(climate_dir)
        manifest = json.loads((self.dir / "climate_sources_manifest.json").read_text())
        pts = {}
        for ch in manifest["chunks"]:
            pts[ch["point_index"]] = (float(ch["latitude"]), float(ch["longitude"]))
        self.points = pts
        self._cache = {}

    @staticmethod
    def _utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    def _file_for(self, point_idx: int, dt: datetime) -> Path:
        year = dt.year
        matches = sorted(self.dir.glob(f"point_{point_idx:03d}_*_{year}m*.csv.gz"))
        if not matches:
            # 2010 and 2025 have partial-year names but still match above; fallback explicit scan.
            matches = sorted(self.dir.glob(f"point_{point_idx:03d}_*.csv.gz"))
            matches = [p for p in matches if f"_{year}m" in p.name]
        if not matches:
            raise FileNotFoundError(f"No ERA5 chunk for point {point_idx} and year {year}")
        return matches[0]

    def _load(self, point_idx: int, dt: datetime):
        path = self._file_for(point_idx, dt)
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        times, rows = [], []
        with gzip.open(path, "rt", newline="") as f:
            for row in csv.DictReader(f):
                t = datetime.fromisoformat(row["time"]).astimezone(timezone.utc)
                times.append(t)
                rows.append((float(row["temperature_2m"]), float(row["relative_humidity_2m"]),
                             float(row["wind_speed_10m"]), float(row["wind_direction_10m"])))
        self._cache[key] = (times, rows)
        return times, rows

    def _temporal(self, point_idx: int, dt: datetime):
        dt = self._utc(dt)
        times, rows = self._load(point_idx, dt)
        i = bisect_left(times, dt)
        if i <= 0: return rows[0]
        if i >= len(times): return rows[-1]
        t0, t1 = times[i-1], times[i]
        a = (dt - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())
        r0, r1 = rows[i-1], rows[i]
        # Interpolate wind as vector to avoid 359°/1° discontinuity.
        def uv(r):
            sp, d = r[2], math.radians(r[3]); return (-sp*math.sin(d), -sp*math.cos(d)) # east,north
        e0,n0 = uv(r0); e1,n1 = uv(r1)
        temp = r0[0] + a*(r1[0]-r0[0]); rh = r0[1] + a*(r1[1]-r0[1])
        e = e0 + a*(e1-e0); n = n0 + a*(n1-n0)
        sp = math.hypot(e,n)
        # meteorological FROM direction from resulting TO vector
        d = (math.degrees(math.atan2(-e, -n)) + 360.0) % 360.0
        return temp, rh, sp, d

    def sample(self, lat: float, lon: float, dt: datetime) -> ClimateSample:
        # The attached dataset is a regular 3x3 grid. Bilinear interpolation on four enclosing nodes.
        lats = sorted(set(v[0] for v in self.points.values()))
        lons = sorted(set(v[1] for v in self.points.values()))
        def bracket(vals, x):
            if x <= vals[0]: return vals[0], vals[0]
            if x >= vals[-1]: return vals[-1], vals[-1]
            for a,b in zip(vals, vals[1:]):
                if a <= x <= b: return a,b
            return vals[-1], vals[-1]
        y0,y1 = bracket(lats, lat); x0,x1 = bracket(lons, lon)
        idx_by = {(la,lo): idx for idx,(la,lo) in self.points.items()}
        corners = []
        for y,x in ((y0,x0),(y0,x1),(y1,x0),(y1,x1)):
            corners.append((y,x,self._temporal(idx_by[(y,x)], dt)))
        tx = 0.0 if x1 == x0 else (lon-x0)/(x1-x0)
        ty = 0.0 if y1 == y0 else (lat-y0)/(y1-y0)
        weights = [(1-tx)*(1-ty), tx*(1-ty), (1-tx)*ty, tx*ty]
        temp = sum(w*c[2][0] for w,c in zip(weights,corners))
        rh = sum(w*c[2][1] for w,c in zip(weights,corners))
        # vector-average wind
        east=north=0.0
        for w,c in zip(weights,corners):
            sp,d = c[2][2], math.radians(c[2][3])
            east += w*(-sp*math.sin(d)); north += w*(-sp*math.cos(d))
        sp = math.hypot(east,north)
        d = (math.degrees(math.atan2(-east,-north))+360.0)%360.0
        return ClimateSample(temp, rh, sp, d)
