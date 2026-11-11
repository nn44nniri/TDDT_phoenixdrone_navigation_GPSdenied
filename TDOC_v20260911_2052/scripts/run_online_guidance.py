#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT/'src') not in sys.path: sys.path.insert(0,str(ROOT/'src'))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.orchestrator import TDOCOrchestrator
from tddt_tdoc.cli import main as tdoc_cli_main


def main(argv=None):
    ap=argparse.ArgumentParser(description='TDDT GNSS-denied online guidance driver: L2->L3->L4->L5->L6/PT_LIB/ArduPilot/JSBSim->L7')
    ap.add_argument('--config',default=str(ROOT/'configs/tdoc.yaml'))
    ap.add_argument('--online-episodes',type=int,default=1)
    ap.add_argument('--online-max-cycles-per-episode',type=int,default=320)
    ap.add_argument('--online-max-cycles-per-episode-auto',action='store_true')
    ap.add_argument('--online-origin-tolerance-m',type=float,default=50.0)
    ap.add_argument('--online-goal-tolerance-m',type=float,default=25.0)
    ap.add_argument('--online-touchdown-altitude-m',type=float,default=2.5)
    ap.add_argument('--terrain-grid-n',type=int,default=70)
    ap.add_argument('--no-online-auto-commission',action='store_true')
    ap.add_argument('--preflight-only',action='store_true')
    args=ap.parse_args(argv)
    cfg=TDOCConfig.from_yaml(args.config)
    l6root=cfg.path_value('TDDT_LAYER6_ROOT')
    layer6_pf={'ok':False,'reason':'TDDT_LAYER6_ROOT_missing'}
    if l6root:
        if str(l6root/'src') not in sys.path: sys.path.insert(0,str(l6root/'src'))
        from tddt_layer6 import Layer6Contract
        layer6_pf=Layer6Contract.from_root(l6root).preflight()
    manifest=cfg.runtime_root/'knowledge'/'handoff'/'offline_latest.json.gz'
    handoff=OfflineKnowledgeHandoff(cfg.runtime_root).validate_online_start({'offline_knowledge_manifest':str(manifest)})
    with TDOCOrchestrator(cfg) as orch:
        tdoc_pf=orch.preflight()
        control_readiness=orch.online_control_readiness()
    reference=cfg.package_root/'reports'/'offline'/'mission'/'MISSION_TIMELINE.csv'
    reference_ready=reference.is_file()
    infrastructure_ok=bool(layer6_pf.get('ok') and handoff.get('ok') and tdoc_pf.get('ok') and reference_ready)
    control_ready=bool(control_readiness.get('ok'))
    auto_commission_allowed=not bool(args.no_online_auto_commission)
    launch_ready=bool(infrastructure_ok and (control_ready or auto_commission_allowed))
    launch_status=('READY' if control_ready else ('READY_FOR_AUTO_COMMISSION' if launch_ready else 'NOT_READY'))
    report={
        'schema':'TDOC-online-guidance-driver-preflight-v3','ok':launch_ready,'status':launch_status,
        'readiness_scope':'INFRASTRUCTURE_AND_OFFLINE_PRIOR_NOT_PHASE_RELEASE_OR_MISSION_SUCCESS',
        'mission_reference':{'ready':reference_ready,'path':str(reference),
            'reason':'reference_present' if reference_ready else 'restore_supplied_reports_before_online'},
        'gps_gnss_dependency_scope':'Layer6 contract and MESA sensor inputs only; not native startup',
        'native_startup_gps_assisted':True,
        'native_startup_gps_policy':'Permitted before confirmed ARM; denied before physics release by existing PT_LIB profile',
        'gnss_free_mission_experimentally_verified':False,
        'infrastructure_ok':infrastructure_ok,'control_ready_before_commission':control_ready,
        'auto_commission_allowed':auto_commission_allowed,
        'layer6':layer6_pf,'offline_prior':{k:handoff.get(k) for k in ('ok','status','reason','setd_integrity_valid','offline_control_coverage_complete','mission_certification_status','missing_control_phases','control_knowledge_valid_by_phase','manifest_path')},
        'tdoc_preflight_ok':bool(tdoc_pf.get('ok')),'online_control_readiness':control_readiness,
        'loop':'JSBSim current state -> Layer2(MESA authority) -> L3 -> L4 -> L5 FINAL -> L6 -> PT_LIB -> ArduPilot -> JSBSim realized state -> L7 -> next-cycle Layer2',
        'state_ingress_before_layer2':True,'navigation_authority':'MESA_STD','gps_gnss_dependency':False,'jsbsim_truth_injected_to_mesa':False,
        'direct_pwm':False,'direct_jsbsim_actuator_injection':False,'offline_prior_semantics':'SETD prior only; RTMO current/next phase release remains fail-closed',
        'commissioning_semantics':'If nominal SITL control is not yet commissioned, train-online performs bounded held-SITL auto-commissioning before operational Layer6 release unless --no-online-auto-commission is selected.',
    }
    out=cfg.package_root/'reports'/'online'/'ONLINE_GUIDANCE_PREFLIGHT.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)
    if args.preflight_only: return 0 if report['ok'] else 2
    if not report['ok']:
        print('[online-guidance] preflight failed; no actuation attempted',file=sys.stderr,flush=True); return 2
    cli=['--config',str(Path(args.config).resolve()),'train-online','--online-episodes',str(args.online_episodes),
         '--online-max-cycles-per-episode',str(args.online_max_cycles_per_episode),'--online-origin-tolerance-m',str(args.online_origin_tolerance_m),
         '--online-goal-tolerance-m',str(args.online_goal_tolerance_m),'--online-touchdown-altitude-m',str(args.online_touchdown_altitude_m),
         '--terrain-grid-n',str(args.terrain_grid_n),'--reuse-existing-offline-knowledge']
    if args.online_max_cycles_per_episode_auto: cli.append('--online-max-cycles-per-episode-auto')
    if args.no_online_auto_commission: cli.append('--no-online-auto-commission')
    return int(tdoc_cli_main(cli) or 0)

if __name__=='__main__': raise SystemExit(main())
