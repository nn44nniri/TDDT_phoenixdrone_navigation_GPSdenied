#!/usr/bin/env python3
from __future__ import annotations
import argparse,importlib.util
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default=str(Path(__file__).resolve().parents[1])); a=ap.parse_args(); root=Path(a.root).resolve()
    p=root/'scripts/run_training_workflows.py'; spec=importlib.util.spec_from_file_location('tdoc_workflow_post',p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    out=root/'reports/offline/mission'; result=m.postprocess_mission_diagnostics(out); print(result); return 0
if __name__=='__main__': raise SystemExit(main())
