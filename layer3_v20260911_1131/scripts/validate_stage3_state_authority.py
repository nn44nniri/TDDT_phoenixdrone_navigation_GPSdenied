#!/usr/bin/env python3
"""Read-only check of explicitly labelled Stage-3 source evidence; never transmit a command."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from layer3.state_authority import bind_state_authority, StateAuthorityError

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--reference-time-s',type=float,required=True)
    ap.add_argument('--max-age-s',type=float,default=1.)
    ap.add_argument('--max-future-skew-s',type=float,default=.05)
    ap.add_argument('--output',type=Path,default=None)
    args=ap.parse_args()
    try:
        obj=json.loads(args.input.read_text(encoding='utf-8'))
        result=bind_state_authority(obj,reference_time_s=args.reference_time_s,
             max_age_s=args.max_age_s,max_future_skew_s=args.max_future_skew_s)
        status='STATE_BINDING_DIAGNOSTIC_PASS_UNCERTIFIED'
        code=0
    except (ValueError, OSError, TypeError) as exc:
        result={'reason':str(exc),'flight_authorized':False,'native_controller_certified':False}
        status='BLOCKED_INVALID_STATE_AUTHORITY'
        code=2
    result={'test':'L3_STAGE3_STATE_AUTHORITY','status':status,**result}
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    if args.output:
        if args.output.exists():
            print('refusing to overwrite report',file=sys.stderr);return 3
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return code
if __name__=='__main__':sys.exit(main())
