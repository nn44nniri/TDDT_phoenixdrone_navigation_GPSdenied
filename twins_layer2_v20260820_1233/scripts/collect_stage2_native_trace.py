#!/usr/bin/env python3
"""Assemble real native FCU/JSBSim property exports into real_native_pairs.jsonl.

This is NOT an exporter/instrumenter. Requires independently recorded native
sample and observation events with native property names, units and source-file
hashes. MAVLink GUI state or PWM alone is insufficient. Never sends commands.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from twins_fmi.native_actuator_binding import NativeReplayRejected
from twins_fmi.stage2_native_event_assembler import assemble, read_events, sha256


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--native-events',type=Path,required=True,
                    help='Actual independently logged native sample/observation events JSONL')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--report',type=Path)
    args=ap.parse_args()
    report={'test':'L2_STAGE2_NATIVE_PAIR_ASSEMBLY','status':'BLOCKED_EXTERNAL',
            'flight_authorized':False,'native_controller_prediction_certified':False,
            'scope':'INDEPENDENT_NATIVE_SOURCE_PAIRS_NOT_ONLINE_RELEASE'}
    rc=2
    try:
        if args.output.exists():
            raise NativeReplayRejected('output exists; refusing to overwrite actual site evidence')
        events=list(read_events(args.native_events))
        pairs=assemble(events)
        if len(pairs)<3:
            raise NativeReplayRejected('at least three independently recorded native pairs required')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        fd,temp=tempfile.mkstemp(prefix='.stage2_pairs_',suffix='.tmp',dir=args.output.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                for row in pairs: f.write(json.dumps(row,sort_keys=True)+'\n')
            os.replace(temp,args.output)
        finally:
            if os.path.exists(temp):os.unlink(temp)
        report.update(status='NATIVE_PAIRS_ASSEMBLED_UNCERTIFIED',count=len(pairs),
                      output=str(args.output),native_events_sha256=sha256(args.native_events),
                      output_sha256=sha256(args.output))
        rc=0
    except (NativeReplayRejected,OSError,ValueError,TypeError,KeyError,OverflowError) as e:
        report['reason']=str(e)
    print(json.dumps(report,indent=2,ensure_ascii=False))
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    return rc


if __name__=='__main__':raise SystemExit(main())
