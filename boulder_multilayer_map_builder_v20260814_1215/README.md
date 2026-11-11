# Real-data multilayer 2.5D regional map builder

This package builds a compact, georeferenced map from real public data.

## Default test region

Boulder, Colorado foothills, USA:

- Center: `40.015000 N, -105.292000 E`
- Projected CRS: `EPSG:32613`
- Nominal size: `3000 m × 3000 m`
- Nominal area: `9.0 km²`

The square deliberately spans part of urban Boulder and the foothill transition,
so it normally contains roads, buildings, vegetation, drainage/water features,
steep natural relief, slope variation, and some mapped artificial structures.

The exact WGS84 bounds are calculated at runtime and written to `metadata.json`.

## Data sources

- OpenStreetMap/Overpass: roads, buildings, water, natural and artificial obstacles.
- Copernicus DEM GLO-30 via Microsoft Planetary Computer: terrain elevation.
- Open-Meteo Historical Weather API: temperature, relative humidity, wind speed
  and wind direction. Wind direction is converted to `u` and `v` before clustering.

## Important accuracy limitations

- Copernicus GLO-30 has about 30 m source resolution. A 10 m output grid is a
  resampled grid, not a new 10 m elevation measurement.
- Historical reanalysis weather is much coarser than a 3 km square. Climate
  rasters within this sample are interpolated regional context, not microclimate CFD.
- OpenStreetMap is volunteered geographic information; completeness varies.
- Missing building heights are estimated from levels or assigned 7.5 m.
- The output is a research/sample map, not a certified aviation obstacle database.

## Installation

Python 3.11 or 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Debian/Ubuntu, GDAL/GEOS/PROJ wheels usually install directly. If required:

```bash
sudo apt-get install -y gdal-bin libgdal-dev libgeos-dev proj-bin
```

## Build the default 9 km² map

```bash
python build_regional_map.py \
  --output outputs/boulder_9km2 \
  --center-lat 40.015 \
  --center-lon -105.292 \
  --size-m 3000 \
  --resolution-m 10 \
  --start-date 2020-05-20 \
  --end-date 2020-05-20
```

The terminal shows a live progress bar for OSM downloads, DEM processing,
terrain derivatives, climate retrieval, clustering, rasterization and file writing.

## Main outputs

### terrain_obstacles.tif

1. terrain elevation, m
2. slope, degrees
3. aspect, degrees clockwise from north
4. road class
5. building mask
6. inferred/mapped building height, m
7. natural obstacle class
8. artificial obstacle class

### climate_contexts.tif

For each UTC bin `00_06`, `06_12`, `12_18`, `18_24`:

- temperature
- relative humidity
- wind `u` east
- wind `v` north
- wind vector speed
- meteorological wind direction FROM
- climate cluster ID
- cluster confidence

### vectors.gpkg

Vector layers for roads, buildings, water, natural obstacles and artificial obstacles.

## Point query

```bash
python map_mosaic_query.py point \
  --terrain outputs/boulder_9km2/terrain_obstacles.tif \
  --climate outputs/boulder_9km2/climate_contexts.tif \
  --centroids outputs/boulder_9km2/climate_centroids.csv \
  --lon -105.292 \
  --lat 40.015 \
  --hour 14
```

Only one raster pixel is read from each file.

## Mosaic

```bash
python map_mosaic_query.py mosaic \
  --input "outputs/tiles/*/terrain_obstacles.tif" \
  --output terrain_mosaic.tif
```

## Extract a geographic tile

```bash
python map_mosaic_query.py tile \
  --input "outputs/tiles/*/terrain_obstacles.tif" \
  --bounds -105.300 40.010 -105.280 40.030 \
  --output terrain_subset.tif
```

## Inspect coverage

```bash
python map_mosaic_query.py info \
  --input outputs/boulder_9km2/terrain_obstacles.tif \
          outputs/boulder_9km2/climate_contexts.tif
```


## 3D Plotly visualisation from the point query script

The `point` command can now generate interactive 3D Plotly HTML outputs.
These files are written into an output folder together with a JSON copy of the
point query result.

