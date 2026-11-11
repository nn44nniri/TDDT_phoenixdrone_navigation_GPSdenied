#!/usr/bin/env python3
"""Read-only validator, not SARG builder, FMU runner or flight release."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sarg_builder.site_audit import audit_site_dataset


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--dataset-dir',type=Path,required=True)
    ap.add_argument('--region',type=Path,required=True)
    ap.add_argument('--crp-catalog',type=Path,required=True)
    ap.add_argument('--twins-root',type=Path,help='Check frozen FMU/config hashes against current Layer2, block if they differ')
    ap.add_argument('--expected-region-sha256')
    ap.add_argument('--expected-records',type=int)
    ap.add_argument('--output',type=Path,required=True)
    opts=ap.parse_args()
    if opts.output.exists():
        print(json.dumps({'status':'BLOCKED_SARG_REFERENCE_CONTRACT','reason':'refuse_overwrite_existing_report','flight_authorized':False}));return 2
    try:
        result=audit_site_dataset(opts.dataset_dir,region_file=opts.region,crp_catalog=opts.crp_catalog,
                 twins_root=opts.twins_root,expected_region_sha256=opts.expected_region_sha256,
                 expected_records=opts.expected_records)
        code=0
    except Exception as ex:
        result={'test':'STAGE10_SARG_SITE_READ_ONLY_AUDIT','status':'BLOCKED_SARG_REFERENCE_CONTRACT',
                'reason':f'{type(ex).__name__}:{ex}','native_controller_certified':False,'flight_authorized':False}
        code=2
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    try:
        if not opts.output.parent.is_dir():raise FileNotFoundError('report parent directory missing')
        with opts.output.open('x',encoding='utf-8') as f:
            json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.write('\n')
    except Exception as exc:
        print(f'cannot save report: {exc}',file=sys.stderr)
        return 3
    return code
if __name__=='__main__':sys.exit(main())
