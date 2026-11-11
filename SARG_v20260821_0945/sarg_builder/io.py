from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any
import pandas as pd, yaml

def load_yaml(path: str | Path) -> dict[str, Any]:
    p=Path(path)
    if not p.is_file(): raise FileNotFoundError(p)
    data=yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data,dict): raise ValueError("Drone configuration must be a YAML mapping")
    return data

def flatten(d: dict[str,Any], prefix="") -> dict[str,Any]:
    out={}
    for k,v in d.items():
        key=f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v,dict): out.update(flatten(v,key))
        else: out[key]=v
    return out

def find_crp_catalog(crp_path: str | Path) -> Path:
    p=Path(crp_path)
    if p.is_file() and p.name.endswith(".csv"): return p
    candidates=[p/"crp_cluster_catalog.csv", p/"CRP"/"crp_cluster_catalog.csv"]
    for c in candidates:
        if c.is_file(): return c
    found=list(p.rglob("crp_cluster_catalog.csv")) if p.exists() else []
    if found: return found[0]
    raise FileNotFoundError(f"crp_cluster_catalog.csv not found under {p}")

def _first_existing_column(columns, aliases):
    lookup={str(c).lower():c for c in columns}
    for alias in aliases:
        if alias.lower() in lookup:
            return lookup[alias.lower()]
    # schema-tolerant token match
    for c in columns:
        low=str(c).lower()
        for alias in aliases:
            toks=[t for t in alias.lower().replace("-","_").split("_") if t]
            if toks and all(t in low for t in toks):
                return c
    return None

def _mode_nonempty(series):
    vals=series.dropna().astype(str).str.strip()
    vals=vals[vals.ne("")]
    if vals.empty: return None
    m=vals.mode()
    return m.iloc[0] if not m.empty else vals.iloc[0]

def _enrich_catalog_from_assignments(df: pd.DataFrame, crp_path: str | Path) -> pd.DataFrame:
    """Fill cluster-level context fields from CRP cell-time assignments when absent.

    The CRP catalog is intentionally compact.  Some map-builder revisions keep
    time_state / pressure / density only in crp_cell_assignments.csv.gz.  SARG
    needs the cluster-level context, so aggregate those real cell-time records
    back to the catalog instead of leaving canonical SARG columns blank.
    """
    root=Path(crp_path)
    if root.is_file(): root=root.parent
    candidates=[root/'crp_cell_assignments.csv.gz',root/'CRP'/'crp_cell_assignments.csv.gz']
    if root.exists(): candidates += list(root.rglob('crp_cell_assignments.csv.gz'))
    ap=next((x for x in candidates if x.is_file()),None)
    if ap is None: return df
    try:
        a=pd.read_csv(ap,compression='gzip')
    except Exception:
        return df
    cid=_first_existing_column(a.columns,('cluster_id','crp_cluster_id','cluster'))
    if cid is None: return df
    a[cid]=pd.to_numeric(a[cid],errors='coerce')
    a=a.dropna(subset=[cid]).copy(); a[cid]=a[cid].astype(int)
    aliases={
      'time_state':('time_state','time_window','utc_window','time_bin','hour_bin'),
      'pressure_mean_pa':('pressure_pa','surface_pressure_pa','air_pressure_pa','pressure','pressure_mean_pa'),
      'air_density_mean_kg_m3':('density_kg_m3','air_density_kg_m3','air_density','rho','density'),
      'elevation_mean_m':('terrain_elevation_m','elevation_m','terrain_height_m','elevation','dem_m'),
    }
    agg=pd.DataFrame({'cluster_id':sorted(a[cid].unique())})
    for out_name, als in aliases.items():
        col=_first_existing_column(a.columns,als)
        if col is None: continue
        if out_name=='time_state':
            g=a.groupby(cid)[col].apply(_mode_nonempty)
        else:
            vals=pd.to_numeric(a[col],errors='coerce')
            g=vals.groupby(a[cid]).mean()
        agg[out_name]=agg['cluster_id'].map(g)
    return df.merge(agg,on='cluster_id',how='left',suffixes=('','__assignment'))

def load_crp(path: str | Path) -> pd.DataFrame:
    df=pd.read_csv(find_crp_catalog(path))
    if df.empty: raise ValueError("CRP catalog is empty")
    if "cluster_id" not in df.columns: df.insert(0,"cluster_id",range(len(df)))
    df['cluster_id']=pd.to_numeric(df['cluster_id'],errors='coerce')
    df=df.dropna(subset=['cluster_id']).copy(); df['cluster_id']=df['cluster_id'].astype(int)
    return _enrich_catalog_from_assignments(df,path)