Example:

```bash
python map_mosaic_query.py point   --terrain outputs/boulder_9km2/terrain_obstacles.tif   --climate outputs/boulder_9km2/climate_contexts.tif   --centroids outputs/boulder_9km2/climate_centroids.csv   --lon -105.292   --lat 40.015   --hour 14   --plot3d   --plot-output-dir outputs/boulder_9km2/plotly_3d
```

Generated interactive HTML files include:

- `*_terrain_obstacles_3d.html`: terrain elevation, slope, buildings,
  roads, natural obstacles, artificial obstacles.
- `*_climate_dashboard_3d_<timebin>.html`: temperature, humidity,
  wind speed, wind direction, climate cluster ID, cluster confidence,
  plus 3D wind cones derived from `u` and `v`.
- `*_plotly_3d_summary.json`: summary of the produced visualisations.
- `*_point_query.json`: saved point query result.

## Bounded mosaic extraction and 3D regional rendering

The `mosaic3d` command extracts only the geographic dimensions requested with
`--bounds`, mosaics all intersecting terrain and climate tiles, and then creates
interactive Plotly graphs equivalent to the files in `plotly_3d_example`.
The extracted GeoTIFF mosaics are retained beside the HTML graphs.

`--bounds` uses the order `WEST SOUTH EAST NORTH`. Its default CRS is WGS84
(`EPSG:4326`); another CRS can be supplied using `--bounds-crs`.

```bash
python3 map_mosaic_query.py mosaic3d \
  --terrain-input "outputs/tiles/*/terrain_obstacles.tif" \
  --climate-input "outputs/tiles/*/climate_contexts.tif" \
  --bounds -105.300 40.010 -105.280 40.025 \
  --bounds-crs EPSG:4326 \
  --hour 14 \
  --output-dir outputs/boulder_9km2/plotly_3d_mosaic \
  --prefix boulder_subset \
  --max-grid 180
```

For the single-tile sample included in this package:

```bash
python3 map_mosaic_query.py mosaic3d \
  --terrain-input outputs/boulder_9km2/terrain_obstacles.tif \
  --climate-input outputs/boulder_9km2/climate_contexts.tif \
  --bounds -105.300 40.010 -105.280 40.025 \
  --hour 14 \
  --output-dir outputs/boulder_9km2/plotly_3d_mosaic_example \
  --prefix boulder_subset
```

The command produces:

- `<prefix>_terrain_mosaic.tif`
- `<prefix>_climate_mosaic.tif`
- `<prefix>_terrain_obstacles_3d.html`
- `<prefix>_climate_dashboard_3d_<time-bin>.html`
- `<prefix>_mosaic_3d_manifest.json`
- `<prefix>_plotly_3d_summary.json`
- `mosaic_3d_index.html`

The terrain graph shows elevation, slope, buildings, roads, natural obstacles,
and artificial obstacles. The climate dashboard shows temperature, relative
humidity, wind speed, wind direction, horizontal wind vectors, climate cluster
IDs, and cluster confidence over the terrain surface.

When `--bounds` is omitted, the complete union of the input tiles is mosaicked.
All terrain inputs must share the same CRS and band schema; the same requirement
applies independently to all climate inputs.

## Route-based sequential 3D mosaics

The `route3d` command accepts an origin and a destination that must lie inside
the available raster coverage. It creates a route corridor, divides it into
sequential mosaics, extracts terrain and climate GeoTIFFs for every mosaic, and
produces interactive Plotly reports. Origin is shown as a green diamond and
destination as a red diamond in the overall terrain/climate graphs and in the
individual mosaics that contain them.

### Partition by an exact number of mosaics

```bash
python3 map_mosaic_query.py route3d \
  --terrain-input outputs/boulder_9km2/terrain_obstacles.tif \
  --climate-input outputs/boulder_9km2/climate_contexts.tif \
  --origin -105.300 40.010 \
  --destination -105.280 40.025 \
  --mosaic-count 3 \
  --corridor-width-m 300 \
  --hour 14 \
  --output-dir outputs/boulder_9km2/route_3d_output \
  --prefix boulder_route
```

