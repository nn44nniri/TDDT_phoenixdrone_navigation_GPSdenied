from __future__ import annotations
import argparse
from .builder import build_sarg, BuildOptions
from .twins_library import discover_assets
from pathlib import Path


def parser():
    p=argparse.ArgumentParser(description="Build SARG through the independent twins_layer2 Python library")
    p.add_argument("--twins-library", required=True,
                   help="Root of twins_layer2 package containing python/, fmus/ and config/")
    p.add_argument("--wingmpet-fmu", help="Optional override; default: <twins-library>/fmus/WingMPET.fmu")
    p.add_argument("--mesa-nav-fmu", help="Optional override; default: <twins-library>/fmus/MESA_NAV.fmu")
    p.add_argument("--drone-config", help="Optional override; default: <twins-library>/config/phoenix_drone_v1.yaml")
    p.add_argument("--maps",required=True); p.add_argument("--crp",required=True)
    p.add_argument("--region-file", default=str(Path(__file__).resolve().parents[1] / "region" / "region_v1.geojson"),
                   help="Authoritative WGS 84 GeoJSON containing region polygon, Origin and Destination")
    p.add_argument("--output",required=True); p.add_argument("--samples-per-pattern",type=int,default=48); p.add_argument("--seed",type=int,default=42)
    p.add_argument("--allow-mock-fmu",action="store_true",help="Deprecated and rejected: FMI-only strict mode never permits mock FMUs")
    return p


def main(argv=None):
    a=parser().parse_args(argv)
    assets=discover_assets(a.twins_library, wingmpet_fmu=a.wingmpet_fmu,
                           mesa_nav_fmu=a.mesa_nav_fmu, drone_config=a.drone_config)
    out=build_sarg(assets.wingmpet_fmu,assets.mesa_nav_fmu,assets.drone_config,a.maps,a.crp,a.output,
                   BuildOptions(samples_per_pattern=a.samples_per_pattern,seed=a.seed,
                                allow_mock_fmu=a.allow_mock_fmu,twins_library=str(assets.root),region_file=a.region_file))
    print(out)
if __name__=="__main__": main()
