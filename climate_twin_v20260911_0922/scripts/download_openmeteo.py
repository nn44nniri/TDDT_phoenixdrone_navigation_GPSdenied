#!/usr/bin/env python3
"""Download future Open-Meteo forcing data for a WGS 84 GeoJSON region."""
from __future__ import annotations

import argparse
import csv
import json
import hashlib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from region_geojson import load_region_geojson, sampling_points


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download an offline Open-Meteo forcing bundle for a WGS 84 GeoJSON region."
    )
    ap.add_argument("--region", default="region/region_v1.geojson",
                    help="RFC 7946 GeoJSON region in WGS 84 longitude/latitude coordinates")
    ap.add_argument("--output", default="climate_dataset/openmeteo_24h.csv")
    ap.add_argument("--start", help="ISO local time; default is current rounded hour")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--timezone", default="auto")
    ap.add_argument("--max-source-points", type=int, default=5,
                    help="Maximum source points requested from Open-Meteo (center plus boundary samples)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate GeoJSON and print request URLs without downloading")
    args = ap.parse_args()

    if args.hours < 1:
        raise ValueError("--hours must be at least 1")
    region = load_region_geojson(args.region)
    points = sampling_points(region, args.max_source_points)
    start = parse_time(args.start)
    end = start + timedelta(hours=args.hours - 1)
    metadata = {
        "schema": "micromet_openmeteo_region_bundle_v2",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "hours": args.hours,
        "source": "Open-Meteo Forecast API",
        "region_file": str(args.region),
        "region_name": region["name"],
        "region_bbox_wgs84": region["bbox"],
        "region_geometry_type": region["geometry_type"],
        "region_crs": region["crs"],
        "region_sha256": hashlib.sha256(Path(args.region).read_bytes()).hexdigest(),
        "region_feature_properties": region["feature"].get("properties") or {},
        "points": [],
    }
    rows: list[dict] = []

    for point in points:
        query = {
            "latitude": point["latitude"],
            "longitude": point["longitude"],
            "hourly": (
                "temperature_2m,relative_humidity_2m,precipitation,surface_pressure,"
                "wind_speed_10m,wind_direction_10m,shortwave_radiation"
            ),
            "wind_speed_unit": "ms",
            "timezone": args.timezone,
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(query)
        point_meta = {
            "name": point["name"],
            "latitude": point["latitude"],
            "longitude": point["longitude"],
            "url": url,
        }
        if args.dry_run:
            metadata["points"].append(point_meta)
            continue

        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.load(response)
        point_meta["elevation"] = data.get("elevation")
        metadata["points"].append(point_meta)
        hourly = data["hourly"]
        for i, timestamp in enumerate(hourly["time"]):
            dt = datetime.fromisoformat(timestamp)
            if dt < start.replace(tzinfo=None) or dt > end.replace(tzinfo=None):
                continue
            rows.append({
                "point": point["name"],
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "elevation_m": data.get("elevation", 0),
                "time": timestamp,
                "temperature_2m_C": hourly["temperature_2m"][i],
                "relative_humidity_2m_pct": hourly["relative_humidity_2m"][i],
                "precipitation_mm": hourly["precipitation"][i],
                "surface_pressure_hPa": hourly["surface_pressure"][i],
                "wind_speed_10m_ms": hourly["wind_speed_10m"][i],
                "wind_direction_10m_deg": hourly["wind_direction_10m"][i],
                "shortwave_radiation_Wm2": hourly["shortwave_radiation"][i],
            })

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = output.with_suffix(output.suffix + ".meta.json")
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "region": str(args.region),
            "region_bbox_wgs84": region["bbox"],
            "source_points": len(points),
            "metadata_file": str(metadata_path),
            "requests": [p["url"] for p in metadata["points"]],
        }, indent=2))
        return

    if not rows:
        raise RuntimeError("Open-Meteo returned no rows in the requested future time interval")
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "output": str(output),
        "records": len(rows),
        "source_points": len(points),
        "hours": args.hours,
        "region": str(args.region),
        "region_bbox_wgs84": region["bbox"],
        "metadata_file": str(metadata_path),
    }, indent=2))


if __name__ == "__main__":
    main()
