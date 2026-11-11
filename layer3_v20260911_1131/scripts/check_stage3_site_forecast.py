#!/usr/bin/env python3
"""Read-only, fail-closed precheck for Layer-3 integration test forecast.

This checks ONLY whether test date is supported by the active climate forcing.
It does not assert correct climate physics, emit authority, change runtime config,
or write unless --output is an explicit new file.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/'tests'))
from site_forecast_fixture import effective_file, covered_instant


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--deps-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    ns=parser.parse_args()
    if ns.output.exists():
        print(json.dumps({'status':'BLOCKED_EXTERNAL','reason':'refuse_overwrite','path':str(ns.output)}))
        return 2
    try:
        weather=effective_file(ns.deps_root.resolve())
        if weather is None: raise RuntimeError('No forcing source resolved')
        epoch, detail=covered_instant(weather,os.environ.get('L3_TEST_CLIMATE_TIME_UTC') or None)
        report={'status':'SITE_FORECAST_TEST_TIME_SUPPORTED','test_epoch_utc':epoch,
                'evidence':detail,'data_mutated':False,'runtime_mission_epoch_mutated':False,
                'native_controller_certified':False,'flight_authorized':False}
    except Exception as exc:
        report={'status':'BLOCKED_EXTERNAL','reason':str(exc),'flight_authorized':False}
    ns.output.parent.mkdir(parents=True,exist_ok=True)
    with ns.output.open('x',encoding='utf-8') as f: json.dump(report,f,indent=2,ensure_ascii=False);f.write('\n')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0 if report['status']=='SITE_FORECAST_TEST_TIME_SUPPORTED' else 2

if __name__=='__main__': raise SystemExit(main())