### Partition by maximum nominal area per mosaic

```bash
python3 map_mosaic_query.py route3d \
  --terrain-input outputs/boulder_9km2/terrain_obstacles.tif \
  --climate-input outputs/boulder_9km2/climate_contexts.tif \
  --origin -105.300 40.010 \
  --destination -105.280 40.025 \
  --mosaic-area-km2 0.35 \
  --corridor-width-m 300 \
  --hour 14 \
  --output-dir outputs/boulder_9km2/route_3d_by_area \
  --prefix boulder_route_area
```

Only one of `--mosaic-count` and `--mosaic-area-km2` may be supplied. If
neither is given, four sequential mosaics are produced. The route corridor is
the projected bounding rectangle of the two points, expanded by half the
requested corridor width on every side. It is split along its dominant axis so
that mosaic numbering follows the route direction spatially.

Main outputs include:

- an overall route-corridor terrain GeoTIFF and climate GeoTIFF;
- an overall 3D terrain/obstacle graph with both points marked;
- an overall 3D climate dashboard with both points marked;
- one terrain and one climate GeoTIFF per sequential mosaic;
- one terrain and one climate Plotly report per mosaic;
- per-mosaic JSON reports;
- a route manifest with geodesic distance, azimuth, bounds and areas;
- `route_3d_index.html`, which links all generated artifacts.

A tested example is included in:

```text
outputs/boulder_9km2/route_3d_example/
```

## Build the classified CRP dataset

```bash
python3 build_crp_dataset.py \
  --terrain outputs/boulder_9km2/terrain_obstacles.tif \
  --climate outputs/boulder_9km2/climate_contexts.tif \
  --clusters 32
```

The command creates `outputs/boulder_9km2/CRP`. See `doc/crp_dataset.md`.

### Optional per-cluster 3D Plotly views

When requested, the same script can generate one separate interactive 3D map
for every CRP cluster. Each graph shows the cluster footprint draped on the
terrain surface and written as an HTML file.

```bash
python3 build_crp_dataset.py \
  --terrain outputs/boulder_9km2/terrain_obstacles.tif \
  --climate outputs/boulder_9km2/climate_contexts.tif \
  --clusters 32 \
  --plot3d-clusters \
  --plot3d-dir outputs/boulder_9km2/CRP/plotly_3d_clusters \
  --plot-max-grid 140 \
  --plot-max-points 6000
```

This creates, by default:

- one HTML file per cluster, such as `crp_cluster_00_3d.html`;
- `crp_cluster_3d_index.html`;
- `crp_cluster_3d_manifest.json`.

A tested example is included in:

```text
outputs/boulder_9km2/CRP/plotly_3d_clusters_example/
```


## NOAA EMAG2 magnetic-anomaly layer

`build_regional_map.py` now creates a persistent offline magnetic-source bundle
for the requested dataset. The default source is NOAA NCEI EMAG2 v3 (upward
continued to 4 km above the WGS84 ellipsoid). Before cropping or reprojection,
the program downloads the complete anomaly, error and source-code GeoTIFFs into
`<output>/magnetic/`. Rasterio then reads only these local files. No magnetic
processing is performed directly from the remote URL.

```bash
python3 build_regional_map.py \
  --output outputs/boulder_9km2 \
  --center-lat 40.015 \
  --center-lon -105.292 \
  --size-m 3000 \
  --resolution-m 10 \
  --start-date 2020-05-20 \
  --end-date 2020-05-20
```

The new `magnetic_anomaly.tif` contains:

1. `magnetic_anomaly_nt`
2. `magnetic_anomaly_error_nt`
3. `magnetic_source_code`
4. `magnetic_anomaly_gradient_nt_per_m`

Useful options are:

- `--skip-magnetic` to retain the previous build behaviour;
- `--magnetic-anomaly-url`, `--magnetic-error-url`, and `--magnetic-code-url`
  to use a mirror or local GeoTIFF;
- `--magnetic-dir` to select the persistent offline source directory;
- `--magnetic-offline` to forbid network access and use only previously downloaded files;
- `--refresh-magnetic` to redownload or recopy the complete sources;
- `--magnetic-cache-dir` remains as a deprecated alias for backward compatibility.

