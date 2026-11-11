#!/usr/bin/env python3
"""Read-only strict spatial source audit; never rebuilds sources or authorizes flight."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from site_spatial_contract import audit_dataset


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--region',required=True)
    parser.add_argument('--dataset-dir',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--expected-region-sha256')
    parser.add_argument('--expected-terrain-sha256')
    parser.add_argument('--vehicle-altitude-msl-m',type=float)
    parser.add_argument('--require-native-agl',action='store_true')
    args=parser.parse_args()
    out=Path(args.output)
    if out.exists() or not out.parent.is_dir():
        print(json.dumps({'status':'BLOCKED_REPORT_PATH','reason':'report_exists_or_parent_missing','flight_authorized':False},indent=2));return 3
    try:
        data=audit_dataset(args.region,args.dataset_dir,
                           vehicle_altitude_msl_m=args.vehicle_altitude_msl_m,
                           expected_region_sha256=args.expected_region_sha256,
                           expected_terrain_sha256=args.expected_terrain_sha256)
        if args.require_native_agl:
            data['status']='BLOCKED_NATIVE_AGL_CONFIRMATION_UNAVAILABLE'
            data['reason']='vertical_datum_and_native_sensor_authority_not_verified'
            rc=2
        else: rc=0
    except Exception as exc:
        data={'schema':'TDDT-Boulder-SpatialBinding-v1','status':'BLOCKED_SPATIAL_CONTRACT',
              'reason':f'{type(exc).__name__}: {exc}',
              'native_agl_verified':False,'native_controller_certified':False,'flight_authorized':False}
        rc=2
    data={'test':'STAGE9_BOULDER_SITE_MAP_AUDIT',**data}
    # Exclusive creation only; errors are not silently replaced.
    with out.open('x',encoding='utf-8') as f:
        json.dump(data,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n')
    print(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False))
    return rc

if __name__=='__main__':sys.exit(main())
