#!/usr/bin/env python3
"""Read-only state-pair evidence inspection; NO telemetry capture/actuation/commit."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tddt_layer7.tdoc_adapter import Layer7SideModule
from tddt_layer7.native_pair import check_pair


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',required=True,type=Path)
    ap.add_argument('--output',type=Path)
    ns=ap.parse_args()
    try:
        src=ns.input.resolve(strict=True)
        inp=Layer7SideModule.input_from_dict(json.loads(src.read_text(encoding='utf-8')))
        pair=check_pair(inp)
        valid=pair.required and pair.comparable
        status=('PAIR_COMPARABLE_DIAGNOSTIC_ONLY' if valid else 'BLOCKED_UNPAIRED_NATIVE_EVIDENCE')
        result={'test':'STAGE6_NATIVE_PAIR_DIAGNOSTIC','status':status,**pair.to_dict(),
                'native_controller_certified':False,'dynamic_validation_certified':False,
                'operational_commit_allowed':False,'flight_authorized':False,
                'input':str(src)}
        rc=0 if valid else 2
    except (OSError,ValueError,TypeError,KeyError) as exc:
        result={'test':'STAGE6_NATIVE_PAIR_DIAGNOSTIC','status':'BLOCKED_INVALID_INPUT',
                'reason':str(exc),'native_controller_certified':False,
                'dynamic_validation_certified':False,'operational_commit_allowed':False,
                'flight_authorized':False}
        rc=2
    if ns.output:
        dst=ns.output.expanduser().resolve()
        if not dst.parent.is_dir() or dst.exists():
            print('report parent missing or output already exists',file=sys.stderr)
            return 3
        with dst.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False);f.write('\n')
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False))
    return rc
if __name__=='__main__':raise SystemExit(main())