After the first online execution, the directory normally contains:

```text
outputs/boulder_9km2/magnetic/
├── EMAG2_V3_UpCont_DataTiff.tif
├── EMAG2_V3_Error_DataTiff.tif
├── EMAG2_V3_Code.tif
└── magnetic_sources_manifest.json
```

A later fully offline execution uses:

```bash
python3 build_regional_map.py \
  --output outputs/boulder_9km2 \
  --center-lat 40.015 \
  --center-lon -105.292 \
  --size-m 3000 \
  --resolution-m 10 \
  --start-date 2020-05-20 \
  --end-date 2025-05-20 \
  --magnetic-offline
```

Point extraction and 3D display:

```bash
python3 map_mosaic_query.py point \
  --terrain outputs/boulder_9km2/terrain_obstacles.tif \
  --climate outputs/boulder_9km2/climate_contexts.tif \
  --magnetic outputs/boulder_9km2/magnetic_anomaly.tif \
  --centroids outputs/boulder_9km2/climate_centroids.csv \
  --lon -105.292 --lat 40.015 --hour 14 --plot3d
```

Bounded magnetic mosaics can be produced with `mosaic3d` by adding:

```text
--magnetic-input outputs/boulder_9km2/magnetic_anomaly.tif
```

CRP generation automatically detects `magnetic_anomaly.tif` beside the terrain
raster, or accepts an explicit `--magnetic` path. The CRP signature now includes
`magnetic_anomaly_state` and `magnetic_gradient_state`; numeric anomaly, error,
and gradient values are retained in cell assignments and cluster statistics.

EMAG2 has a source resolution of 2 arc-minutes. Resampling it to a 10 m output
grid does not create 10 m magnetic measurements. NOAA describes this global
product as unsuitable for operational navigation; it should be treated as a
regional prior and validated with local magnetometer surveys before safety-
critical use.

## EMAG2 CRS compatibility fix

Some NOAA EMAG2 GeoTIFF distributions contain a valid geographic affine grid
but omit the embedded CRS tag. `build_regional_map.py` now uses the explicitly
configurable fallback `EPSG:4326` for these files and correctly converts
western-hemisphere WGS84 longitudes to the EMAG2 `0..360` longitude convention.

The fallback may be changed explicitly:

```bash
python3 build_regional_map.py \
  --output outputs/boulder_9km2 \
  --magnetic-source-crs EPSG:4326
```

The magnetic reader also expands sub-cell requests to a valid raster window,
validates cached downloads, accepts local source paths directly, and reports a
clear error when the requested region does not intersect the source grid.

## EMAG2 western-hemisphere longitude handling

NOAA EMAG2 grids may use the longitude interval `0..360` and may begin at a
slightly negative pixel edge. The magnetic reader detects this convention from
the source span and eastern bound, wraps western-hemisphere requests into the
source interval, and shifts the cropped affine transform back into `-180..180`
before reprojection. This supports locations such as Boulder without changing
any existing terrain, climate, mosaic, route, CRP, or Plotly interfaces.


## Download-first offline magnetic workflow

The magnetic implementation deliberately materializes every configured EMAG2
source as a complete local file before it is opened for processing. This fixes
the earlier behavior where successful HTTP range access produced
`magnetic_anomaly.tif` but did not create a persistent `magnetic/` source folder.
The JSON manifest records source URLs, local filenames, byte sizes and SHA-256
hashes. Partial downloads use a `.part` suffix and are never treated as valid
source files.


## Magnetic anomaly support in query, mosaic, route and CRP reports

After `build_regional_map.py` creates `magnetic_anomaly.tif`, the other scripts can consume it without changing the existing terrain/climate workflow.

### Point query and optional 3D report

```bash
python3 map_mosaic_query.py point \
  --terrain outputs/boulder_9km2/terrain_obstacles.tif \
  --climate outputs/boulder_9km2/climate_contexts.tif \
  --magnetic outputs/boulder_9km2/magnetic_anomaly.tif \
  --centroids outputs/boulder_9km2/climate_centroids.csv \
  --lon -105.292 --lat 40.015 --hour 14 --plot3d
```

