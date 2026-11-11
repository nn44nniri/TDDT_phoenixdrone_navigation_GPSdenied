from __future__ import annotations
import json, tarfile
from pathlib import Path

ROOT = "internet_datasets/boulder_multilayer_map_builder"
DEM_REL = ROOT + "/offline_sources/dem"
CLIMATE_REL = ROOT + "/offline_sources/climate"

class DatasetLayout:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dem_dir = self.root / DEM_REL
        self.climate_dir = self.root / CLIMATE_REL
        self.dem_manifest = self.dem_dir / "dem_sources_manifest.json"
        self.climate_manifest = self.climate_dir / "climate_sources_manifest.json"

    @classmethod
    def from_tar(cls, tar_path: str | Path, cache_dir: str | Path) -> "DatasetLayout":
        tar_path, cache_dir = Path(tar_path), Path(cache_dir)
        marker = cache_dir / ".phoenix_env_extracted"
        if not marker.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tar_path, "r:gz") as tf:
                wanted = (DEM_REL + "/", CLIMATE_REL + "/")
                members = [m for m in tf.getmembers() if any(m.name.startswith(p) for p in wanted)]
                tf.extractall(cache_dir, members=members, filter="data")
            marker.write_text(str(tar_path.resolve()))
        return cls(cache_dir)

    def validate(self):
        if not self.dem_manifest.exists():
            raise FileNotFoundError(self.dem_manifest)
        if not self.climate_manifest.exists():
            raise FileNotFoundError(self.climate_manifest)
        dm = json.loads(self.dem_manifest.read_text())
        cm = json.loads(self.climate_manifest.read_text())
        tile = self.dem_dir / dm["tiles"][0]["file"]
        if not tile.exists():
            raise FileNotFoundError(tile)
        return {"dem": dm, "climate": cm, "dem_path": tile}
