from __future__ import annotations

import argparse
from pathlib import Path
from .report import ReportOptions, build_reports


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create one Plotly 3D report per SARG policy (regime × CRP), showing all phases")
    p.add_argument("--sarg", required=True, help="Generated SARG dataset directory")
    p.add_argument("--maps", required=True, help="Map directory or terrain GeoTIFF")
    p.add_argument("--reports", help="Output report directory; default: <SARG>/reports")
    p.add_argument("--region-file", default=str(Path(__file__).resolve().parents[1] / "region" / "region_v1.geojson"),
                   help="Authoritative WGS 84 GeoJSON mission region")
    p.add_argument("--origin-x", type=float)
    p.add_argument("--origin-y", type=float)
    p.add_argument("--destination-x", type=float)
    p.add_argument("--destination-y", type=float)
    p.add_argument("--terrain-max-size", type=int, default=180)
    p.add_argument("--route-points", type=int, default=180)
    p.add_argument("--vertical-exaggeration", type=float, default=1.0)
    return p


def main() -> None:
    a = parser().parse_args()
    options = ReportOptions(
        terrain_max_size=a.terrain_max_size,
        route_points=a.route_points,
        vertical_exaggeration=a.vertical_exaggeration,
        origin_x=a.origin_x,
        origin_y=a.origin_y,
        destination_x=a.destination_x,
        destination_y=a.destination_y,
        region_file=a.region_file,
    )
    output = build_reports(a.sarg, a.maps, a.reports, options)
    print(output)


if __name__ == "__main__":
    main()
