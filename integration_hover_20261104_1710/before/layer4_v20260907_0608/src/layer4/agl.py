from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=2)
def _load_dem(path: str, mtime_ns: int, size_bytes: int):
    """Load band 1 once for an exact immutable DEM file identity."""
    import numpy as np
    import rasterio
    del mtime_ns,size_bytes
    with rasterio.open(path) as dataset:
        band=dataset.read(1,masked=True)
        data=np.asarray(band.filled(np.nan),dtype=float)
        bounds=(float(dataset.bounds.left),float(dataset.bounds.bottom),
                float(dataset.bounds.right),float(dataset.bounds.top))
        return data,dataset.transform,bounds,str(dataset.crs) if dataset.crs else None


def _sample_bilinear(data, transform, east_m: float, north_m: float) -> float:
    """Package-local copy of TDOC's pixel-centre terrain sampling contract."""
    height,width=data.shape
    col_corner, row_corner = (~transform) * (float(east_m), float(north_m))
    col = min(max(float(col_corner) - 0.5, 0.0), float(width - 1))
    row = min(max(float(row_corner) - 0.5, 0.0), float(height - 1))
    c0, r0 = int(math.floor(col)), int(math.floor(row))
    c1, r1 = min(c0 + 1, width - 1), min(r0 + 1, height - 1)
    dc, dr = col - c0, row - r0
    terrain=0.0
    for key,weight in (((r0,c0),(1-dr)*(1-dc)),((r0,c1),(1-dr)*dc),((r1,c0),dr*(1-dc)),((r1,c1),dr*dc)):
        if weight <= 1e-15: continue
        scalar=float(data[key])
        if not math.isfinite(scalar):
            raise ValueError("dem_nodata_or_non_finite")
        terrain += weight*scalar
    return float(terrain)


def _parts(snapshot_like: Any):
    if isinstance(snapshot_like, dict):
        return dict(snapshot_like.get("context") or {}), dict(snapshot_like.get("std") or {})
    return dict(getattr(snapshot_like, "context", {}) or {}), dict(getattr(snapshot_like, "std", {}) or {})


def resolve_operational_terrain(north_m: float, east_m: float, config) -> dict[str, Any]:
    """Resolve the continuous canonical DEM surface at a hypothetical MESA point."""
    authority={
        "schema":"Layer4-Operational-Terrain-v1",
        "terrain_source":"TDDT_MAP_ROOT/terrain_obstacles.tif",
        "terrain_interpolation":"BILINEAR_PIXEL_CENTRE",
        "simulator_truth_used":False,
        "valid":False,
    }
    try:
        north=float(north_m); east=float(east_m)
        if not all(math.isfinite(v) for v in (north,east)):
            raise ValueError("terrain_position_non_finite")
        map_root=config.env_path("TDDT_MAP_ROOT")
        dem=(Path(map_root)/"terrain_obstacles.tif") if map_root else None
        if dem is None or not dem.exists():
            raise FileNotFoundError("canonical_dem_unavailable")
        stat=dem.stat()
        data,transform,bounds,crs=_load_dem(str(dem.resolve()),int(stat.st_mtime_ns),int(stat.st_size))
        left,bottom,right,top=bounds
        if not (left <= east <= right and bottom <= north <= top):
            raise ValueError("mesa_position_outside_dem_coverage")
        terrain=_sample_bilinear(data,transform,east,north)
        authority.update({"valid":True,"reason":"MESA_HYPOTHETICAL_POSITION_PLUS_DEM",
                          "map_crs":crs,"position_n_m":north,"position_e_m":east,
                          "terrain_elevation_m":terrain})
        return {"ok":True,"terrain_elevation_m":terrain,"authority":authority}
    except Exception as exc:
        authority["reason"]=str(exc) or exc.__class__.__name__
        return {"ok":False,"terrain_elevation_m":None,"authority":authority,
                "reason":authority["reason"]}