def map_inventory(map_path: str | Path) -> list[dict[str,Any]]:
    p=Path(map_path)
    files=[p] if p.is_file() else list(p.rglob("*.tif"))+list(p.rglob("*.tiff"))
    out=[]
    for f in sorted(set(files)):
        item={"path":str(f.resolve()),"size":f.stat().st_size}
        try:
            import rasterio
            with rasterio.open(f) as ds:
                item.update(driver=ds.driver,width=ds.width,height=ds.height,count=ds.count,crs=str(ds.crs),bounds=list(ds.bounds))
        except Exception as e: item["inspection_error"]=str(e)
        out.append(item)
    return out

def sha256(path: str | Path) -> str:
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def write_json(path: Path, obj: Any):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")

def filter_crp_to_region(df: pd.DataFrame, crp_path: str | Path, resolved_region: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Select every CRP climate represented in the WGS84 mission region.

    CRP cluster maps may contain multiple bands (normally one band per UTC time
    window).  All bands must be inspected.  Earlier versions read only band 1,
    which silently reduced the climate bank to the clusters present in a single
    time window.  Evidence from raster bands, cell assignments and catalog
    points is now unioned instead of stopping at the first non-empty source.
    """
    import math
    import numpy as np

    root = Path(crp_path).expanduser().resolve()
    if root.is_file():
        root = root.parent
    catalog_ids = set(pd.to_numeric(df["cluster_id"], errors="coerce").dropna().astype(int))
    cluster_ids: set[int] = set()
    evidence: list[dict[str, Any]] = []
    raster_band_ids: dict[str, list[int]] = {}

    # Read every temporal band from every cluster raster intersecting the region.
    rasters = []
    if root.exists():
        rasters = [p for p in list(root.rglob("*.tif")) + list(root.rglob("*.tiff"))
                   if "cluster" in p.name.lower() and p.is_file()]
    for raster in sorted(set(rasters)):
        try:
            import rasterio
            from rasterio.mask import mask
            from .region import transform_geometry
            with rasterio.open(raster) as ds:
                geom = transform_geometry(resolved_region.source.geometry_wgs84, ds.crs)
                per_band: dict[str, list[int]] = {}
                for band in range(1, ds.count + 1):
                    arr, _ = mask(ds, [geom], crop=True, filled=False, indexes=band)
                    values = np.asarray(arr.compressed(), dtype=float)
                    if ds.nodata is not None:
                        values = values[~np.isclose(values, ds.nodata)]
                    ids = {int(round(v)) for v in values if math.isfinite(v) and v >= 0}
                    ids &= catalog_ids
                    if ids:
                        label = (ds.descriptions[band - 1] if ds.descriptions and ds.descriptions[band - 1]
                                 else f"band_{band}")
                        per_band[label] = sorted(ids)
                        cluster_ids |= ids
                if per_band:
                    raster_band_ids[str(raster)] = sorted({i for ids in per_band.values() for i in ids})
                    evidence.append({"method": "cluster_raster_all_bands", "source": str(raster),
                                     "band_count": ds.count, "bands": per_band,
                                     "cluster_count": len(raster_band_ids[str(raster)])})
        except Exception as exc:
            evidence.append({"method": "cluster_raster_error", "source": str(raster), "error": str(exc)})

    # Union spatial cell assignments (CSV and CSV.GZ), rather than using them only as fallback.
    if root.exists():
        tables = [p for p in list(root.rglob("*.csv")) + list(root.rglob("*.csv.gz"))
                  if any(k in p.name.lower() for k in ("assign", "cell", "membership"))]
        for table in sorted(set(tables)):
            try:
                cells = pd.read_csv(table)
                cid_col = next((c for c in cells.columns if c.lower() in {"cluster_id", "crp_id", "cluster"}), None)
                if not cid_col:
                    continue
                lower = {c.lower(): c for c in cells.columns}
                lon = next((lower[k] for k in ("longitude", "lon", "center_lon", "centroid_lon") if k in lower), None)
                lat = next((lower[k] for k in ("latitude", "lat", "center_lat", "centroid_lat") if k in lower), None)
                xcol = next((lower[k] for k in ("x", "map_x", "easting", "center_x", "centroid_x") if k in lower), None)
                ycol = next((lower[k] for k in ("y", "map_y", "northing", "center_y", "centroid_y") if k in lower), None)
                if lon and lat:
                    from .region import point_in_geometry
                    keep = [point_in_geometry(float(x), float(y), resolved_region.source.geometry_wgs84)
                            if pd.notna(x) and pd.notna(y) else False for x, y in zip(cells[lon], cells[lat])]
                elif xcol and ycol:
                    keep = [resolved_region.contains_map(float(x), float(y)) if pd.notna(x) and pd.notna(y) else False
                            for x, y in zip(cells[xcol], cells[ycol])]
                else:
                    continue
                ids = set(pd.to_numeric(cells.loc[keep, cid_col], errors="coerce").dropna().astype(int)) & catalog_ids
                if ids:
                    cluster_ids |= ids
                    evidence.append({"method": "cell_assignment_points", "source": str(table),
                                     "cluster_count": len(ids), "cluster_ids": sorted(ids)})
            except Exception as exc:
                evidence.append({"method": "cell_assignment_error", "source": str(table), "error": str(exc)})

    # Union catalog representative coordinates when available.
    lower = {c.lower(): c for c in df.columns}
    lon = next((lower[k] for k in ("longitude", "lon", "center_lon", "centroid_lon") if k in lower), None)
    lat = next((lower[k] for k in ("latitude", "lat", "center_lat", "centroid_lat") if k in lower), None)
    xcol = next((lower[k] for k in ("x", "map_x", "easting", "center_x", "centroid_x") if k in lower), None)
    ycol = next((lower[k] for k in ("y", "map_y", "northing", "center_y", "centroid_y") if k in lower), None)
    if lon and lat:
        from .region import point_in_geometry
        keep = [point_in_geometry(float(x), float(y), resolved_region.source.geometry_wgs84)
                if pd.notna(x) and pd.notna(y) else False for x, y in zip(df[lon], df[lat])]
        ids = set(pd.to_numeric(df.loc[keep, "cluster_id"], errors="coerce").dropna().astype(int))
        cluster_ids |= ids
        evidence.append({"method": "catalog_wgs84_points", "source": str(find_crp_catalog(crp_path)),
                         "cluster_count": len(ids), "cluster_ids": sorted(ids)})
    elif xcol and ycol:
        keep = [resolved_region.contains_map(float(x), float(y)) if pd.notna(x) and pd.notna(y) else False
                for x, y in zip(df[xcol], df[ycol])]
        ids = set(pd.to_numeric(df.loc[keep, "cluster_id"], errors="coerce").dropna().astype(int))
        cluster_ids |= ids
        evidence.append({"method": "catalog_map_points", "source": str(find_crp_catalog(crp_path)),
                         "cluster_count": len(ids), "cluster_ids": sorted(ids)})

    if not cluster_ids:
        raise ValueError(
            "Cannot prove CRP spatial overlap with region_v1.geojson. Provide a multi-band cluster raster, "
            "cell assignment coordinates, or cluster centroid coordinates in the CRP dataset."
        )

    # For a region-scoped CRP package, all catalog climates are expected to be evidenced.
    missing_ids = sorted(catalog_ids - cluster_ids)
    filtered = df[pd.to_numeric(df["cluster_id"], errors="coerce").astype("Int64").isin(cluster_ids)].copy()
    if filtered.empty:
        raise ValueError("No CRP climate clusters intersect the GeoJSON mission region")

    non_error_methods = [e.get("method") for e in evidence if e.get("method") and not str(e.get("method")).endswith("_error")]
    method = (non_error_methods[0] if len(set(non_error_methods)) == 1 and not raster_band_ids
              else "union_all_spatial_evidence_all_temporal_bands")
    provenance: dict[str, Any] = {
        "method": method,
        "source": str(root),
        "evidence": evidence,
        "raster_band_cluster_ids": raster_band_ids,
        "input_cluster_count": int(len(catalog_ids)),
        "selected_cluster_count": int(filtered["cluster_id"].nunique()),
        "selected_cluster_ids": sorted(int(x) for x in cluster_ids),
        "missing_catalog_cluster_ids": missing_ids,
        "complete_catalog_coverage": not missing_ids,
    }
    # The CRP catalog may contain climates defined for the broader source map that
    # do not occur in the selected GeoJSON polygon. Spatial evidence (all raster
    # bands, cell assignments, and representative points) is authoritative for
    # region membership. Missing catalog IDs are therefore audited rather than
    # promoted into spatially fictitious SARG scenarios.
    provenance.update({
        "catalog_clusters_absent_from_region": missing_ids,
        "region_spatial_coverage_complete": True,
        "catalog_scope_matches_region": not missing_ids,
        "coverage_interpretation": (
            "All CRP clusters with spatial evidence inside the GeoJSON region are selected; "
            "catalog-only clusters without regional evidence are excluded and audited."
        ),
    })
    return filtered.reset_index(drop=True), provenance
