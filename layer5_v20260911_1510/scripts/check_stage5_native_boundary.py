#!/usr/bin/env python3
"""Read-only audit of the Layer4->Layer5 native authorization boundary."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/'src'))
from layer5.native_pilot_boundary import audit


def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--learning-mode',choices=['OFFLINE_SE','ONLINE_JSBSIM'],required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        obj=json.loads(args.input.read_text(encoding='utf-8'))
        if not isinstance(obj,dict):raise ValueError('input Layer4 JSON must be an object')
        result={'test':'STAGE5_NATIVE_PILOT_BOUNDARY','input':str(args.input.resolve()),
                'learning_mode':args.learning_mode,**audit(obj,learning_mode=args.learning_mode)}
        exitcode=2 if result['online_native_pilot_required'] else 0
        if args.output is not None:
            with args.output.open('x',encoding='utf-8') as f:
                json.dump(result,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n')
        print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False))
        return exitcode
    except (ValueError,OSError,TypeError) as exc:
        print(json.dumps({'test':'STAGE5_NATIVE_PILOT_BOUNDARY','status':'BLOCKED_INVALID_INPUT',
                          'reason':str(exc),'flight_authorized':False},ensure_ascii=False))
        return 3


if __name__=='__main__':raise SystemExit(main())
