from __future__ import annotations
from pathlib import Path
import rasterio

class TerrainProvider:
    def __init__(self, dem_path: str | Path):
        self.path = Path(dem_path)
        self.ds = rasterio.open(self.path)

    def elevation_msl(self, lat: float, lon: float) -> float:
        val = next(self.ds.sample([(lon, lat)]))[0]
        if self.ds.nodata is not None and val == self.ds.nodata:
            raise ValueError(f"DEM nodata at {lat},{lon}")
        return float(val)

    def close(self):
        self.ds.close()
