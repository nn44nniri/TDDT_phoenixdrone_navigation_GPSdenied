from __future__ import annotations
import argparse,json,subprocess,sys
from pathlib import Path
from .config import TDOCConfig
from .orchestrator import TDOCOrchestrator
from .reporting import TDOCReportGenerator
from .storage import write_json_gz, read_json_auto, iter_jsonl_auto
from .reset import reset_tdoc
from .knowledge_handoff import OfflineKnowledgeHandoff

def _write(data,path):
    text=json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False)
    if path:
        p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
        if p.suffix==".gz": write_json_gz(p,data,indent=2)
        else: p.write_text(text,encoding="utf-8")
    print(text)
def main(argv=None):
    p=argparse.ArgumentParser(prog="tddt-tdoc",description="TDOC Runtime Orchestrator for Dynamic N-Twin TDDT")
    p.add_argument("--config",default="configs/tdoc.yaml")
    sp=p.add_subparsers(dest="cmd",required=True)
    q=sp.add_parser("preflight"); q.add_argument("--output")
    q=sp.add_parser("audit-integration",help="Read-only cross-layer site evidence audit; never starts SITL/actuation")
    q.add_argument("--stage8-report",required=True)
    q.add_argument("--stage9-report",required=True)
    q.add_argument("--stage10-report",required=True)
    q.add_argument("--require-native",action="store_true")
    q.add_argument("--output")
    q=sp.add_parser("run-cycle"); q.add_argument("--request",required=True); q.add_argument("--output")
    q=sp.add_parser("demo"); q.add_argument("--request",default="examples/runtime_request.json"); q.add_argument("--output",default="runtime/demo_result.json.gz")
    q=sp.add_parser("status"); q.add_argument("--output")
    q=sp.add_parser("check-unit-adapter"); q.add_argument("--output")
    q=sp.add_parser("generate-reports"); q.add_argument("--cycle"); q.add_argument("--transaction-prefix",help="Generate one coherent report from all persisted cycles whose transaction id/file starts with this prefix"); q.add_argument("--output-dir",default=None); q.add_argument("--terrain-grid-n",type=int,default=90); q.add_argument("--output")
    q=sp.add_parser("read-storage"); q.add_argument("--path",required=True); q.add_argument("--jsonl",action="store_true"); q.add_argument("--limit",type=int,default=0)
    q=sp.add_parser("train-online",aliases=["nav-online"],help="Run explicit ONLINE_JSBSIM training continuation using finalized OFFLINE_SE SETD knowledge, native Layer6Client/GUI/MAVLink, and Layer7")
    q.add_argument("--debug-log",nargs="?",const="auto",metavar="PATH",help="Record Online output, readiness events and tracebacks; omitted PATH creates runtime/logs/nav-online_<timestamp>.log")
    q.add_argument("--online-cycles",type=int,default=1,help="Maximum consecutive ONLINE_JSBSIM iterations for one continuous invocation; not a mission count")
    q.add_argument("--online-episodes","--online-missions",dest="online_episodes",type=int,default=0,help="Number of complete online origin-to-destination episodes/missions; 0 keeps continuous --online-cycles mode")
    q.add_argument("--online-max-cycles-per-episode",type=int,default=320,help="Safety cap for each online episode/mission")
    q.add_argument("--online-max-cycles-per-episode-auto",action="store_true",help="Automatically resolve the per-mission safety cap from the offline baseline or route geometry")
    q.add_argument('--online-local-hover-seconds',type=float,help='Explicit local takeoff/hover/landing test, 1..60 seconds; keeps normal safety gates')
    q.add_argument('--online-local-hover-height-m',type=float,default=3.,help='Local hover objective height, 1..10 metres')
    q.add_argument('--online-realtime-vtol',action='store_true',help='Sensor-restricted 10 Hz TDDT VTOL prototype with bounded continuous command leases')
    q.add_argument('--online-realtime-goal-offset-m',type=float,help='Explicit short-route commissioning distance along the configured mission bearing')
    q.add_argument('--online-realtime-output',help='Directory for Native wire evidence, all seven stages and mission outcome')
    q.add_argument("--online-origin-tolerance-m",type=float,default=50.0,help="Maximum allowed horizontal offset from the mission origin at the first live cycle of each episode")
    q.add_argument("--online-goal-tolerance-m",type=float,default=25.0,help="Horizontal destination tolerance used to terminate an online episode")
    q.add_argument("--online-touchdown-altitude-m",type=float,default=2.5,help="Maximum relative/AGL altitude for automatic touchdown completion when altitude evidence is available")
    q.add_argument("--reuse-existing-offline-knowledge",action="store_true",help="Force reuse of an existing verified TDOC offline handoff/SETD; fail closed if unavailable")
    q.add_argument("--run-offline-first",action="store_true",help="Force a fresh OFFLINE_SE episode before online training instead of auto-reusing an existing handoff")
    q.add_argument("--offline-knowledge-manifest",help="Optional runtime/knowledge/handoff/*.json.gz manifest to bind")
    q.add_argument("--online-validation-fixture",help="Optional validation-only Layer6 observation fixture; does not authorize actuation")
    q.add_argument("--online-observation-only",action="store_true",help="Explicitly allow ONLINE_JSBSIM telemetry/Layer7 validation when the Phoenix actuation bridge is not control-ready; no mission-progress/actuation claim is made")
    q.add_argument("--no-online-auto-commission",action="store_true",help="Disable automatic control-readiness preparation when nominal Phoenix control readiness is missing")
    q.add_argument("--terrain-grid-n",type=int,default=70)
    q=sp.add_parser("reset",help="Delete TDOC runtime/knowledge and safely-contained generated external datasets; optionally rebuild them with --refresh-external")
    q.add_argument("--dry-run",action="store_true",help="Show reset targets without deleting them")
    q.add_argument("--no-external",action="store_true",help="Reset only TDOC-owned runtime/reports; do not remove external generated datasets")
    q.add_argument("--output",help="Optional reset report path; default reports/reset/RESET_REPORT.json")
    q.add_argument("--refresh-external",action="store_true",help="After a successful external purge, run the official prepare_all pipeline to rebuild/refresh CRP, TEP, SARG and current climate forcing")
    q.add_argument("--build-fmus",action="store_true",help="With --refresh-external, rebuild the three Twin FMUs before dataset acquisition (MESA_NAV, WingMPET, MicroMetTwin)")
    q.add_argument("--build-fmus-refresh",action="store_true",help="Implies --build-fmus and removes only reproducible FMU build caches/staging directories after successful builds; final FMUs, source, configs and scripts are preserved")
    q.add_argument("--climate-hours",type=int,default=24,help="Climate forcing horizon for --refresh-external; default 24, e.g. 72")
    q.add_argument("--crp-clusters",type=int,default=32,help="CRP cluster count passed to --refresh-external")
    q.add_argument("--historical-start",default="2010-05-20",help="CRP historical source start passed to --refresh-external")
    q.add_argument("--historical-end",default="2025-05-20",help="CRP historical source end passed to --refresh-external")
    q.add_argument("--refresh-crp-sources",action="store_true",help="Force source refresh during --refresh-external preparation")
    q.add_argument("--no-download",action="store_true",help="Deprecated alias for --reuse-downloaded-datasets")
    q.add_argument("--reuse-downloaded-datasets",action="store_true",help="With --refresh-external, forbid network downloads and rebuild from TDDT_INTERNET_DATASETS_ROOT caches")
    q.add_argument("--refresh-timeout-s",type=float,default=0.0,help="Optional whole refresh timeout in seconds; 0 means no timeout. Progress remains visible while running.")
    args=p.parse_args(argv); cfg=TDOCConfig.from_yaml(args.config)
    # Explicitly bypass diagnostic_session/runtime initialization: this audit
    # is read-only apart from the caller-selected output JSON report.
    if args.cmd=="audit-integration":
        return _dispatch(args,cfg)
    from .diagnostics import diagnostic_session
    with diagnostic_session(cfg,args):
        return _dispatch(args,cfg)

