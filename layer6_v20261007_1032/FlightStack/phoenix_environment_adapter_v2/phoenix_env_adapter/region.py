from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

class Region:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        obj = json.loads(self.path.read_text())
        self.name = obj.get("name", self.path.stem)
        self.bbox = obj.get("bbox")
        self._features = {}
        self.boundary = None
        for f in obj.get("features", []):
            role = f.get("properties", {}).get("feature_role")
            if role:
                self._features[role] = f
            if role == "region_boundary":
                self.boundary = f["geometry"]["coordinates"][0]
        self.start = self._point("start")
        self.destination = self._point("destination")
        self.center = self._point("center") if "center" in self._features else None

    def _point(self, role: str) -> Point:
        lon, lat = self._features[role]["geometry"]["coordinates"]
        return Point(lat=float(lat), lon=float(lon))

    def contains(self, lat: float, lon: float) -> bool:
        # Ray-casting on GeoJSON lon/lat polygon.
        if not self.boundary:
            return True
        x, y = lon, lat
        inside = False
        pts = self.boundary
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if ((yi > y) != (yj > y)):
                xcross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi
                if x < xcross:
                    inside = not inside
            j = i
        return inside
