#!/usr/bin/env python3
"""Read-only Stage-4 pilot/native readiness audit. No actuation; never issues certificate."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--snapshot",required=True,help="Layer-3 snapshot JSON; immutable input")
    parser.add_argument("--output",required=True,help="New report filename (no overwrite)")
    args=parser.parse_args()
    from layer4.native_controller_contract import readiness
    from types import SimpleNamespace
    try:
        path=Path(args.snapshot).resolve(strict=True)
        out=Path(args.output).resolve()
        if out.exists(): raise ValueError("refusing to overwrite report")
        raw=json.loads(path.read_text(encoding="utf-8"))
        snap=raw.get("snapshot",raw)
        if not isinstance(snap,dict): raise ValueError("snapshot JSON must be an object")
        obj=SimpleNamespace(context=snap.get("context") or {},provenance=snap.get("provenance") or {})
        audit=readiness(obj)
        audit.update({"test":"STAGE4_NATIVE_PILOT_READINESS","status":("BLOCKED_NATIVE_CONTROLLER" if audit["native_pilot_execution_requested"] else "MODEL_ONLY_FMU_SCOPE"),"snapshot_source":str(path),"native_controller_certified":False,"flight_authorized":False})
        if not out.parent.is_dir(): raise ValueError("report parent directory missing")
        with out.open('x',encoding='utf-8') as stream: json.dump(audit,stream,indent=2,ensure_ascii=False);stream.write('\n')
        print(json.dumps(audit,indent=2,ensure_ascii=False))
        return 2 if audit["native_pilot_execution_requested"] else 0
    except (OSError,ValueError,TypeError,json.JSONDecodeError) as e:
        print(json.dumps({"status":"BLOCKED_INVALID_INPUT","reason":str(e),"flight_authorized":False}));return 2

if __name__=="__main__": sys.exit(main())