This adds magnetic values to the point-query JSON and creates `*_magnetic_anomaly_3d.html`.

### Bounded mosaic report

Use `--magnetic-input` with `mosaic3d`. The command writes an extracted magnetic GeoTIFF, a magnetic Plotly report, and magnetic entries in the manifest/index.

### Route report

`route3d` also accepts `--magnetic-input`. It generates an overall magnetic mosaic and report, plus one magnetic GeoTIFF and one magnetic Plotly graph for every sequential route mosaic. Origin and destination markers remain visible on the shared route reports.

```bash
python3 map_mosaic_query.py route3d \
  --terrain-input outputs/boulder_9km2/terrain_obstacles.tif \
  --climate-input outputs/boulder_9km2/climate_contexts.tif \
  --magnetic-input outputs/boulder_9km2/magnetic_anomaly.tif \
  --origin -105.300 40.010 \
  --destination -105.280 40.025 \
  --mosaic-count 3 --corridor-width-m 300 --hour 14 \
  --output-dir outputs/boulder_9km2/route_3d_magnetic \
  --prefix boulder_route
```

### CRP integration and optional cluster reports

`build_crp_dataset.py` auto-detects `magnetic_anomaly.tif` beside the terrain raster, or accepts `--magnetic`. The CRP signature includes `magnetic_anomaly_state` and `magnetic_gradient_state`; numeric anomaly/error/gradient statistics are saved in the cell assignments and cluster catalog. With `--plot3d-clusters`, each cluster HTML graph uses magnetic anomaly values in its hover information and marker colouring.

## Complete offline-first source workflow

`build_regional_map.py` now persists every source required to rebuild the same
requested region without network access.

The first online run downloads and stores:

- the complete queried OSM feature layers for the requested geographic bounds;
- the complete intersecting Copernicus DEM GLO-30 source tiles;
- Open-Meteo ERA5 hourly data in validated calendar-year chunks for every
  climate sampling point and for the complete requested historical interval;
- the complete EMAG2 magnetic source grids.

The default persistent layout is:

```text
outputs/boulder_9km2/
├── offline_sources/
│   ├── osm/
│   │   ├── roads.gpkg
│   │   ├── buildings.gpkg
│   │   ├── water.gpkg
│   │   ├── natural.gpkg
│   │   ├── artificial.gpkg
│   │   └── osm_sources_manifest.json
│   ├── dem/
│   │   ├── <complete Copernicus source tile>.tif
│   │   └── dem_sources_manifest.json
│   └── climate/
│       ├── point_..._<year-range>.csv.gz
│       └── climate_sources_manifest.json
└── magnetic/
    ├── <complete EMAG2 source files>
    └── magnetic_sources_manifest.json
```


### Climate model completeness and fallback

The default historical reanalysis model is now `era5`. This avoids the null-valued
relative-humidity and wind-direction fields observed for some explicit
`era5_land` requests. The required fields remain:

```text
temperature_2m
relative_humidity_2m
wind_speed_10m
wind_direction_10m
```

The model can be selected explicitly:

```bash
--climate-model era5
--climate-fallback-model era5
```

If a requested model returns all expected timestamps but one or more required
variables are null, the script does not waste all HTTP retries on the same
deterministic response. It immediately tries the fallback model, validates every
hour and variable, and records both the requested and actually used model in
`climate_sources_manifest.json`. Cache filenames also include the model name.

A long historical request is divided into calendar-year chunks. Every chunk is
retried independently, checked for duplicate or missing UTC hours, checked for
null values, written atomically, and recorded with its SHA-256 checksum. A
partially completed run can therefore continue by downloading only missing
chunks.

First online build:

```bash
python3 build_regional_map.py \
  --output outputs/boulder_9km2 \
  --center-lat 40.015 \
  --center-lon -105.292 \
  --size-m 3000 \
  --resolution-m 10 \
  --start-date 2015-05-20 \
  --end-date 2025-05-20
```