def _dispatch(args,cfg):
    if args.cmd=="audit-integration":
        from .integration_audit import audit
        d=audit(cfg.package_root.parent,stage8_report=args.stage8_report,
                stage9_report=args.stage9_report,stage10_report=args.stage10_report,
                require_native=args.require_native)
        _write(d,args.output)
        return 0 if d["status"]=="CROSS_LAYER_REFERENCE_BINDING_PASS_UNCERTIFIED" else 2
    if args.cmd in {"train-online","nav-online"}:
        if args.online_local_hover_seconds is not None and (
                not args.online_episodes or not 1<=args.online_local_hover_seconds<=60
                or not 1<=args.online_local_hover_height_m<=10):
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"local hover requires --online-episodes, 1..60 seconds and 1..10 metre height"},indent=2)); return 2
        if (args.reuse_existing_offline_knowledge or args.cmd=="nav-online") and args.run_offline_first:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--reuse-existing-offline-knowledge and --run-offline-first are mutually exclusive"},indent=2)); return 2
        script=cfg.package_root/"scripts"/"run_training_workflows.py"
        if args.online_cycles <= 0:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--online-cycles must be > 0"},indent=2)); return 2
        if args.online_origin_tolerance_m <= 0 or args.online_goal_tolerance_m <= 0 or args.online_touchdown_altitude_m < 0:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"online origin/goal/touchdown tolerances are invalid"},indent=2)); return 2
        if args.online_episodes < 0 or args.online_max_cycles_per_episode <= 0:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--online-episodes must be >= 0 and --online-max-cycles-per-episode must be > 0"},indent=2)); return 2
        if args.online_max_cycles_per_episode_auto and not args.online_episodes:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--online-max-cycles-per-episode-auto requires --online-episodes/--online-missions"},indent=2)); return 2
        cmd=[sys.executable,str(script),"--config",str(Path(args.config).resolve()),"--online-training","--online-cycles",str(args.online_cycles),"--terrain-grid-n",str(args.terrain_grid_n)]
        if args.online_observation_only: cmd.append("--online-observation-only")
        if args.no_online_auto_commission: cmd.append("--no-online-auto-commission")
        if args.online_episodes:
            cmd += ["--online-episodes",str(args.online_episodes),"--online-max-cycles-per-episode",str(args.online_max_cycles_per_episode),
                    "--online-origin-tolerance-m",str(args.online_origin_tolerance_m),"--online-goal-tolerance-m",str(args.online_goal_tolerance_m),"--online-touchdown-altitude-m",str(args.online_touchdown_altitude_m)]
            if args.online_max_cycles_per_episode_auto: cmd.append("--online-max-cycles-per-episode-auto")
            if args.online_local_hover_seconds is not None:
                cmd+=['--online-local-hover-seconds',str(args.online_local_hover_seconds),
                      '--online-local-hover-height-m',str(args.online_local_hover_height_m)]
        manifest=Path(args.offline_knowledge_manifest).expanduser() if args.offline_knowledge_manifest else (cfg.runtime_root/"knowledge"/"handoff"/"offline_latest.json.gz")
        force_reuse=bool(args.cmd=="nav-online" or args.reuse_existing_offline_knowledge or args.offline_knowledge_manifest)
        auto_reuse=bool(not args.run_offline_first and manifest.is_file())
        if force_reuse or auto_reuse:
            # Validate the finalized control handoff before native Layer6 session preparation.
            # This prevents stale/safety-only/incomplete Offline runs from reaching Online setup.
            handoff=OfflineKnowledgeHandoff(cfg.runtime_root).validate_online_start({"offline_knowledge_manifest":str(manifest)})
            if not handoff.get("ok"):
                out={"ok":False,"status":"NOT_READY","reason":str(handoff.get("reason") or "VALID_FINALIZED_SETD_INTEGRITY_REQUIRED"),"handoff":{k:handoff.get(k) for k in ("status","reason","handoff_reason","manifest_path","blockers","consistency_errors")}}
                print(json.dumps(out,indent=2,ensure_ascii=False),flush=True); return 2
            hm=handoff.get("manifest") or {}
            cmd += ["--skip-offline","--offline-knowledge-manifest",str(manifest.resolve())]
            print(f"[{args.cmd}] offline knowledge mode={'FORCE_REUSE' if force_reuse else 'AUTO_REUSE_EXISTING'} manifest={manifest}",flush=True)
        else:
            print(f"[{args.cmd}] offline knowledge mode=RUN_OFFLINE_FIRST (no reusable handoff detected)",flush=True)
        if args.offline_knowledge_manifest and "--offline-knowledge-manifest" not in cmd: cmd += ["--offline-knowledge-manifest",args.offline_knowledge_manifest]
        if args.online_realtime_vtol or cfg.get('tdoc.realtime.enabled',False):
            if args.online_episodes!=1 or not (force_reuse or auto_reuse):
                print(json.dumps({'ok':False,'status':'REFUSED','reason':'realtime profile requires one episode and an accepted current Offline prior'}));return 2
            from .realtime import run_realtime_mission
            goal=None
            if args.online_realtime_goal_offset_m is not None:
                if args.online_realtime_goal_offset_m<0:
                    print(json.dumps({'ok':False,'status':'REFUSED','reason':'negative commissioning route distance'}));return 2
                from pyproj import Geod
                region=json.loads((cfg.package_root/'region/region_v1.geojson').read_text())
                roles={f['properties'].get('feature_role'):f['geometry']['coordinates'] for f in region['features']}
                geod=Geod(ellps='WGS84');bearing,_,_=geod.inv(*roles['start'],*roles['destination'])
                lon,lat,_=geod.fwd(*roles['start'],bearing,args.online_realtime_goal_offset_m);goal=[lon,lat]
            result=run_realtime_mission(cfg,hover_seconds=args.online_local_hover_seconds,
                height_m=args.online_local_hover_height_m,
                max_cycles=20000 if args.online_max_cycles_per_episode_auto else args.online_max_cycles_per_episode,
                goal_override=goal,output=args.online_realtime_output)
            return 0 if result['status']=='PASS' else 2
        if args.online_validation_fixture: cmd += ["--online-validation-fixture",args.online_validation_fixture]
        from .diagnostics import run_workflow
        return run_workflow(cmd,cfg.package_root,enabled=bool(args.debug_log))
    if args.cmd=="reset":
        if (args.build_fmus or args.build_fmus_refresh) and not args.refresh_external:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--build-fmus/--build-fmus-refresh require --refresh-external"},indent=2)); return 2
        if args.refresh_external and args.no_external:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--refresh-external cannot be combined with --no-external"},indent=2)); return 2
        if args.climate_hours <= 0:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--climate-hours must be > 0"},indent=2)); return 2
        if args.refresh_timeout_s < 0:
            print(json.dumps({"ok":False,"status":"REFUSED","reason":"--refresh-timeout-s must be >= 0"},indent=2)); return 2
        refresh_options={"climate_hours":args.climate_hours,"crp_clusters":args.crp_clusters,"historical_start":args.historical_start,"historical_end":args.historical_end,"refresh_crp_sources":args.refresh_crp_sources,"no_download":bool(args.no_download or args.reuse_downloaded_datasets),"reuse_downloaded_datasets":args.reuse_downloaded_datasets,"refresh_timeout_s":args.refresh_timeout_s,"build_fmus":bool(args.build_fmus or args.build_fmus_refresh),"build_fmus_refresh":bool(args.build_fmus_refresh)}
        progress=(lambda msg: print(msg,flush=True))
        d=reset_tdoc(cfg,dry_run=args.dry_run,include_external=not args.no_external,output=args.output,refresh_external=args.refresh_external,refresh_options=refresh_options,progress=progress)
        print(json.dumps(d,indent=2,ensure_ascii=False)); return 0 if d.get("ok") else 2
    if args.cmd=="read-storage":
        path=Path(args.path)
        if args.jsonl or path.name.endswith(".jsonl.gz") or path.suffix==".jsonl":
            rows=list(iter_jsonl_auto(path)); rows=rows[:args.limit] if args.limit>0 else rows; print(json.dumps(rows,indent=2,ensure_ascii=False)); return 0
        print(json.dumps(read_json_auto(path),indent=2,ensure_ascii=False)); return 0
    if args.cmd=="generate-reports":
        summary, artifacts=TDOCReportGenerator(cfg).generate(cycle=args.cycle,transaction_prefix=args.transaction_prefix,output_dir=args.output_dir,terrain_grid_n=args.terrain_grid_n)
        d={"ok":True,"summary":summary,"artifacts":artifacts.to_dict()}
        _write(d,args.output); return 0
    with TDOCOrchestrator(cfg) as o:
        if args.cmd=="preflight": d=o.preflight(); _write(d,args.output); return 0 if d["ok"] else 2
        if args.cmd in ("run-cycle","demo"):
            req=json.loads(Path(args.request).read_text(encoding="utf-8")); d=o.run_cycle(req).to_dict(); _write(d,args.output); return 0 if d["status"]=="OK" else 3
        if args.cmd=="status": _write({"registry":o.registry.snapshot(),"version":o.versions.snapshot(),"provenance":o.provenance.verify()},args.output); return 0
        if args.cmd=="check-unit-adapter":
            d=o.unit_adapter.preflight(refresh=True); _write(d,args.output); return 0 if d.get("ok") else 2
    return 1
if __name__=="__main__":raise SystemExit(main())
