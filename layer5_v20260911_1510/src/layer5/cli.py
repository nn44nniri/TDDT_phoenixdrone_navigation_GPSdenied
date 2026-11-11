from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from .config import Layer5Config
from .engine import Layer5Engine
from .utils import atomic_write_json

def _load(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def _engine(args): return Layer5Engine(Layer5Config.from_yaml(args.config))

def main(argv=None):
    root=Path(__file__).resolve().parents[2]; default=str(root/"configs/configuration_phoenix.yaml")
    p=argparse.ArgumentParser(prog="tddt-layer5",description="TDDT Layer 5 ERA-qLPV-MPC runtime")
    sub=p.add_subparsers(dest="cmd",required=True)
    q=sub.add_parser("preflight"); q.add_argument("--config",default=default); q.add_argument("--output"); q.add_argument("--strict-external",action="store_true")
    q=sub.add_parser("run-layer4"); q.add_argument("--config",default=default); q.add_argument("--input",required=True); q.add_argument("--output",default=str(root/"runtime/layer5_result.json")); q.add_argument("--learning-mode",choices=["OFFLINE_SE","ONLINE_JSBSIM"]); q.add_argument("--human-approval",action="store_true"); q.add_argument("--human-approver"); q.add_argument("--knowledge-refs"); q.add_argument("--policy-update"); q.add_argument("--requested-mode",choices=["AUTO","SUPERVISED","ADVISORY","HOLD"]); q.add_argument("--execute-environment",action="store_true")
    q=sub.add_parser("demo"); q.add_argument("--config",default=default); q.add_argument("--input",default=str(root/"examples/layer4_result_unverified_example.json")); q.add_argument("--output",default=str(root/"runtime/demo_result.json"))
    q=sub.add_parser("kstore-finalize"); q.add_argument("--config",default=default); q.add_argument("--knowledge")
    q=sub.add_parser("verify-setd"); q.add_argument("--config",default=default)
    q=sub.add_parser("inspect-phoenix"); q.add_argument("--config",default=default)
    a=p.parse_args(argv); eng=_engine(a)
    if a.cmd=="preflight":
        r=eng.preflight(a.strict_external); print(json.dumps(r,indent=2));
        if a.output: atomic_write_json(a.output,r)
        return 0 if r["ok"] else 2
    if a.cmd in {"run-layer4","demo"}:
        refs=_load(a.knowledge_refs) if getattr(a,"knowledge_refs",None) else None
        pup=_load(a.policy_update) if getattr(a,"policy_update",None) else None
        r=eng.run(_load(a.input),learning_mode=getattr(a,"learning_mode",None),human_approval=getattr(a,"human_approval",False),human_approver=getattr(a,"human_approver",None),knowledge_refs=refs,policy_update=pup,requested_mode=getattr(a,"requested_mode",None),execute_environment=getattr(a,"execute_environment",False)).to_dict()
        atomic_write_json(a.output,r); print(json.dumps({"ok":r["ok"],"output":a.output,"mode":r["operational_mode"],"final_gate":r["gates"].get("FINAL"),"fallback":r["fallback"].get("used"),"layer6_package":r["layer6_package"] is not None},indent=2)); return 0
    if a.cmd=="kstore-finalize":
        if eng.kstore.episode_id is None:
            print(json.dumps({"ok":False,"reason":"no_active_offline_episode"},indent=2)); return 2
        r=eng.finalize_offline_episode(_load(a.knowledge) if a.knowledge else None); print(json.dumps(r,indent=2)); return 0 if r.get("committed") else 2
    if a.cmd=="verify-setd": print(json.dumps(eng.kstore.load_setd(),indent=2)); return 0 if eng.kstore.load_setd().get("ok") else 2
    if a.cmd=="inspect-phoenix": print(json.dumps(eng.phoenix.status(),indent=2)); return 0
    return 2

if __name__=="__main__": raise SystemExit(main())
