from __future__ import annotations
import json
from pathlib import Path
from typing import Any

REFERENCE_SUMMARY = "boulder_route_overall_plotly_3d_summary.json"
REFERENCE_RELATIVE = Path("route_3d_magnetic/overall_route") / REFERENCE_SUMMARY
REFERENCE_LONLAT = (-105.300, 40.010, -105.280, 40.025)


def _point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try: return float(value[0]), float(value[1])
        except Exception: return None
    if not isinstance(value, dict): return None
    pairs = [
        ("x", "y"), ("map_x", "map_y"), ("easting", "northing"),
        ("lon", "lat"), ("longitude", "latitude"),
    ]
    lower = {str(k).lower(): v for k, v in value.items()}
    for a,b in pairs:
        if a in lower and b in lower:
            try: return float(lower[a]), float(lower[b])
            except Exception: pass
    for key in ("coordinates", "coordinate", "xy", "lonlat"):
        if key in lower:
            p=_point(lower[key])
            if p: return p
    return None


def _find_named(obj: Any, names: set[str]) -> tuple[float,float] | None:
    if isinstance(obj, dict):
        for k,v in obj.items():
            if str(k).lower() in names:
                p=_point(v)
                if p: return p
        for v in obj.values():
            p=_find_named(v,names)
            if p: return p
    elif isinstance(obj, list):
        for v in obj:
            p=_find_named(v,names)
            if p: return p
    return None


def find_reference_summary(map_root: str | Path) -> Path | None:
    root=Path(map_root).expanduser().resolve()
    if root.is_file(): root=root.parent
    preferred=root/REFERENCE_RELATIVE
    if preferred.is_file(): return preferred
    direct=root/REFERENCE_SUMMARY
    if direct.is_file(): return direct
    found=sorted(root.rglob(REFERENCE_SUMMARY)) if root.exists() else []
    return found[0] if found else None


def _transform_if_needed(x: float, y: float, raster_crs: Any, bounds: tuple[float,float,float,float]) -> tuple[float,float]:
    left,bottom,right,top=bounds
    if left <= x <= right and bottom <= y <= top: return x,y
    # Reference route inputs are geographic longitude/latitude. Transform when the DEM is projected.
    if -180 <= x <= 180 and -90 <= y <= 90 and raster_crs:
        try:
            from rasterio.warp import transform
            xx,yy=transform("EPSG:4326", raster_crs, [x], [y])
            return float(xx[0]),float(yy[0])
        except Exception: pass
    return x,y


def resolve_reference_endpoints(map_root: str | Path, raster_crs: Any,
                                bounds: tuple[float,float,float,float],
                                explicit: tuple[float|None,float|None,float|None,float|None] = (None,None,None,None)) -> tuple[tuple[float,float,float,float], dict[str,Any]]:
    if all(v is not None for v in explicit):
        vals=tuple(float(v) for v in explicit)
        source={"type":"explicit_cli","path":None,"raw":list(vals)}
    else:
        summary=find_reference_summary(map_root)
        vals=None
        if summary:
            try:
                data=json.loads(summary.read_text(encoding="utf-8"))
                origin=_find_named(data,{"origin","start","source","route_origin"})
                destination=_find_named(data,{"destination","end","target","goal","route_destination"})
                if origin and destination:
                    vals=(*origin,*destination)
                    source={"type":"boulder_route_summary","path":str(summary),"raw":list(vals)}
            except Exception:
                vals=None
        if vals is None:
            vals=REFERENCE_LONLAT
            source={"type":"documented_boulder_route_reference","path":str(summary) if summary else None,"raw":list(vals)}
    ox,oy,dx,dy=vals
    ox,oy=_transform_if_needed(ox,oy,raster_crs,bounds)
    dx,dy=_transform_if_needed(dx,dy,raster_crs,bounds)
    left,bottom,right,top=bounds
    for label,x,y in (("origin",ox,oy),("destination",dx,dy)):
        if not (left <= x <= right and bottom <= y <= top):
            raise ValueError(f"Reference {label} ({x}, {y}) is outside terrain bounds {bounds}")
    source["resolved"]=[ox,oy,dx,dy]
    source["raster_crs"]=str(raster_crs) if raster_crs else "unknown"
    return (ox,oy,dx,dy),source
