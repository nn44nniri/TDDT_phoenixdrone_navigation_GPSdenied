#!/usr/bin/env python3
"""Documented L5 -> Layer6Client -> L7 Python interface owned by TDOC.

Without --execute this only checks the input file. It never starts a flight,
re-trains, resets knowledge, or invents a Layer5 decision. With --execute it delegates to the canonical mission workflow, which generates
fresh L2-L5 decisions, invokes Layer6 and processes/routes Layer7 feedback.
"""
from __future__ import annotations
import argparse
import json
import sys
import subprocess
import tempfile
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default="configs/tdoc.yaml")
    parser.add_argument("--request",required=True,help="Complete TDOC transaction JSON; L5/L4/L3 artifacts required")
    parser.add_argument("--output",help="Write the exchange result/Layer7Input as JSON")
    parser.add_argument("--execute",action="store_true",help="Run the canonical current-state L2-L7 mission; archived decisions are never replayed")
    args=parser.parse_args()
    payload=json.loads(Path(args.request).read_text())
    for key in ("transaction_id","layer5_output","layer4_result","network_snapshot"):
        if not payload.get(key): parser.error("missing "+key)
    if not args.execute:
        result={"ok":True,"status":"INPUT_CHECK_ONLY","actuation_executed":False,
                "transaction_id":payload["transaction_id"],"route":"Layer5 -> Layer6Client -> GUI -> MAVLink -> Layer7Input"}
    else:
        cfg=TDOCConfig.from_yaml(args.config)
        # A serialized packet cannot carry the process-local current-decision
        # registration required by NativeMissionBinding. Use the normal mission
        # owner so L2/L3, prediction, MPC, execution and L7 routing all run.
        gate=OfflineKnowledgeHandoff(cfg.runtime_root).validate_online_start()
        if not gate.get("ok"):
            result={"ok":False,"status":"NOT_READY","reason":gate.get("reason"),
                    "actuation_executed":False}
        else:
            request=payload.get("request")
            if not isinstance(request,dict) or not request:
                request=json.loads((cfg.package_root/"examples/runtime_request.json").read_text())
            with tempfile.TemporaryDirectory(prefix="tdoc-live-bridge-") as folder:
                request_path=Path(folder)/"request.json"
                request_path.write_text(json.dumps(request,allow_nan=False))
                summary=cfg.package_root/"reports/online/ONLINE_JSBSIM_SUMMARY.json"
                previous=summary.stat() if summary.is_file() else None
                started=time.time()
                command=[sys.executable,str(cfg.package_root/"scripts/run_training_workflows.py"),
                    "--config",str(cfg.path),"--request",str(request_path),
                    "--skip-offline","--online-training","--online-episodes","1",
                    "--online-max-cycles-per-episode-auto","--offline-knowledge-manifest",gate["manifest_path"]]
                completed=subprocess.run(command,cwd=cfg.package_root)
            # Filesystems may round mtimes slightly below time.time(). Require
            # an actual write/replacement as well as a bounded wall-clock age.
            current=summary.stat() if summary.is_file() else None
            fresh=bool(current and current!=previous and current.st_mtime>=started-1.)
            data=json.loads(summary.read_text()) if fresh else {}
            contract=data.get("online_training_contract") or {}
            result={"ok":completed.returncode==0 and contract.get("contract_ok") is True,
                    "status":"CANONICAL_ONLINE_WORKFLOW_FINISHED",
                    "workflow_exit_code":completed.returncode,"fresh_summary":fresh,
                    "summary_path":str(summary) if fresh else None,
                    "online_training_contract":contract,
                    "archived_decision_replayed":False,
                    "visual_acceptance_required":True}
    rendered=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output: Path(args.output).write_text(rendered+"\n")
    else: print(rendered)
    return 0 if result.get("ok") else 2


if __name__=="__main__":
    raise SystemExit(main())
