#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
from layer3.config import Layer3Config
from layer3.scenario_report import generate_scenario_reports


def main() -> int:
    ap=argparse.ArgumentParser(description='Run the Layer-3 A* closed-loop three-Twin Fast/Meso/Slow scenario and build 3D/2D Plotly reports.')
    ap.add_argument('--config',default='configs/configuration_boulder.yaml')
    ap.add_argument('--request',default='examples/runtime_request.json')
    ap.add_argument('--steps',type=int,default=36,help='Maximum Fast ticks; scenario may stop after arrival and a Slow boundary.')
    ap.add_argument('--output-dir',default='reports/scenario')
    ap.add_argument('--terrain-grid-n',type=int,default=90,help='Downsampled terrain surface grid size for the standalone 3D report.')
    args=ap.parse_args()
    cfg=Layer3Config.from_yaml(args.config); req=json.loads(Path(args.request).read_text(encoding='utf-8'))
    summary,artifacts=generate_scenario_reports(cfg,req,steps=args.steps,output_dir=args.output_dir,terrain_grid_n=args.terrain_grid_n)
    print(json.dumps({'ok':summary['ok'],'summary':summary,'artifacts':artifacts.to_dict()},indent=2))
    return 0 if summary['ok'] else 2

if __name__=='__main__': raise SystemExit(main())
