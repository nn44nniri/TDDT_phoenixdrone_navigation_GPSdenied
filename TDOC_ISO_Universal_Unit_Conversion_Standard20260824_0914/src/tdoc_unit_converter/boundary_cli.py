"""Read-only CLI: diagnostic cross-layer envelope conversion and comparison."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from .boundary import convert_boundary_envelope
from .errors import ConversionError


def main(argv=None):
    p=argparse.ArgumentParser(description='Opt-in strict, diagnostic-only TDOC cross-layer boundary validator')
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--output',type=Path,help='new, previously nonexistent report file')
    p.add_argument('--target-unit',required=True)
    p.add_argument('--target-frame',required=True)
    p.add_argument('--expected-semantic',required=True)
    p.add_argument('--expected-role',required=True)
    p.add_argument('--expected-producer')
    p.add_argument('--expected-transaction-id')
    p.add_argument('--expected-snapshot-hash')
    p.add_argument('--expected-epoch-id')
    p.add_argument('--reference-time-s',required=True,type=float)
    p.add_argument('--max-age-s',type=float,default=1.)
    p.add_argument('--max-future-skew-s',type=float,default=.05)
    args=p.parse_args(argv)
    if args.output and args.output.exists():
        print(json.dumps({'status':'BLOCKED_REPORT_OVERWRITE','flight_authorized':False},ensure_ascii=False));return 3
    try:
        obj=json.loads(args.input.read_text(encoding='utf-8'))
        result=convert_boundary_envelope(obj,target_unit=args.target_unit,target_frame=args.target_frame,
          expected_semantic=args.expected_semantic,expected_role=args.expected_role,
          reference_time_s=args.reference_time_s,expected_epoch_id=args.expected_epoch_id,
          expected_producer=args.expected_producer,expected_transaction_id=args.expected_transaction_id,
          expected_snapshot_hash=args.expected_snapshot_hash,max_age_s=args.max_age_s,
          max_future_skew_s=args.max_future_skew_s)
        report={'test':'STAGE7_TDOC_CROSS_LAYER_CONTRACT','status':'CONVERSION_DIAGNOSTIC_PASS_UNCERTIFIED',
                'result':result,'native_controller_certified':False,'flight_authorized':False}
        rc=0
    except (OSError,TypeError,ValueError,ConversionError) as exc:
        report={'test':'STAGE7_TDOC_CROSS_LAYER_CONTRACT','status':'BLOCKED_INVALID_BOUNDARY',
                'reason':str(exc),'native_controller_certified':False,'flight_authorized':False}
        rc=2
    if args.output:
        try:
            if not args.output.parent.exists(): raise FileNotFoundError('report parent directory must already exist')
            with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')
        except OSError as exc:
            print(json.dumps({'status':'BLOCKED_REPORT_WRITE','reason':str(exc),'flight_authorized':False},ensure_ascii=False));return 3
    print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    return rc

if __name__=='__main__':sys.exit(main())