def resolve_operational_agl(snapshot_like: Any, config) -> dict[str, Any]:
    """Resolve AGL from operational MESA/STD + canonical DEM, never simulator truth.

    A valid TDOC producer-side authority packet is accepted directly. Otherwise this
    function derives the same quantity from MESA canonical N/E/D and band-1 terrain
    elevation from TDDT_MAP_ROOT/terrain_obstacles.tif. Failure is explicit.
    """
    ctx, std = _parts(snapshot_like)
    auth = dict(ctx.get("agl_authority") or {})
    raw = ctx.get("altitude_agl_m")
    try:
        agl = float(raw) if raw is not None else None
    except Exception:
        agl = None
    if agl is not None and math.isfinite(agl) and auth.get("valid") is True and auth.get("simulator_truth_used") is not True:
        return {"ok": True, "altitude_agl_m": agl, "terrain_elevation_m": auth.get("terrain_elevation_m", ctx.get("terrain_elevation_m")), "authority": auth, "source": "snapshot_context"}
    online_markers=any(k in ctx for k in ("online_episode_id","online_cycle_in_episode","rtmo_run_context","rtmo_data_authority","online_state_ingress","rtmo_pre_release_verify"))
    if agl is not None and math.isfinite(agl) and not auth and not online_markers:
        legacy={"schema":"Layer4-Operational-AGL-v1","valid":True,"reason":"LEGACY_STANDALONE_CONTEXT","authority_class":"REFERENCE_PRIOR","simulator_truth_used":False}
        return {"ok":True,"altitude_agl_m":agl,"terrain_elevation_m":ctx.get("terrain_elevation_m"),"authority":legacy,"source":"legacy_standalone_context"}

    authority = {
        "schema": "Layer4-Operational-AGL-v1",
        "authority_class": "OPERATIONAL",
        "navigation_source": "MESA_NAV",
        "terrain_source": "TDDT_MAP_ROOT/terrain_obstacles.tif",
        "simulator_truth_used": False,
        "valid": False,
    }
    try:
        reps = (std.get("representations") or {}) if isinstance(std, dict) else {}
        mesa = (reps.get("MESA_NAV") or {}) if isinstance(reps, dict) else {}
        cs = mesa.get("canonical_state") or {}
        pos = cs.get("position_ned_m")
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            raw_state = cs.get("raw") or {}
            if all(k in raw_state for k in ("position_n", "position_e", "position_d")):
                pos = [raw_state["position_n"], raw_state["position_e"], raw_state["position_d"]]
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            raise ValueError("mesa_position_unavailable")
        north, east, down = map(float, pos[:3])
        if not all(math.isfinite(v) for v in (north, east, down)):
            raise ValueError("mesa_position_non_finite")
        terrain_res=resolve_operational_terrain(north,east,config)
        if not terrain_res.get("ok"):
            raise ValueError(str(terrain_res.get("reason") or "terrain_unavailable"))
        terrain=float(terrain_res["terrain_elevation_m"])
        terrain_authority=dict(terrain_res.get("authority") or {})
        crs=terrain_authority.get("map_crs")
        altitude_msl = -down
        agl = altitude_msl - terrain
        if not math.isfinite(agl):
            raise ValueError("derived_agl_non_finite")
        authority.update({
            "valid": True, "reason": "MESA_STD_PLUS_DEM", "map_crs": crs,
            "terrain_interpolation": "BILINEAR_PIXEL_CENTRE",
            "position_n_m": north, "position_e_m": east, "position_d_m": down,
            "altitude_msl_m": altitude_msl, "terrain_elevation_m": terrain, "altitude_agl_m": agl,
        })
        return {"ok": True, "altitude_agl_m": agl, "terrain_elevation_m": terrain, "authority": authority, "source": "derived_from_snapshot"}
    except Exception as e:
        authority["reason"] = str(e) or e.__class__.__name__
        return {"ok": False, "altitude_agl_m": None, "terrain_elevation_m": None, "authority": authority, "source": "unavailable", "reason": authority["reason"]}
