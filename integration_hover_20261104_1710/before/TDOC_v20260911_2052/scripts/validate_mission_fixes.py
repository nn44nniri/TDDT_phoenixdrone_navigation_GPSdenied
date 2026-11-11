#!/usr/bin/env python3
"""Write a source-bound OFFLINE audit, including a negative report on missing data."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from tddt_tdoc.release_gate import write_mission_release_gate

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args()
    report=write_mission_release_gate(args.root)
    print(json.dumps(report,indent=2))
    return 0 if report['ok'] else 2
if __name__=='__main__':
    raise SystemExit(main())
