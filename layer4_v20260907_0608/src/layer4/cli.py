from __future__ import annotations
import argparse,json,sys
from pathlib import Path
import numpy as np
from .config import Layer4Config
from .engine import Layer4Engine
from .dependency import check_dependencies
from .knowledge import KnowledgeContext
from .snapshot import Layer3SnapshotAdapter
from .utils import read_json,write_json
from .rom import PODDEIMSurrogate
from .offline import fit_qlpv_npz

DEFAULT="configs/configuration_boulder.yaml"

def cmd_preflight(args):
    cfg=Layer4Config.from_yaml(args.config); deps=check_dependencies(cfg); knowledge=KnowledgeContext(cfg).validate()
    core=[cfg.package_root/cfg.get("qlpv.model_file"),cfg.package_root/cfg.get("qlpv.state_schema_file"),cfg.package_root/cfg.get("scenario.templates_file")]
    core_ok=all(p.exists() for p in core)
    overall=core_ok and (deps["ok"] if getattr(args,"strict_external",False) else True)
    out={"ok":overall,"core_resources_ok":core_ok,"external_dependencies":deps,"knowledge":knowledge,"strict_external":bool(getattr(args,"strict_external",False)),"note":"Layer 4 can consume an already-harmonized Layer-3 snapshot without directly importing external domain packages; full-fidelity verification requires all external roots."}
    if args.output: write_json(args.output,out)
    print(json.dumps(out,indent=2)); return 0 if overall else 2

def cmd_run(args):
    cfg=Layer4Config.from_yaml(args.config); eng=Layer4Engine(cfg); result=eng.run(read_json(args.snapshot)); write_json(args.output,result.to_dict()); print(json.dumps({"ok":True,"output":args.output,"snapshot_version":result.snapshot_version,"candidates":len(result.candidates),"pareto":len(result.pareto_front),"verified":len(result.verified_candidates),"budget":result.budget},indent=2)); return 0

def cmd_demo(args):
    args.snapshot=args.snapshot or "examples/layer3_snapshot_example.json"; return cmd_run(args)

def cmd_fit_rom(args):
    cfg=Layer4Config.from_yaml(args.config); files=sorted(Path(args.snapshots).glob("*.json")); adapter=Layer3SnapshotAdapter(cfg); eng=Layer4Engine(cfg); X=[]
    for p in files:
        try:X.append(eng.codec.encode(adapter.adapt(read_json(p))))
        except Exception: pass
    if len(X)<2: raise SystemExit("need at least 2 valid snapshots")
    rank=min(int(args.rank),cfg.get("edge.rom_rank_max",10),len(X))
    mean,V,S,idx=PODDEIMSurrogate.fit(np.asarray(X),rank,min(cfg.get("edge.deim_points_max",rank),rank))
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True); np.savez_compressed(out,V=V,mean=mean,singular_values=S,deim_indices=idx)
    meta=out.with_name("pod_metadata.json"); meta.write_text(json.dumps({"schema":"layer4-pod-deim-v1","rank":V.shape[1],"deim_points":len(idx),"deim_indices":idx.tolist(),"source_snapshots":len(X),"offline_only":True},indent=2)+"\n")
    print(json.dumps({"ok":True,"snapshots":len(X),"rank":V.shape[1],"deim_points":len(idx),"output":str(out),"metadata":str(meta)},indent=2)); return 0

def cmd_fit_qlpv(args):
    meta=fit_qlpv_npz(args.dataset,args.output,args.metadata,args.ridge); print(json.dumps({"ok":True,**meta},indent=2)); return 0

def build_parser():
    p=argparse.ArgumentParser(prog="tddt-layer4"); sub=p.add_subparsers(dest="cmd",required=True)
    q=sub.add_parser("preflight"); q.add_argument("--config",default=DEFAULT); q.add_argument("--output"); q.add_argument("--strict-external",action="store_true"); q.set_defaults(func=cmd_preflight)
    q=sub.add_parser("run-snapshot"); q.add_argument("--config",default=DEFAULT); q.add_argument("--snapshot",required=True); q.add_argument("--output",default="runtime/layer4_result.json"); q.set_defaults(func=cmd_run)
    q=sub.add_parser("demo"); q.add_argument("--config",default=DEFAULT); q.add_argument("--snapshot"); q.add_argument("--output",default="runtime/demo_result.json"); q.set_defaults(func=cmd_demo)
    q=sub.add_parser("fit-rom"); q.add_argument("--config",default=DEFAULT); q.add_argument("--snapshots",required=True); q.add_argument("--rank",type=int,default=8); q.add_argument("--output",default="resources/rom/pod_basis.npz"); q.set_defaults(func=cmd_fit_rom)
    q=sub.add_parser("fit-qlpv"); q.add_argument("--dataset",required=True); q.add_argument("--output",default="resources/edge_model/qlpv_model.npz"); q.add_argument("--metadata",default="resources/edge_model/qlpv_model.json"); q.add_argument("--ridge",type=float,default=1e-8); q.set_defaults(func=cmd_fit_qlpv)
    return p

def main(argv=None):
    args=build_parser().parse_args(argv); raise SystemExit(args.func(args))
if __name__=="__main__": main()
