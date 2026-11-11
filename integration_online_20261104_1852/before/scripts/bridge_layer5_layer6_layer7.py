#!/usr/bin/env python3
"""Documented L5 -> Layer6Client -> L7 Python interface owned by TDOC.

Without --execute this only checks the input file. It never starts a flight,
re-trains, resets knowledge, or invents a Layer5 decision. The integrated online
workflow uses the same Layer6OnlineAdapter/Layer6Bridge exchange.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.adapters.layer7 import Layer7Adapter


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default="configs/tdoc.yaml")
    parser.add_argument("--request",required=True,help="Complete TDOC transaction JSON; L5/L4/L3 artifacts required")
    parser.add_argument("--output",help="Write the exchange result/Layer7Input as JSON")
    parser.add_argument("--execute",action="store_true",help="Submit the existing FINAL-authorized L5 packet to an already running native stack")
    args=parser.parse_args()
    payload=json.loads(Path(args.request).read_text())
    for key in ("transaction_id","layer5_output","layer4_result","network_snapshot"):
        if not payload.get(key): parser.error("missing "+key)
    if not args.execute:
        result={"ok":True,"status":"INPUT_CHECK_ONLY","actuation_executed":False,
                "transaction_id":payload["transaction_id"],"route":"Layer5 -> Layer6Client -> GUI -> MAVLink -> Layer7Input"}
    else:
        cfg=TDOCConfig.from_yaml(args.config)
        l6=Layer6OnlineAdapter(cfg,Layer5Adapter(cfg))
        try:
            result=l6.invoke(payload)
            if result.get("execution_evidence"):
                result["Layer7Input"]=Layer7Adapter.build_input(
                    transaction_id=payload["transaction_id"],version=payload.get("version","TDOC-runtime-v1"),
                    reference_time_s=(payload.get("request") or {}).get("reference_time_s",0.),
                    execution_evidence=result["execution_evidence"],layer4_result=payload["layer4_result"],
                    layer5_result=payload["layer5_output"],network_snapshot=payload["network_snapshot"],
                    request=payload.get("request") or {},layer4_root=cfg.path_value("TDDT_LAYER4_ROOT"))
        finally:
            l6.close()
    rendered=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output: Path(args.output).write_text(rendered+"\n")
    else: print(rendered)
    return 0 if result.get("ok") else 2


if __name__=="__main__":
    raise SystemExit(main())