Complete rerun without network access:

```bash
python3 build_regional_map.py \
  --output outputs/boulder_9km2 \
  --center-lat 40.015 \
  --center-lon -105.292 \
  --size-m 3000 \
  --resolution-m 10 \
  --start-date 2015-05-20 \
  --end-date 2025-05-20 \
  --offline
```

Source-specific offline modes are also available:

```text
--osm-offline
--dem-offline
--climate-offline
--magnetic-offline
```

Source-specific refresh controls are:

```text
--refresh-osm
--refresh-dem
--refresh-climate
--refresh-magnetic
```

`--offline` cannot be combined with a refresh option. Persisted OSM and DEM
manifests are checked against the requested geographic bounds. Climate cache
filenames include sampling coordinates, date intervals, and the selected reanalysis model,
so a changed region, grid, historical interval, or model cannot silently reuse an incompatible chunk.

## Default WGS 84 GeoJSON region

All main scripts now use `region/region_v1.geojson` by default. The GeoJSON contains the requested polygon boundary plus origin and destination points in WGS 84 longitude/latitude order. See `doc/region_geojson_wgs84.md`.

```bash
python3 build_regional_map.py --start-date 2010-05-20 --end-date 2025-05-20
python3 map_mosaic_query.py point --hour 14
python3 map_mosaic_query.py route3d --mosaic-count 3 --hour 14
python3 build_crp_dataset.py --clusters 32
```

Default generated data are placed under `outputs/region_v1_map/`. Explicit paths and legacy build coordinates remain supported. Tiled GeoTIFF block dimensions are always multiples of 16.

## Exact GeoJSON geometry and endpoint consistency

The package now treats `region/region_v1.geojson` as the single authoritative source for the requested boundary, origin and destination. All Plotly outputs from `map_mosaic_query.py` and `build_crp_dataset.py` overlay the exact boundary and endpoint coordinates from that file. Output manifests also record the GeoJSON SHA-256 digest and exact WGS 84 coordinates. See `doc/region_geojson_exact_consistency.md`.

## GeoJSON coverage validation and blank-margin prevention

The `point`, `mosaic3d`, `route3d`, and CRP workflows use
`region/region_v1.geojson` as the spatial authority. `mosaic3d` and the overall
`route3d` report require complete terrain, climate, and magnetic coverage for
the requested geometry. Route-tile and CRP Plotly boundaries are clipped to
the current raster footprint, preventing a full-region boundary from enlarging
the axes around a smaller mosaic. CRP records are additionally masked to the
exact GeoJSON Polygon/MultiPolygon at pixel-center precision.

## GeoJSON route mosaics: complete raster alignment

When `route3d` is called with `--region-file`, its numbered mosaics are exact
pixel-aligned partitions of the complete regional raster. Their union equals
the full raster associated with the GeoJSON region; they are not limited to the
smaller route corridor. See `doc/route3d_full_region_aligned_mosaics_fix.md`.

## Overpass API multi-server failover

OSM acquisition now uses an ordered, configurable failover pool rather than a single `overpass-api.de` endpoint. The default pool is stored in `config/overpass_endpoints.json`. For each OSM layer, servers are tried round-robin and a failed server is immediately followed by the next configured mirror. The successful endpoint and all attempts are recorded in `offline_sources/osm/osm_sources_manifest.json`.

The normal build command is unchanged. Optional controls are:

```text
--overpass-config
--overpass-endpoints
--osm-retries-per-endpoint
--osm-timeout
--osm-endpoint-cooldown
```

See `doc/osm_overpass_failover.md` for configuration, failover sequence, offline behavior and examples.

### CRP canonical runtime statistics

`build_crp_dataset.py` now preserves every CRP cluster as `C_k=(mu_k,Sigma_k,n_k,q_k)` without changing the existing clustering or spatial products. `CRP/crp_runtime_statistics.json` stores the centroid, empirical covariance, explicit sample count, quality score, feature order, and an optional regularized covariance for stable Mahalanobis-like inversion. The existing cluster catalog is retained and gains `sample_count` and `quality_score` columns.
