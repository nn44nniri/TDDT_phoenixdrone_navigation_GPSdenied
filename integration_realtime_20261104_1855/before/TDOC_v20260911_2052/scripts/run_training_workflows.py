#!/usr/bin/env python3
from __future__ import annotations
import argparse, atexit, copy, gzip, hashlib, html, json, math, os, signal, sys, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from tddt_tdoc import TDOCConfig, TDOCOrchestrator
from tddt_tdoc.diagnostics import event as diagnostic_event, online_preflight_diagnostics
from tddt_tdoc.knowledge_assessment import offline_validation_view
from tddt_tdoc.reporting import TDOCReportGenerator
from tddt_tdoc.storage import write_json_gz, iter_jsonl_auto, read_json_auto
from tddt_tdoc.climate_forcing import resolve_climate_forcing
from tddt_tdoc.participation import aggregate_mpc_participation
from tddt_tdoc.vehicle_state import phoenix_state_from_ptlib_snapshot, quaternion_to_euler
from tddt_tdoc.model_compatibility import capture_model_dependencies, integrated_native
from tddt_tdoc.rtmo import MissionPhaseManager, GoalSupervisor, RTClock, RunContext, build_capability_manifest, LiveCapabilityRegistry
from tddt_tdoc.rtmo.capability_gate import online_on_ground_boundary_evidence
from tddt_tdoc.rtmo.readiness_taxonomy import write_taxonomy

PHASES={0:'on_ground',1:'transition_forward',2:'climb',3:'cruise',4:'turn',5:'approach',6:'transition_back',7:'vertical_landing',8:'touchdown',9:'hover',10:'vertical_takeoff'}
MISSION_SUBSTATES={'terminal_reposition'}

def envpath(name):
    v=os.environ.get(name,'').strip(); return Path(v).expanduser().resolve() if v else None

def _num(x,default=None):
    try:
        if isinstance(x,dict) and 'value' in x:x=x['value']
        return float(x)
    except Exception:return default

def _format_online_terminal_metrics(dgoal,progress,progress_pct,sim,dsim):
    def metric(v,spec,suffix=''):
        n=_num(v,None)
        return 'N/A' if n is None else f"{format(n,spec)}{suffix}"
    dg=metric(dgoal,'+.2f','m')
    pr='N/A' if _num(progress,None) is None else f"{metric(progress,'.1f','m')}/{metric(progress_pct,'.1f','%')}"
    return dg,pr,metric(sim,'.2f','s'),metric(dsim,'+.2f','s')

def _set_env(req:dict,path:list[str],value,unit,frame,semantic):
    cur=req
    for k in path[:-1]:cur=cur.setdefault(k,{})
    cur[path[-1]]={'value':value,'unit':unit,'frame':frame,'semantic':semantic,'uncertainty':None,'source':'TDOC Mission Driver','provenance':{'origin':'stateful offline mission','contract':'explicit TDOC envelope'}}

def load_region(path:Path):
    d=json.loads(path.read_text()); roles={str((f.get('properties') or {}).get('feature_role')):f for f in d.get('features',[])}
    return d,roles['start']['geometry']['coordinates'],roles['destination']['geometry']['coordinates']

def route_candidates(sarg_root:Path,regime_id:int,energy_wh:float):
    import pandas as pd
    records=pd.read_csv(sarg_root/'sarg_records.csv'); q=records[records['regime_id']==regime_id].copy()
    if q.empty:q=records.copy()
    out=[]
    for (scenario,crp),g in q.groupby(['scenario_id','crp_id']):
        energy=float(g['energy_used_wh'].sum()) if 'energy_used_wh' in g else math.inf
        t=float(g['flight_time_s'].sum()) if 'flight_time_s' in g else math.inf
        obs_raw=float(g['min_obstacle_clearance_m'].min()) if 'min_obstacle_clearance_m' in g else math.nan
        # SARG uses -9999 as "unknown/not assessed" in some summary rows; never treat it as a real negative clearance.
        obs=(math.nan if (math.isfinite(obs_raw) and obs_raw <= -9000.0) else obs_raw)
        terr=float(g['min_ground_clearance_m'].min()) if 'min_ground_clearance_m' in g else math.nan
        flight_ok=bool((g['flight_ok']==1).all()) if 'flight_ok' in g else True; nav_ok=bool((g['nav_ok']==1).all()) if 'nav_ok' in g else True
        # P1-2: the SARG takeoff/transition record is allowed to remain a mission
        # geometry/reference diagnostic, but it must not make the route eligible
        # for controller-readiness evidence unless it is a valid joint WingMPET +
        # MESA reference.  Phase 1 is the canonical SARG takeoff_transition stage.
        trows=(g[g['phase_id']==1] if 'phase_id' in g else g.iloc[0:0])
        if len(trows):
            transition_reference_eligible=bool(
                (trows['record_status'].isin([0,1])).all() if 'record_status' in trows else True
            ) and bool((trows['wing_status']==0).all() if 'wing_status' in trows else True) \
              and bool((trows['mesa_status']==0).all() if 'mesa_status' in trows else True) \
              and bool((trows['flight_ok']==1).all() if 'flight_ok' in trows else True) \
              and bool((trows['nav_ok']==1).all() if 'nav_ok' in trows else True) \
              and bool((trows['phase_used']==1).all() if 'phase_used' in trows else True)
        else:
            transition_reference_eligible=False
        feasible_energy=energy <= max(energy_wh,1e-9)
        route=sarg_root/'routes'/f'S{int(scenario):02d}_C{int(crp):03d}.csv'
        if route.is_file():
            try:
                rdf=pd.read_csv(route,usecols=lambda c:c in {'flight_valid','navigation_valid','clearance_m','goal_distance_m'})
                flight_fraction=float(rdf['flight_valid'].mean()) if 'flight_valid' in rdf else (1.0 if flight_ok else 0.0)
                nav_fraction=float(rdf['navigation_valid'].mean()) if 'navigation_valid' in rdf else (1.0 if nav_ok else 0.0)
                point_min_clearance=float(rdf['clearance_m'].min()) if 'clearance_m' in rdf else terr
                final_goal_distance=float(rdf['goal_distance_m'].iloc[-1]) if 'goal_distance_m' in rdf and len(rdf) else math.nan
            except Exception:
                flight_fraction=1.0 if flight_ok else 0.0; nav_fraction=1.0 if nav_ok else 0.0; point_min_clearance=terr; final_goal_distance=math.nan
            validity_penalty=(1.0-flight_fraction)*10000.0+(1.0-nav_fraction)*10000.0
            score=(0 if feasible_energy else 5e5)+validity_penalty+energy*100+t+(0 if math.isnan(obs) else max(0,30-obs)*100)
            admission_reasons=[]
            if not feasible_energy: admission_reasons.append('energy_budget_failed')
            # Summary booleans are retained as diagnostics only. Strict admission is
            # defined by energy, pointwise validity fractions and clearance evidence.
            # A single startup/zero-time summary warning must not make an otherwise
            # >99% valid route permanently ineligible for controller-readiness evidence.
            summary_warnings=[]
            if not flight_ok: summary_warnings.append('flight_ok_summary_false')
            if not nav_ok: summary_warnings.append('nav_ok_summary_false')
            if flight_fraction < 0.99: admission_reasons.append('flight_valid_fraction_below_0.99')
            if nav_fraction < 0.99: admission_reasons.append('navigation_valid_fraction_below_0.99')
            if not transition_reference_eligible: admission_reasons.append('transition_sarg_reference_not_flight_navigation_admissible')
            if not (math.isfinite(terr) and terr >= 0.0): admission_reasons.append('ground_clearance_invalid')
            if not (math.isfinite(point_min_clearance) and point_min_clearance >= 0.0): admission_reasons.append('point_ground_clearance_invalid')
            strict=not admission_reasons
            out.append({'scenario_id':int(scenario),'crp_id':int(crp),'energy_wh':energy,'time_s':t,'min_obstacle_clearance_m':None if math.isnan(obs) else obs,'min_obstacle_clearance_raw':obs_raw,'min_ground_clearance_m':terr,'point_min_ground_clearance_m':point_min_clearance,'flight_ok_summary':flight_ok,'nav_ok_summary':nav_ok,'flight_valid_fraction':flight_fraction,'navigation_valid_fraction':nav_fraction,'final_goal_distance_m':final_goal_distance,'energy_feasible':feasible_energy,'score':score,'route':route,
                        'strict_admissible':strict,'admission_reasons':admission_reasons,'admission_warnings':summary_warnings,
                        'transition_reference_eligible':bool(transition_reference_eligible),
                        'transition_reference_policy':'VALID_OR_VALID_LOW_CONF_WITH_WING_MESA_OK',
                        'admission_status':'STRICT_ADMITTED' if strict else 'DEGRADED_REFERENCE_ONLY',
                        'control_readiness_eligible':strict})
    return sorted(out,key=lambda x:(not bool(x.get('strict_admissible')),x['score']))

def load_reference_route(sarg_root:Path,regime_id:int,energy_wh:float):
    import pandas as pd
    cand=route_candidates(sarg_root,regime_id,energy_wh)
    if not cand:raise RuntimeError(f'No SARG route available under {sarg_root}')
    strict=[x for x in cand if x.get('strict_admissible')]
    choice=(strict[0] if strict else sorted(cand,key=lambda x:x['score'])[0])
    # A degraded route remains a reference/prior so Offline mission/knowledge training
    # can proceed, but it can never prove controller readiness.
    if not strict:
        choice=dict(choice); choice['admission_status']='DEGRADED_REFERENCE_ONLY'; choice['control_readiness_eligible']=False
    df=pd.read_csv(choice['route']).sort_values('point_id').reset_index(drop=True)
    return choice,df,cand[:10]

def forcing_window(climate_root:Path):
    import pandas as pd
    p=resolve_climate_forcing(climate_root,require_exists=True)
    if not p: raise FileNotFoundError(f'No active climate forcing under {climate_root}/climate_dataset')
    df=pd.read_csv(p,usecols=['time']); vals=pd.to_datetime(df['time'],errors='coerce').dropna().sort_values().unique()
    if len(vals)==0:return None,None
    return pd.Timestamp(vals[0]).to_pydatetime(),pd.Timestamp(vals[-1]).to_pydatetime()


def _select_offline_vertical_proof_epoch(climate_root:Path, twins_root:Path, mission_duration_s:float, *, mode:str="auto"):
    """Select a real forcing epoch whose VTOL wind envelope can support proof acquisition.

    Priority 6: this never edits forcing values or relaxes Phoenix limits.  In ``auto``
    mode it chooses the earliest contiguous portion of the *active forcing file* for
    which every spatial forcing point needed across the mission duration stays below
    the Phoenix hover-wind authority with a conservative model margin.  The selected
    epoch is recorded separately from the forcing-window identity so Offline->Online
    climate provenance remains exact.
    """
    mode=str(mode or "auto").strip().lower()
    forcing_path=resolve_climate_forcing(climate_root,require_exists=True)
    import pandas as pd, yaml
    df=pd.read_csv(forcing_path)
    required={"time","wind_speed_10m_ms"}
    if not required.issubset(df.columns):
        return None,{"schema":"TDOC-offline-vertical-proof-climate-window-v1","mode":mode,"selected":False,"reason":"forcing_missing_time_or_wind_columns","forcing_file":str(forcing_path),"synthetic_environment":False}
    times=pd.to_datetime(df["time"],errors="coerce")
    winds=pd.to_numeric(df["wind_speed_10m_ms"],errors="coerce")
    valid=df.loc[times.notna() & winds.notna()].copy()
    valid["_time"]=times[times.notna() & winds.notna()]
    valid["_wind"]=winds[times.notna() & winds.notna()]
    if valid.empty:
        return None,{"schema":"TDOC-offline-vertical-proof-climate-window-v1","mode":mode,"selected":False,"reason":"forcing_has_no_valid_wind_samples","forcing_file":str(forcing_path),"synthetic_environment":False}
    phoenix=yaml.safe_load((twins_root/"config/phoenix_drone_v3.yaml").read_text()) or {}
    hover_limit=float((((phoenix.get("mission") or {}).get("maximum_hover_wind_m_s")) or 5.0))
    # Terrain/micromet can accelerate local wind above 10-m source forcing.  Do not
    # consume that uncertainty by weakening the physical limit; reserve it here.
    model_margin=min(max(float(hover_limit)*0.20,0.5),1.0)
    source_limit=max(0.0,hover_limit-model_margin)
    grouped=valid.groupby("_time")["_wind"].max().sort_index()
    if mode in {"forcing-start","disabled","off","none"}:
        first=grouped.index[0].to_pydatetime()
        return first,{"schema":"TDOC-offline-vertical-proof-climate-window-v1","mode":mode,"selected":True,"reason":"forcing_start_requested","effective_epoch":first.isoformat(),"hover_limit_m_s":hover_limit,"source_wind_limit_m_s":source_limit,"model_margin_m_s":model_margin,"synthetic_environment":False,"forcing_file":str(forcing_path)}
    horizon=max(float(mission_duration_s),0.0)
    idx=list(grouped.index)
    selected=None; inspected=[]
    for t0 in idx:
        tend=t0+pd.Timedelta(seconds=horizon)
        # Include the bracketing forcing sample after mission end because MicroMet
        # may interpolate toward it during the final seconds.
        covered=grouped[(grouped.index>=t0) & (grouped.index<=tend)]
        later=grouped[grouped.index>tend]
        if not later.empty:
            covered=pd.concat([covered,later.iloc[:1]])
        mx=float(covered.max()) if not covered.empty else float("inf")
        inspected.append({"epoch":t0.isoformat(),"max_source_wind_m_s":mx})
        if mx <= source_limit + 1e-12:
            selected=t0.to_pydatetime(); break
    if selected is None:
        return None,{"schema":"TDOC-offline-vertical-proof-climate-window-v1","mode":mode,"selected":False,"reason":"no_contiguous_forcing_window_within_phoenix_hover_wind_envelope","hover_limit_m_s":hover_limit,"source_wind_limit_m_s":source_limit,"model_margin_m_s":model_margin,"mission_duration_s":horizon,"forcing_file":str(forcing_path),"candidates":inspected[:24],"synthetic_environment":False}
    return selected,{"schema":"TDOC-offline-vertical-proof-climate-window-v1","mode":mode,"selected":True,"reason":"earliest_real_forcing_window_with_vertical_control_margin","effective_epoch":selected.isoformat(),"hover_limit_m_s":hover_limit,"source_wind_limit_m_s":source_limit,"model_margin_m_s":model_margin,"mission_duration_s":horizon,"forcing_file":str(forcing_path),"forcing_file_sha256":_sha256_file(Path(forcing_path)),"candidates_inspected":inspected[:24],"synthetic_environment":False,"constraints_relaxed":False}


def _layer3_mission_epoch_utc():
    """Return the external Layer-3 mission epoch used for FMI elapsed-time alignment.

    Climate packet timestamps must map to the *same* elapsed-time origin as Layer 3.
    Using forcing-window start as an independent epoch can make a perfectly current
    MicroMet packet appear hours stale and silently remove it from the CoC.
    """
    try:
        import yaml
        root=envpath('TDDT_LAYER3_ROOT')
        cfg=yaml.safe_load((root/'configs/configuration_boulder.yaml').read_text()) or {}
        text=((cfg.get('time') or {}).get('mission_epoch_utc'))
        if not text:return None
        return datetime.fromisoformat(str(text).replace('Z','+00:00'))
    except Exception:
        return None

def _coerce_datetime_to_forcing_clock(value:datetime|None, forcing_start:datetime)->datetime|None:
    """Normalize a datetime onto the forcing file clock without host-TZ ambiguity.

    TDOC treats naive forcing CSV timestamps as UTC-like common time, matching the
    existing Layer-3/TDOC convention.  A timezone-aware Layer-3 epoch is therefore
    converted to UTC before tzinfo is removed when the forcing is naive.
    """
    if value is None:
        return None
    if forcing_start.tzinfo is None:
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=forcing_start.tzinfo)
    return value.astimezone(forcing_start.tzinfo)


def _climate_time_for_elapsed(forcing, elapsed_s:float, *, return_metadata:bool=False, effective_epoch_override:datetime|None=None):
    """Map OFFLINE mission elapsed time into the active forcing window.

    Use the configured Layer-3 mission epoch only when it is represented by the
    active forcing dataset.  Preparation may refresh the Climate forcing while
    Layer-3 intentionally retains a static/reference epoch; in that case rebasing
    the *climate query clock* to the active forcing start is required.  This does
    not alter Layer-3 reference_time_s, model state, or Online time semantics.

    The mapping remains fail-closed when elapsed mission time itself exceeds the
    available forcing horizon; no end-of-window clamping is performed Offline.
    """
    if not forcing:
        return (None,{"basis":"forcing_unavailable"}) if return_metadata else None
    start,end=forcing
    if start is None:
        return (None,{"basis":"forcing_start_unavailable"}) if return_metadata else None
    elapsed=max(0.0,float(elapsed_s))
    configured_epoch=_coerce_datetime_to_forcing_clock(_layer3_mission_epoch_utc(),start)
    end_cmp=_coerce_datetime_to_forcing_clock(end,start) if end is not None else None
    epoch_inside=bool(configured_epoch is not None and configured_epoch>=start and (end_cmp is None or configured_epoch<=end_cmp))
    override=_coerce_datetime_to_forcing_clock(effective_epoch_override,start) if effective_epoch_override is not None else None
    if override is not None:
        if override < start or (end_cmp is not None and override > end_cmp):
            raise RuntimeError(f'OFFLINE selected effective climate epoch {override.isoformat()} lies outside active forcing window {start.isoformat()}..{end_cmp.isoformat() if end_cmp else "open"}')
        effective_epoch=override
        rebased=bool(configured_epoch is None or configured_epoch != override)
    else:
        effective_epoch=configured_epoch if epoch_inside else start
        rebased=bool(configured_epoch is not None and not epoch_inside)
    t=effective_epoch+timedelta(seconds=elapsed)
    if t<start or (end_cmp is not None and t>end_cmp):
        raise RuntimeError(
            f'OFFLINE climate timestamp {t.isoformat()} lies outside active forcing window '
            f'{start.isoformat()}..{end_cmp.isoformat() if end_cmp else "open"}; '
            f'effective_epoch={effective_epoch.isoformat()}, elapsed_s={elapsed:.3f}. '
            'Increase/refresh the forcing horizon; TDOC will not clamp Offline climate time.'
        )
    meta={
        "basis":("offline_selected_vertical_proof_safe_forcing_epoch" if override is not None else "layer3_mission_epoch_utc" if epoch_inside else "active_offline_forcing_window_start"),
        "configured_layer3_epoch":configured_epoch.isoformat() if configured_epoch is not None else None,
        "effective_climate_epoch":effective_epoch.isoformat(),
        "forcing_start":start.isoformat(),
        "forcing_end":end_cmp.isoformat() if end_cmp is not None else None,
        "epoch_rebased_to_forcing_start":rebased,
        "elapsed_s":elapsed,
        "safety_semantics":"offline_rebase_only_when_layer3_epoch_outside_forcing; no_end_clamp",
    }
    return (t,meta) if return_metadata else t


def _sha256_file(path:Path|None)->str|None:
    if path is None or not Path(path).is_file(): return None
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def _sha256_tree(root:Path|None, *, include_names=("src","scripts","configs"))->str|None:
    """Deterministic source-tree identity; excludes mutable/generated state."""
    if root is None or not Path(root).is_dir(): return None
    root=Path(root).resolve(); files=[]
    for name in include_names:
        base=root/name
        if not base.exists(): continue
        for f in base.rglob('*'):
            if not f.is_file(): continue
            if '__pycache__' in f.parts or f.suffix in {'.pyc','.pyo'}: continue
            files.append(f)
    h=hashlib.sha256()
    for f in sorted(files,key=lambda x:x.relative_to(root).as_posix()):
        rel=f.relative_to(root).as_posix().encode('utf-8')
        h.update(rel+b'\0')
        with f.open('rb') as fh:
            for block in iter(lambda:fh.read(1024*1024),b''): h.update(block)
        h.update(b'\0')
    return h.hexdigest()


def _pin_run_provenance(report_root:Path, runtime_root:Path)->dict:
    """Pin the bundle manifest and current source/execution identity before Offline/Online."""
    integrated=envpath('TDDT_INTEGRATED_ROOT') or ROOT.parent.resolve()
    root_manifest=integrated/'MANIFEST.json'
    release=integrated.name
    manifest_release=None
    manifest_error=None
    try:
        manifest=json.loads(root_manifest.read_text())
        if not isinstance(manifest,dict) or not isinstance(manifest.get('release'),str):
            raise ValueError("root manifest must be an object with a string release")
        manifest_release=manifest['release']
    except FileNotFoundError:
        manifest_error='root_manifest_missing'
    except (OSError,ValueError,TypeError) as exc:
        manifest_error='root_manifest_invalid: '+str(exc)
    layer4=envpath('TDDT_LAYER4_ROOT'); layer5=envpath('TDDT_LAYER5_ROOT')
    layer6=envpath('TDDT_LAYER6_ROOT')
    prov={
      'schema':'TDOC-run-provenance-v1',
      'generated_at':datetime.now(timezone.utc).isoformat(),
      'bundle_release':release,
      'manifest_release':manifest_release,
      'release_identity_match':bool(manifest_release==release),
      'root_manifest_path':str(root_manifest),
      'root_manifest_sha256':_sha256_file(root_manifest),
      'root_manifest_error':manifest_error,
      'TDOC_source_hash':_sha256_tree(ROOT),
      'Layer4_source_hash':_sha256_tree(layer4),
      'Layer5_source_hash':_sha256_tree(layer5),
      'Layer6_source_hash':_sha256_tree(layer6, include_names=('src','scripts','config')) if layer6 else None,
      'execution_transport':'Layer6Client_GUI_MAVLink',
      'absolute_execution_root':str(integrated),
      'tdoc_root':str(ROOT.resolve()),
      'layer4_root':str(layer4) if layer4 else None,
      'layer5_root':str(layer5) if layer5 else None,
      'layer6_root':str(layer6) if layer6 else None,
      'authority':'current execution identity; imported historical reports/runtime are audit-only and may contain older absolute paths',
      'historical_absolute_paths_are_current_authority':False,
    }
    blockers=[]
    if manifest_error: blockers.append(manifest_error.split(':',1)[0])
    if not prov['release_identity_match']: blockers.append('bundle_release_manifest_release_mismatch')
    for k in ('root_manifest_sha256','TDOC_source_hash','Layer4_source_hash','Layer5_source_hash'):
        if not prov.get(k): blockers.append(f'missing_{k}')
    if not prov['Layer6_source_hash']: blockers.append('missing_Layer6_source_hash')
    prov['ok']=not blockers; prov['blockers']=blockers
    for out in (report_root/'provenance'/'RUN_PROVENANCE.json', runtime_root/'provenance'/'run_provenance.json'):
        out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(prov,indent=2,ensure_ascii=False)+"\n")
    return prov


def _offline_alignment_from_cycle(cycle:dict)->dict:
    """Recover the exact effective climate epoch used by a legacy Offline handoff."""
    candidates=[]
    l3=cycle.get('layer3_result') or {}
    snap=l3.get('snapshot') or {}
    if isinstance(snap.get('context'),dict): candidates.append(snap['context'].get('offline_climate_time_alignment'))
    for twin in ('MicroMetTwin','MESA_NAV','WingMPET'):
        pkt=((cycle.get('layer2_packets') or {}).get(twin) or {})
        if isinstance(pkt.get('context'),dict): candidates.append(pkt['context'].get('offline_climate_time_alignment'))
    for x in candidates:
        if isinstance(x,dict) and x.get('effective_climate_epoch') and x.get('forcing_start'):
            return dict(x)
    return {}


def _online_datetime_to_forcing_clock(value,forcing_start):
    if value is None:return None
    if (value.utcoffset() is None)!=(forcing_start.utcoffset() is None):
        raise RuntimeError('ONLINE clock domain mismatch: unspecified dataset timezone cannot imply UTC or inherit an offset')
    return value  # Compare explicit offsets without normalizing the stored representation.


def _load_online_climate_alignment(runtime_root:Path, manifest_path:Path|None, forcing, climate_root:Path)->dict:
    """Resolve the Offline climate-clock contract bound to the reused SETD handoff.

    New handoffs carry the contract explicitly.  For pre-fix handoffs, recover it only
    from the handoff's own source transaction cycle.  Never invent/rebase Online time
    independently of the Offline run being reused.
    """
    mp=Path(manifest_path) if manifest_path else Path(runtime_root)/'knowledge'/'handoff'/'offline_latest.json.gz'
    if not mp.is_file(): return {'ok':False,'reason':'offline_handoff_missing','manifest_path':str(mp)}
    try: hm=read_json_auto(mp)
    except Exception as exc: return {'ok':False,'reason':'offline_handoff_unreadable','error':repr(exc),'manifest_path':str(mp)}
    contract=dict(((hm.get('reuse_contract') or {}).get('climate_time_alignment') or {}))
    source='handoff_reuse_contract'
    if not contract:
        tx=str(hm.get('source_transaction_id') or '').strip()
        cp=Path(runtime_root)/'cycles'/f'{tx}.json.gz' if tx else None
        if cp is not None and cp.is_file():
            try: contract=_offline_alignment_from_cycle(read_json_auto(cp)); source='legacy_source_cycle_recovery'
            except Exception: contract={}
    if not contract:
        return {'ok':False,'reason':'offline_climate_alignment_contract_missing','manifest_path':str(mp),'source_transaction_id':hm.get('source_transaction_id')}
    start,end=forcing or (None,None)
    if start is None: return {'ok':False,'reason':'online_forcing_start_missing'}
    try:
        end_cmp=_online_datetime_to_forcing_clock(end,start) if end is not None else None
        eff=_online_datetime_to_forcing_clock(datetime.fromisoformat(str(contract.get('effective_climate_epoch')).replace('Z','+00:00')),start)
        old_start=_online_datetime_to_forcing_clock(datetime.fromisoformat(str(contract.get('forcing_start')).replace('Z','+00:00')),start)
        old_end_raw=contract.get('forcing_end'); old_end=_online_datetime_to_forcing_clock(datetime.fromisoformat(str(old_end_raw).replace('Z','+00:00')),start) if old_end_raw else None
    except Exception as exc:
        diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
        return {'ok':False,'reason':'offline_climate_alignment_contract_invalid','error':repr(exc),'contract':contract}
    if old_start!=start or ((old_end is None)!=(end_cmp is None)) or (old_end is not None and old_end!=end_cmp):
        return {'ok':False,'reason':'offline_online_forcing_window_mismatch','offline_start':old_start.isoformat(),'offline_end':old_end.isoformat() if old_end else None,'online_start':start.isoformat(),'online_end':end_cmp.isoformat() if end_cmp else None,'source':source}
    if eff<start or (end_cmp is not None and eff>end_cmp):
        return {'ok':False,'reason':'offline_effective_climate_epoch_outside_current_forcing','effective_epoch':eff.isoformat()}
    fp=resolve_climate_forcing(climate_root,require_exists=True)
    current_hash=_sha256_file(fp)
    expected_hash=contract.get('forcing_file_sha256')
    if expected_hash and current_hash!=expected_hash:
        return {'ok':False,'reason':'offline_online_forcing_hash_mismatch','expected_hash':expected_hash,'actual_hash':current_hash,'forcing_file':str(fp)}
    return {'ok':True,'source':source,'effective_climate_epoch':contract['effective_climate_epoch'],'forcing_start':start.isoformat(),'forcing_end':end_cmp.isoformat() if end_cmp else None,'forcing_file':str(fp) if fp else None,'forcing_file_sha256':current_hash,'configured_layer3_epoch':contract.get('configured_layer3_epoch'),'epoch_rebased_to_forcing_start':bool(contract.get('epoch_rebased_to_forcing_start')),'handoff_hash':hm.get('handoff_hash'),'source_transaction_id':hm.get('source_transaction_id'),'legacy_window_only_identity':bool(source=='legacy_source_cycle_recovery' and not expected_hash)}


def _apply_online_climate_time(req:dict, forcing, elapsed_s:float, alignment_contract:dict|None=None,*,native_binding_path=None,plant_time_s=None):
    """Bind ONLINE climate queries to the exact clock contract of the reused Offline run.

    A supplied Offline alignment must be valid and its recorded effective epoch
    remains authoritative even if the configured Layer-3 epoch is also in range.
    Without an Offline alignment, the configured epoch may serve only when it is
    inside the forcing window. Independent rebasing, end clamping and freshness
    shortcuts are forbidden.
    """
    if not forcing: raise RuntimeError('ONLINE climate forcing window is unavailable')
    start,end=forcing
    if start is None: raise RuntimeError('ONLINE climate forcing start is unavailable')
    configured=_layer3_mission_epoch_utc()  # Diagnostic field keeps its own declared clock domain.
    end_cmp=_online_datetime_to_forcing_clock(end,start) if end is not None else None
    configured_comparable=configured is not None and (configured.utcoffset() is None)==(start.utcoffset() is None)
    configured_inside=bool(configured_comparable and configured>=start and (end_cmp is None or configured<=end_cmp))
    contract=dict(alignment_contract or {})
    # A failed Offline binding is a blocker even when the configured epoch falls
    # inside the same forcing window. Never let clock selection bypass its hash.
    if alignment_contract is not None and contract.get('ok') is not True:
        raise RuntimeError('ONLINE must reuse the same finalized Offline handoff; climate alignment rejected: '
                           +str(contract.get('reason') or 'offline_climate_alignment_contract_required'))
    if contract.get('ok') is True and contract.get('effective_climate_epoch'):
        epoch=_online_datetime_to_forcing_clock(datetime.fromisoformat(str(contract['effective_climate_epoch']).replace('Z','+00:00')),start)
        basis='offline_handoff_effective_climate_epoch'
    elif configured_inside and alignment_contract is None:
        epoch=configured; basis='Layer3 mission_epoch_utc'
    else:
        if not configured_comparable and alignment_contract is None:
            raise RuntimeError('ONLINE clock domain mismatch: unspecified dataset timezone cannot imply UTC')
        reason=contract.get('reason') or 'offline_climate_alignment_contract_required'
        diagnostic=(f'Layer3 mission epoch {configured.isoformat()} is outside active ONLINE forcing window '
                    if not configured_inside else f'No valid Offline alignment for configured epoch {configured.isoformat()}; forcing window=')
        raise RuntimeError(
            diagnostic+
            f'{start.isoformat()}..{end_cmp.isoformat() if end_cmp else "open"}; '
            f'Online must reuse the effective climate epoch from the same finalized Offline handoff; reason={reason}. '
            'Do not refresh forcing between Offline and Online.'
        )
    projection=None
    if native_binding_path is not None:
        from tddt_tdoc.native_time import project_native_time
        projection=project_native_time(native_binding_path,plant_time_s)
        if contract.get('effective_climate_epoch')!=projection['effective_climate_epoch']:
            raise RuntimeError('native binding and Offline accepted epoch differ')
        elapsed=projection['mission_elapsed_s']
    else:
        if isinstance(elapsed_s,bool) or not isinstance(elapsed_s,(int,float)) or not math.isfinite(elapsed_s) or elapsed_s<0:
            raise RuntimeError('valid Online elapsed time required; no negative clamp or fabricated clock')
        elapsed=float(elapsed_s)
    t=epoch+timedelta(seconds=elapsed)
    if t<start or (end_cmp is not None and t>end_cmp):
        raise RuntimeError(
            f'ONLINE climate timestamp {t.isoformat()} lies outside active forcing window '
            f'{start.isoformat()}..{end_cmp.isoformat() if end_cmp else "open"}; TDOC will not clamp Online climate time.'
        )
    _set_env(req,['climate','time'],t.isoformat(),'ISO8601','SCALAR','time')
    req['climate']['time']['time_reference']='ACCEPTED_DATASET_NAIVE' if t.utcoffset() is None else 'OFFSET_EXPLICIT'
    if projection is not None:
        if projection['dataset_timestamp']!=t.isoformat():raise RuntimeError('native climate query projection differs')
        req['climate']['time']['time_binding']=projection
        req.setdefault('context',{})['native_climate_time_projection']=projection
    ctx=req.setdefault('context',{}); ctx['online_climate_forcing_time']=t.isoformat(); ctx['online_climate_forcing_start']=start.isoformat(); ctx['online_climate_forcing_end']=end_cmp.isoformat() if end_cmp else None
    ctx['online_climate_configured_layer3_epoch']=configured.isoformat() if configured is not None else None; ctx['online_climate_effective_epoch']=epoch.isoformat(); ctx['online_climate_mission_epoch']=epoch.isoformat(); ctx['online_climate_elapsed_s']=elapsed
    ctx['climate_time_alignment_basis']=basis+' + ONLINE simulator elapsed_s'; ctx['online_climate_time_safety']='same-offline-handoff clock reuse; fail_closed_on_window_or_hash_mismatch; no_clamp'
    if contract:
        ctx['online_climate_alignment_contract']={k:contract.get(k) for k in ('source','forcing_file_sha256','handoff_hash','source_transaction_id','legacy_window_only_identity')}
    return t

def physical_energy_wh(twins_root:Path):
    try:
        import yaml
        d=yaml.safe_load((twins_root/'config/phoenix_drone_v3.yaml').read_text()) or {}; return float((d.get('battery') or {}).get('usable_energy_Wh',20.5))
    except Exception:return 20.5

def l4_best(layer4:dict|None):
    # Safety invariant: unverified actions are never treated as physical actuation.
    if not isinstance(layer4,dict):return None
    cands=[c for c in (layer4.get('candidates') or []) if c.get('feasible') is True]
    if not cands:return None
    def rank(c):
        v=c.get('pareto_rank'); return 999 if v is None else int(v)
    def cost(c):
        v=c.get('scalar_cost'); return 1e99 if v is None else float(v)
    return sorted(cands,key=lambda c:(not bool(c.get('verified') or c.get('candidate_verified')),rank(c),cost(c)))[0]

def l5_result(cycle:dict):
    o=(((cycle.get('downstream_layers') or {}).get('5') or {}).get('output') or {})
    return o.get('result') if isinstance(o.get('result'),dict) else o

def climate_state(cycle):return (((cycle.get('layer2_packets') or {}).get('MicroMetTwin') or {}).get('state') or {})
def _pre_release_verify_evidence(cycle:dict, *, phase:str, cycle_id:int, reference_time_s:float)->dict[str,Any]:
    """Build fail-closed, non-actuating RTMO bootstrap evidence from one L2→L5 transaction."""
    l4=dict(cycle.get('layer4_result') or {})
    l5=dict(l5_result(cycle) or {})
    statuses=dict(cycle.get('layer_status') or {})
    ds=dict(cycle.get('downstream_layers') or {})
    prov=dict(cycle.get('provenance') or {})
    pmeta=dict(prov.get('rtmo_pre_release_verify') or {})
    current_obs=dict(prov.get('current_online_observation') or {})
    post_obs=dict(prov.get('post_execution_observation') or {})
    gates=dict(l5.get('gates') or {}); decision=dict(l5.get('decision') or {}); approval=dict(decision.get('approval') or {})
    l4_verified=bool(l4.get('verified_candidates'))
    l5_final=bool(gates.get('FINAL') is True or approval.get('final_gate') is True)
    l2_ok=bool(cycle.get('layer2_packets')) and bool((statuses.get('2') or {}).get('ready'))
    l3_ok=bool(cycle.get('layer3_result')) and bool((statuses.get('3') or {}).get('ready'))
    l6_status=str((ds.get('6') or {}).get('status') or '')
    l7_status=str((ds.get('7') or {}).get('status') or '')
    non_actuating=bool(pmeta.get('enabled') is True and pmeta.get('non_actuating') is True and l6_status=='NOT_INVOKED_PRE_RELEASE_VERIFY' and l7_status=='NOT_INVOKED_PRE_RELEASE_VERIFY' and (cycle.get('provenance') or {}).get('online_actuation_executed') is not True)
    qualified=bool(l2_ok and l3_ok and l4_verified and l5_final and non_actuating)
    return {
        'schema':'TDOC-RTMO-pre-release-verify-v1',
        'phase':str(phase),'cycle_id':int(cycle_id),'reference_time_s':float(reference_time_s),
        'transaction_id':cycle.get('transaction_id'),'qualified':qualified,'non_actuating':non_actuating,
        'layer2_current_state':l2_ok,'layer3_atomic_state':l3_ok,
        'l4_full_fidelity_verified':l4_verified,'l5_final':l5_final,'control_effect_applied':False,
        'l5_decision_id':decision.get('decision_id'),'layer6_status':l6_status,'layer7_status':l7_status,
        'l5_hold_reason':approval.get('reason') or ((l5.get('recommendation') or {}).get('tradeoff') or {}).get('reason'),
        'l4_rejection_reasons':list(dict.fromkeys(str(reason)
            for candidate in (cycle.get('layer4_result') or {}).get('candidates',[])
            for reason in (candidate.get('verification') or {}).get('reason_codes',[])
            if not candidate.get('verified'))),
        'current_online_observation':current_obs or None,'post_execution_observation':post_obs or None,
        'reason':'fresh_current_state_l4_l5_qualified' if qualified else 'fresh_current_state_l4_l5_not_qualified',
        'durable_joint_control_proof':False,'synthetic_capability':False,
    }

from tddt_tdoc.online_mission import load_mission_prior, apply_mission_prior, bind_phase_reference, bind_pending_navigation_initialization

_NATIVE_IMU_VALUES=('accel_x_m_s2','accel_y_m_s2','accel_z_m_s2','gyro_x_rad_s','gyro_y_rad_s','gyro_z_rad_s')

def _validated_native_clock_samples(samples,gui):
    """Recheck consumed historical identity/wire data, never declare it fresh."""
    from layer6.telemetry import dialect
    target=(gui.get('target_system'),gui.get('target_component'))
    session,domain=gui.get('session_id'),gui.get('monotonic_domain_id')
    if (not isinstance(session,str) or len(session)!=32 or not isinstance(domain,str) or not domain
        or any(type(x) is not int or not 1<=x<=255 for x in target)):
        raise ValueError('Native measured clock has no GUI source/session/domain')
    result=[];previous_time=previous_seq=previous_receipt=None
    for original in samples or []:
        sample=dict(original);stamp=sample.get('plant_time_s');boot=sample.get('time_boot_ms')
        seq=sample.get('history_sequence');receipt=sample.get('timestamp_monotonic');wire_hash=sample.get('wire_sha256')
        if (sample.get('source')!='MAVLink/SCALED_IMU' or sample.get('frame')!='BODY_FRD'
            or sample.get('session_id')!=session or sample.get('monotonic_domain_id')!=domain
            or type(boot) is not int or not 0<=boot<2**32 or type(seq) is not int or seq<1
            or isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp) or stamp!=boot*.001
            or isinstance(receipt,bool) or not isinstance(receipt,(int,float)) or not math.isfinite(receipt) or receipt<0
            or previous_time is not None and (stamp<=previous_time or seq<=previous_seq or receipt<previous_receipt)
            or not isinstance(wire_hash,str) or len(wire_hash)!=64 or any(x not in '0123456789abcdef' for x in wire_hash)
            or any(isinstance(sample.get(k),bool) or not isinstance(sample.get(k),(int,float)) or not math.isfinite(sample[k])
                   for k in _NATIVE_IMU_VALUES)):
            raise ValueError('Native consumed IMU clock source/order/identity invalid')
        # Retained history includes original wire. The fresh GUI tail was already
        # validated by Layer2; its endpoint is independently bound below to the
        # exact GUI metadata/current IMU digest and all normalized channels.
        if sample.get('wire_hex') is not None:
            try:
                wire=bytes.fromhex(sample['wire_hex']);message=dialect.MAVLink(None).parse_char(wire)
            except Exception as exc:raise ValueError('Native consumed IMU wire decode failed') from exc
            if (not wire or hashlib.sha256(wire).hexdigest()!=wire_hash or message is None
                or message.get_type()!='SCALED_IMU' or (message.get_srcSystem(),message.get_srcComponent())!=target
                or message.time_boot_ms!=boot):
                raise ValueError('Native consumed IMU wire/source/time mismatch')
            for key,raw,factor in zip(_NATIVE_IMU_VALUES,
                ('xacc','yacc','zacc','xgyro','ygyro','zgyro'),(9.80665/1000.,)*3+(.001,)*3):
                if not math.isclose(sample[key],getattr(message,raw)*factor,rel_tol=1e-12,abs_tol=1e-12):
                    raise ValueError('Native consumed IMU normalized values differ from wire')
        result.append(sample);previous_time=stamp;previous_seq=seq;previous_receipt=receipt
    return result


def _consumed_native_imu_clock(cycle):
    """Extract only the real Native IMU history that MESA actually consumed."""
    mesa=(cycle.get('layer2_packets') or {}).get('MESA_NAV') or {}
    ingress=((mesa.get('context') or {}).get('online_state_ingress') or {}).get('ptlib_ingress') or {}
    if ingress.get('source')!='Layer6/GUI/MAVLink':
        raise ValueError('MESA consumed Native IMU ingress missing')
    proof=(mesa.get('provenance') or {}).get('online_imu_ingress') or {}
    if (proof.get('measured_simulated_imu') is not True or proof.get('gnss_used') is not False
        or proof.get('truth_position_used') is not False or proof.get('truth_velocity_used') is not False):
        raise ValueError('MESA consumed measured IMU provenance missing')
    gui=ingress.get('gui_state') or {};imu=gui.get('imu') or {}
    samples=_validated_native_clock_samples(ingress.get('imu_samples'),gui)
    if not samples:raise ValueError('MESA consumed Native IMU history empty')
    endpoint=samples[-1];meta=(gui.get('telemetry_metadata') or {}).get('SCALED_IMU') or {}
    source_time=meta.get('source_time') or {};state=ingress.get('state') or {}
    if (endpoint['plant_time_s']!=imu.get('plant_time_s') or endpoint['plant_time_s']!=proof.get('plant_time_s')
        or endpoint['plant_time_s']!=state.get('imu_time_s') or endpoint['history_sequence']!=imu.get('history_sequence')
        or endpoint['history_sequence']!=meta.get('history_sequence')
        or endpoint['wire_sha256']!=imu.get('wire_sha256') or endpoint['wire_sha256']!=meta.get('wire_sha256')
        or source_time!={'field':'time_boot_ms','value':endpoint['time_boot_ms'],'unit':'ms','clock_domain':'FCU_BOOT'}
        or (meta.get('source_system'),meta.get('source_component'))!=(gui.get('target_system'),gui.get('target_component'))
        or any(endpoint[k]!=imu.get(k) or endpoint[k]!=state.get(k) for k in _NATIVE_IMU_VALUES)):
        raise ValueError('MESA consumed Native IMU endpoint differs from original GUI observation')
    snapshot=((cycle.get('layer3_result') or {}).get('snapshot') or {})
    reference=snapshot.get('reference_time_s');mesa_time=mesa.get('timestamp')
    if (isinstance(reference,bool) or not isinstance(reference,(int,float)) or not math.isfinite(reference)
        or isinstance(mesa_time,bool) or not isinstance(mesa_time,(int,float)) or not math.isfinite(mesa_time)
        or abs(reference-endpoint['plant_time_s'])>1e-8 or abs(mesa_time-reference)>1e-8):
        raise ValueError('MESA consumed IMU and Layer3 snapshot clocks differ')
    return {'samples':samples,'endpoint_s':endpoint['plant_time_s'],'gui':gui}


def _merge_consumed_native_clock_samples(cycle,execution_samples):
    """Merge actual predecision and execution history; conflicts are never repaired."""
    consumed=_consumed_native_imu_clock(cycle)
    groups=(consumed['samples'],_validated_native_clock_samples(execution_samples,consumed['gui']))
    by_sequence={};by_time={}
    compare=('plant_time_s','time_boot_ms','wire_sha256','session_id','monotonic_domain_id',
             'source','frame','timestamp_monotonic',*_NATIVE_IMU_VALUES)
    for group in groups:
        for sample in group:
            seq=sample['history_sequence'];stamp=sample['plant_time_s']
            if seq in by_sequence:
                if any(sample.get(k)!=by_sequence[seq].get(k) for k in compare):
                    raise ValueError('Native L2/execution history overlapping wire values conflict')
                if 'wire_hex' in sample:by_sequence[seq].setdefault('wire_hex',sample['wire_hex'])
                continue
            if stamp in by_time:raise ValueError('Native IMU source time has conflicting history identities')
            by_sequence[seq]=dict(sample);by_time[stamp]=seq
    merged=[by_sequence[seq] for seq in sorted(by_sequence)]
    return _validated_native_clock_samples(merged,consumed['gui'])


def _run_pre_release_verify(orch, req:dict, *, phase:str, cycle_id:int, reference_time_s:float, registry:LiveCapabilityRegistry|None=None,rtclock=None)->tuple[dict[str,Any],dict[str,Any]]:
    """Run L2→L3→fresh L4→L5 only; never invoke Layer6/Layer7."""
    q=copy.deepcopy(req); q['stage']=str(phase); q['rtmo_pre_release_verify']=True
    q['reference_time_s']=float(reference_time_s)
    q['transaction_id']=f"{str(req.get('transaction_id') or 'online')}-pre-release-{phase}"
    ctx=dict(q.get('context') or {})
    try: phase_id=list(MissionPhaseManager().phase_order).index(str(phase))+1
    except ValueError: phase_id=None
    ctx['flight_phase']=str(phase)
    ctx['rtmo_pre_release_verify']={'schema':'TDOC-RTMO-pre-release-verify-request-v1','phase':str(phase),'cycle_id':int(cycle_id),'non_actuating':True,'layer6_forbidden':True,'layer7_forbidden':True}
    if phase_id is not None:
        old=dict(ctx.get('rtmo_phase_token') or {})
        ctx['rtmo_phase_token']={**old,'phase_id':phase_id,'phase_name':str(phase),'phase_seq':phase_id,'cycle':int(cycle_id)}
    if isinstance(ctx.get('rtmo_run_context'),dict):
        ctx['rtmo_run_context']={**ctx['rtmo_run_context'],'phase_id':phase}
    q['context']=ctx
    bind_phase_reference(q,phase)
    cyc=orch.run_cycle(q).to_dict()
    # The PRE_RELEASE transaction itself samples Layer6/GUI/MAVLink before L2.  Bind the
    # reusable artifact to that authoritative plant timestamp; the caller's
    # scheduler reference is retained only as diagnostic provenance.
    _bootstrap_obs=dict((cyc.get('provenance') or {}).get('current_online_observation') or {})
    _bootstrap_plant_time=_num(_bootstrap_obs.get('plant_time_s'),None) if _bootstrap_obs.get('plant_time_present') is True else None
    _artifact_reference_time=float(_bootstrap_plant_time if _bootstrap_plant_time is not None else reference_time_s)
    _native_clock_required=(_bootstrap_obs.get('source')=='Layer6/GUI/MAVLink')
    _native_clock_diagnostic=None
    if _native_clock_required:
        try:
            consumed=_consumed_native_imu_clock(cyc)
            _artifact_reference_time=consumed['endpoint_s']
            if rtclock is None:raise ValueError('Native bootstrap measured clock consumer missing')
            _native_clock_diagnostic=rtclock.observe_measured_samples(_artifact_reference_time,consumed['samples'])
        except (ValueError,TypeError,KeyError) as exc:
            _native_clock_diagnostic={'ok':False,'state_fresh':False,'reason':'invalid_consumed_native_clock','error':str(exc)}
    ev=_pre_release_verify_evidence(
        cyc,phase=str(phase),cycle_id=int(cycle_id),
        reference_time_s=_artifact_reference_time)
    ev['requested_reference_time_s']=float(reference_time_s)
    ev['artifact_time_authority']='Layer6/GUI/MAVLink plant clock' if _bootstrap_plant_time is not None else 'SCHEDULER_REFERENCE_NO_PLANT_SAMPLE'
    if _native_clock_required:
        ev['consumed_native_clock']=_native_clock_diagnostic
        ev['artifact_time_authority']='Actual measured MESA-consumed Native IMU endpoint, matching Layer3 atomic snapshot'
        if not _native_clock_diagnostic.get('ok'):
            ev['qualified']=False;ev['reason']='pre_release_native_clock_continuity_failed'
    # P0-4: PRE_RELEASE_CURRENT_PHASE_BOOTSTRAP atomically seeds the reusable
    # Layer-4 cache only when the same non-actuating transaction is fully qualified.
    # This closes cold-start/phase-entry starvation without weakening Full-Fidelity
    # or Layer-5 FINAL gates and without invoking Layer6/Layer7.
    try:
        l3_snapshot=((cyc.get('layer3_result') or {}).get('snapshot') or {})
        pub=orch.rtmo_l4_artifacts.publish_bootstrap(
            result=dict(cyc.get('layer4_result') or {}), snapshot=l3_snapshot, request=q,
            orchestration_version=str(cyc.get('orchestration_version') or orch.versions.active),
            reference_time_s=_artifact_reference_time, l5_final=bool(ev.get('l5_final')),
            bootstrap_qualified=bool(ev.get('qualified')), transaction_id=str(cyc.get('transaction_id') or q.get('transaction_id') or ''),
        )
        ev['l4_artifact_bootstrap']={
            'ok':bool(pub.ok),'reason':pub.reason,'metadata':dict(pub.metadata or {}),
            'atomic_publish':bool(pub.ok),'fast_lane_seeded':bool(pub.ok),
        }
    except Exception as exc:
        diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
        ev['l4_artifact_bootstrap']={
            'ok':False,'reason':'bootstrap_publish_exception','error':repr(exc),
            'atomic_publish':False,'fast_lane_seeded':False,
        }
    if registry is not None and str(phase) not in ('on_ground','touchdown'):
        registry.record_pre_release_verification(phase=str(phase),evidence=ev)
    return ev,cyc


def meso_slow(cycle):
    k=((cycle.get('provenance') or {}).get('knowledge_ledger') or {})
    return bool(k.get('layer3_has_meso')),bool(k.get('layer3_has_slow')),k

def update_request_from_reference(req:dict,row,nextrow,origin_x,origin_y,dest_x,dest_y,forcing_start,forcing_end,remaining_wh):
    dt=max(float(row.get('step_dt_s',1.0)),1e-6); x=float(row['x_m']-origin_x); y=float(row['y_m']-origin_y); h=float(row.get('z_msl_m',row.get('h_m',1800.0)))
    nx=float(nextrow['x_m']-origin_x); ny=float(nextrow['y_m']-origin_y); nh=float(nextrow.get('z_msl_m',nextrow.get('h_m',h)))
    dx,dy,dz=nx-x,ny-y,nh-h; ds=max(math.hypot(dx,dy),1e-9); va=float(row.get('airspeed_ms',max(ds/dt,1.0)))
    vx=va*dx/ds; vy=va*dy/ds; vh=dz/dt
    _set_env(req,['wing_inputs','state_x'],x,'m','ENU','state_x'); _set_env(req,['wing_inputs','state_y'],y,'m','ENU','state_y'); _set_env(req,['wing_inputs','state_h'],h,'m','ENU','state_h')
    _set_env(req,['wing_inputs','air_velocity_x'],vx,'m/s','ENU','air_velocity_x'); _set_env(req,['wing_inputs','air_velocity_y'],vy,'m/s','ENU','air_velocity_y'); _set_env(req,['wing_inputs','air_velocity_h'],vh,'m/s','ENU','air_velocity_h')
    _set_env(req,['context','target_enu_m'],[float(dest_x-origin_x),float(dest_y-origin_y),float(nextrow.get('z_msl_m',h))],'m','ENU','target_position')
    _set_env(req,['context','target_projected_enu_m'],[float(dest_x),float(dest_y),float(nextrow.get('z_msl_m',h))],'m','PROJECTED_ENU','target_position_absolute')
    _set_env(req,['climate','latitude'],float(row['lat_deg']),'deg','WGS84','latitude'); _set_env(req,['climate','longitude'],float(row['lon_deg']),'deg','WGS84','longitude')
    _set_env(req,['climate','elevation_ground_level'],max(1.0,float(row.get('clearance_m',100.0))),'m','AGL','elevation_ground_level')
    t=_climate_time_for_elapsed((forcing_start,forcing_end),float(row.get('time_s',0.0))) if forcing_start else None
    if t:_set_env(req,['climate','time'],t.isoformat(),'ISO8601','SCALAR','time'); req['climate']['time']['time_reference']='UTC'; req.setdefault('context',{})['climate_time_alignment_basis']='Layer3 mission_epoch_utc + offline elapsed_s'
    req['stage']=PHASES.get(int(row.get('phase_id',3)),'cruise'); req['reference_time_s']=float(row.get('time_s',0.0))+0.001
    req.setdefault('context',{})['mission_progress']=float(row.get('mission_progress',0.0)); req['context']['remaining_energy_wh']=float(remaining_wh); req['context']['terrain_clearance_m']=float(row.get('clearance_m',math.nan)); req['context']['reference_route_point_id']=int(row.get('point_id',0)); req['context']['reference_state_source']='SARG_REFERENCE_REPLAY'
    return {'x_enu_m':x,'y_enu_m':y,'h_msl_m':h,'vx_m_s':vx,'vy_m_s':vy,'vh_m_s':vh}


def _clamp(v,lo,hi): return max(lo,min(hi,v))

def _terrain_from_row(row):
    z=float(row.get('z_msl_m',row.get('h_m',0.0))); c=float(row.get('clearance_m',0.0)); return z-c

def build_vtol_reference(route):
    import pandas as pd
    base=route.copy().reset_index(drop=True)
    first=base.iloc[0].to_dict(); last=base.iloc[-1].to_dict(); g0=_terrain_from_row(first); g1=_terrain_from_row(last)
    rows=[]; t=0.0
    def add(src,phase,z=None,airspeed=None):
        nonlocal t
        r=dict(src); r['time_s']=t; r['step_dt_s']=1.0; r['tdoc_phase']=phase
        if z is not None:r['z_msl_m']=float(z); r['clearance_m']=float(z)-(_terrain_from_row(src))
        if airspeed is not None:r['airspeed_ms']=float(airspeed)
        rows.append(r); t+=1.0
    add(first,'on_ground',g0,0.0)
    for k in range(1,7): add(first,'vertical_takeoff',g0+12.0*k/6.0,0.0)
    for _ in range(3): add(first,'hover',g0+12.0,0.0)
    for k in range(1,6): add(first,'transition_forward',g0+12.0+18.0*k/5.0,3.0+2.0*k)
    # Airborne SARG sequence: geometry/reference only; never copied into realized state.
    for _,rr in base.iterrows():
        d=rr.to_dict(); phase=PHASES.get(int(d.get('phase_id',3)),'cruise'); add(d,phase,None,None)
    # Reference-only end envelope. Actual terminal capture may extend beyond these rows.
    for k in range(1,5): add(last,'transition_back',max(g1+12.0,float(last.get('z_msl_m',g1+12.0))-(float(last.get('z_msl_m',g1+12.0))-(g1+12.0))*k/4.0),6.0)
    return pd.DataFrame(rows)

class OfflineWingMPETPlant:
    """TDOC-owned offline plant using the external WingMPET formulas.

    op07 is authoritative for realized and same-command/no-wind counterfactual kinematics.
    op27/28/30/31 are diagnostic shadows only and never become Layer-5 physical commands.
    """
    _ROTATION_STATE_KEYS=('qw','qx','qy','qz','p_rate','q_rate','r_rate')
    def __init__(self,twins_root:Path,start_x:float,start_y:float,start_h:float,initial_energy_wh:float):
        sys.path.insert(0,str(twins_root/'python'))
        from twins_fmi import FormulaFMU,DroneConfig
        import yaml
        self._FormulaFMU=FormulaFMU; self.root=twins_root; self.cfg=DroneConfig(twins_root/'config/phoenix_drone_v3.yaml')
        self.fmu=FormulaFMU(twins_root/'fmus/WingMPET.fmu',config=self.cfg)
        y=yaml.safe_load((twins_root/'config/phoenix_drone_v3.yaml').read_text()) or {}
        self.mass=float((y.get('mass') or {}).get('empty_or_flight_mass_kg',0.65)); self.voltage=float((y.get('battery') or {}).get('nominal_voltage_V',11.1)); self.initial_wh=float(initial_energy_wh)
        self.state={'x':float(start_x),'y':float(start_y),'h':float(start_h),'air_vx':0.0,'air_vy':0.0,'air_vh':0.0,'soc':1.0,'omega_left':0.0,'omega_right':0.0}
        self.cf=dict(self.state)
        # Phoenix is a tailsitter. In canonical FRD/NED the rotor thrust axis is
        # +body-X, so the passive/vertical initial attitude is +90 deg pitch.
        _c=math.sqrt(0.5)
        self.shadow={'pn':0.0,'pe':0.0,'pd':0.0,'vn':0.0,'ve':0.0,'vd':0.0,'qw':_c,'qx':0.0,'qy':_c,'qz':0.0,'p_rate':0.0,'q_rate':0.0,'r_rate':0.0}
        self._last_phase=None
        self._last_wind=(0.0,0.0,0.0)
        self._vtol_reference={'vertical_takeoff':0.78,'hover':0.67,'vertical_landing':0.67}
        self._vtol_reference_source='legacy_fallback'
        self._landing_descent_ref_m_s=1.0
        # Priority 4: use the exact Layer5 physics-derived OFFLINE VTOL seed for
        # simulation-reference motor spool in takeoff/hover.  This path remains
        # diagnostic/non-operational, but it must not create a phase-entry state
        # inconsistent with the controller/FF contract that immediately consumes it.
        try:
            l5root=envpath('TDDT_LAYER5_ROOT')
            if l5root is not None:
                sys.path.insert(0,str(l5root/'src'))
                from layer5.config import Layer5Config
                from layer5.phoenix import PhoenixSpecialization
                ph=PhoenixSpecialization(Layer5Config.from_yaml(l5root/'configs/configuration_phoenix.yaml'))
                seed=ph.offline_vtol_seed_controls({'air_density_kg_m3':1.225,'battery_soc':1.0})
                phases=seed.get('phases') or {}
                to=float((phases.get('vertical_takeoff') or {}).get('throttle_request_norm'))
                hv=float((phases.get('hover') or {}).get('throttle_request_norm'))
                ld=float((phases.get('vertical_landing') or {}).get('throttle_request_norm',hv))
                if not (0.0 <= to <= 1.0 and 0.0 <= hv <= 1.0 and 0.0 <= ld <= 1.0): raise ValueError('invalid_vtol_seed')
                self._vtol_reference={'vertical_takeoff':to,'hover':hv,'vertical_landing':ld}
                pb=seed.get('physics_basis') or {}; lb=pb.get('vertical_landing_physics_basis') or {}
                self._landing_descent_ref_m_s=max(0.2,min(2.0,float(lb.get('descent_rate_target_m_s',1.0))))
                self._vtol_reference_source='Layer5_PhoenixSpecialization.offline_vtol_seed_controls'
        except Exception:
            # Fail-safe compatibility: inability to import Layer5 must not alter
            # authority. Tests/preflight expose the fallback; it is never FINAL.
            pass
    def close(self):
        try:self.fmu.close()
        except Exception:pass
    def _release_unmodeled_rotation(self):
        """Do not carry a closed-loop rotation through an op07-only HOLD step.

        Operation 07 advances translation only.  Consequently a quaternion/body-rate
        tuple copied from a previous Layer2 physical-control result ceases to be an
        authoritative plant observation as soon as op07 advances the state without a
        new physical-control result.  Removing it makes the next Fast boundary use the
        documented velocity-derived fallback instead of presenting stale rotation to
        Layer4 full-fidelity verification.  A successful same-cycle Layer5 feedback
        transaction re-installs the fresh op27 rotation after ``propagate`` returns.
        """
        removed=False
        for state in (self.state,self.cf):
            for key in self._ROTATION_STATE_KEYS:
                removed=bool(state.pop(key,None) is not None) or removed
        return removed
    def _actuator_ref(self,phase,substate=None):
        refs=getattr(self,'_vtol_reference',{}) or {}; to=float(refs.get('vertical_takeoff',0.78)); hv=float(refs.get('hover',0.67)); ld=float(refs.get('vertical_landing',hv))
        m={'on_ground':(0,0,0,0),'vertical_takeoff':(to,to,0,0),'hover':(hv,hv,0,0),'transition_forward':(.68,.68,.04,.04),'climb':(.62,.62,.02,.02),'cruise':(.52,.52,0,0),'turn':(.57,.51,.035,-.035),'approach':(.46,.46,0,0),'transition_back':(.64,.64,-.035,-.035),'terminal_reposition':(ld,ld,0,0),'vertical_landing':(ld,ld,0,0),'touchdown':(0,0,0,0)}
        mode=substate if substate in MISSION_SUBSTATES else phase
        l,r,e1,e2=m.get(mode,(.5,.5,0,0)); return {'left_motor_cmd':l,'right_motor_cmd':r,'left_elevon_rad':e1,'right_elevon_rad':e2,'source':'simulation_reference_non_operational','tdoc_substate':substate,'vtol_reference_source':getattr(self,'_vtol_reference_source','legacy_fallback')}
    def prepare_phase_entry(self,phase,*,target_h=None):
        """Stabilize VTOL phase-entry boundaries of the simplified OFFLINE plant.

        Priority 4 owns vertical_takeoff→hover. Priority 5 adds terminal capture for
        the first vertical_landing sample. Position, altitude, SOC and rotor spool are
        preserved. Only kinematic state that is incompatible with the rotor-supported
        tailsitter regime is reset. No Layer4/Layer5 proof, certificate, FINAL or
        actuator authority is manufactured.
        """
        cur=str(phase or '')
        prev=str(getattr(self,'_last_phase',None) or '')
        hover_entry=bool(cur=='hover' and prev=='vertical_takeoff')
        landing_entry=bool(cur=='vertical_landing' and prev in {'transition_back','approach'})
        applied=bool(hover_entry or landing_entry)
        transition_departure=bool(cur=='transition_forward' and prev=='hover')
        schema='TDOC-Phoenix-hover-phase-entry-v1' if hover_entry else ('TDOC-Phoenix-vertical-landing-phase-entry-v1' if landing_entry else ('TDOC-Phoenix-transition-departure-v1' if transition_departure else 'TDOC-Phoenix-phase-entry-v1'))
        ev={'schema':schema,'from_phase':prev or None,'to_phase':cur,'applied':applied,'position_preserved':True,'energy_preserved':True,'motor_spool_preserved':True,'synthetic_control_proof':False}
        if applied:
            wx,wy,wh=(float(x) for x in (getattr(self,'_last_wind',(0.0,0.0,0.0)) or (0.0,0.0,0.0)))
            before={'h':float(self.state['h']),'air_vx':float(self.state.get('air_vx',0.0)),'air_vy':float(self.state.get('air_vy',0.0)),'air_vh':float(self.state.get('air_vh',0.0)),'omega_left':float(self.state.get('omega_left',0.0)),'omega_right':float(self.state.get('omega_right',0.0))}
            # Both hover and terminal landing enter with zero horizontal ground speed.
            # Landing starts from a bounded 1 m/s downward ground rate, matching the
            # Layer5 landing physics basis, instead of carrying transition_back airspeed.
            landing_ref=max(0.2,float(getattr(self,'_landing_descent_ref_m_s',1.0)))
            descent_ground_m_s=0.0 if hover_entry else -landing_ref
            if landing_entry and target_h is not None:
                remaining=max(0.0,float(self.state['h'])-float(target_h))
                descent_ground_m_s=-min(landing_ref,max(0.2,remaining)) if remaining>0.0 else 0.0
            self.state['air_vx']=-wx; self.state['air_vy']=-wy; self.state['air_vh']=descent_ground_m_s-wh
            self.cf['air_vx']=0.0; self.cf['air_vy']=0.0; self.cf['air_vh']=descent_ground_m_s
            c=math.sqrt(0.5)
            # Priority 6: the state handed to Layer4 Full-Fidelity is plant.state,
            # not only the diagnostic 6-DoF shadow.  Keep both representations on
            # the same physical phase-entry boundary.  Previously metadata/shadow
            # advertised +90 deg / zero rates while offline_plant_state retained
            # the preceding approach quaternion/rates, causing landing FF to start
            # from stale wing-borne attitude and accelerate to 20--40 m/s.
            phase_attitude={'qw':c,'qx':0.0,'qy':c,'qz':0.0,
                            'p_rate':0.0,'q_rate':0.0,'r_rate':0.0}
            self.state.update(phase_attitude)
            self.cf.update(phase_attitude)
            self.shadow.update({'vn':0.0,'ve':0.0,'vd':-descent_ground_m_s,**phase_attitude})
            basis='zero_ground_velocity_at_hover_entry_using_last_MicroMet_wind' if hover_entry else 'terminal_capture_vertical_landing_using_last_MicroMet_wind_and_Layer5_descent_reference'
            ev.update({'basis':basis,'last_wind_enu_m_s':[wx,wy,wh],'target_h_m':None if target_h is None else float(target_h),'ground_vertical_rate_m_s':descent_ground_m_s,'before':before,'after':{'h':float(self.state['h']),'air_vx':float(self.state['air_vx']),'air_vy':float(self.state['air_vy']),'air_vh':float(self.state['air_vh']),'omega_left':float(self.state.get('omega_left',0.0)),'omega_right':float(self.state.get('omega_right',0.0))},'attitude':'FRD_to_NED_pitch_plus_90deg','body_rates_rad_s':[0.0,0.0,0.0]})
        elif transition_departure:
            # op07 has no rotational state.  The +90-degree quaternion installed at
            # hover entry is initialization evidence, not an authoritative attitude
            # for every forward-transition boundary.  Remove it once at departure so
            # Layer2/Layer3 construct their documented kinematic FRD/NED fallback from
            # the current realized velocity.  The op27 shadow stays diagnostic only.
            for state in (self.state,self.cf):
                for key in ('qw','qx','qy','qz','p_rate','q_rate','r_rate'):
                    state.pop(key,None)
            ev.update({'applied':True,'basis':'release_hover_initialization_attitude_to_kinematic_transition_fallback',
                       'position_preserved':True,'energy_preserved':True,'motor_spool_preserved':True,
                       'attitude_removed_from_op07_state':True,'synthetic_control_proof':False})
        self._last_phase=cur
        return ev
    def command(self,target,phase,wind,base_airspeed,yaw_adv=0.0,airspeed_adv=0.0,substate=None):
        s=self.state; dx=float(target[0])-s['x']; dy=float(target[1])-s['y']; dz=float(target[2])-s['h']; horiz=math.hypot(dx,dy)
        if phase in ('on_ground','touchdown'): return (0.0,0.0,0.0)
        if phase=='vertical_takeoff':
            # Ground-relative vertical climb with explicit wind compensation.  This
            # prevents the final takeoff sample from overshooting the hover target or
            # accumulating horizontal drift that corrupts the hover phase-entry state.
            return (-wind[0],-wind[1],_clamp(dz,-3.0,3.0)-wind[2])
        if phase=='hover': return (_clamp(dx,-1,1)-wind[0],_clamp(dy,-1,1)-wind[1],_clamp(dz,-1,1)-wind[2])
        speed=_clamp(float(base_airspeed)+float(airspeed_adv),2.0,22.0)
        heading=math.atan2(dy,dx) if horiz>1e-6 else 0.0; heading+=_clamp(float(yaw_adv),-.15,.15)
        if phase=='vertical_landing':
            vx=_clamp(dx,-1.5,1.5)-wind[0]; vy=_clamp(dy,-1.5,1.5)-wind[1]; descent=max(0.2,float(getattr(self,'_landing_descent_ref_m_s',1.0))); vz=_clamp(dz,-descent,-0.2)-wind[2]; return (vx,vy,vz)
        if substate=='terminal_reposition':
            # TDOC-internal approach substate: wind-compensated ground guidance at safe AGL.
            gs=min(speed,max(2.0,horiz)); return (gs*math.cos(heading)-wind[0],gs*math.sin(heading)-wind[1],_clamp(dz,-2,2)-wind[2])
        vz=_clamp(dz,-3.0,3.0)
        return (speed*math.cos(heading),speed*math.sin(heading),vz)
    def _op7(self,state,cmd,wind,target,dt):
        o=self.fmu.evaluate_operation(7,dt=dt,x=state['x'],y=state['y'],h=state['h'],air_velocity_x=cmd[0],air_velocity_y=cmd[1],air_velocity_h=cmd[2],wind_x=wind[0],wind_y=wind[1],wind_h=wind[2],target_x=target[0],target_y=target[1],target_h=target[2])
        return {**state,'x':float(o['next_x']),'y':float(o['next_y']),'h':float(o['next_h']),'air_vx':float(cmd[0]),'air_vy':float(cmd[1]),'air_vh':float(cmd[2])},o
    def propagate(self,target,phase,wind,air_density,base_airspeed,yaw_adv=0.0,airspeed_adv=0.0,dt=1.0,ground=0.0,substate=None):
        self._last_wind=tuple(float(x) for x in wind)
        cmd=self.command(target,phase,wind,base_airspeed,yaw_adv,airspeed_adv,substate=substate)
        passive_boundary=phase in ('on_ground','touchdown')
        if passive_boundary:
            # Priority 9: both mission boundary phases use one explicit ground-contact
            # physics policy.  A passive vehicle in contact with terrain is not a free
            # body: wind may remain present in climate/diagnostic channels, but it may
            # not advect the realized or counterfactual state.  Terminal location
            # acceptance remains a separate fail-closed mission condition below; this
            # block only enforces the symmetric contact mechanics.
            x0,y0=float(self.state['x']),float(self.state['y'])
            cx0,cy0=float(self.cf['x']),float(self.cf['y'])
            self.state,o=self._op7(self.state,(0.0,0.0,0.0),(0.0,0.0,0.0),(x0,y0,float(ground)),dt)
            self.cf,ocf=self._op7(self.cf,(0.0,0.0,0.0),(0.0,0.0,0.0),(cx0,cy0,float(ground)),dt)
            self.state.update({'x':x0,'y':y0,'h':float(ground),'air_vx':0.0,'air_vy':0.0,'air_vh':0.0})
            self.cf.update({'x':cx0,'y':cy0,'h':float(ground),'air_vx':0.0,'air_vy':0.0,'air_vh':0.0})
            cmd=(0.0,0.0,0.0)
        else:
            self.state,o=self._op7(self.state,cmd,wind,target,dt); self.cf,ocf=self._op7(self.cf,cmd,(0.0,0.0,0.0),target,dt)
        act=self._actuator_ref(phase,substate=substate); airspeed=math.sqrt(sum(v*v for v in cmd)); fp=math.atan2(cmd[2],max(1e-9,math.hypot(cmd[0],cmd[1])))
        motor=self.fmu.evaluate_operation(28,dt=dt,omega_left=self.state.get('omega_left',0),omega_right=self.state.get('omega_right',0),left_motor_cmd=act['left_motor_cmd'],right_motor_cmd=act['right_motor_cmd'])
        self.state['omega_left']=float(motor['next_omega_left']); self.state['omega_right']=float(motor['next_omega_right'])
        env=self.fmu.evaluate_operation(30,airspeed=airspeed,sideslip=0.0,bank_angle=0.0,flightpath_angle=fp,p_rate=0.0,q_rate=0.0,r_rate=float(yaw_adv),load_factor=1.0,total_thrust=float(motor['total_thrust']),mass=self.mass,left_motor_cmd=act['left_motor_cmd'],right_motor_cmd=act['right_motor_cmd'],left_elevon_rad=act['left_elevon_rad'],right_elevon_rad=act['right_elevon_rad'])
        power=self.fmu.evaluate_operation(31,dt=dt,air_density=float(air_density or 1.0),airspeed=airspeed,total_thrust=float(motor['total_thrust']),soc=float(self.state.get('soc',1.0)),battery_voltage=self.voltage,battery_current=0.0,battery_telemetry_valid=0.0)
        self.state['soc']=float(power['next_soc'])
        sh=self.shadow
        six=self.fmu.evaluate_operation(27,dt=dt,pn=sh['pn'],pe=sh['pe'],pd=sh['pd'],vn=sh['vn'],ve=sh['ve'],vd=sh['vd'],qw=sh['qw'],qx=sh['qx'],qy=sh['qy'],qz=sh['qz'],p_rate=sh['p_rate'],q_rate=sh['q_rate'],r_rate=sh['r_rate'],omega_left=self.state['omega_left'],omega_right=self.state['omega_right'],soc=self.state['soc'],left_motor_cmd=act['left_motor_cmd'],right_motor_cmd=act['right_motor_cmd'],left_elevon_rad=act['left_elevon_rad'],right_elevon_rad=act['right_elevon_rad'],wind_n=wind[1],wind_e=wind[0],wind_d=-wind[2],air_density=float(air_density or 1.0))
        for a,b in [('pn','next_pn'),('pe','next_pe'),('pd','next_pd'),('vn','next_vn'),('ve','next_ve'),('vd','next_vd'),('qw','next_qw'),('qx','next_qx'),('qy','next_qy'),('qz','next_qz'),('p_rate','next_p_rate'),('q_rate','next_q_rate'),('r_rate','next_r_rate')]:sh[a]=float(six[b])
        if passive_boundary:
            # Keep the diagnostic 6-DoF representation on the same passive-ground
            # contact semantics as the realized/counterfactual states.  Position is
            # preserved, while all velocities/rates are zero and the Phoenix remains
            # in its canonical tailsitter ground attitude.  Mirror the normalized
            # values into the returned op27 packet so reporting cannot describe a
            # moving touchdown while boundary evidence says grounded.
            _c=math.sqrt(0.5)
            sh.update({'vn':0.0,'ve':0.0,'vd':0.0,'qw':_c,'qx':0.0,'qy':_c,'qz':0.0,'p_rate':0.0,'q_rate':0.0,'r_rate':0.0})
            six.update({'next_vn':0.0,'next_ve':0.0,'next_vd':0.0,'next_qw':_c,'next_qx':0.0,'next_qy':_c,'next_qz':0.0,'next_p_rate':0.0,'next_q_rate':0.0,'next_r_rate':0.0})
        rotation_released=False
        if not passive_boundary:
            # op07 supplied the authoritative realized translation for this HOLD
            # path, but it has no rotational dynamics.  Never make an older op27
            # rotation look like a same-boundary observation.  If Layer5 feedback
            # was accepted, the driver overwrites this with its fresh physical state.
            rotation_released=self._release_unmodeled_rotation()
        cfdisp=math.sqrt((self.state['x']-self.cf['x'])**2+(self.state['y']-self.cf['y'])**2+(self.state['h']-self.cf['h'])**2)
        return {'command_air_enu_m_s':cmd,'op07':o,'counterfactual_op07':ocf,'counterfactual_state':dict(self.cf),'wind_counterfactual_displacement_m':cfdisp,'simulation_reference_actuators':act,'motor_op28':motor,'envelope_op30':env,'power_op31':power,'shadow_6dof_op27':six,'realized_state':dict(self.state),'op07_rotation_authority_released':rotation_released,'agl_m':max(0.0,self.state['h']-ground)}

def _lonlat_from_projected(x,y,route):
    try:
        import numpy as np
        # Stable local affine fit from the supplied route; exact enough for the small mission region.
        X=np.column_stack([route['x_m'].astype(float),route['y_m'].astype(float),np.ones(len(route))])
        a=np.linalg.lstsq(X,route['lon_deg'].astype(float),rcond=None)[0]; b=np.linalg.lstsq(X,route['lat_deg'].astype(float),rcond=None)[0]
        return float(a[0]*x+a[1]*y+a[2]),float(b[0]*x+b[1]*y+b[2])
    except Exception:return float(route.iloc[0]['lon_deg']),float(route.iloc[0]['lat_deg'])

def update_request_from_realized(req,state,target_ref,route,dest_x,dest_y,forcing_start,forcing_end,remaining_wh,phase,cycle_index,ground_m,*,effective_climate_epoch=None):
    origin_x=float(route.iloc[0]['x_m']); origin_y=float(route.iloc[0]['y_m']); x=state['x']-origin_x; y=state['y']-origin_y; h=state['h']; lon,lat=_lonlat_from_projected(state['x'],state['y'],route)
    _set_env(req,['wing_inputs','state_x'],x,'m','ENU','state_x'); _set_env(req,['wing_inputs','state_y'],y,'m','ENU','state_y'); _set_env(req,['wing_inputs','state_h'],h,'m','ENU','state_h')
    _set_env(req,['wing_inputs','air_velocity_x'],state.get('air_vx',0.0),'m/s','ENU','air_velocity_x'); _set_env(req,['wing_inputs','air_velocity_y'],state.get('air_vy',0.0),'m/s','ENU','air_velocity_y'); _set_env(req,['wing_inputs','air_velocity_h'],state.get('air_vh',0.0),'m/s','ENU','air_velocity_h')
    _set_env(req,['context','target_enu_m'],[float(target_ref['x_m']-origin_x),float(target_ref['y_m']-origin_y),float(target_ref['z_msl_m'])],'m','ENU','target_position')
    _set_env(req,['context','target_projected_enu_m'],[float(target_ref['x_m']),float(target_ref['y_m']),float(target_ref['z_msl_m'])],'m','PROJECTED_ENU','target_position_absolute')
    _set_env(req,['climate','latitude'],lat,'deg','WGS84','latitude'); _set_env(req,['climate','longitude'],lon,'deg','WGS84','longitude'); _set_env(req,['climate','elevation_ground_level'],max(0.0,h-ground_m),'m','AGL','elevation_ground_level')
    t=None; time_alignment=None
    if forcing_start:
        t,time_alignment=_climate_time_for_elapsed((forcing_start,forcing_end),float(cycle_index),return_metadata=True,effective_epoch_override=effective_climate_epoch)
    if t:
        _set_env(req,['climate','time'],t.isoformat(),'ISO8601','SCALAR','time'); req['climate']['time']['time_reference']='UTC'
        ctx=req.setdefault('context',{}); ctx['climate_time_alignment_basis']=time_alignment['basis']; ctx['offline_climate_time_alignment']=time_alignment
    req['stage']=phase; req['reference_time_s']=float(cycle_index)+0.001; req.setdefault('context',{})['mission_progress']=float(target_ref.get('mission_progress',0.0)); req['context']['remaining_energy_wh']=float(remaining_wh); req['context']['terrain_clearance_m']=max(0.0,h-ground_m); req['context']['reference_state_source']='REALIZED_WINGMPET'; req['context']['sarg_reference_only']=True; req['context']['teacher_forced_reference_replay']=False

def flatten_action(action):
    if not isinstance(action,dict):return {'l5_control_present':False}
    fm=action.get('first_move'); vals=list(fm) if isinstance(fm,(list,tuple)) else []
    out={'l5_control_present':bool(vals),'l5_control_mode':action.get('mode'),'l5_actuator_targets':','.join(map(str,action.get('actuator_targets') or []))}
    for i,v in enumerate(vals[:4]):out[f'l5_pilot_cmd_{i}']=v
    phys=action.get('physical_actuator_command') or action.get('physical_actuators')
    if isinstance(phys,dict):
        for k,v in phys.items():
            if isinstance(v,(int,float)):out[f'physical_{k}']=v
    return out


def enrich_mission_dataframe(df):
    """Add map obstacle context and route-change diagnostics without inventing causality."""
    import pandas as pd
    if df is None or df.empty:
        return df
    df=df.copy()
    # Reference turn angle: geometry only; it does not claim why the route turned.
    turns=[float('nan')]*len(df)
    for i in range(1,len(df)-1):
        try:
            ax=float(df.iloc[i]['reference_x_m'])-float(df.iloc[i-1]['reference_x_m'])
            ay=float(df.iloc[i]['reference_y_m'])-float(df.iloc[i-1]['reference_y_m'])
            bx=float(df.iloc[i+1]['reference_x_m'])-float(df.iloc[i]['reference_x_m'])
            by=float(df.iloc[i+1]['reference_y_m'])-float(df.iloc[i]['reference_y_m'])
            na=math.hypot(ax,ay); nb=math.hypot(bx,by)
            if na>0 and nb>0:
                c=max(-1.0,min(1.0,(ax*bx+ay*by)/(na*nb)))
                cross=ax*by-ay*bx
                turns[i]=math.copysign(math.degrees(math.acos(c)),cross if cross else 1.0)
        except Exception:
            pass
    df['route_turn_angle_deg']=turns
    # How far the Layer-4 next-step forecast is from the next reference sample.
    if {'l4_pred_x_m','l4_pred_y_m','reference_x_m','reference_y_m'} <= set(df.columns):
        nx=df['reference_x_m'].shift(-1); ny=df['reference_y_m'].shift(-1)
        df['l4_next_reference_deviation_m']=((df['l4_pred_x_m']-nx)**2+(df['l4_pred_y_m']-ny)**2)**0.5
    # Sample the authoritative external terrain/obstacle raster. No raster is copied into TDOC.
    map_root=envpath('TDDT_MAP_ROOT')
    raster=(map_root/'terrain_obstacles.tif') if map_root else None
    if raster and raster.is_file():
        try:
            import rasterio
            xcol='realized_x_m' if 'realized_x_m' in df else 'reference_x_m'; ycol='realized_y_m' if 'realized_y_m' in df else 'reference_y_m'; coords=list(zip(df[xcol].astype(float),df[ycol].astype(float)))
            with rasterio.open(raster) as ds:
                vals=list(ds.sample(coords))
            fields=['map_terrain_elevation_m','road_class','building_mask','building_height_m','natural_obstacle_class','artificial_obstacle_class']
            # bands 1,4,5,6,7,8 from documented raster schema
            indices=[0,3,4,5,6,7]
            for name,j in zip(fields,indices):
                df[name]=[float(v[j]) if len(v)>j and math.isfinite(float(v[j])) else float('nan') for v in vals]
            df['vertical_clearance_over_building_m']=df['reference_z_msl_m']-(df['map_terrain_elevation_m']+df['building_height_m'].fillna(0.0))
            df['obstacle_context_present']=((df['building_mask'].fillna(0)>0)|(df['natural_obstacle_class'].fillna(0)>0)|(df['artificial_obstacle_class'].fillna(0)>0))
            df['turn_near_obstacle_context']=df['obstacle_context_present'] & (df['route_turn_angle_deg'].abs().fillna(0)>=5.0)
            df['obstacle_context_source']='external TDDT_MAP_ROOT/terrain_obstacles.tif; correlation/context only, not causal attribution'
        except Exception as exc:
            diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
            df['obstacle_context_error']=repr(exc)
    return df

def _flight_hms(seconds:float)->str:
    seconds=max(0,int(round(float(seconds))))
    h,rem=divmod(seconds,3600); m,sec=divmod(rem,60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _route_distance_m(route_df)->float:
    total=0.0
    for i in range(1,len(route_df)):
        try:
            dx=float(route_df.iloc[i]['x_m'])-float(route_df.iloc[i-1]['x_m'])
            dy=float(route_df.iloc[i]['y_m'])-float(route_df.iloc[i-1]['y_m'])
            z1=float(route_df.iloc[i].get('z_msl_m',route_df.iloc[i].get('h_m',0.0)))
            z0=float(route_df.iloc[i-1].get('z_msl_m',route_df.iloc[i-1].get('h_m',0.0)))
            total+=math.sqrt(dx*dx+dy*dy+(z1-z0)*(z1-z0))
        except Exception: pass
    return total


def _terrain_surface_for_route(df,grid_n=90):
    if df is None or df.empty:return None
    map_root=envpath('TDDT_MAP_ROOT'); raster=(map_root/'terrain_obstacles.tif') if map_root else None
    if not raster or not raster.is_file():return None
    try:
        import numpy as np, rasterio
        from rasterio.windows import from_bounds
        xmin,xmax=float(df['reference_x_m'].min()),float(df['reference_x_m'].max())
        ymin,ymax=float(df['reference_y_m'].min()),float(df['reference_y_m'].max())
        padx=max(25.0,(xmax-xmin)*0.05); pady=max(25.0,(ymax-ymin)*0.05)
        with rasterio.open(raster) as ds:
            win=from_bounds(xmin-padx,ymin-pady,xmax+padx,ymax+pady,ds.transform).round_offsets().round_lengths()
            win=win.intersection(rasterio.windows.Window(0,0,ds.width,ds.height))
            z=ds.read(1,window=win,out_shape=(int(grid_n),int(grid_n)),resampling=rasterio.enums.Resampling.bilinear).astype(float)
            if ds.nodata is not None:z[np.isclose(z,float(ds.nodata))]=np.nan
            left,bottom,right,top=rasterio.windows.bounds(win,ds.transform)
            x=np.linspace(left,right,int(grid_n)); y=np.linspace(top,bottom,int(grid_n))
            xx,yy=np.meshgrid(x,y)
        return {'x':xx,'y':yy,'z':z,'source':str(raster)}
    except Exception as exc:return {'error':repr(exc),'source':str(raster)}


def _scalar_pairs(obj,prefix='',limit=160):
    out=[]
    def walk(v,p):
        if len(out)>=limit:return
        if isinstance(v,dict):
            for k,x in v.items():walk(x,f'{p}.{k}' if p else str(k))
        elif isinstance(v,(list,tuple)):
            if len(v)<=8 and all(isinstance(x,(int,float,str,bool)) or x is None for x in v):out.append((p,v))
            else:
                for i,x in enumerate(v):walk(x,f'{p}[{i}]')
        elif isinstance(v,(int,float,str,bool)) or v is None:out.append((p,v))
    walk(obj,prefix); return out


DETAIL_SECTIONS=('time_location','mission','uav_navigation','flight_state','sensors','actuators','climate','layer3_return','layer4_return','layer5_decision','tdoc_layer_status')


def _detail_scalar_items(obj,prefix=''):
    """Flatten a report detail object to scalar paths; lists are indexed, not collapsed."""
    out=[]
    def walk(v,p):
        if isinstance(v,dict):
            for k,x in v.items(): walk(x,f'{p}.{k}' if p else str(k))
        elif isinstance(v,(list,tuple)):
            for i,x in enumerate(v): walk(x,f'{p}[{i}]')
        elif isinstance(v,(int,float,str,bool)) or v is None:
            out.append((p,v))
    walk(obj,prefix)
    return out


def _finite_numeric(v):
    if isinstance(v,bool): return float(int(v))
    if isinstance(v,(int,float)):
        try:
            x=float(v); return x if math.isfinite(x) else None
        except Exception: return None
    return None


def _nondefault_for_hover(v):
    if v is None or v is False:return False
    if isinstance(v,(int,float)) and not isinstance(v,bool):
        try:return math.isfinite(float(v)) and abs(float(v))>1e-15
        except Exception:return False
    if isinstance(v,str):return bool(v.strip())
    return True


def _decision_hover(detail:dict)->str:
    # Keep hover concise and informative. Complete scalar details for every cycle are
    # available in the embedded Cycle Detail Explorer and the gzip sidecar.
    lines=[]
    for section in DETAIL_SECTIONS:
        data=detail.get(section) or {}
        if not data:continue
        shown=[]
        for k,v in _detail_scalar_items(data):
            # Dense covariance matrices and default zero/false/null values used to fill
            # the hover before useful mission values became visible.
            if '.covariance.matrix[' in k or not _nondefault_for_hover(v):continue
            shown.append((k,v))
            if len(shown)>=18:break
        if not shown:continue
        lines.append(f'<b>{html.escape(section.replace("_"," ").title())}</b>')
        for k,v in shown:lines.append(f'{html.escape(str(k))}: {html.escape(str(v))}')
    lines.append('<i>Use the Cycle Detail Explorer below the 3-D graph for complete per-cycle fields; zero/false/null fields are hidden there by default but can be enabled.</i>')
    return '<br>'.join(lines)


def _detail_rows(decision_details:list[dict],elapsed:list[float]):
    rows=[]
    for i,detail in enumerate(decision_details):
        row={'cycle':detail.get('cycle',i+1),'flight_elapsed_s':elapsed[i] if i<len(elapsed) else float(i)}
        for section in DETAIL_SECTIONS:
            for k,v in _detail_scalar_items(detail.get(section) or {},section):
                row[k]=v
        rows.append(row)
    return rows


def _detail_numeric_series(decision_details:list[dict],section:str):
    paths={}
    n=len(decision_details)
    for i,detail in enumerate(decision_details):
        for k,v in _detail_scalar_items(detail.get(section) or {},section):
            paths.setdefault(k,[None]*n)[i]=_finite_numeric(v)
    # A constant zero/false channel carries no 2-D shape and is intentionally kept in
    # the detail table/explorer instead of consuming a graph trace.
    out={}
    for k,vals in paths.items():
        finite=[v for v in vals if v is not None]
        if not finite:continue
        if all(abs(v)<=1e-15 for v in finite):continue
        out[k]=vals
    return out


def _detail_section_figure(go,decision_details,section,elapsed,duration):
    fig=go.Figure(); series=_detail_numeric_series(decision_details,section)
    for name,vals in series.items():
        fig.add_trace(go.Scatter(x=elapsed,y=vals,mode='lines',name=name,connectgaps=False))
    if not series:
        fig.add_annotation(text='No non-zero numeric time-series in this section; categorical/zero/null fields remain available in the Cycle Detail Explorer.',xref='paper',yref='paper',x=.5,y=.5,showarrow=False)
    fig.update_layout(title=f'3-D detail fields in 2-D — {section}',height=max(480,min(1150,360+16*len(series))),legend={'orientation':'h'})
    fig.update_xaxes(title_text='Flight elapsed time [s]',range=[0,max(float(duration or 0),1.0)])
    return fig


def _detail_explorer_html(decision_details:list[dict])->str:
    def safe(v):
        if isinstance(v,dict):return {str(k):safe(x) for k,x in v.items()}
        if isinstance(v,(list,tuple)):return [safe(x) for x in v]
        if isinstance(v,float) and not math.isfinite(v):return None
        if isinstance(v,(str,int,float,bool)) or v is None:return v
        return str(v)
    compact=[]
    for d in decision_details:
        compact.append({'cycle':d.get('cycle'),'transaction_id':d.get('transaction_id'),**{s:safe(d.get(s) or {}) for s in DETAIL_SECTIONS}})
    payload=json.dumps(compact,ensure_ascii=False,separators=(',',':')).replace('</','<\\/')
    return f'''<section id="tdoc-cycle-detail-explorer" style="margin:24px 12px;font-family:Arial,sans-serif">
<h2>Cycle Detail Explorer</h2>
<p>All scalar fields that back the 3-D decision hover are available per cycle. Default-zero/false/null values are hidden to avoid the previous wall of zeros; enable the switches to inspect them explicitly. The complete raw payload remains in <code>DECISION_POINT_DETAILS.json.gz</code>.</p>
<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
<label>Cycle <select id="tdoc-detail-cycle"></select></label>
<label>Search <input id="tdoc-detail-search" type="search" placeholder="field or value"></label>
<label><input id="tdoc-detail-zero" type="checkbox"> show zero / false</label>
<label><input id="tdoc-detail-null" type="checkbox"> show null / empty</label>
<span id="tdoc-detail-count"></span>
</div>
<div style="max-height:720px;overflow:auto;margin-top:12px;border:1px solid #aaa">
<table style="border-collapse:collapse;width:100%;font-size:12px"><thead><tr><th style="position:sticky;top:0;background:#fff;text-align:left;border-bottom:1px solid #aaa">Field</th><th style="position:sticky;top:0;background:#fff;text-align:left;border-bottom:1px solid #aaa">Value</th></tr></thead><tbody id="tdoc-detail-body"></tbody></table>
</div>
<script>
const TDOC_DETAILS={payload};
const sel=document.getElementById('tdoc-detail-cycle'), search=document.getElementById('tdoc-detail-search'), showZero=document.getElementById('tdoc-detail-zero'), showNull=document.getElementById('tdoc-detail-null'), body=document.getElementById('tdoc-detail-body'), count=document.getElementById('tdoc-detail-count');
TDOC_DETAILS.forEach((d,i)=>{{const o=document.createElement('option');o.value=i;o.textContent=`${{d.cycle ?? i+1}} — ${{d.transaction_id ?? ''}}`;sel.appendChild(o);}});
function flat(v,p,out){{if(v!==null && typeof v==='object'){{if(Array.isArray(v))v.forEach((x,i)=>flat(x,`${{p}}[${{i}}]`,out));else Object.entries(v).forEach(([k,x])=>flat(x,p?`${{p}}.${{k}}`:k,out));}}else out.push([p,v]);}}
function render(){{const d=TDOC_DETAILS[Number(sel.value)||0]||{{}}, all=[];flat(d,'',all);const q=search.value.trim().toLowerCase();let shown=0;body.innerHTML='';for(const [k,v] of all){{const isNull=(v===null||v===undefined||v===''), isZero=(v===false||v===0);if(isNull&&!showNull.checked)continue;if(isZero&&!showZero.checked)continue;const txt=String(v);if(q && !(k.toLowerCase().includes(q)||txt.toLowerCase().includes(q)))continue;const tr=document.createElement('tr'), a=document.createElement('td'), b=document.createElement('td');a.textContent=k;b.textContent=txt;a.style.cssText=b.style.cssText='vertical-align:top;border-bottom:1px solid #ddd;padding:4px 6px;white-space:pre-wrap;word-break:break-word';tr.append(a,b);body.appendChild(tr);shown++;}}count.textContent=`${{shown}} / ${{all.length}} scalar fields shown`;}}
[sel,search,showZero,showNull].forEach(x=>x.addEventListener(x===search?'input':'change',render));render();
</script></section>'''


def _write_report_with_explorer(path:Path,title:str,figures:list,decision_details:list[dict]):
    parts=[f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head><body>',f'<h1 style="font-family:Arial,sans-serif;margin:18px">{html.escape(title)}</h1>']
    for i,fig in enumerate(figures):
        parts.append(fig.to_html(full_html=False,include_plotlyjs='cdn' if i==0 else False))
    parts.append(_detail_explorer_html(decision_details)); parts.append('</body></html>')
    path.write_text(''.join(parts),encoding='utf-8')

def _compact_json(obj,limit=900):
    try:text=json.dumps(obj,ensure_ascii=False,allow_nan=False,separators=(',',':'))
    except Exception:text=str(obj)
    return text if len(text)<=limit else text[:limit-1]+'…'


def _return_behavior(layer:int,detail:dict)->tuple[str,str]:
    """Classify returned payload behavior without relabelling expected fail-closed states as transport errors."""
    if layer==2:
        sensors=detail.get('sensors') or {}; warnings_count=0; invalid=[]
        for name,p in sensors.items():
            prov=(p or {}).get('provenance') or {}; wf=prov.get('tdoc_external_warning_filter') or {}
            try:warnings_count+=int(wf.get('count') or 0)
            except Exception:pass
            validity=(p or {}).get('validity') or {}
            if isinstance(validity,dict):
                if validity.get('valid') is False:invalid.append(str(name))
                try:
                    if validity.get('score') is not None and float(validity.get('score'))<=0:invalid.append(str(name))
                except Exception:pass
        if invalid:return 'INVALID_PACKET',f"invalid={sorted(set(invalid))}; filtered_warnings={warnings_count}"
        if warnings_count:return 'WARNING_FILTERED',f"known external warning(s) filtered={warnings_count}; packet validity remains authoritative"
        return 'NORMAL',f"packets={','.join(sorted(sensors)) or 'none'}"
    if layer==3:
        d=detail.get('layer3_return') or {}; h=d.get('tdoc_health')
        degraded=False
        if isinstance(h,dict):degraded=(h.get('ok') is False or str(h.get('mode','')).upper() in {'DEGRADED','FAILED'})
        if d.get('stability') is False or degraded:return 'DEGRADED',_compact_json(d)
        return 'NORMAL',_compact_json(d)
    if layer==4:
        d=detail.get('layer4_return') or {}; n=int(d.get('candidate_count') or 0); f=int(d.get('feasible_count') or 0); v=int(d.get('verified_count') or 0)
        if n and f==0:return 'NO_FEASIBLE',f"candidates={n}; feasible=0; verified={v}"
        if f and v==0:return 'FAIL_CLOSED_UNVERIFIED',f"candidates={n}; feasible={f}; verified=0; advisory may exist but physical actuation remains prohibited"
        return 'NORMAL',f"candidates={n}; feasible={f}; verified={v}"
    if layer==5:
        d=detail.get('layer5_decision') or {}; dec=d.get('decision') or {}; approval=dec.get('approval') or {}
        final_gate=approval.get('final_gate') if isinstance(approval,dict) else None
        fallback=d.get('fallback'); mode=d.get('operational_mode'); status=d.get('status')
        if status in {'FAILED','DEGRADED'}:return str(status),_compact_json(d)
        if final_gate is False or fallback:return 'HOLD_FAIL_CLOSED',f"mode={mode}; final_gate={final_gate}; fallback={_compact_json(fallback,300)}"
        return 'NORMAL',f"mode={mode}; final_gate={final_gate}"
    return 'NORMAL',''


def _external_return_pulses(runtime_root:Path,df,decision_details:list[dict]):
    txmeta={}
    if not df.empty and 'transaction_id' in df:
        for _,r in df.iterrows():
            tx=str(r.get('transaction_id')); txmeta[tx]={'cycle':int(r.get('cycle')) if r.get('cycle') is not None else None,'flight_elapsed_s':_num(r.get('flight_elapsed_s')),'phase':r.get('phase'),'tdoc_substate':r.get('tdoc_substate')}
    detail_by_tx={str(d.get('transaction_id')):d for d in decision_details if d.get('transaction_id')}
    root=Path(runtime_root)/'events'/'invocations'; paths=sorted((root/'chunks').glob('chunk_*.jsonl.gz')) if (root/'chunks').is_dir() else []
    legacy=Path(runtime_root)/'events'/'invocations.jsonl'
    if not paths and legacy.is_file():paths=[legacy]
    pulses=[]
    for p in paths:
        try:records=iter_jsonl_auto(p)
        except Exception:continue
        for e in records:
            src=str(e.get('source') or ''); dest=str(e.get('destination') or '')
            if dest!='TDOC' or src not in {'Layer2','Layer3','Layer4','Layer5'}:continue
            if str(e.get('event_type') or '') not in {'Route','Alarm'}:continue
            layer=int(src.replace('Layer','')); tx=str(e.get('transaction_id') or '')
            if tx not in txmeta:continue
            meta=txmeta.get(tx,{})
            behavior,behavior_detail=_return_behavior(layer,detail_by_tx.get(tx,{}) or {})
            transport=str(e.get('status') or 'UNKNOWN'); combined=transport if transport not in {'OK','STARTED'} else behavior
            det=e.get('details') or {}; elapsed_call=_num(det.get('elapsed_s'))
            pulses.append({'cycle':meta.get('cycle'),'flight_elapsed_s':meta.get('flight_elapsed_s'),'phase':meta.get('phase'),'tdoc_substate':meta.get('tdoc_substate'),'transaction_id':tx,'layer':layer,'source':src,'destination':'TDOC','event_type':e.get('event_type'),'transport_status':transport,'behavior':behavior,'pulse_status':combined,'library_elapsed_s':elapsed_call,'deadline_s':det.get('deadline_s'),'deadline_ok':det.get('deadline_ok'),'reason':e.get('reason'),'event_time':e.get('event_time'),'error':det.get('error'),'behavior_detail':behavior_detail})
    pulses=sorted(pulses,key=lambda r:((r.get('cycle') or 10**9),str(r.get('event_time') or ''),r.get('layer') or 99))
    seq_by_cycle={}; occurrence={}
    for p in pulses:
        cyc=p.get('cycle'); seq_by_cycle[cyc]=seq_by_cycle.get(cyc,0)+1; p['sequence_in_cycle']=seq_by_cycle[cyc]
        key=(cyc,p.get('layer')); occurrence[key]=occurrence.get(key,0)+1; p['source_occurrence_in_cycle']=occurrence[key]
        p['cycle_plot_x']=(float(cyc) if cyc is not None else math.nan)+0.06*(p['source_occurrence_in_cycle']-1)
    return pulses


def _external_return_pulse_panel(go,pulses:list[dict],duration:float):
    from plotly.subplots import make_subplots
    anomalies=[p for p in pulses if p.get('pulse_status') not in {'NORMAL','OK'}]
    show=anomalies[:250]
    fig=make_subplots(rows=2,cols=1,specs=[[{'type':'xy'}],[{'type':'table'}]],row_heights=[0.62,0.38],vertical_spacing=0.08,subplot_titles=['External Layer → TDOC return-pulse raster','Returned behaviors / errors (first 250 non-normal pulses; complete list in CSV/JSON.GZ)'])
    for layer in (2,3,4,5):
        q=[p for p in pulses if p.get('layer')==layer]
        xs=[p.get('cycle_plot_x') if p.get('cycle_plot_x') is not None else p.get('cycle') for p in q]
        text=[f"cycle={p.get('cycle')}<br>phase={p.get('phase')}<br>substate={p.get('tdoc_substate') or '-'}<br>{p.get('source')} → TDOC<br>transport={p.get('transport_status')}<br>behavior={p.get('behavior')}<br>call_elapsed_s={p.get('library_elapsed_s')}<br>reason={p.get('reason')}<br>{html.escape(str(p.get('behavior_detail') or ''))}" for p in q]
        fig.add_trace(go.Scatter(x=xs,y=[layer]*len(q),mode='markers',name=f'Layer {layer} → TDOC',text=text,hovertemplate='%{text}<extra></extra>'),row=1,col=1)
    fig.update_yaxes(tickvals=[2,3,4,5],ticktext=['Layer 2','Layer 3','Layer 4','Layer 5'],range=[1.5,5.5],title_text='External source lane',row=1,col=1)
    fig.update_xaxes(title_text='Mission cycle (duplicate returns from the same layer are offset by +0.06 for visibility)',range=[0,max([float(p.get('cycle') or 0) for p in pulses]+[1.0])+1.0],row=1,col=1)
    headers=['Cycle','Elapsed [s]','Layer','Transport','Behavior','Call [s]','Phase','Substate','Detail']
    values=[[p.get('cycle') for p in show],[p.get('flight_elapsed_s') for p in show],[p.get('layer') for p in show],[p.get('transport_status') for p in show],[p.get('behavior') for p in show],[p.get('library_elapsed_s') for p in show],[p.get('phase') for p in show],[p.get('tdoc_substate') for p in show],[p.get('behavior_detail') or p.get('error') or p.get('reason') for p in show]]
    fig.add_trace(go.Table(header={'values':headers},cells={'values':values}),row=2,col=1)
    fig.update_layout(height=1150,title=f'External Library Return Pulse / Error-Behavior Panel — pulses={len(pulses)}, non-normal={len(anomalies)}',legend={'orientation':'h'})
    return fig


def sarg_crp_treemap(outdir:Path):
    import pandas as pd
    import plotly.graph_objects as go
    sarg=envpath('TDDT_SARG_ROOT'); crp=envpath('TDDT_CRP_ROOT')
    src=(sarg/'sarg_records.csv') if sarg else None
    if not src or not src.is_file():return {'generated':False,'reason':'sarg_records_missing'}
    d=pd.read_csv(src)
    needed=[c for c in ['regime_id','scenario_id','crp_id'] if c in d.columns]
    if 'crp_id' not in needed:return {'generated':False,'reason':'crp_id_missing_from_sarg'}
    group=d.groupby(needed,dropna=False).size().reset_index(name='records')
    catalog=None
    cp=(crp/'crp_cluster_catalog.csv') if crp else None
    if cp and cp.is_file():
        try:
            catalog=pd.read_csv(cp); key=next((c for c in ['crp_id','cluster_id','id'] if c in catalog.columns),None)
            if key:
                catalog=catalog.rename(columns={key:'crp_id'})
                keep=['crp_id']+[c for c in catalog.columns if c!='crp_id'][:10]
                group=group.merge(catalog[keep],on='crp_id',how='left')
        except Exception: catalog=None
    group.to_csv(outdir/'SARG_CRP_RELATION.csv',index=False)
    labels=['SARG']; parents=['']; values=[int(group['records'].sum())]; custom=['All SARG records linked to CRP clusters']
    regime_col='regime_id' if 'regime_id' in group else None; scenario_col='scenario_id' if 'scenario_id' in group else None
    if regime_col:
        for regime,rg in group.groupby(regime_col):
            rid=f'Regime {regime}'; labels.append(rid); parents.append('SARG'); values.append(int(rg['records'].sum())); custom.append(f'regime_id={regime}')
            if scenario_col:
                for scenario,sg in rg.groupby(scenario_col):
                    sid=f'R{regime} / Scenario {scenario}'; labels.append(sid); parents.append(rid); values.append(int(sg['records'].sum())); custom.append(f'scenario_id={scenario}')
                    for _,r in sg.iterrows():
                        cid=f'R{regime}/S{scenario}/CRP {r.crp_id}'; labels.append(cid); parents.append(sid); values.append(int(r.records)); custom.append(' | '.join(f'{k}={r[k]}' for k in group.columns if k not in needed+['records'] and str(r.get(k))!='nan')[:1500])
            else:
                for _,r in rg.iterrows():
                    cid=f'R{regime}/CRP {r.crp_id}'; labels.append(cid); parents.append(rid); values.append(int(r.records)); custom.append('')
    else:
        for _,r in group.iterrows():
            cid=f'CRP {r.crp_id}'; labels.append(cid); parents.append('SARG'); values.append(int(r.records)); custom.append('')
    fig=go.Figure(go.Treemap(labels=labels,parents=parents,values=values,customdata=custom,branchvalues='total',hovertemplate='<b>%{label}</b><br>records=%{value}<br>%{customdata}<extra></extra>'))
    fig.update_layout(title='SARG ↔ CRP Relationship Treemap (SARG records grouped by referenced CRP)')
    fig.write_html(outdir/'SARG_CRP_TREEMAP.html',include_plotlyjs='cdn')
    return {'generated':True,'sarg_records':int(len(d)),'relations':int(len(group)),'crp_catalog_used':bool(catalog is not None),'source':'$TDDT_SARG_ROOT/sarg_records.csv','crp_catalog':'$TDDT_CRP_ROOT/crp_cluster_catalog.csv' if cp and cp.is_file() else None}


def layer4_optimization_reports(outdir:Path,cdf):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    if cdf is None or cdf.empty:return {'generated':False,'reason':'no_layer4_candidates'}
    fig=make_subplots(rows=2,cols=2,subplot_titles=['Time vs Energy objective','Mission vs Risk objective','Candidate scalar cost by Fast tick','Feasible / verified candidates by Fast tick'])
    fig.add_trace(go.Scatter(x=cdf.get('time'),y=cdf.get('energy'),mode='markers',text=cdf.get('candidate_id'),name='candidate'),row=1,col=1)
    fig.add_trace(go.Scatter(x=cdf.get('mission'),y=cdf.get('risk'),mode='markers',text=cdf.get('candidate_id'),name='mission/risk'),row=1,col=2)
    fig.add_trace(go.Scatter(x=cdf.get('cycle'),y=cdf.get('scalar_cost'),mode='markers',text=cdf.get('candidate_id'),name='scalar cost'),row=2,col=1)
    by=cdf.groupby('cycle').agg(feasible=('feasible','sum'),verified=('verified','sum'),candidates=('candidate_id','count')).reset_index()
    for c in ['feasible','verified','candidates']:fig.add_trace(go.Scatter(x=by['cycle'],y=by[c],mode='lines+markers',name=c),row=2,col=2)
    fig.update_xaxes(title_text='Layer4 time objective',row=1,col=1); fig.update_yaxes(title_text='Layer4 energy objective',row=1,col=1)
    fig.update_xaxes(title_text='Layer4 mission objective',row=1,col=2); fig.update_yaxes(title_text='Layer4 risk objective',row=1,col=2)
    fig.update_xaxes(title_text='Fast tick / route decision point',row=2,col=1); fig.update_yaxes(title_text='Scalar cost',row=2,col=1)
    fig.update_xaxes(title_text='Fast tick / route decision point',row=2,col=2); fig.update_yaxes(title_text='Candidate count',row=2,col=2)
    fig.update_layout(height=1100,title='Layer 4 Optimization Across Mission Decision Points')
    fig.write_html(outdir/'LAYER4_OPTIMIZATION_2D.html',include_plotlyjs='cdn')
    # The former 3-D scatter became unreadable with hundreds of candidates.  Keep the
    # filename for backward links, but replace the content with a decision timeline
    # that summarizes each Fast tick in directly comparable 2-D panels.
    timeline=make_subplots(rows=4,cols=1,shared_xaxes=True,vertical_spacing=0.055,
        subplot_titles=['Best feasible scalar cost per decision point','Candidate availability','Selected feasible objective values','Feasibility ratio per decision point'])
    work=cdf.copy(); work['feasible_bool']=work['feasible'].fillna(False).astype(bool)
    feasible_rows=work[work['feasible_bool']].copy()
    if not feasible_rows.empty:
        idx=feasible_rows.groupby('cycle')['scalar_cost'].idxmin(); best=feasible_rows.loc[idx].sort_values('cycle')
        timeline.add_trace(go.Scatter(x=best['cycle'],y=best['scalar_cost'],mode='lines+markers',name='best feasible scalar cost',
                         customdata=[[r.get('candidate_id'),r.get('scenario_id'),r.get('pareto_rank'),r.get('verified')] for _,r in best.iterrows()],
                         hovertemplate='cycle=%{x}<br>cost=%{y}<br>candidate=%{customdata[0]}<br>scenario=%{customdata[1]}<br>pareto=%{customdata[2]}<br>verified=%{customdata[3]}<extra></extra>'),row=1,col=1)
        for c in ['time','energy','risk','mission']:
            if c in best:timeline.add_trace(go.Scatter(x=best['cycle'],y=best[c],mode='lines+markers',name=f'selected {c}'),row=3,col=1)
    by=work.groupby('cycle').agg(candidates=('candidate_id','count'),feasible=('feasible_bool','sum'),verified=('verified','sum')).reset_index(); by['feasible_ratio']=by['feasible']/by['candidates'].clip(lower=1)
    for c in ['candidates','feasible','verified']:timeline.add_trace(go.Scatter(x=by['cycle'],y=by[c],mode='lines+markers',name=c),row=2,col=1)
    timeline.add_trace(go.Scatter(x=by['cycle'],y=by['feasible_ratio'],mode='lines+markers',name='feasible ratio'),row=4,col=1)
    timeline.update_yaxes(title_text='scalar cost',row=1,col=1); timeline.update_yaxes(title_text='count',row=2,col=1); timeline.update_yaxes(title_text='objective value',row=3,col=1); timeline.update_yaxes(title_text='ratio',range=[0,1.05],row=4,col=1)
    timeline.update_xaxes(title_text='Fast tick / route decision point',row=4,col=1)
    timeline.update_layout(height=1250,title='Layer 4 Optimization Decision Timeline — 2-D replacement for dense 3-D objective scatter',hovermode='x unified',legend={'orientation':'h'})
    timeline.write_html(outdir/'LAYER4_OPTIMIZATION_TIMELINE.html',include_plotlyjs='cdn')
    timeline.write_html(outdir/'LAYER4_OPTIMIZATION_3D.html',include_plotlyjs='cdn')
    summary={'generated':True,'candidate_records':int(len(cdf)),'decision_points':int(cdf['cycle'].nunique()),'feasible_ratio':float(cdf['feasible'].fillna(False).astype(bool).mean()),'verified_ratio':float(cdf['verified'].fillna(False).astype(bool).mean()),'pareto_rank_1_ratio':float((cdf['pareto_rank']==1).mean()) if 'pareto_rank' in cdf else None}
    (outdir/'LAYER4_OPTIMIZATION_SUMMARY.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    return summary


def accuracy_efficiency_reports(outdir:Path,df,cdf,summary,route_df):
    import numpy as np
    import pandas as pd
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    reference_distance=_route_distance_m(route_df); duration=float(summary.get('flight_duration_s') or 0.0); energy=max(0.0,float(summary.get('initial_energy_wh',0))-float(summary.get('remaining_energy_wh',0))); route_distance=0.0
    if {'realized_x_m','realized_y_m','realized_z_msl_m'} <= set(df.columns):
        for i in range(1,len(df)):
            route_distance += math.sqrt((float(df.iloc[i].realized_x_m)-float(df.iloc[i-1].realized_x_m))**2+(float(df.iloc[i].realized_y_m)-float(df.iloc[i-1].realized_y_m))**2+(float(df.iloc[i].realized_z_msl_m)-float(df.iloc[i-1].realized_z_msl_m))**2)
    else: route_distance=reference_distance
    final_goal=float(df['goal_distance_m'].dropna().iloc[-1]) if 'goal_distance_m' in df and df['goal_distance_m'].notna().any() else None
    dev=df['l4_next_reference_deviation_m'].dropna().astype(float) if 'l4_next_reference_deviation_m' in df else np.array([])
    verified=float(cdf['verified'].fillna(False).astype(bool).mean()) if cdf is not None and not cdf.empty and 'verified' in cdf else 0.0
    feasible=float(cdf['feasible'].fillna(False).astype(bool).mean()) if cdf is not None and not cdf.empty and 'feasible' in cdf else 0.0
    final_gate=float(df['l5_final_gate'].fillna(False).astype(bool).mean()) if 'l5_final_gate' in df else 0.0
    teacher=float(df['teacher_forced'].fillna(False).astype(bool).mean()) if 'teacher_forced' in df else 0.0
    closed=float(df['layer2_control_effect_applied'].fillna(False).astype(bool).mean()) if 'layer2_control_effect_applied' in df else 0.0
    knowledge=float(df['knowledge_target_reached'].fillna(False).astype(bool).mean()) if 'knowledge_target_reached' in df else 0.0
    metrics={
        'reference_route_distance_m':reference_distance,'realized_route_distance_m':route_distance,'flight_duration_s':duration,'flight_duration_hms':_flight_hms(duration),'average_route_speed_m_s':route_distance/duration if duration>0 else None,
        'energy_used_wh':energy,'distance_per_wh_m_wh':route_distance/energy if energy>0 else None,'wh_per_km':energy/(route_distance/1000.0) if route_distance>0 else None,
        'final_goal_distance_m':final_goal,'goal_tolerance_m':summary.get('goal_tolerance_m'),'goal_reached':bool(summary.get('goal_reference_reached')),
        'endpoint_normalized_accuracy':max(0.0,1.0-final_goal/max(route_distance,1e-9)) if final_goal is not None else None,
        'layer4_next_step_rmse_m':float(math.sqrt(float((dev**2).mean()))) if len(dev) else None,'layer4_next_step_mae_m':float(dev.abs().mean()) if hasattr(dev,'abs') and len(dev) else None,
        'layer4_next_step_p95_m':float(dev.quantile(.95)) if hasattr(dev,'quantile') and len(dev) else None,'layer4_forecast_within_25m_ratio':float((dev<=25.0).mean()) if len(dev) else None,
        'layer4_candidate_feasible_ratio':feasible,'layer4_candidate_verified_ratio':verified,'layer5_final_decision_ratio':final_gate,
        'layer4_candidates_per_decision_mean':float(cdf.groupby('cycle').size().mean()) if cdf is not None and not cdf.empty else None,
        'knowledge_target_reached_ratio':knowledge,'teacher_forced_ratio':teacher,'layer5_physical_closed_loop_ratio':closed,'wingmpet_simulation_propagation_ratio':1.0-closed,
        'min_terrain_clearance_m':float(df['terrain_clearance_m'].min()) if 'terrain_clearance_m' in df else None,
        'obstacle_context_ratio':float(df['obstacle_context_present'].fillna(False).astype(bool).mean()) if 'obstacle_context_present' in df else None,
        'meso_per_fast_ratio':float(df['meso_event'].fillna(False).astype(bool).mean()) if 'meso_event' in df else None,
        'slow_per_fast_ratio':float(df['slow_event'].fillna(False).astype(bool).mean()) if 'slow_event' in df else None,
    }
    # Accuracy/confidence evidence attached to the same selected SARG scenario/CRP.
    try:
        ref=summary.get('reference_route') or {}; scenario=int(ref.get('scenario_id')); crp_id=int(ref.get('crp_id'))
        sarg=envpath('TDDT_SARG_ROOT'); rec=pd.read_csv(sarg/'sarg_records.csv')
        q=rec[(rec['scenario_id']==scenario)&(rec['crp_id']==crp_id)]
        def _valid_metric(c):
            if c not in q: return pd.Series(dtype=float)
            v=pd.to_numeric(q[c],errors='coerce').replace([np.inf,-np.inf],np.nan).dropna()
            # SARG uses large negative sentinels (e.g. -9999) for unavailable metrics.
            # Error magnitudes, confidence and observability are non-negative by definition.
            if c in {'position_error_m','velocity_error_ms','heading_error_deg','observability_score','record_confidence'}:
                v=v[v>=0]
            if c in {'observability_score','record_confidence','flight_ok','nav_ok'}:
                v=v[v<=1]
            return v
        def mean(c):
            v=_valid_metric(c); return float(v.mean()) if len(v) else None
        def p95(c):
            v=_valid_metric(c); return float(v.quantile(.95)) if len(v) else None
        metrics.update({
            'sarg_selected_scenario_id':scenario,'sarg_selected_crp_id':crp_id,'sarg_records_evaluated':int(len(q)),
            'sarg_position_error_mean_m':mean('position_error_m'),'sarg_position_error_p95_m':p95('position_error_m'),
            'sarg_velocity_error_mean_m_s':mean('velocity_error_ms'),'sarg_heading_error_mean_deg':mean('heading_error_deg'),
            'sarg_observability_score_mean':mean('observability_score'),'sarg_record_confidence_mean':mean('record_confidence'),
            'sarg_flight_ok_ratio':mean('flight_ok'),'sarg_nav_ok_ratio':mean('nav_ok')})
    except Exception as exc:
        diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
        metrics['sarg_accuracy_evidence_error']=repr(exc)
    metrics['interpretation_note']='Forecast accuracy compares Layer4 next-step forecast against the next SARG reference point. SARG accuracy/confidence metrics are simulation/training evidence for the selected scenario/CRP. The realized offline path is WingMPET-simulated with MicroMet wind; these remain simulation/training metrics, not field-control accuracy metrics.'
    (outdir/'DECISION_ACCURACY_EFFICIENCY.json').write_text(json.dumps(metrics,indent=2,ensure_ascii=False,allow_nan=False))
    names=['Endpoint accuracy','L4 forecast ≤25m','L4 feasible','L4 verified','L5 FINAL','Knowledge complete','Closed-loop','SARG flight OK','SARG nav OK']
    vals=[metrics.get('endpoint_normalized_accuracy'),metrics.get('layer4_forecast_within_25m_ratio'),feasible,verified,final_gate,knowledge,metrics.get('layer5_physical_closed_loop_ratio'),metrics.get('sarg_flight_ok_ratio'),metrics.get('sarg_nav_ok_ratio')]
    fig=make_subplots(rows=2,cols=1,specs=[[{'type':'xy'}],[{'type':'table'}]],row_heights=[0.38,0.62],vertical_spacing=0.08)
    fig.add_trace(go.Bar(x=names,y=[0 if v is None else v for v in vals],name='ratio'),row=1,col=1)
    fig.add_trace(go.Table(header={'values':['Metric','Value']},cells={'values':[list(metrics.keys()),[str(v) for v in metrics.values()]]}),row=2,col=1)
    fig.update_yaxes(title_text='Ratio [0–1]',range=[0,1.05],row=1,col=1)
    fig.update_layout(title='TDOC Decision Accuracy / Efficiency Metrics — simulation/training evidence',height=1500)
    fig.write_html(outdir/'DECISION_ACCURACY_EFFICIENCY_REPORT.html',include_plotlyjs='cdn')
    return metrics


def mission_mpc_participation(df):
    def bcount(col):
        return int(df[col].fillna(False).astype(bool).sum()) if col in df else 0
    n=int(len(df)); l4_inv=bcount('l4_invoked'); l4_mpc=bcount('l4_mpc_participated'); l5_inv=bcount('l5_invoked'); l5_mpc=bcount('l5_mpc_participated')
    def ratio(a,b): return float(a/b) if b else 0.0
    return {
        'schema':'TDOC-MPC-participation-mission-v1','cycle_count':n,
        'layer4':{
            'invoked_cycles':l4_inv,'invocation_ratio_total':ratio(l4_inv,n),
            'mpc_participation_cycles':l4_mpc,'mpc_participation_ratio_total':ratio(l4_mpc,n),'mpc_participation_ratio_when_invoked':ratio(l4_mpc,l4_inv),
            'cycles_with_feasible_candidate':int((df.get('l4_feasible_count',0).fillna(0)>0).sum()) if 'l4_feasible_count' in df else 0,
            'cycles_with_verified_candidate':int((df.get('l4_verified_count',0).fillna(0)>0).sum()) if 'l4_verified_count' in df else 0,
            'advisory_applied_cycles':bcount('l4_advisory_applied'),
            'interpretation':'Layer4 MPC contribution measures optimizer+tube/qLPV computation and advisory availability; it does not imply verified physical actuation.'},
        'layer5':{
            'invoked_cycles':l5_inv,'invocation_ratio_total':ratio(l5_inv,n),
            'mpc_first_move_cycles':l5_mpc,'mpc_participation_ratio_total':ratio(l5_mpc,n),'mpc_participation_ratio_when_invoked':ratio(l5_mpc,l5_inv),
            'hold_cycles':int((df.get('l5_mode','').astype(str)=='HOLD').sum()) if 'l5_mode' in df else 0,
            'final_gate_cycles':bcount('l5_final_gate'),'final_gate_ratio_when_invoked':ratio(bcount('l5_final_gate'),l5_inv),
            'feedback_accepted_cycles':bcount('l5_feedback_accepted'),
            'physical_control_effect_cycles':bcount('layer2_control_effect_applied'),'physical_control_effect_ratio_when_invoked':ratio(bcount('layer2_control_effect_applied'),l5_inv),
            'interpretation':'Layer5 MPC first-move reasoning is separated from FINAL authorization, TDOC feedback acceptance, and a demonstrated control effect.'}
    }

def _mpc_participation_panel(go, make_subplots, df, metrics):
    f=make_subplots(rows=3,cols=1,subplot_titles=['Layer4 MPC / optimizer participation by cycle','Layer5 MPC / FINAL / control-effect participation by cycle','Participation ratios'],vertical_spacing=0.12)
    x=df['flight_elapsed_s'] if 'flight_elapsed_s' in df else list(range(len(df)))
    for c,label in [('l4_invoked','L4 invoked'),('l4_optimizer_active','L4 optimizer'),('l4_mpc_participated','L4 qLPV/tube MPC'),('l4_advisory_applied','L4 advisory')]:
        if c in df:f.add_trace(go.Scatter(x=x,y=df[c].fillna(False).astype(int),mode='lines',name=label),row=1,col=1)
    for c,label in [('l5_invoked','L5 invoked'),('l5_mpc_participated','L5 MPC first move'),('l5_final_gate','L5 FINAL'),('layer2_control_effect_applied','Control effect applied')]:
        if c in df:f.add_trace(go.Scatter(x=x,y=df[c].fillna(False).astype(int),mode='lines',name=label),row=2,col=1)
    l4=metrics.get('layer4') or {}; l5=metrics.get('layer5') or {}
    names=['L4 invoked/total','L4 MPC/total','L4 MPC/when invoked','L5 invoked/total','L5 MPC/total','L5 MPC/when invoked','L5 FINAL/when invoked','L5 effect/when invoked']
    vals=[l4.get('invocation_ratio_total',0),l4.get('mpc_participation_ratio_total',0),l4.get('mpc_participation_ratio_when_invoked',0),l5.get('invocation_ratio_total',0),l5.get('mpc_participation_ratio_total',0),l5.get('mpc_participation_ratio_when_invoked',0),l5.get('final_gate_ratio_when_invoked',0),l5.get('physical_control_effect_ratio_when_invoked',0)]
    f.add_trace(go.Bar(x=names,y=vals,name='ratio'),row=3,col=1); f.update_yaxes(range=[0,1.05],row=1,col=1); f.update_yaxes(range=[0,1.05],row=2,col=1); f.update_yaxes(range=[0,1.05],row=3,col=1)
    f.update_layout(height=1200,title='MPC Participation — computation is distinct from FINAL/actuation',legend={'orientation':'h'}); return f

def mission_reports(outdir:Path,rows:list[dict],candidate_rows:list[dict],choice:dict,route_df,summary:dict,decision_details:list[dict]):
    import pandas as pd
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    outdir.mkdir(parents=True,exist_ok=True); df=enrich_mission_dataframe(pd.DataFrame(rows)); cdf=pd.DataFrame(candidate_rows)
    if df.empty:
        # Preserve the primary failure and emit machine-readable artifacts even when
        # the mission aborts before the first successful cycle. Reporting must never
        # mask the causal runtime/configuration error with a secondary KeyError.
        pd.DataFrame(rows).to_csv(outdir/'MISSION_TIMELINE.csv',index=False)
        cdf.to_csv(outdir/'LAYER4_CANDIDATES.csv',index=False)
        write_json_gz(outdir/'DECISION_POINT_DETAILS.json.gz',decision_details,indent=2)
        summary.setdefault('flight_duration_s',0.0)
        summary.setdefault('flight_duration_hms',_flight_hms(0.0))
        summary['reporting_status']='EARLY_ABORT_NO_SUCCESSFUL_CYCLE'
        (outdir/'MISSION_SUMMARY.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
        (outdir/'MISSION_ANALYTICS.json').write_text(json.dumps({'schema':'TDOC-mission-analytics-v3','summary':summary,'layer4':None,'sarg_crp':None,'accuracy_efficiency':None,'mpc_participation':None},indent=2,ensure_ascii=False))
        return
    if not df.empty:
        t0=float(df['reference_time_s'].iloc[0]); df['flight_elapsed_s']=df['reference_time_s'].astype(float)-t0
        summary['flight_start_reference_time_s']=t0; summary['flight_end_reference_time_s']=float(df['reference_time_s'].iloc[-1]); summary['flight_duration_s']=float(df['flight_elapsed_s'].iloc[-1]); summary['flight_duration_hms']=_flight_hms(summary['flight_duration_s'])
        summary['reference_route_distance_m']=_route_distance_m(route_df); realized_distance=0.0
        if {'realized_x_m','realized_y_m','realized_z_msl_m'} <= set(df.columns):
            for j in range(1,len(df)):
                realized_distance += math.sqrt((float(df.iloc[j].realized_x_m)-float(df.iloc[j-1].realized_x_m))**2+(float(df.iloc[j].realized_y_m)-float(df.iloc[j-1].realized_y_m))**2+(float(df.iloc[j].realized_z_msl_m)-float(df.iloc[j-1].realized_z_msl_m))**2)
        else: realized_distance=summary['reference_route_distance_m']
        summary['realized_route_distance_m']=realized_distance; summary['average_route_speed_m_s']=realized_distance/summary['flight_duration_s'] if summary['flight_duration_s']>0 else None
        summary['final_goal_distance_m']=float(df['goal_distance_m'].dropna().iloc[-1]) if df['goal_distance_m'].notna().any() else None
    df.to_csv(outdir/'MISSION_TIMELINE.csv',index=False); cdf.to_csv(outdir/'LAYER4_CANDIDATES.csv',index=False)
    mpc_metrics=mission_mpc_participation(df); (outdir/'MPC_PARTICIPATION.json').write_text(json.dumps(mpc_metrics,indent=2,ensure_ascii=False)); summary['mpc_participation']=mpc_metrics
    write_json_gz(outdir/'DECISION_POINT_DETAILS.json.gz',decision_details,indent=2)
    pulses=_external_return_pulses(ROOT/'runtime',df,decision_details)
    pulse_columns=['cycle','cycle_plot_x','sequence_in_cycle','source_occurrence_in_cycle','flight_elapsed_s','phase','tdoc_substate','transaction_id','layer','source','destination','event_type','transport_status','behavior','pulse_status','library_elapsed_s','deadline_s','deadline_ok','reason','event_time','error','behavior_detail']
    pd.DataFrame(pulses,columns=pulse_columns).to_csv(outdir/'EXTERNAL_LAYER_RETURN_PULSES.csv',index=False)
    write_json_gz(outdir/'EXTERNAL_LAYER_RETURN_PULSES.json.gz',pulses,indent=2)
    elapsed=df['flight_elapsed_s'].astype(float).tolist() if 'flight_elapsed_s' in df else list(range(len(decision_details)))
    pd.DataFrame(_detail_rows(decision_details,elapsed)).to_csv(outdir/'MISSION_DETAIL_TIMESERIES.csv',index=False)
    # Narrow purpose-specific tables for operations teams.
    for name,cols in {
        'CLIMATE_HISTORY.csv':['cycle','flight_elapsed_s','reference_time_s','latitude_deg','longitude_deg','wind_speed_m_s','wind_u_m_s','wind_v_m_s','wind_w_m_s','temperature_c','humidity_pct','pressure_pa','air_density_kg_m3'],
        'ENERGY_HISTORY.csv':['cycle','flight_elapsed_s','reference_time_s','energy_used_wh_reference','energy_remaining_wh','l4_energy_cost','l4_time_cost','l4_risk_cost'],
        'PHASE_HISTORY.csv':['cycle','flight_elapsed_s','reference_time_s','phase','tdoc_substate','mission_progress','fast_event','meso_event','slow_event'],
        'ACTUATOR_HISTORY.csv':['cycle','flight_elapsed_s','reference_time_s','l5_mode','l5_final_gate','l5_decision_reason','l5_control_present','l5_actuator_targets','l5_pilot_cmd_0','l5_pilot_cmd_1','l5_pilot_cmd_2','l5_pilot_cmd_3','vehicle_roll_deg','vehicle_pitch_deg','vehicle_yaw_deg','vehicle_roll_rate_rad_s','vehicle_pitch_rate_rad_s','vehicle_yaw_rate_rad_s','vehicle_airspeed_m_s','vehicle_groundspeed_m_s','vehicle_climb_m_s','vehicle_heading_deg','left_elevon_command_rad','left_elevon_command_deg','right_elevon_command_rad','right_elevon_command_deg','left_motor_command_norm','left_motor_speed_rad_s','left_motor_speed_rpm','right_motor_command_norm','right_motor_speed_rad_s','right_motor_speed_rpm','vehicle_state_semantics','actuation_semantics'],
        'ROUTE_HISTORY.csv':['cycle','flight_elapsed_s','reference_time_s','reference_lon_deg','reference_lat_deg','reference_x_m','reference_y_m','reference_z_msl_m','realized_x_m','realized_y_m','realized_z_msl_m','counterfactual_x_m','counterfactual_y_m','counterfactual_z_msl_m','wind_counterfactual_displacement_m','goal_distance_m','agl_m','terrain_clearance_m','map_terrain_elevation_m','building_mask','building_height_m','natural_obstacle_class','artificial_obstacle_class','vertical_clearance_over_building_m','route_turn_angle_deg','turn_near_obstacle_context','l4_next_reference_deviation_m','state_source'],
    }.items():
        use=[c for c in cols if c in df.columns]; df[use].to_csv(outdir/name,index=False)

    titles=[
        'Route / reference progress (2-D)','Geographic path (WGS84)','Altitude / AGL / goal distance','Wind components',
        'Climate state','Energy','Layer4 objective costs — gaps mean no selected feasible candidate','Layer4 candidate / feasibility availability',
        'Flight phase','Layer3.3 loop events','Layer5 pilot-domain command','Safety / decision state',
        'Simulation-reference actuator diagnostics','Route turn / obstacle context','Layer4 forecast vs next reference','Terrain / building clearance context']
    fig=make_subplots(rows=8,cols=2,subplot_titles=titles,vertical_spacing=0.035)
    # 2-D projections of all geometry carried by the 3-D mission report.
    fig.add_trace(go.Scatter(x=df.get('reference_x_m'),y=df.get('reference_y_m'),mode='lines',name='SARG / VTOL reference'),row=1,col=1)
    if 'realized_x_m' in df: fig.add_trace(go.Scatter(x=df['realized_x_m'],y=df['realized_y_m'],mode='lines',name='realized WingMPET + wind'),row=1,col=1)
    if 'counterfactual_x_m' in df: fig.add_trace(go.Scatter(x=df['counterfactual_x_m'],y=df['counterfactual_y_m'],mode='lines',name='same-command no-wind'),row=1,col=1)
    if 'l4_pred_x_m' in df:fig.add_trace(go.Scatter(x=df['l4_pred_x_m'],y=df['l4_pred_y_m'],mode='markers',name='L4 next-step forecast'),row=1,col=1)
    if {'longitude_deg','latitude_deg'} <= set(df.columns):fig.add_trace(go.Scatter(x=df['longitude_deg'],y=df['latitude_deg'],mode='lines',name='realized WGS84'),row=1,col=2)
    if {'reference_lon_deg','reference_lat_deg'} <= set(df.columns):fig.add_trace(go.Scatter(x=df['reference_lon_deg'],y=df['reference_lat_deg'],mode='lines',name='reference WGS84'),row=1,col=2)
    for c in ['reference_z_msl_m','realized_z_msl_m','agl_m','goal_distance_m']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=2,col=1)
    for c in ['wind_u_m_s','wind_v_m_s','wind_w_m_s','wind_speed_m_s']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=2,col=2)
    for c in ['temperature_c','humidity_pct','air_density_kg_m3','pressure_pa']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=3,col=1)
    if 'energy_remaining_wh' in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df['energy_remaining_wh'],mode='lines',name='remaining Wh'),row=3,col=2)
    if 'energy_used_wh_reference' in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df['energy_used_wh_reference'],mode='lines',name='reference energy used Wh'),row=3,col=2)
    for c in ['l4_mission_cost','l4_time_cost','l4_energy_cost','l4_risk_cost','l4_uncertainty_cost']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines+markers',name=c,connectgaps=False),row=4,col=1)
    for c in ['l4_candidate_count','l4_feasible_count','l4_verified_count']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c),row=4,col=2)
    if 'l4_advisory_applied' in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df['l4_advisory_applied'].astype(int),mode='lines',name='l4_advisory_applied'),row=4,col=2)
    fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df['phase_id'],mode='lines+markers',name='phase_id'),row=5,col=1)
    for c in ['fast_event','meso_event','slow_event']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c].astype(int),mode='lines',name=c),row=5,col=2)
    for c in ['l5_pilot_cmd_0','l5_pilot_cmd_1','l5_pilot_cmd_2','l5_pilot_cmd_3']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=6,col=1)
    for c in ['l5_final_gate','l5_feedback_accepted','layer2_control_effect_applied','l4_candidate_verified']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c].astype(int),mode='lines',name=c),row=6,col=2)
    for c in ['simulation_reference_left_motor_cmd','simulation_reference_right_motor_cmd','simulation_reference_left_elevon_rad','simulation_reference_right_elevon_rad']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c),row=7,col=1)
    for c in ['left_motor_speed_rpm','right_motor_speed_rpm','left_elevon_command_deg','right_elevon_command_deg']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=7,col=1)
    for c in ['vehicle_roll_deg','vehicle_pitch_deg','vehicle_yaw_deg','vehicle_airspeed_m_s','vehicle_groundspeed_m_s']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=6,col=1)
    for c in ['route_turn_angle_deg','building_height_m','building_mask','natural_obstacle_class','artificial_obstacle_class']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=7,col=2)
    for c in ['l4_next_reference_deviation_m','wind_counterfactual_displacement_m']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=8,col=1)
    for c in ['terrain_clearance_m','map_terrain_elevation_m','vertical_clearance_over_building_m']:
        if c in df:fig.add_trace(go.Scatter(x=df['flight_elapsed_s'],y=df[c],mode='lines',name=c,connectgaps=False),row=8,col=2)
    fig.update_xaxes(title_text='Projected X / Easting [m]',row=1,col=1); fig.update_yaxes(title_text='Projected Y / Northing [m]',row=1,col=1)
    fig.update_xaxes(title_text='Longitude [deg]',row=1,col=2); fig.update_yaxes(title_text='Latitude [deg]',row=1,col=2)
    duration=float(summary.get('flight_duration_s',0) or 0); loops=summary.get('loop_counts') or {}; x_end=max(duration,1.0)
    for rr,cc in [(2,1),(2,2),(3,1),(3,2),(4,1),(4,2),(5,1),(5,2),(6,1),(6,2),(7,1),(7,2),(8,1),(8,2)]:
        fig.update_xaxes(title_text='Flight elapsed time [s]',range=[0,x_end],row=rr,col=cc)
    budget=summary.get('cycle_budget_max'); budget_text=f"; completed={summary.get('cycles_completed')} of safety budget≤{budget}" if budget is not None else f"; completed={summary.get('cycles_completed')}"
    fig.update_layout(height=3600,title=f"TDOC Offline Mission Management Dashboard — elapsed {duration:.3f} s ({_flight_hms(duration)}){budget_text}; Fast={loops.get('fast')}, Meso={loops.get('meso')}, Slow={loops.get('slow')}",legend={'orientation':'h'})
    pulse_fig=_external_return_pulse_panel(go,pulses,duration)
    mpc_fig=_mpc_participation_panel(go,make_subplots,df,mpc_metrics)
    detail_figs=[_detail_section_figure(go,decision_details,section,elapsed,duration) for section in DETAIL_SECTIONS if any((d.get(section) or {}) for d in decision_details)]
    _write_report_with_explorer(outdir/'MISSION_MANAGEMENT_DASHBOARD.html','TDOC Offline Mission Management Dashboard — complete 2-D projection of 3-D report details',[pulse_fig,mpc_fig,fig,*detail_figs],decision_details)

    # 3-D terrain + route + decision points. Hover is concise; the embedded per-cycle
    # explorer exposes all hover-backed scalar fields without truncating rows.
    fig3=go.Figure(); surf=_terrain_surface_for_route(df,grid_n=90)
    if surf and 'z' in surf:fig3.add_trace(go.Surface(x=surf['x'],y=surf['y'],z=surf['z'],name='Terrain raster',showscale=True,opacity=0.58))
    fig3.add_trace(go.Scatter3d(x=route_df['x_m'],y=route_df['y_m'],z=route_df['z_msl_m'],mode='lines',name='SARG / VTOL reference prior'))
    if not df.empty:
        hover=[_decision_hover(d) for d in decision_details[:len(df)]]
        fig3.add_trace(go.Scatter3d(x=df['realized_x_m'],y=df['realized_y_m'],z=df['realized_z_msl_m'],mode='lines+markers',name='Realized WingMPET + MicroMet wind',text=hover,customdata=df['cycle'] if 'cycle' in df else None,hovertemplate='Cycle %{customdata}<br>%{text}<extra></extra>'))
        fig3.add_trace(go.Scatter3d(x=df['counterfactual_x_m'],y=df['counterfactual_y_m'],z=df['counterfactual_z_msl_m'],mode='lines',name='Same-command no-wind counterfactual'))
        step=max(1,len(df)//24); q=df.iloc[::step]
        fig3.add_trace(go.Cone(x=q['realized_x_m'],y=q['realized_y_m'],z=q['realized_z_msl_m'],u=q['wind_u_m_s'].fillna(0),v=q['wind_v_m_s'].fillna(0),w=q['wind_w_m_s'].fillna(0),sizemode='absolute',sizeref=6.0,anchor='tail',name='MicroMet wind vectors',showscale=False))
    fig3.update_layout(title=f"TDOC Offline 3D Mission — reference vs realized vs no-wind; duration {_flight_hms(duration)}",scene={'xaxis_title':'Projected X / Easting [m]','yaxis_title':'Projected Y / Northing [m]','zaxis_title':'Altitude MSL [m]'})
    _write_report_with_explorer(outdir/'MISSION_ROUTE_3D.html','TDOC Offline 3-D Mission Route + complete per-cycle detail explorer',[fig3],decision_details)
    l4summary=layer4_optimization_reports(outdir,cdf); treemap=sarg_crp_treemap(outdir); metrics=accuracy_efficiency_reports(outdir,df,cdf,summary,route_df)
    summary['layer4_optimization_report']=l4summary; summary['sarg_crp_treemap']=treemap; summary['accuracy_efficiency']=metrics; summary['decision_detail_file']='DECISION_POINT_DETAILS.json.gz'; summary['decision_detail_timeseries_file']='MISSION_DETAIL_TIMESERIES.csv'; summary['external_layer_return_pulses']={'count':len(pulses),'non_normal_count':sum(p.get('pulse_status') not in {'NORMAL','OK'} for p in pulses),'csv':'EXTERNAL_LAYER_RETURN_PULSES.csv','json_gz':'EXTERNAL_LAYER_RETURN_PULSES.json.gz','layers':[2,3,4,5]}
    (outdir/'MISSION_SUMMARY.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    (outdir/'MISSION_ANALYTICS.json').write_text(json.dumps({'schema':'TDOC-mission-analytics-v3','summary':summary,'layer4':l4summary,'sarg_crp':treemap,'accuracy_efficiency':metrics,'mpc_participation':mpc_metrics},indent=2,ensure_ascii=False))

def postprocess_mission_diagnostics(outdir:Path):
    import pandas as pd
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    df=pd.read_csv(outdir/'MISSION_TIMELINE.csv')
    # Direct causal wind evidence: same air commands, WingMPET op07, wind vs zero wind.
    wind_metrics={'schema':'TDOC-wind-causality-v1','samples':int(len(df)),'max_counterfactual_displacement_m':float(df['wind_counterfactual_displacement_m'].max()),'final_counterfactual_displacement_m':float(df['wind_counterfactual_displacement_m'].iloc[-1]),'mean_wind_speed_m_s':float(df['wind_speed_m_s'].mean()),'wind_effect_nonzero':bool(df['wind_counterfactual_displacement_m'].max()>1e-6),'interpretation':'Realized and counterfactual use identical air commands; only MicroMet wind is zeroed in the counterfactual.'}
    (outdir/'WIND_CAUSALITY_METRICS.json').write_text(json.dumps(wind_metrics,indent=2))
    f=make_subplots(rows=2,cols=1,subplot_titles=['Realized vs no-wind projected path','Wind causal displacement'])
    f.add_trace(go.Scatter(x=df.realized_x_m,y=df.realized_y_m,mode='lines',name='realized wind-aware'),row=1,col=1); f.add_trace(go.Scatter(x=df.counterfactual_x_m,y=df.counterfactual_y_m,mode='lines',name='no-wind same command'),row=1,col=1); f.add_trace(go.Scatter(x=df.flight_elapsed_s,y=df.wind_counterfactual_displacement_m,mode='lines',name='3-D separation [m]'),row=2,col=1); f.update_layout(height=900,title='Wind Causality — WingMPET same-command counterfactual'); f.write_html(outdir/'WIND_CAUSALITY_REPORT.html',include_plotlyjs='cdn')
    # Full VTOL phase evidence.  Seeing a phase label is not equivalent to
    # demonstrating that the phase was controllable.  problem-12 separates
    # observed taxonomy coverage from strict phase completion.
    rows_for_phase=df.to_dict(orient='records')
    completion=_mission_phase_completion(rows_for_phase)
    phases=completion['phases_seen']
    phase_metrics={'schema':'TDOC-vtol-phase-evidence-v3','phases_seen':phases,'tdoc_substates_seen':list(dict.fromkeys(str(x) for x in df.get('tdoc_substate',[]).dropna().tolist())) if 'tdoc_substate' in df else [],
                   'starts_on_ground':completion['starts_on_ground'],
                   'touchdown_seen':completion['touchdown_seen'],
                   'final_agl_m':completion['final_agl_m'],
                   'final_goal_distance_m':completion['final_goal_distance_m'],
                   'all_required_phases_seen':completion['all_required_phases_seen'],
                   'all_required_control_phases':completion['all_required_control_phases'],
                   'missing_control_phases':completion['missing_control_phases'],
                   'phase_completion':completion['phase_completion'],
                   'all_required_phases':completion['all_required_phases'],
                   'all_required_phases_semantics':'strict: passive boundary evidence + same-cycle L4-verified/L5-FINAL/control-effect proof for every controlled phase'}
    (outdir/'VTOL_PHASE_METRICS.json').write_text(json.dumps(phase_metrics,indent=2))
    pf=make_subplots(rows=3,cols=1,subplot_titles=['Altitude AGL / goal distance','Simulation-reference actuators','Phase / 6-DOF mode'])
    pf.add_trace(go.Scatter(x=df.flight_elapsed_s,y=df.agl_m,name='AGL [m]'),row=1,col=1); pf.add_trace(go.Scatter(x=df.flight_elapsed_s,y=df.goal_distance_m,name='goal distance [m]'),row=1,col=1)
    for c in ['simulation_reference_left_motor_cmd','simulation_reference_right_motor_cmd','simulation_reference_left_elevon_rad','simulation_reference_right_elevon_rad']:
        if c in df:pf.add_trace(go.Scatter(x=df.flight_elapsed_s,y=df[c],name=c),row=2,col=1)
    phase_codes={p:i for i,p in enumerate(phases)}; pf.add_trace(go.Scatter(x=df.flight_elapsed_s,y=[phase_codes[x] for x in df.phase.astype(str)],mode='lines+markers',name='phase code'),row=3,col=1)
    if 'shadow_flight_mode_code' in df:pf.add_trace(go.Scatter(x=df.flight_elapsed_s,y=df.shadow_flight_mode_code,name='WingMPET op27 flight_mode_code'),row=3,col=1)
    pf.update_layout(height=1100,title='Full VTOL Ground-to-Touchdown Evidence'); pf.write_html(outdir/'VTOL_PHASE_DASHBOARD.html',include_plotlyjs='cdn')
    return {'wind':wind_metrics,'vtol':phase_metrics}

def _vertical_control_proof_dwell_decision(*, phase:str, next_phase:str|None, joint_proof_seen:bool, dwell_used:int, dwell_limit:int)->dict:
    """Decide whether to hold a reference boundary for real vertical-phase proof.

    This function only controls reference progression.  It never changes Layer4/5
    outputs, the physical state, the capability registry, or knowledge validity.
    """
    p=str(phase or '')
    controlled=p in {'vertical_takeoff','hover'}
    at_boundary=bool(controlled and str(next_phase or '') != p)
    hold=bool(at_boundary and not bool(joint_proof_seen) and int(dwell_used)<max(0,int(dwell_limit)))
    return {'hold_reference':hold,'at_control_proof_boundary':at_boundary,'reason':('awaiting_real_same_cycle_joint_control_proof' if hold else ('joint_control_proof_present' if at_boundary and joint_proof_seen else ('dwell_limit_exhausted_fail_closed' if at_boundary and not joint_proof_seen else 'not_vertical_reference_boundary'))),'synthetic_capability':False}


def _offline_terminal_phase(*, horizontal_m:float, agl_m:float, capture_m:float, terminal_captured:bool,
                            capture_agl_m:float|None=None):
    """Map terminal correction to the canonical 11-phase mission plus a TDOC substate.

    ``terminal_reposition`` is orchestration metadata only.  While horizontal
    capture is outstanding the canonical phase remains ``transition_back``: this
    preserves altitude and the commissioned low-speed transition contract.  Only
    horizontal capture permits entry to ``vertical_landing``.  No twelfth phase is
    invented and Layer2-L5 retain ownership of planning, verification and control.
    """
    outside_horizontal=float(horizontal_m)>float(capture_m)
    # Do not start the descent while hundreds of metres remain: the vertical
    # landing controller has bounded horizontal authority and would land short.
    # The transition airspeed floor comes from Layer4's source contract below.
    if (not terminal_captured) and outside_horizontal:
        return "transition_back", "terminal_reposition", False
    return ("vertical_landing" if float(agl_m) > 1.0 else "touchdown"), None, True


def _offline_terminal_reposition_airspeed(horizontal_m:float, *, minimum_m_s:float=2.0)->float:
    """Distance-scheduled terminal speed for a capture corridor smaller than turn radius.

    A fixed 12 m/s reference made the physically executed Phoenix controller orbit the
    destination at roughly 110--220 m while the terminal capture gate remained 8 m.
    The schedule preserves forward-flight authority far away, then reduces turn radius
    and kinetic energy as the vehicle closes.  It does not relax any FF/FINAL gate.
    """
    return _clamp(float(horizontal_m)/25.0,float(minimum_m_s),8.0)

def _offline_terminal_reposition_agl(cfg, requested_agl_m:float)->dict:
    """Bind approach/reposition altitude to Layer-4's certified safety envelope.

    The source Layer-4 configuration is authoritative for both the en-route terrain
    floor and the maximum robust tube radius.  Their sum is a conservative target
    whose complete prediction tube remains above the unchanged terrain gate.  This
    is schedule alignment, not a safety-threshold override.
    """
    import yaml
    layer4_root=cfg.path_value('TDDT_LAYER4_ROOT')
    config_rel=str(cfg.get('layers.layer4.config','configs/configuration_boulder.yaml'))
    config_path=(layer4_root/config_rel) if layer4_root else None
    if config_path is None or not config_path.is_file():
        raise RuntimeError('Layer4 source config unavailable for terminal reposition safety binding')
    source=yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
    floor=float(((source.get('feasibility') or {}).get('min_terrain_clearance_m')))
    tube_max=float(((source.get('robust') or {}).get('tube_radius_max')))
    transition_min=float((((source.get('vtol') or {}).get('min_airspeed_by_mode_m_s') or {}).get('transition')))
    if not (math.isfinite(floor) and floor>=0.0 and math.isfinite(tube_max) and tube_max>=0.0
            and math.isfinite(transition_min) and transition_min>=0.0):
        raise RuntimeError('Layer4 terminal reposition safety values are invalid')
    requested=float(requested_agl_m)
    resolved=max(requested,floor+tube_max)
    return {'resolved_agl_m':resolved,'requested_agl_m':requested,
            'min_terrain_clearance_m':floor,'tube_radius_max_m':tube_max,
            'transition_min_airspeed_m_s':transition_min,
            'source_config':str(config_path),
            'semantics':'transition-back target keeps the complete certified Layer4 tube above the unchanged en-route terrain floor and uses the source transition airspeed floor'}

def _offline_terminal_cycle_budget(*, horizontal_m:float, capture_m:float,
                                   capture_agl_m:float, landing_rate_m_s:float,
                                   configured_floor:int, dt_s:float=1.0,
                                   minimum_airspeed_m_s:float=2.0)->int:
    """Derive a finite safety budget from the terminal guidance law itself.

    The configured value remains a conservative floor.  The computed part follows
    the same distance-scheduled ground speed used by ``terminal_reposition`` and
    then reserves the cycles required for the source-bound vertical landing rate.
    This keeps the safety cap consistent with the executable schedule without
    relaxing capture, touchdown, FF, or FINAL gates.
    """
    dt=max(float(dt_s),1e-9)
    remaining=max(0.0,float(horizontal_m))
    capture=max(0.0,float(capture_m))
    reposition_cycles=0
    while remaining>capture:
        scheduled=_offline_terminal_reposition_airspeed(
            remaining,minimum_m_s=minimum_airspeed_m_s)
        ground_step=min(scheduled,max(2.0,remaining))*dt
        remaining=max(0.0,remaining-ground_step)
        reposition_cycles+=1
    landing_rate=max(0.2,float(landing_rate_m_s))
    landing_height=max(0.0,float(capture_agl_m)-1.0)
    landing_cycles=int(math.ceil(landing_height/(landing_rate*dt)))+1
    return max(0,int(configured_floor),reposition_cycles+landing_cycles)

def run_offline_mission(orch,cfg,req0,args,report_root):
    import pandas as pd
    sarg_root=envpath('TDDT_SARG_ROOT'); twins=envpath('TDDT_TWINS_ROOT'); climate=envpath('TDDT_CLIMATE_ROOT'); region=envpath('TDDT_REGION_FILE')
    if region is None:
        raise SystemExit('TDDT_REGION_FILE is not exported; source configs/environment.sh')
    initial_energy=args.initial_energy_wh if args.initial_energy_wh is not None else physical_energy_wh(twins)
    choice,route,shortlist=load_reference_route(sarg_root,int(req0.get('regime_id',3)),float(initial_energy)); reference=build_vtol_reference(route); _,start_ll,dest_ll=load_region(region)
    origin_x=float(route.iloc[0]['x_m']); origin_y=float(route.iloc[0]['y_m']); dest_x=float(route.iloc[-1]['x_m']); dest_y=float(route.iloc[-1]['y_m']); ground0=_terrain_from_row(route.iloc[0]); ground1=_terrain_from_row(route.iloc[-1]); forcing_start,forcing_end=forcing_window(climate)
    plant=OfflineWingMPETPlant(twins,origin_x,origin_y,ground0,float(initial_energy))
    try:
        _offline_forcing_file=resolve_climate_forcing(climate,require_exists=True)
    except Exception:
        _offline_forcing_file=None
    _offline_forcing_sha256=_sha256_file(_offline_forcing_file)
    # Priority 6: keep the mission single-episode, but reserve a bounded number of
    # additional physical cycles for vertical-phase control-proof acquisition.  The
    # dwell does not manufacture capability: every counted proof must still be a
    # same-cycle L4 verified + L5 FINAL + realized Layer2/WingMPET control effect.
    vertical_proof_dwell_limit=max(0,int(getattr(args,'offline_vertical_proof_dwell_cycles',3) or 0))
    vertical_proof_phases=('vertical_takeoff','hover','vertical_landing')
    proof_budget=(2*vertical_proof_dwell_limit)  # takeoff + hover reference-boundary dwell
    route_distance=math.hypot(dest_x-origin_x,dest_y-origin_y)
    terminal_reposition_contract=_offline_terminal_reposition_agl(
        cfg,float(args.offline_terminal_reposition_altitude_m))
    terminal_reposition_agl=float(terminal_reposition_contract['resolved_agl_m'])
    terminal_transition_min_airspeed=float(
        terminal_reposition_contract['transition_min_airspeed_m_s'])
    terminal_budget=_offline_terminal_cycle_budget(
        horizontal_m=route_distance,
        capture_m=float(args.offline_terminal_capture_m),
        capture_agl_m=terminal_reposition_agl,
        landing_rate_m_s=float(getattr(plant,'_landing_descent_ref_m_s',1.0)),
        configured_floor=int(args.offline_terminal_correction_cycles),
        dt_s=1.0,
        minimum_airspeed_m_s=terminal_transition_min_airspeed,
    )
    hardmax=args.offline_max_cycles if args.offline_max_cycles>0 else len(reference)+terminal_budget+proof_budget+1
    vertical_proof_epoch,vertical_proof_climate_window=_select_offline_vertical_proof_epoch(
        climate,twins,float(hardmax),mode=getattr(args,'offline_vertical_proof_climate_window','auto'))
    if vertical_proof_epoch is None:
        raise RuntimeError('Priority-6 vertical control proof acquisition has no physically valid forcing window: '+json.dumps(vertical_proof_climate_window,sort_keys=True))
    rows=[]; crows=[]; records=[]; decision_details=[]; remaining=float(initial_energy); reached=False; closed_loop_count=0; teacher_count=0; terminal_captured=False; l4_advisory_cycles=0; termination_reason='cycle_budget_exhausted'
    ref_cursor=0
    vertical_joint_proof_seen={p:False for p in vertical_proof_phases}
    vertical_proof_dwell_used={p:0 for p in vertical_proof_phases}
    offline_run_id='offline-'+uuid.uuid4().hex
    offline_episode_id='OFFLINE_SE-001'
    capability_registry=LiveCapabilityRegistry(ROOT/'runtime'/'rtmo'/'capability_registry.json.gz')
    capability_registry.reset(run_id=offline_run_id,episode_id=offline_episode_id)
    print(f"\n=== OFFLINE_SE REALIZED MISSION: SARG prior {choice['route'].name}, reference_cycles={len(reference)}, safety_cycle_budget<={hardmax} ===",flush=True)
    try:
      for i in range(hardmax):
        s0=dict(plant.state); agl=max(0.0,s0['h']-ground1); horizontal=math.hypot(dest_x-s0['x'],dest_y-s0['y']); tdoc_substate=None
        if ref_cursor < len(reference):
            ref=reference.iloc[ref_cursor].to_dict(); phase=str(ref.get('tdoc_phase') or PHASES.get(int(ref.get('phase_id',3)),'cruise')); ground=_terrain_from_row(ref)
            _reference_active=True
            _next_reference_phase=(str(reference.iloc[ref_cursor+1].get('tdoc_phase') or PHASES.get(int(reference.iloc[ref_cursor+1].get('phase_id',3)),'cruise')) if ref_cursor+1 < len(reference) else None)
        else:
            _reference_active=False; _next_reference_phase=None
            ground=ground1
            phase,tdoc_substate,terminal_captured=_offline_terminal_phase(
                horizontal_m=horizontal,agl_m=agl,
                capture_m=float(args.offline_terminal_capture_m),
                capture_agl_m=terminal_reposition_agl,
                terminal_captured=terminal_captured,
            )
            ref=route.iloc[-1].to_dict()
            if tdoc_substate=='terminal_reposition':
                terminal_speed=_offline_terminal_reposition_airspeed(
                    horizontal,minimum_m_s=terminal_transition_min_airspeed)
                ref.update({'tdoc_phase':phase,'tdoc_substate':tdoc_substate,'x_m':dest_x,'y_m':dest_y,'z_msl_m':ground1+terminal_reposition_agl,'airspeed_ms':terminal_speed,'time_s':float(i),'mission_progress':1.0})
            else:
                ref.update({'tdoc_phase':phase,'x_m':dest_x,'y_m':dest_y,'z_msl_m':ground1,'airspeed_ms':0.0,'time_s':float(i),'mission_progress':1.0})
        phase_entry=plant.prepare_phase_entry(phase,target_h=float(ref.get('z_msl_m',ref.get('h_m',s0['h']))))
        s0=dict(plant.state)
        req=copy.deepcopy(req0); req['learning_mode']='OFFLINE_SE'; req['transaction_id']=f"mission-offline-{i+1:04d}"; req.setdefault('context',{})['mission_driver']='wingmpet_realized_origin_to_touchdown'
        req['context']['tdoc_substate']=tdoc_substate
        req['context']['phoenix_phase_entry_state']=phase_entry
        req['context']['tdoc_run_id']=offline_run_id
        req['context']['offline_episode_id']=offline_episode_id
        req['context']['offline_cycle_id']=i+1
        update_request_from_realized(req,s0,ref,route,dest_x,dest_y,forcing_start,forcing_end,remaining,phase,i,ground,effective_climate_epoch=vertical_proof_epoch)
        req.setdefault('context',{})['effective_climate_fingerprint']=_offline_forcing_sha256
        req['context']['offline_vertical_proof_climate_window']=vertical_proof_climate_window
        _ca=dict(req['context'].get('offline_climate_time_alignment') or {})
        if _ca.get('effective_climate_epoch'): req['context']['effective_climate_epoch']=_ca.get('effective_climate_epoch')
        # Preserve the actual OFFLINE plant state at the same Fast-cycle boundary so
        # full-fidelity verification and the direct L5->TDOC->Layer2 control-effect
        # transaction start from the same physical condition.
        req['context']['offline_plant_state']={k:float(v) for k,v in s0.items() if isinstance(v,(int,float))}
        req['context']['offline_control_dt_s']=1.0
        print(f"[{i+1:04d}] phase={phase:20s} substate={(tdoc_substate or '-'):20s} goal={horizontal:8.2f}m AGL={max(0,s0['h']-ground):6.2f}m energy={remaining:6.2f}Wh  budget<={hardmax:04d}",flush=True)
        try:r=orch.run_cycle(req).to_dict()
        except Exception as exc: termination_reason='cycle_failed'; records.append({'cycle':i+1,'status':'FAILED','error':repr(exc)}); print('      FAILED',exc,flush=True); break
        l4=r.get('layer4_result') or {}; best=l4_best(l4) or {}; l5=l5_result(r) or {}; action=l5.get('control_action'); meso,slow,k=meso_slow(r); climate_s=climate_state(r)
        if r.get('layer3_result') is None:
            raise RuntimeError(f"OFFLINE Layer3 invocation failed at cycle {i+1}; refusing fast/partial training run. Check invocation event log and climate/dependency binding.")
        fb_meta=((r.get('downstream_layers') or {}).get('2_offline_feedback') or {}); fb=fb_meta.get('status'); fb_out=fb_meta.get('output') or {}; feedback_accepted=bool(action and fb=='OK'); dynamic_applied=bool(fb_out.get('control_effect_applied') is True); l5_valid=bool(feedback_accepted and dynamic_applied)
        state_source='L5_CLOSED_LOOP' if l5_valid else 'L4_ADVISORY_WINGMPET_SIMULATION'; closed_loop_count+=int(l5_valid); teacher_count+=0
        act0=(best.get('actions') or [None])[0] if best else None; yaw_adv=0.0; air_adv=0.0
        if isinstance(act0,(list,tuple)) and len(act0)>=5:
            yaw_adv=float(act0[3]); air_adv=float(act0[4]); l4_advisory_cycles+=int(abs(yaw_adv)>1e-12 or abs(air_adv)>1e-12)
        wind=(_num(climate_s.get('wind_u_3dw'),0.0) or 0.0,_num(climate_s.get('wind_v_3dw'),0.0) or 0.0,_num(climate_s.get('diagnostic_vertical_wind'),0.0) or 0.0); rho=_num(climate_s.get('air_density'),1.0) or 1.0
        target=(float(ref['x_m']),float(ref['y_m']),float(ref.get('z_msl_m',ref.get('h_m',s0['h'])))); base=float(ref.get('airspeed_ms',13.0) or 0.0)
        sim=plant.propagate(target,phase,wind,rho,base,yaw_adv,air_adv,dt=1.0,ground=ground,substate=tdoc_substate)
        # For a verified FINAL decision, the authoritative next state is the state
        # produced by the direct Layer2/WingMPET physical-control transaction.  The
        # simulation-reference propagation above remains diagnostic only.
        if l5_valid and isinstance(fb_out.get('physical_state'),dict):
            ps=fb_out['physical_state']; cps=(fb_out.get('control_effect') or {}).get('counterfactual_physical_state') or fb_out.get('counterfactual_physical_state')
            def to_plant(st,windv):
                return {'x':float(st['pe']),'y':float(st['pn']),'h':-float(st['pd']),
                        'air_vx':float(st['ve'])-float(windv[0]),'air_vy':float(st['vn'])-float(windv[1]),'air_vh':-float(st['vd'])-float(windv[2]),
                        'soc':float(st.get('soc',1.0)),'omega_left':float(st.get('omega_left',0.0)),'omega_right':float(st.get('omega_right',0.0)),
                        'p_rate':float(st.get('p_rate',0.0)),'q_rate':float(st.get('q_rate',0.0)),'r_rate':float(st.get('r_rate',0.0)),
                        'qw':float(st.get('qw',1.0)),'qx':float(st.get('qx',0.0)),'qy':float(st.get('qy',0.0)),'qz':float(st.get('qz',0.0))}
            plant.state.update(to_plant(ps,wind))
            if isinstance(cps,dict): plant.cf.update(to_plant(cps,(0.0,0.0,0.0)))
            sim['realized_state']=dict(plant.state); sim['counterfactual_state']=dict(plant.cf)
            sim['wind_counterfactual_displacement_m']=math.sqrt((plant.state['x']-plant.cf['x'])**2+(plant.state['y']-plant.cf['y'])**2+(plant.state['h']-plant.cf['h'])**2)
            sim['command_air_enu_m_s']=(plant.state.get('air_vx',0.0),plant.state.get('air_vy',0.0),plant.state.get('air_vh',0.0))
            sim['layer5_control_effect']=(fb_out.get('control_effect') or {})
        s1=sim['realized_state']; cf=sim['counterfactual_state']; remaining=max(0.0,float(initial_energy)*float(s1.get('soc',1.0)))
        six=sim.get('shadow_6dof_op27') or {}; mot=sim.get('motor_op28') or {}; aref=sim.get('simulation_reference_actuators') or {}
        eul=quaternion_to_euler(six.get('next_qw'),six.get('next_qx'),six.get('next_qy'),six.get('next_qz'))
        cmd=sim.get('command_air_enu_m_s') or (0.0,0.0,0.0); sim_air=math.sqrt(sum(float(v or 0.0)**2 for v in cmd)); sim_ground=(0.0 if phase in ('on_ground','touchdown') else math.sqrt(sum((float(cmd[j] or 0.0)+float(wind[j] or 0.0))**2 for j in range(3))))
        wl=_num(mot.get('next_omega_left'),_num(s1.get('omega_left'),None)); wr=_num(mot.get('next_omega_right'),_num(s1.get('omega_right'),None))
        offline_vehicle={'schema':'TDOC-Phoenix-decision-vehicle-state-v1','source':'WingMPET offline simulation state','source_semantics':'simulation reference/plant state; not physical actuator feedback',
          'attitude':{**eul,'roll_rate_rad_s':_num(six.get('next_p_rate')),'pitch_rate_rad_s':_num(six.get('next_q_rate')),'yaw_rate_rad_s':_num(six.get('next_r_rate'))},
          'speed':{'airspeed_m_s':sim_air,'groundspeed_m_s':sim_ground,'climb_m_s':(0.0 if phase in ('on_ground','touchdown') else float(cmd[2] or 0.0)+float(wind[2] or 0.0)),'heading_deg':(None if phase in ('on_ground','touchdown') else (math.degrees(math.atan2(float(cmd[1] or 0.0)+float(wind[1] or 0.0),float(cmd[0] or 0.0)+float(wind[0] or 0.0))) if sim_ground>1e-9 else None))},
          'actuators':{'feedback_source':'WingMPET simulation state',
            'left_elevon':{'command_rad':_num(aref.get('left_elevon_rad')),'command_deg':math.degrees(_num(aref.get('left_elevon_rad'),0.0)),'angle_measured':False},
            'right_elevon':{'command_rad':_num(aref.get('right_elevon_rad')),'command_deg':math.degrees(_num(aref.get('right_elevon_rad'),0.0)),'angle_measured':False},
            'left_motor':{'command_norm':_num(aref.get('left_motor_cmd')),'speed_rad_s':wl,'speed_rpm':(wl*60/(2*math.pi) if wl is not None else None),'speed_measured':False},
            'right_motor':{'command_norm':_num(aref.get('right_motor_cmd')),'speed_rad_s':wr,'speed_rpm':(wr*60/(2*math.pi) if wr is not None else None),'speed_measured':False},
            'physical_mapping':{'SERVO1':'left_elevon','SERVO2':'right_elevon','SERVO3':'left_motor','SERVO4':'right_motor'}},
          'motion':{'moving':bool(sim_ground>0.25),'state':'SIMULATED_MOVING' if sim_ground>0.25 else 'SIMULATED_STATIONARY'}}
        lon,lat=_lonlat_from_projected(s1['x'],s1['y'],route); goal=math.hypot(dest_x-s1['x'],dest_y-s1['y']); agl1=max(0.0,s1['h']-ground)
        # Touchdown is an explicit cycle/state, not inferred from the last approach row.
        if phase=='touchdown':
            # Passive contact has already stopped all motion.  Terminal location is a
            # separate fail-closed condition: only a state already within the mission
            # goal tolerance is snapped to the exact destination.  Keep realized and
            # counterfactual terminal states symmetric so wind history cannot create a
            # fictitious touchdown displacement.
            if goal <= args.goal_tolerance_m:
                s1['x']=dest_x; s1['y']=dest_y
                cf['x']=dest_x; cf['y']=dest_y
            s1['h']=ground1; cf['h']=ground1
            for _st in (s1,cf):
                _st.update({'air_vx':0.0,'air_vy':0.0,'air_vh':0.0})
            plant.state.update(s1); plant.cf.update(cf)
            goal=math.hypot(dest_x-s1['x'],dest_y-s1['y']); agl1=0.0
        traj=best.get('trajectory') or []; pred=traj[1] if len(traj)>1 and isinstance(traj[1],list) else None; l5d=l5.get('decision') or {}; gate=bool((l5d.get('approval') or {}).get('final_gate')); obj=best.get('objective_vector') or {}; ar=flatten_action(action)
        rowrec={'cycle':i+1,'transaction_id':r['transaction_id'],'status':r['status'],'reference_time_s':float(req['reference_time_s']),'phase':phase,'tdoc_substate':tdoc_substate,'phase_id':next((k for k,v in PHASES.items() if v==phase),-1),'mission_progress':float(ref.get('mission_progress',0.0)),'reference_lon_deg':float(ref.get('lon_deg',dest_ll[0])),'reference_lat_deg':float(ref.get('lat_deg',dest_ll[1])),'latitude_deg':lat,'longitude_deg':lon,'reference_x_m':float(ref['x_m']),'reference_y_m':float(ref['y_m']),'reference_z_msl_m':float(ref.get('z_msl_m',ref.get('h_m',s1['h']))),'realized_x_m':float(s1['x']),'realized_y_m':float(s1['y']),'realized_z_msl_m':float(s1['h']),'counterfactual_x_m':float(cf['x']),'counterfactual_y_m':float(cf['y']),'counterfactual_z_msl_m':float(cf['h']),'wind_counterfactual_displacement_m':float(sim['wind_counterfactual_displacement_m']),'goal_distance_m':goal,'agl_m':agl1,'terrain_clearance_m':max(0.0,s1['h']-ground),'energy_remaining_wh':remaining,'state_source':state_source,'teacher_forced':False,'sarg_used_as_next_state':False,'fast_event':True,'meso_event':meso,'slow_event':slow,'layer3_fast_index':k.get('layer3_fast_index'),'knowledge_target_reached':bool(k.get('knowledge_target_reached')),'l4_invoked':bool(l4),'l4_optimizer_active':bool((l4.get('conformance') or {}).get('C_optimizer')),'l4_tube_mpc_active':bool((l4.get('conformance') or {}).get('C_tube')),'l4_mpc_participated':bool((l4.get('conformance') or {}).get('C_optimizer') and (l4.get('conformance') or {}).get('C_tube')),'l4_candidate_count':len(l4.get('candidates') or []),'l4_feasible_count':sum(c.get('feasible') is True for c in (l4.get('candidates') or [])),'l4_verified_count':len(l4.get('verified_candidates') or []),'l4_best_candidate_id':best.get('candidate_id'),'l4_candidate_verified':bool(best.get('verified') or best.get('candidate_verified')),'l4_advisory_applied':bool(best),'l4_delta_yaw_rate_rad_s':yaw_adv,'l4_delta_airspeed_m_s':air_adv,'l4_mission_cost':obj.get('mission'),'l4_time_cost':obj.get('time'),'l4_energy_cost':obj.get('energy'),'l4_risk_cost':obj.get('risk'),'l4_uncertainty_cost':obj.get('uncertainty'),'l5_invoked':bool(l5),'l5_mpc_participated':bool((l5.get('provenance') or {}).get('only_first_mpc_move')),'l5_mode':l5.get('operational_mode'),'l5_final_gate':gate,'l5_decision_id':l5d.get('decision_id'),'l5_decision_reason':(l5d.get('approval') or {}).get('reason'),'offline_feedback_status':fb,'l5_feedback_accepted':feedback_accepted,'layer2_control_effect_applied':dynamic_applied,'actuation_semantics':'physical/pilot command only if Layer5 FINAL=true; otherwise no actuator command; simulation_reference actuators are diagnostic only',**ar,'wind_speed_m_s':math.sqrt(sum(v*v for v in wind)),'wind_u_m_s':wind[0],'wind_v_m_s':wind[1],'wind_w_m_s':wind[2],'temperature_c':_num(climate_s.get('temperature_local')),'humidity_pct':_num(climate_s.get('relative_humidity_local')),'pressure_pa':_num(climate_s.get('surface_pressure_pa')),'air_density_kg_m3':rho,'simulation_reference_left_motor_cmd':sim['simulation_reference_actuators']['left_motor_cmd'],'simulation_reference_right_motor_cmd':sim['simulation_reference_actuators']['right_motor_cmd'],'simulation_reference_left_elevon_rad':sim['simulation_reference_actuators']['left_elevon_rad'],'simulation_reference_right_elevon_rad':sim['simulation_reference_actuators']['right_elevon_rad'],'shadow_flight_mode_code':sim['shadow_6dof_op27'].get('flight_mode_code'),'shadow_vtol_feasible':sim['shadow_6dof_op27'].get('vtol_feasible'),'vtol_envelope_valid':sim['envelope_op30'].get('vtol_envelope_valid'),'predicted_power_w':sim['power_op31'].get('selected_power')}
        oat=offline_vehicle['attitude']; osp=offline_vehicle['speed']; oac=offline_vehicle['actuators']
        rowrec.update({'vehicle_roll_deg':oat.get('roll_deg'),'vehicle_pitch_deg':oat.get('pitch_deg'),'vehicle_yaw_deg':oat.get('yaw_deg'),'vehicle_roll_rate_rad_s':oat.get('roll_rate_rad_s'),'vehicle_pitch_rate_rad_s':oat.get('pitch_rate_rad_s'),'vehicle_yaw_rate_rad_s':oat.get('yaw_rate_rad_s'),
                       'vehicle_airspeed_m_s':osp.get('airspeed_m_s'),'vehicle_groundspeed_m_s':osp.get('groundspeed_m_s'),'vehicle_climb_m_s':osp.get('climb_m_s'),'vehicle_heading_deg':osp.get('heading_deg'),
                       'left_elevon_command_rad':oac['left_elevon'].get('command_rad'),'left_elevon_command_deg':oac['left_elevon'].get('command_deg'),'right_elevon_command_rad':oac['right_elevon'].get('command_rad'),'right_elevon_command_deg':oac['right_elevon'].get('command_deg'),
                       'left_motor_command_norm':oac['left_motor'].get('command_norm'),'left_motor_speed_rad_s':oac['left_motor'].get('speed_rad_s'),'left_motor_speed_rpm':oac['left_motor'].get('speed_rpm'),'right_motor_command_norm':oac['right_motor'].get('command_norm'),'right_motor_speed_rad_s':oac['right_motor'].get('speed_rad_s'),'right_motor_speed_rpm':oac['right_motor'].get('speed_rpm'),
                       'vehicle_state_semantics':'offline WingMPET simulation state; Layer5 command authorization remains separately identified by l5_final_gate'})
        # Layer4 trajectories are already expressed in the same absolute projected
        # frame as SARG x_m/y_m.  Adding the route origin a second time corrupts
        # only the quality report (not control) by roughly one UTM coordinate.
        if pred: rowrec['l4_pred_x_m']=float(pred[1]); rowrec['l4_pred_y_m']=float(pred[0]); rowrec['l4_pred_z_like']=float(pred[2])
        for c in l4.get('candidates') or []:
            o=c.get('objective_vector') or {}; crows.append({'cycle':i+1,'reference_time_s':float(req['reference_time_s']),'realized_x_m':s1['x'],'realized_y_m':s1['y'],'candidate_id':c.get('candidate_id'),'scenario_id':c.get('scenario_id'),'verified':bool(c.get('verified') or c.get('candidate_verified')),'pareto_rank':c.get('pareto_rank'),'feasible':c.get('feasible'),'scalar_cost':c.get('scalar_cost'),'mission':o.get('mission'),'time':o.get('time'),'energy':o.get('energy'),'risk':o.get('risk'),'uncertainty':o.get('uncertainty'),'consistency':o.get('consistency'),'first_abstract_action':(c.get('actions') or [None])[0]})
        l3snap=((r.get('layer3_result') or {}).get('snapshot') or {})
        l4cands=l4.get('candidates') or []
        detail={'cycle':i+1,'transaction_id':r.get('transaction_id'),'time_location':{'reference_time_s':float(req['reference_time_s']),'latitude_deg':lat,'longitude_deg':lon,'realized_x_m':s1['x'],'realized_y_m':s1['y'],'altitude_msl_m':s1['h'],'agl_m':agl1},'mission':{'phase':phase,'tdoc_substate':tdoc_substate,'goal_distance_m':goal,'energy_remaining_wh':remaining,'state_source':state_source,'teacher_forced':False,'sarg_used_as_next_state':False},'uav_navigation':(((r.get('layer2_packets') or {}).get('MESA_NAV') or {}).get('state') or {}),'flight_state':(((r.get('layer2_packets') or {}).get('WingMPET') or {}).get('state') or {}),'sensors':{name:{'state':(packet or {}).get('state'),'quality':(packet or {}).get('quality'),'uncertainty':(packet or {}).get('uncertainty'),'validity':(packet or {}).get('validity'),'reliability':(packet or {}).get('reliability'),'provenance':(packet or {}).get('provenance')} for name,packet in (r.get('layer2_packets') or {}).items()},'climate':climate_s,'layer3_return':{'policy_version':l3snap.get('policy_version'),'consistency_margin':l3snap.get('consistency_margin'),'mismatch':l3snap.get('mismatch'),'stability':l3snap.get('stability'),'tdoc_health':l3snap.get('tdoc_health'),'active_edges':((l3snap.get('topology') or {}).get('active_edges'))},'layer4_return':{'candidate_count':len(l4cands),'feasible_count':sum(c.get('feasible') is True for c in l4cands),'verified_count':len(l4.get('verified_candidates') or []),'pareto_count':len(l4.get('pareto_front') or []),'budget':l4.get('budget'),'conformance':l4.get('conformance'),'actuator_command':l4.get('actuator_command'),'network_commit':l4.get('network_commit')},'layer4_selected_candidate':best,'layer5_decision':{'status':((r.get('downstream_layers') or {}).get('5') or {}).get('status'),'operational_mode':l5.get('operational_mode'),'decision':l5.get('decision'),'gates':l5.get('gates'),'fallback':l5.get('fallback'),'actuator_vehicle_state':offline_vehicle,'command_authorized':gate,'control_action':action},'tdoc_layer_status':r.get('layer_status') or {},'offline_plant':sim}
        _boundary=None
        if phase in ('on_ground','touchdown'):
            _terminal_ok=(phase!='touchdown' or goal<=args.goal_tolerance_m)
            _boundary={'supported':bool(agl1<=1.0 and _terminal_ok),
                       'agl_m':float(agl1),'goal_distance_m':float(goal),
                       'source':'offline_realized_passive_ground_contact',
                       'physics_policy':'TDOC-passive-ground-contact-v1',
                       'boundary_phase':phase,
                       'wind_advection_suppressed':True,
                       'ground_velocity_zero':True,
                       'terminal_location_required':bool(phase=='touchdown')}
        capability_registry.record_cycle(phase=phase,cycle_id=i+1,reference_time_s=float(req['reference_time_s']),
            layer4_result=l4,layer5_result=l5,control_effect_applied=dynamic_applied,boundary_evidence=_boundary,context=req.get('context'))
        _joint_now=bool(rowrec.get('l4_candidate_verified') and rowrec.get('l5_final_gate') and rowrec.get('layer2_control_effect_applied'))
        if phase in vertical_joint_proof_seen and _joint_now:
            vertical_joint_proof_seen[phase]=True
        # At the last reference sample of takeoff/hover, hold the same physical phase
        # for a small bounded number of cycles until one genuine joint proof exists.
        # State continues to evolve through the real Layer2/WingMPET path; only the
        # reference cursor is held.  A failed verifier/controller remains fail-closed.
        _proof_dwell_active=False
        if _reference_active:
            _dwell=_vertical_control_proof_dwell_decision(phase=phase,next_phase=_next_reference_phase,joint_proof_seen=vertical_joint_proof_seen.get(phase,False),dwell_used=vertical_proof_dwell_used.get(phase,0),dwell_limit=vertical_proof_dwell_limit)
            if _dwell['hold_reference']:
                vertical_proof_dwell_used[phase]+=1; _proof_dwell_active=True
            else:
                ref_cursor+=1
        rowrec['vertical_control_proof_dwell']=bool(_proof_dwell_active)
        rowrec['vertical_control_joint_proof_seen']=bool(vertical_joint_proof_seen.get(phase,False)) if phase in vertical_joint_proof_seen else None
        rowrec['vertical_control_proof_dwell_used']=int(vertical_proof_dwell_used.get(phase,0)) if phase in vertical_proof_dwell_used else 0
        decision_details.append(detail); rows.append(rowrec); records.append({'transaction_id':r['transaction_id'],'status':r['status'],'knowledge':k,'state_source':state_source,'phase':phase,'tdoc_substate':tdoc_substate,'goal_distance_m':goal,'vertical_control_proof_dwell':bool(_proof_dwell_active)})
        if phase=='touchdown' and goal <= args.goal_tolerance_m and agl1 <= 1.0:
            reached=True; termination_reason='touchdown_goal_reached'; print(f'      TOUCHDOWN / GOAL REACHED — mission complete at cycle {i+1}; safety budget {hardmax}, unused {max(0,hardmax-(i+1))}',flush=True); break
      loops={'fast':len(rows),'meso':sum(bool(x.get('meso_event')) for x in rows),'slow':sum(bool(x.get('slow_event')) for x in rows)}
      verified_candidate_cycles=sum(1 for d in decision_details if int(((d.get('layer4_return') or {}).get('verified_count') or 0))>0)
      final_authorized_cycles=sum(1 for d in decision_details if bool((d.get('layer5_decision') or {}).get('command_authorized')))
      ff_runtime_error_cycles=sum(1 for d in decision_details if ((d.get('layer4_return') or {}).get('conformance') or {}).get('C_FF_runtime_clean') is False)
      mission_valid=bool(reached and teacher_count==0 and len(rows)>0)
      readiness=_offline_control_readiness(rows,route_control_readiness_eligible=bool(choice.get('control_readiness_eligible')),ff_runtime_error_cycles=ff_runtime_error_cycles)
      phase_control=readiness['phase_control_coverage']
      joint_control_proof_cycles=int(readiness['joint_control_proof_cycles'])
      control_ready=bool(readiness['offline_control_ready'])
      knowledge_quality={'safety_hold_cycles':max(0,len(rows)-final_authorized_cycles),'verified_candidate_cycles':verified_candidate_cycles,
                         'final_authorized_cycles':final_authorized_cycles,'closed_loop_effect_cycles':closed_loop_count,
                         'joint_control_proof_cycles':joint_control_proof_cycles,
                         'control_learning_cycles':joint_control_proof_cycles,
                         'non_control_learning_cycles':max(0,len(rows)-joint_control_proof_cycles),
                         'full_fidelity_runtime_error_cycles':ff_runtime_error_cycles,
                         'phase_control_coverage':phase_control,
                         'safety_knowledge_valid':bool(len(rows)>0),'control_knowledge_valid':control_ready,
                         'control_learning_semantics':'only same-cycle L4 verified + L5 FINAL + accepted Layer2 control effect counts; HOLD/state-propagation is excluded'}
      validation={'schema':'TDOC-offline-validation-v1','OFFLINE_MISSION_VALID':mission_valid,'OFFLINE_KNOWLEDGE_VALID':None,'OFFLINE_CONTROL_READY':control_ready,
                  'route_admission_status':choice.get('admission_status'),'route_control_readiness_eligible':bool(choice.get('control_readiness_eligible')),
                  'knowledge_quality':knowledge_quality,'reasons':([] if control_ready else [x for x,ok in [
                    ('route_not_strictly_admitted',bool(choice.get('control_readiness_eligible'))),('full_fidelity_runtime_errors',ff_runtime_error_cycles==0),
                    ('no_verified_candidate_cycle',verified_candidate_cycles>0),('no_layer5_final_cycle',final_authorized_cycles>0),('no_closed_loop_control_effect',closed_loop_count>0),('phase_control_coverage_incomplete',phase_control['all_required_control_phases'])] if not ok])}
      summary={'schema':'TDOC-offline-realized-wingmpet-v5','package':'TDOC_v20260911_2052','mode':'OFFLINE_SE','reference_route':{**{k:(str(v) if isinstance(v,Path) else v) for k,v in choice.items()},'shortlist':[{k:(str(v) if isinstance(v,Path) else v) for k,v in x.items()} for x in shortlist]},'initial_energy_wh':initial_energy,'remaining_energy_wh':remaining,'goal_tolerance_m':args.goal_tolerance_m,'goal_reference_reached':reached,'touchdown_reached':reached,'mission_complete':bool(reached),'offline_control_ready':bool(control_ready),'reference_cycle_count':len(reference),'terminal_reposition_contract':terminal_reposition_contract,'terminal_correction_cycle_budget':int(terminal_budget),'terminal_correction_cycle_budget_floor':int(args.offline_terminal_correction_cycles),'terminal_correction_budget_basis':'route_distance_plus_distance_scheduled_reposition_plus_source_bound_vertical_landing','cycle_budget_max':hardmax,'cycles_completed':len(rows),'unused_cycle_budget':max(0,hardmax-len(rows)),'termination_reason':termination_reason,'cycle_budget_semantics':'cycle_budget_max is a safety upper bound derived from the executable terminal schedule, not a required run length; successful touchdown terminates the mission immediately','loop_counts':loops,'state_sources':{'closed_loop_cycles':closed_loop_count,'teacher_forced_reference_cycles':0,'wingmpet_simulation_cycles':len(rows)-closed_loop_count},'sarg_used_as_next_state':False,'l4_advisory_cycles':l4_advisory_cycles,'knowledge_quality':knowledge_quality,'offline_validation':validation,
               'vertical_control_proof_acquisition':{'schema':'TDOC-offline-vertical-control-proof-acquisition-v1','phases':list(vertical_proof_phases),'joint_proof_seen':dict(vertical_joint_proof_seen),'bounded_dwell_limit_per_reference_boundary':vertical_proof_dwell_limit,'dwell_cycles_used':dict(vertical_proof_dwell_used),'climate_window':vertical_proof_climate_window,'synthetic_capability':False,'synthetic_environment':False,'constraints_relaxed':False,'single_episode_preserved':True},
               'semantic_note':'Mission completion and reusable control knowledge are independent claims. SARG is reference/prior only; WingMPET/HOLD propagation may complete geometry but cannot prove control readiness without same-cycle joint control proof in every required controlled phase.'}
      out=report_root/'offline'/'mission'; mission_reports(out,rows,crows,choice,reference,summary,decision_details)
      if rows:
          postprocess_mission_diagnostics(out)
      return {'mission':summary,'records':records,'report_dir':str(out),'offline_validation':validation,'knowledge_quality':knowledge_quality}
    finally:
      plant.close()

CONTROL_REQUIRED_PHASES = (
    "vertical_takeoff", "hover", "transition_forward", "climb", "cruise",
    "turn", "approach", "transition_back", "vertical_landing",
)
MISSION_REQUIRED_PHASES = (
    "on_ground", "vertical_takeoff", "hover", "transition_forward", "climb", "cruise",
    "turn", "approach", "transition_back", "vertical_landing", "touchdown",
)

def _mission_phase_completion(rows:list[dict])->dict:
    """Strict 11-phase completion: labels alone cannot convert HOLD into capability."""
    phases=list(dict.fromkeys(str(r.get("phase")) for r in rows if r.get("phase") is not None))
    first_agl=(rows[0].get("agl_m") if rows else None)
    starts_on_ground=bool(rows and str(rows[0].get("phase")) == "on_ground" and first_agl is not None and float(first_agl) <= 1.0)
    touchdown_rows=[r for r in rows if str(r.get("phase")) == "touchdown"]
    touchdown_seen=bool(touchdown_rows)
    last=rows[-1] if rows else {}
    _fa=last.get("agl_m"); _fg=last.get("goal_distance_m")
    final_agl=float(_fa) if _fa is not None else 9999.0
    final_goal=float(_fg) if _fg is not None else 1e12
    touchdown_boundary_valid=bool(touchdown_seen and str(last.get("phase")) == "touchdown" and final_agl <= 1.0 and final_goal <= 25.0)
    ctrl=_phase_control_coverage(rows)
    phase_completion={
        "on_ground":{"semantic_role":"passive_boundary","seen":"on_ground" in phases,"completed":starts_on_ground},
        **{p:{"semantic_role":"controlled_phase","seen":p in phases,"completed":bool(ctrl["coverage"][p]["control_capable"]),"joint_proof_cycles":int(ctrl["coverage"][p]["joint_proof_cycles"])} for p in CONTROL_REQUIRED_PHASES},
        "touchdown":{"semantic_role":"passive_boundary","seen":"touchdown" in phases,"completed":touchdown_boundary_valid},
    }
    all_seen=all(p in phases for p in MISSION_REQUIRED_PHASES)
    all_completed=all(bool(phase_completion[p]["completed"]) for p in MISSION_REQUIRED_PHASES)
    return {
        "schema":"TDOC-offline-11phase-completion-v1",
        "phases_seen":phases,
        "starts_on_ground":starts_on_ground,
        "touchdown_seen":touchdown_seen,
        "final_agl_m":final_agl,
        "final_goal_distance_m":final_goal,
        "all_required_phases_seen":all_seen,
        "all_required_control_phases":bool(ctrl["all_required_control_phases"]),
        "missing_control_phases":list(ctrl["missing_control_phases"]),
        "phase_completion":phase_completion,
        "all_required_phases":all_completed,
    }

def _phase_control_coverage(rows:list[dict])->dict:
    """Phase-level proof that control knowledge is reusable, not merely globally successful."""
    coverage={}
    for phase in CONTROL_REQUIRED_PHASES:
        phase_rows=[r for r in rows if str(r.get("phase")) == phase]
        proof=[r for r in phase_rows if bool(r.get("l4_candidate_verified")) and bool(r.get("l5_final_gate")) and bool(r.get("layer2_control_effect_applied"))]
        coverage[phase]={
            "cycles":len(phase_rows),
            "verified_cycles":sum(bool(r.get("l4_candidate_verified")) for r in phase_rows),
            "l5_final_cycles":sum(bool(r.get("l5_final_gate")) for r in phase_rows),
            "control_effect_cycles":sum(bool(r.get("layer2_control_effect_applied")) for r in phase_rows),
            "joint_proof_cycles":len(proof),
            "control_capable":bool(proof),
        }
    missing=[p for p,v in coverage.items() if not v["control_capable"]]
    return {
        "schema":"TDOC-offline-phase-control-coverage-v1",
        "required_control_phases":list(CONTROL_REQUIRED_PHASES),
        "coverage":coverage,
        "missing_control_phases":missing,
        "all_required_control_phases":not missing,
    }


def _offline_control_readiness(rows:list[dict], *, route_control_readiness_eligible:bool, ff_runtime_error_cycles:int)->dict:
    """Compute reusable-control readiness only from same-cycle phase-level joint proof.

    Mission completion is intentionally not an input.  HOLD/reference-only state
    propagation can remain useful safety/diagnostic knowledge, but never increments
    control-learning evidence.
    """
    phase_control=_phase_control_coverage(rows)
    joint=sum(int(v.get("joint_proof_cycles",0) or 0) for v in phase_control["coverage"].values())
    ready=bool(route_control_readiness_eligible and int(ff_runtime_error_cycles or 0)==0 and phase_control["all_required_control_phases"])
    return {
        "schema":"TDOC-offline-control-readiness-v1",
        "offline_control_ready":ready,
        "joint_control_proof_cycles":joint,
        "control_learning_cycles":joint,
        "non_control_learning_cycles":max(0,len(rows)-joint),
        "phase_control_coverage":phase_control,
        "route_control_readiness_eligible":bool(route_control_readiness_eligible),
        "full_fidelity_runtime_error_cycles":int(ff_runtime_error_cycles or 0),
        "mission_completion_used_for_control_readiness":False,
    }

def _l6_output_from_cycle(cycle:dict)->dict:
    d6=((cycle.get('downstream_layers') or {}).get('6') or {})
    if not isinstance(d6,dict): return {}
    out=d6.get('output')
    return out if isinstance(out,dict) else d6

def _l6_evidence_from_cycle(cycle:dict)->dict:
    d6=((cycle.get('downstream_layers') or {}).get('6') or {})
    out=_l6_output_from_cycle(cycle)
    ev=(out.get('execution_evidence') if isinstance(out,dict) else None) or (d6.get('execution_evidence') if isinstance(d6,dict) else None)
    return ev if isinstance(ev,dict) else {}

def _nested_number(obj, keys):
    wanted={str(k).lower() for k in keys}; stack=[obj]; seen=0
    while stack and seen<5000:
        cur=stack.pop(); seen+=1
        if isinstance(cur,dict):
            for k,v in cur.items():
                if str(k).lower() in wanted:
                    n=_num(v,None)
                    if n is not None:return float(n)
                if isinstance(v,(dict,list,tuple)):stack.append(v)
        elif isinstance(cur,(list,tuple)):
            stack.extend(x for x in cur if isinstance(x,(dict,list,tuple)))
    return None

def _nested_text(obj):
    out=[]; stack=[obj]; seen=0
    while stack and seen<2000:
        cur=stack.pop(); seen+=1
        if isinstance(cur,dict):stack.extend(cur.values())
        elif isinstance(cur,(list,tuple)):stack.extend(cur)
        elif isinstance(cur,str):out.append(cur.lower())
    return ' '.join(out)

def _online_position(cycle:dict):
    ev=_l6_evidence_from_cycle(cycle); snap=ev.get('io_snapshot') or {}
    if not isinstance(snap,dict):return None
    lat=_nested_number(snap,('latitude_deg','lat_deg','latitude','lat'))
    lon=_nested_number(snap,('longitude_deg','lon_deg','longitude','lon','lng'))
    if lat is not None and abs(lat)>90:lat/=1e7
    if lon is not None and abs(lon)>180:lon/=1e7
    if lat is None or lon is None or abs(lat)>90 or abs(lon)>180:return None
    alt=_nested_number(snap,('relative_altitude_m','relative_alt_m','agl_m','altitude_agl_m','relative_alt'))
    if alt is not None and abs(alt)>10000:alt/=1000.0
    sim_t=_nested_number(snap,('simulation_time_s','sim_time_s'))
    return {'lat':float(lat),'lon':float(lon),'relative_altitude_m':alt,'simulation_time_s':sim_t,'event_text':_nested_text(snap)}

def _online_battery(cycle:dict):
    ev=_l6_evidence_from_cycle(cycle); snap=ev.get('io_snapshot') or {}
    groups=snap.get('groups') or {}; b=(groups.get('BATTERY') or {}).get('payload') or {}
    if not isinstance(b,dict) or not b:return None
    volts=b.get('voltages') or []; voltage_v=None
    for raw in volts:
        n=_num(raw,None)
        if n is not None and 0<n<65535:
            voltage_v=float(n)/1000.0; break
    current_raw=_num(b.get('current_battery'),None); current_a=(float(current_raw)/100.0 if current_raw is not None and current_raw>=0 else None)
    consumed=_num(b.get('current_consumed'),None); consumed_mah=(float(consumed) if consumed is not None and consumed>=0 else None)
    remaining=_num(b.get('battery_remaining'),None); remaining_pct=(float(remaining) if remaining is not None and remaining>=0 else None)
    return {'voltage_v':voltage_v,'current_a':current_a,'current_consumed_mah':consumed_mah,'battery_remaining_pct':remaining_pct,'source_message':(groups.get('BATTERY') or {}).get('source_message')}

def _haversine_m(lat1,lon1,lat2,lon2):
    r=6371008.8; a1=math.radians(lat1); a2=math.radians(lat2); da=a2-a1; dl=math.radians(lon2-lon1)
    h=math.sin(da/2)**2+math.cos(a1)*math.cos(a2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1.0,math.sqrt(max(0.0,h))))

def _rtmo_operational_position(cycle:dict,destination):
    """Return GNSS-denied operational position from MESA/STD, never JSBSim truth."""
    try:
        mesa=((cycle.get('layer2_packets') or {}).get('MESA_NAV') or {}).get('state') or {}
        n=_num(mesa.get('position_n'),None); e=_num(mesa.get('position_e'),None); d=_num(mesa.get('position_d'),None)
        if n is None or e is None: return None
        from pyproj import Transformer
        lon0=float(destination[0]); lat0=float(destination[1]); zone=int((lon0+180.0)//6.0)+1
        epsg=(32600+zone) if lat0>=0 else (32700+zone)
        tr=Transformer.from_crs(epsg,4326,always_xy=True); lon,lat=tr.transform(float(e),float(n))
        vn=_num(mesa.get('velocity_n'),None); ve=_num(mesa.get('velocity_e'),None); vd=_num(mesa.get('velocity_d'),None)
        vg=(math.hypot(vn,ve) if vn is not None and ve is not None else None)
        return {'lat':float(lat),'lon':float(lon),'altitude_msl_m':(-float(d) if d is not None else None),
                'groundspeed_m_s':vg,'velocity_d_m_s':vd,'authority':'MESA_STD','source':'Layer2/MESA_NAV'}
    except Exception:
        return None

def _rtmo_initial_navigation_state(report_root:Path):
    p=report_root/'offline'/'mission'/'DECISION_POINT_DETAILS.json.gz'
    if not p.is_file(): return None
    try:
        with gzip.open(p,'rt',encoding='utf-8') as f: rows=json.load(f)
        if isinstance(rows,dict): rows=rows.get('records') or rows.get('cycles') or []
        nav=((rows or [])[0].get('uav_navigation') or {})
        _c=math.sqrt(0.5)
        return {'pn':float(nav['position_n']),'pe':float(nav['position_e']),'pd':float(nav['position_d']),
                'vn':0.0,'ve':0.0,'vd':0.0,'qw':_c,'qx':0.0,'qy':_c,'qz':0.0,
                'source':'pinned_offline_on_ground_initial_condition+Phoenix_tailsitter_90deg'}
    except Exception: return None

def _online_terminal(cycle:dict,destination,goal_tolerance_m,touchdown_altitude_m):
    def finite(value):
        return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)
    op=_rtmo_operational_position(cycle,destination)
    if not op:return {'reached':False,'reason':'no_operational_mesa_position','position':None,'goal_distance_m':None,'navigation_authority':'MESA_STD'}
    try:
        geometry=(op['lat'],op['lon'],float(destination[1]),float(destination[0]),float(goal_tolerance_m),float(touchdown_altitude_m))
        if (not all(finite(v) for v in geometry) or not -90<=geometry[0]<=90 or not -90<=geometry[2]<=90
            or not -180<=geometry[1]<=180 or not -180<=geometry[3]<=180 or min(geometry[4:])<0
            or not finite(op.get('altitude_msl_m'))):
            raise ValueError('invalid terminal geometry')
    except (IndexError,KeyError,TypeError,ValueError):
        return {'reached':False,'reason':'invalid_terminal_geometry','position':None,'goal_distance_m':None,'navigation_authority':'MESA_STD'}
    dist=_haversine_m(op['lat'],op['lon'],float(destination[1]),float(destination[0]))
    # Touchdown evidence is execution evidence only; it does not provide navigation.
    simpos=_online_position(cycle) or {}; event=str(simpos.get('event_text') or '').lower()
    landed=any(x in event for x in ('touchdown','landed','on_ground','on ground'))
    alt=simpos.get('relative_altitude_m'); alt_ok=(finite(alt) and abs(alt)<=float(touchdown_altitude_m))
    ev=_l6_evidence_from_cycle(cycle); proof=ev.get('segment_closure_proof')
    native=(ev.get('source')=='Layer6/GUI/MAVLink' or ev.get('schema')=='TDOC-L6-online-native-execution-evidence-v1'
            or isinstance(proof,dict))
    contact=False;contact_reason='legacy_terminal_observation';contact_source=None
    if native:
        # Never let a low altitude or text such as "not landed" prove Native
        # touchdown. Validate the actual observation and its session identities.
        contact_reason='native_touchdown_evidence_unavailable'
        try:
            from layer6.status import validate_observation_status
            from tddt_tdoc.native_control_binding import ground_contact
            current=ev['current_state'];provenance=ev['provenance']
            if proof is not None:
                from layer6.segments import validate_result
                status=proof['snapshot'];context=proof['context'];policy=ev['execution_segment_policy']
                if not validate_result(proof,cycle['transaction_id'],context['instance_id'],policy,
                                       before_session=provenance['session_id']):
                    raise ValueError('invalid Native segment proof')
                contact_source='Layer6/segment_closure_proof/snapshot'
            else:
                mesa=((cycle.get('layer2_packets') or {}).get('MESA_NAV') or {})
                ingress=((mesa.get('context') or {}).get('online_state_ingress') or {}).get('ptlib_ingress') or {}
                status=ingress['gui_state']
                contact_source='Layer6/current_cycle_ingress/gui_state'
            if not validate_observation_status(status)['ok']:
                raise ValueError('stale or invalid Native observation')
            boot=provenance['native_session_id'];session=provenance['session_id']
            if (not isinstance(boot,str) or len(boot)!=32 or any(c not in '0123456789abcdef' for c in boot)
                or boot!=(status.get('status_quality') or {}).get('native_session_id')
                or session!=status['session_id'] or session!=current['session_id']
                or provenance.get('transaction_id')!=cycle.get('transaction_id')
                or not finite(current.get('timestamp')) or not 0<=time.time()-current['timestamp']<=2.
                or not finite(current.get('plant_time_s')) or current['plant_time_s']!=status.get('plant_time_s')):
                raise ValueError('Native touchdown session/time mismatch')
            health=ev.get('native_session_health')
            if health is not None and (health.get('ok') is not True or health.get('session_id')!=boot
                    or not finite(health.get('checked_monotonic_s'))
                    or not 0<=time.monotonic()-health['checked_monotonic_s']<=2.):
                raise ValueError('Native component health stale or mismatched')
            states=[str(obj.get('landed_state') or '').upper() for obj in (status,current,ev.get('x_obs') or {})]
            if any(value in {'IN_AIR','TAKEOFF','LANDING'} for value in states):
                raise ValueError('explicit airborne Native state')
            if status.get('landed_state')=='ON_GROUND':
                if not validate_observation_status(status,required=('EXTENDED_SYS_STATE',))['ok']:
                    raise ValueError('stale or missing Native landed-state channel')
            contact=ground_contact(status)
            contact_reason='fresh_native_ground_contact' if contact else 'native_ground_contact_not_observed'
            alt=status.get('relative_altitude_m');alt_ok=finite(alt) and abs(alt)<=float(touchdown_altitude_m)
        except (KeyError,TypeError,ValueError,AttributeError) as exc:
            contact_reason=str(exc)
        reached=bool(dist<=float(goal_tolerance_m) and contact)
        landed=contact
    else:
        reached=bool(dist<=float(goal_tolerance_m) and (alt_ok or landed))
    pos={**op,'relative_altitude_m':alt,'simulation_time_s':simpos.get('simulation_time_s'),'event_text':simpos.get('event_text')}
    return {'reached':reached,'reason':'touchdown_goal_reached' if reached else 'destination_not_yet_reached','position':pos,
            'goal_distance_m':dist,'altitude_ok':alt_ok,'landed_event':landed,'navigation_authority':'MESA_STD',
            'ground_contact_verified':contact,'touchdown_evidence_reason':contact_reason,'touchdown_evidence_source':contact_source,
            'simulator_truth_used_as_navigation_observation':False}

def _online_vehicle_state(cycle:dict):
    ev=_l6_evidence_from_cycle(cycle); snap=ev.get('io_snapshot') or {}
    ds=(cycle.get('downstream_layers') or {}); l5env=((ds.get('5') or {}).get('output') or {}) if isinstance(ds,dict) else {}
    l5=(l5env.get('result') if isinstance(l5env,dict) and isinstance(l5env.get('result'),dict) else l5env) or {}
    d6=((ds.get('6') or {}).get('output') or {}) if isinstance(ds,dict) else {}
    return phoenix_state_from_ptlib_snapshot(snap,l5,d6) if isinstance(snap,dict) else {}

def _online_progress_diagnostic(cycle:dict,vehicle_state:dict,progress,delta_goal):
    mpc=((cycle.get('provenance') or {}).get('mpc_participation') or {}).get('layer5') or {}
    mode=str(mpc.get('operational_mode') or '')
    reason=str(mpc.get('decision_reason') or '')
    motion=(vehicle_state.get('motion') or {}).get('state')
    blocked=bool(mode=='HOLD' and not mpc.get('actuation_executed'))
    return {'source':'MESA/STD operational state; Layer6 only execution diagnostics','progress_m':_num(progress,None),'delta_goal_m':_num(delta_goal,None),
            'motion_state':motion,'layer5_mode':mode,'layer5_reason':reason,'actuation_executed':bool(mpc.get('actuation_executed')),
            'blocked_by_no_authorized_actuation':blocked,
            'interpretation':('ZERO_PROGRESS_EXPECTED_LAYER5_HOLD_NO_ACTUATION' if blocked and motion=='NO_ACTUATION_STATIONARY' else ('LIVE_MOVEMENT' if motion=='MOVING' else 'NO_TRANSLATION_OBSERVED'))}

def _layer6_was_invoked(layer6_runtime:dict)->bool:
    """Return true only when Layer6 execution/observation was actually entered.

    Placeholder rows such as RTMO WAIT_CAPABILITY and PRE_RELEASE_VERIFY deliberately
    carry a Layer6 status so forensic timelines remain explicit, but they are *not*
    Layer6 invocations and must never make diagnostics claim otherwise.
    """
    if not isinstance(layer6_runtime,dict) or not layer6_runtime:
        return False
    status=str(layer6_runtime.get('status') or '').strip().upper()
    source=str(layer6_runtime.get('source') or '').strip().upper()
    if not status:
        return False
    if status.startswith('NOT_INVOKED') or status in ('NOT_IN_OFFLINE_PATH','NOT_REQUIRED'):
        return False
    if source in ('RTMO_WAIT_CAPABILITY','PRE_RELEASE_VERIFY'):
        return False
    return True

def _resolve_online_termination_reason(terminal:dict, ep_records:list, max_cycles:int)->str:
    """Preserve the first explicit stop cause; use max-cycles only on true exhaustion."""
    terminal = terminal if isinstance(terminal,dict) else {}
    explicit=str(terminal.get('reason') or '').strip()
    # destination_not_yet_reached is a progress observation, not a stop cause.
    if explicit and explicit!='destination_not_yet_reached':
        return explicit
    if ep_records and str((ep_records[-1] or {}).get('status') or '').upper()=='FAILED':
        return 'online_current_state_ingress_failed' if 'ingress' in str(ep_records[-1].get('error') or '') else 'failed'
    if len(ep_records) >= int(max_cycles):
        return 'max_cycles_exhausted'
    return 'stopped_before_cycle_budget_without_terminal_reason'

def _online_record(cycle:dict, *, episode_index=None, cycle_in_episode=None):
    prov=cycle.get('provenance') or {}; ds=(cycle.get('downstream_layers') or {}); d6=ds.get('6') or {}; out6=_l6_output_from_cycle(cycle); ev=_l6_evidence_from_cycle(cycle)
    l6_status=(d6.get('status') if isinstance(d6,dict) else None) or (out6.get('status') if isinstance(out6,dict) else None)
    l7=(ds.get('7') or {}) if isinstance(ds,dict) else {}
    l7_trigger=(l7.get('trigger_eligibility') or (prov.get('layer7_trigger_eligibility') or {})) if isinstance(l7,dict) else {}
    live=bool(ev.get('source')=='Layer6/GUI/MAVLink' and not ev.get('validation_only'))
    timing=(d6.get('output') or {}).get('orchestration_timing') if isinstance(d6.get('output'),dict) else None
    vehicle_state=_online_vehicle_state(cycle)
    current_obs=dict(prov.get('current_online_observation') or {})
    post_obs=dict(prov.get('post_execution_observation') or {})
    ctl=dict(prov.get('rtmo_control_transaction') or {}); ctl_sample=dict(ctl.get('sample') or {}); ctl_summary=dict(ctl.get('summary') or {})
    return {'transaction_id':cycle.get('transaction_id'),'status':cycle.get('status'),'episode_index':episode_index,'cycle_in_episode':cycle_in_episode,
            'position':_online_position(cycle),'battery':_online_battery(cycle),'vehicle_state':vehicle_state,'layer6_elapsed_s':_num((timing or {}).get('elapsed_s'),None),
            'control_transaction_latency_s':_num(ctl_sample.get('latency_s'),None),'control_deadline_ok':ctl_sample.get('deadline_ok'),
            'control_latency_summary':ctl_summary,'online_persistence':prov.get('online_persistence'),
            'current_online_observation':current_obs or None,'post_execution_observation':post_obs or None,
            'current_online_observation_present':bool(current_obs.get('plant_time_present')),
            'current_online_observation_plant_time_s':_num(current_obs.get('plant_time_s'),None),
            'current_online_observation_execution_correlated':bool(current_obs.get('execution_correlated')),
            'current_online_observation_fresh_for_control_state':bool(current_obs.get('fresh_for_control_state')),
            'post_execution_observation_present':bool(post_obs.get('plant_time_present')),
            'post_execution_observation_plant_time_s':_num(post_obs.get('plant_time_s'),None),
            'conformance':cycle.get('conformance'),'knowledge':prov.get('knowledge_ledger'),'offline_knowledge_handoff':prov.get('offline_knowledge_handoff'),'mpc_participation':prov.get('mpc_participation'),'decision_gate_trace':prov.get('decision_gate_trace'),
            'downstream':{k:(v or {}).get('status') for k,v in ds.items()},
            'layer6_runtime':{'status':l6_status,'reason':out6.get('reason') if isinstance(out6,dict) else None,'source':ev.get('source'),'pipeline':ev.get('pipeline'),
                              'prepare':ev.get('prepare'),'connect':ev.get('connect'),'session':ev.get('session'),'validation_only':bool(ev.get('validation_only')),
                              'live_runtime_evidence':live,'io_ready':((ev.get('io_snapshot') or {}).get('ready') if isinstance(ev.get('io_snapshot'),dict) else None)},
            'layer7_status':l7.get('status') if isinstance(l7,dict) else None,
            'layer7_reason':l7.get('reason') if isinstance(l7,dict) else None,
            'layer7_consequence_only':bool(l7.get('consequence_only')) if isinstance(l7,dict) else False,
            'layer7_root_cause':bool(l7.get('root_cause')) if isinstance(l7,dict) else False,
            'layer7_trigger_eligible':bool((l7_trigger or {}).get('eligible')),
            'layer7_upstream_execution_eligible':bool((l7_trigger or {}).get('upstream_eligible'))}

def _resolve_online_episode_cycle_cap(cfg,args,report_root,origin,destination):
    explicit=getattr(args,'online_max_cycles_per_episode',None)
    if not getattr(args,'online_max_cycles_per_episode_auto',False):
        cap=int(explicit if explicit is not None else cfg.get('layers.online_training.default_max_cycles_per_episode',320) or 320)
        return cap,{'mode':'explicit_or_default','resolved_cycles':cap,'source':'CLI' if explicit is not None else 'config_default'}
    ac=cfg.get('layers.online_training.auto_cycle_budget',{}) or {}
    margin=max(1.0,float(ac.get('margin_factor',1.20) or 1.20)); reserve=max(0,int(ac.get('reserve_cycles',20) or 0))
    minimum=max(1,int(ac.get('minimum_cycles',60) or 60)); maximum=max(minimum,int(ac.get('maximum_cycles',3600) or 3600))
    offline=report_root/'offline'/'mission'/'MISSION_SUMMARY.json'; source='route_distance_fallback'; base=None; detail={}
    if bool(ac.get('offline_budget_preferred',True)) and offline.is_file():
        try:
            d=json.loads(offline.read_text()); completed=int(d.get('cycles_completed') or 0); reference=int(d.get('reference_cycle_count') or 0); safety=int(d.get('cycle_budget_max') or 0)
            learned=max(completed,reference,1); derived=int(math.ceil(learned*margin))+reserve
            base=max(safety,derived) if safety>0 else derived; source='offline_mission_baseline'
            detail={'offline_cycles_completed':completed,'offline_reference_cycles':reference,'offline_safety_budget':safety,'derived_with_margin':derived,'offline_summary':str(offline)}
            # Counts from the 1-Hz Offline clock are not 10-Hz Online counts.
            # Convert an evidenced elapsed duration, retaining the configured cap.
            duration=float(d.get('flight_duration_s') or 0.0)
            if completed>1 and math.isfinite(duration) and duration>0:
                offline_dt=duration/(completed-1)
                online_dt=float(cfg.get('tdoc.rtmo.realtime.max_control_period_s',0.1) if cfg.get('tdoc.rtmo.enabled',True) else cfg.get('scheduler.fast_dt_s',1.0))
                if not math.isfinite(online_dt) or online_dt<=0:
                    raise ValueError('Invalid Online control period for cycle budget')
                learned_duration=max(learned,safety)*offline_dt
                base=int(math.ceil(learned_duration*margin/online_dt))+reserve
                source='offline_elapsed_time_rescaled'
                detail.update({'offline_flight_duration_s':duration,'offline_step_estimate_s':offline_dt,
                    'online_control_period_s':online_dt,'time_scaled_required_cycles':base,
                    'budget_capped_below_time_scaled_requirement':base>maximum,
                    'mission_completion_authority':False})
        except Exception as exc: detail={'offline_summary_error':repr(exc)}
    if base is None:
        dt=max(1e-6,float(cfg.get('tdoc.rtmo.realtime.max_control_period_s',0.1) if cfg.get('tdoc.rtmo.enabled',True) else cfg.get('scheduler.fast_dt_s',1.0))); speed=max(0.1,float(ac.get('fallback_nominal_ground_speed_m_s',15.0) or 15.0))
        direct=_haversine_m(float(origin[1]),float(origin[0]),float(destination[1]),float(destination[0]))
        nominal=direct/speed/dt; base=int(math.ceil(nominal*margin))+reserve
        detail.update({'direct_distance_m':direct,'nominal_ground_speed_m_s':speed,'fast_dt_s':dt,'nominal_cycles':nominal})
    cap=max(minimum,min(maximum,int(base)))
    return cap,{'mode':'auto','resolved_cycles':cap,'source':source,'margin_factor':margin,'reserve_cycles':reserve,'minimum_cycles':minimum,'maximum_cycles':maximum,**detail}

def _write_online_performance_report(records,episodes,report_root,origin,destination,cfg=None):
    import pandas as pd
    mode_dir=report_root/'online'; mode_dir.mkdir(parents=True,exist_ok=True)
    direct=_haversine_m(float(origin[1]),float(origin[0]),float(destination[1]),float(destination[0]))
    rows=[]; path_m=0.0; prev=None; first_goal=None; final_goal=None
    first_sim=None; last_sim=None; voltages=[]; first_mah=None; last_mah=None; cumulative_wall=0.0
    for i,r in enumerate(records,1):
        pos=r.get('position') or {}; terminal=r.get('terminal') or {}; bat=r.get('battery') or {}
        if pos and prev:path_m += _haversine_m(float(prev['lat']),float(prev['lon']),float(pos['lat']),float(pos['lon']))
        if pos:prev=pos
        goal=_num(terminal.get('goal_distance_m'),None)
        if goal is None and pos: goal=_haversine_m(float(pos['lat']),float(pos['lon']),float(destination[1]),float(destination[0]))
        if first_goal is None and goal is not None:first_goal=goal
        if goal is not None:final_goal=goal
        sim=_num(pos.get('simulation_time_s'),None) if pos else None
        if sim is not None:
            if first_sim is None:first_sim=sim
            last_sim=sim
        vv=_num(bat.get('voltage_v'),None)
        if vv is not None:voltages.append(vv)
        mah=_num(bat.get('current_consumed_mah'),None)
        if mah is not None:
            if first_mah is None:first_mah=mah
            last_mah=mah
        wall=_num(r.get('wall_cycle_s'),0.0) or 0.0; cumulative_wall+=wall
        vs=r.get('vehicle_state') or {}; at=vs.get('attitude') or {}; ss=vs.get('speed') or {}; aa=vs.get('actuators') or {}; le=aa.get('left_elevon') or {}; re=aa.get('right_elevon') or {}; lm=aa.get('left_motor') or {}; rm=aa.get('right_motor') or {}
        rows.append({'global_cycle':i,'episode_index':r.get('episode_index'),'cycle_in_episode':r.get('cycle_in_episode'),'lat':pos.get('lat'),'lon':pos.get('lon'),'relative_altitude_m':pos.get('relative_altitude_m'),'simulation_time_s':sim,'goal_distance_m':goal,'cumulative_path_m':path_m,'cycle_wall_s':wall,'layer6_elapsed_s':r.get('layer6_elapsed_s'),'battery_voltage_v':vv,'battery_current_a':bat.get('current_a'),'battery_consumed_mah':mah,'battery_remaining_pct':bat.get('battery_remaining_pct'),
                     'control_transaction_latency_s':r.get('control_transaction_latency_s'),'control_deadline_ok':r.get('control_deadline_ok'),
                     'motion_state':(vs.get('motion') or {}).get('state'),'roll_deg':at.get('roll_deg'),'pitch_deg':at.get('pitch_deg'),'yaw_deg':at.get('yaw_deg'),'roll_rate_rad_s':at.get('roll_rate_rad_s'),'pitch_rate_rad_s':at.get('pitch_rate_rad_s'),'yaw_rate_rad_s':at.get('yaw_rate_rad_s'),
                     'airspeed_m_s':ss.get('airspeed_m_s'),'groundspeed_m_s':ss.get('groundspeed_m_s'),'climb_m_s':ss.get('climb_m_s'),'heading_deg':ss.get('heading_deg'),
                     'servo1_left_elevon_pwm_us':le.get('pwm_us'),'left_elevon_estimated_deg':le.get('estimated_angle_deg'),'servo2_right_elevon_pwm_us':re.get('pwm_us'),'right_elevon_estimated_deg':re.get('estimated_angle_deg'),
                     'servo3_left_motor_pwm_us':lm.get('pwm_us'),'left_motor_estimated_rad_s':lm.get('estimated_speed_rad_s'),'left_motor_estimated_rpm':lm.get('estimated_speed_rpm'),'servo4_right_motor_pwm_us':rm.get('pwm_us'),'right_motor_estimated_rad_s':rm.get('estimated_speed_rad_s'),'right_motor_estimated_rpm':rm.get('estimated_speed_rpm'),
                     'actuator_angle_measured':bool(le.get('angle_measured') or re.get('angle_measured')),'motor_speed_measured':bool(lm.get('speed_measured') or rm.get('speed_measured'))})
    sim_elapsed=(last_sim-first_sim) if first_sim is not None and last_sim is not None else None
    cycle_lat=[float(_num(r.get('wall_cycle_s'),0.0) or 0.0) for r in records if _num(r.get('wall_cycle_s'),None) is not None]
    l6_lat=[float(_num(r.get('layer6_elapsed_s'),0.0) or 0.0) for r in records if _num(r.get('layer6_elapsed_s'),None) is not None]
    control_lat=[float(_num(r.get('control_transaction_latency_s'),0.0) or 0.0) for r in records if _num(r.get('control_transaction_latency_s'),None) is not None]
    def pct(vals,q):
        if not vals:return None
        x=sorted(vals); pos=(len(x)-1)*q; lo=int(math.floor(pos)); hi=int(math.ceil(pos)); return x[lo] if lo==hi else x[lo]+(x[hi]-x[lo])*(pos-lo)
    fast_dt=float(cfg.get('tdoc.rtmo.realtime.max_control_period_s',0.1) or 0.1) if cfg is not None else None
    progress=(first_goal-final_goal) if first_goal is not None and final_goal is not None else None
    progress_ratio=(progress/direct if progress is not None and direct>0 else None)
    avg_v=(sum(voltages)/len(voltages) if voltages else None); delta_mah=(last_mah-first_mah if first_mah is not None and last_mah is not None else None)
    energy_wh=(max(0.0,delta_mah)/1000.0*avg_v if delta_mah is not None and avg_v is not None else None)
    reached=bool(episodes and episodes[-1].get('terminal_reached'))
    wh_per_km=(energy_wh/(path_m/1000.0) if energy_wh is not None and path_m>1.0 else None)
    distance_eff=(direct/path_m if reached and path_m>0 else None)
    useful_progress_per_wh=(max(0.0,progress)/energy_wh if energy_wh and progress is not None and energy_wh>0 else None)
    avg_speed=(path_m/sim_elapsed if sim_elapsed and sim_elapsed>0 else None)
    from collections import Counter
    motion_counts=Counter(str(((r.get('vehicle_state') or {}).get('motion') or {}).get('state') or 'UNKNOWN') for r in records)
    hold_no_actuation=sum(1 for r in records if ((r.get('progress_diagnostic') or {}).get('blocked_by_no_authorized_actuation') is True))
    min_claim_samples=int(cfg.get('tdoc.rtmo.realtime.min_executed_samples_for_p99_claim',1000) or 1000) if cfg is not None else 1000
    control_p99=pct(control_lat,0.99); control_misses=sum(1 for x in control_lat if fast_dt is not None and x>fast_dt)
    control_metrics={'schema':'TDOC-online-realtime-control-metrics-v1','measurement':'Layer3 atomic snapshot -> Layer6/GUI/MAVLink execution acknowledgement',
                     'sample_count':len(control_lat),'deadline_s':fast_dt,'p50_s':pct(control_lat,0.50),'p95_s':pct(control_lat,0.95),'p99_s':control_p99,
                     'max_s':max(control_lat) if control_lat else None,'deadline_miss_count':control_misses,
                     'min_samples_for_experimental_claim':min_claim_samples,
                     'experimentally_verified_p99_le_100ms':bool(len(control_lat)>=min_claim_samples and control_p99 is not None and fast_dt is not None and control_p99<=fast_dt),
                     'claim_status':('VERIFIED' if len(control_lat)>=min_claim_samples and control_p99 is not None and fast_dt is not None and control_p99<=fast_dt else ('INSUFFICIENT_EXECUTED_SAMPLES' if len(control_lat)<min_claim_samples else 'P99_DEADLINE_NOT_MET')),
                     'heavy_l4_in_fast_path':False,'post_ack_persistence_in_hot_path':False}
    (mode_dir/'ONLINE_REALTIME_CONTROL_METRICS.json').write_text(json.dumps(control_metrics,indent=2,ensure_ascii=False))
    perf={'schema':'TDOC-online-flight-performance-v2','status':'COMPLETE' if reached else 'INCOMPLETE','mission':{'origin_lon_lat':[float(origin[0]),float(origin[1])],'destination_lon_lat':[float(destination[0]),float(destination[1])],'direct_distance_m':direct,'destination_reached':reached,'episodes':episodes},'distance':{'actual_path_m':path_m,'initial_goal_distance_m':first_goal,'final_goal_distance_m':final_goal,'progress_m':progress,'progress_ratio':progress_ratio,'distance_efficiency_direct_over_actual':distance_eff},'time':{'wall_elapsed_s':cumulative_wall,'simulator_elapsed_s':sim_elapsed,'average_ground_speed_m_s':avg_speed,'real_time_factor':(sim_elapsed/cumulative_wall if sim_elapsed is not None and cumulative_wall>0 else None),'cycle_latency_s':{'p50':pct(cycle_lat,0.50),'p95':pct(cycle_lat,0.95),'p99':pct(cycle_lat,0.99),'max':max(cycle_lat) if cycle_lat else None},'layer6_latency_s':{'p50':pct(l6_lat,0.50),'p95':pct(l6_lat,0.95),'p99':pct(l6_lat,0.99),'max':max(l6_lat) if l6_lat else None},'control_transaction':control_metrics,'fast_deadline_s':fast_dt,'wall_cycle_deadline_misses':sum(1 for x in cycle_lat if fast_dt is not None and x>fast_dt)},'energy':{'source':'MAVLink BATTERY_STATUS current_consumed + measured voltage','initial_consumed_mah':first_mah,'final_consumed_mah':last_mah,'delta_consumed_mah':delta_mah,'average_voltage_v':avg_v,'estimated_energy_used_wh':energy_wh,'energy_efficiency_wh_per_km':wh_per_km,'useful_progress_m_per_wh':useful_progress_per_wh},'actuator_vehicle_state':{'motion_state_counts':dict(motion_counts),'layer5_hold_no_authorized_actuation_cycles':hold_no_actuation,'servo_feedback_semantics':'SERVO_OUTPUT_RAW is observed ArduPilot output; elevon angle and motor speed estimates are derived and marked non-measured'},'interpretation':{'energy_efficiency_valid':bool(energy_wh is not None and path_m>1.0),'distance_efficiency_valid':bool(reached and path_m>0),'note':'Efficiency ratios that require motion or destination completion remain null rather than fabricating a value. Zero progress is retained when live position is stationary and its authorization/motion cause is reported separately.'}}
    (mode_dir/'ONLINE_FLIGHT_PERFORMANCE.json').write_text(json.dumps(perf,indent=2,ensure_ascii=False))
    pd.DataFrame(rows).to_csv(mode_dir/'ONLINE_FLIGHT_PERFORMANCE_TIMESERIES.csv',index=False)
    actuator_cols=['global_cycle','episode_index','cycle_in_episode','motion_state','roll_deg','pitch_deg','yaw_deg','roll_rate_rad_s','pitch_rate_rad_s','yaw_rate_rad_s','airspeed_m_s','groundspeed_m_s','climb_m_s','heading_deg','servo1_left_elevon_pwm_us','left_elevon_estimated_deg','servo2_right_elevon_pwm_us','right_elevon_estimated_deg','servo3_left_motor_pwm_us','left_motor_estimated_rad_s','left_motor_estimated_rpm','servo4_right_motor_pwm_us','right_motor_estimated_rad_s','right_motor_estimated_rpm','actuator_angle_measured','motor_speed_measured']
    pd.DataFrame(rows)[[c for c in actuator_cols if c in pd.DataFrame(rows).columns]].to_csv(mode_dir/'ONLINE_DECISION_ACTUATOR_TIMESERIES.csv',index=False)
    def fmt(v,unit=''):
        if v is None:return 'N/A'
        try:return f'{float(v):.3f}{unit}'
        except Exception:return html.escape(str(v))
    cards=[('Status',perf['status']),('Progress',fmt(progress,' m')),('Path',fmt(path_m,' m')),('Goal remaining',fmt(final_goal,' m')),('Simulator time',fmt(sim_elapsed,' s')),('Wall time',fmt(cumulative_wall,' s')),('Energy used',fmt(energy_wh,' Wh')),('Wh/km',fmt(wh_per_km,' Wh/km')),('Control P99',fmt(control_metrics.get('p99_s'),' s')),('Control samples',control_metrics.get('sample_count')),('Control claim',control_metrics.get('claim_status')),('Cycle p95',fmt(perf['time']['cycle_latency_s']['p95'],' s')),('L6 p95',fmt(perf['time']['layer6_latency_s']['p95'],' s'))]
    body=''.join(f'<div class="card"><b>{html.escape(k)}</b><span>{html.escape(str(v))}</span></div>' for k,v in cards)
    doc=f'''<!doctype html><meta charset="utf-8"><title>TDOC Online Flight Performance</title><style>body{{font:14px system-ui;margin:24px;max-width:1100px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card{{border:1px solid #ccc;border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px}}.card span{{font-size:20px}}table{{border-collapse:collapse;width:100%;margin-top:24px}}td,th{{border:1px solid #ddd;padding:8px;text-align:left}}</style><h1>Online Flight Performance</h1><p>Measured from live Layer6/GUI/MAVLink evidence. Missing metrics remain N/A.</p><div class="grid">{body}</div><table><tr><th>Metric</th><th>Value</th></tr><tr><td>Direct origin→destination distance</td><td>{fmt(direct,' m')}</td></tr><tr><td>Mission progress ratio</td><td>{fmt(progress_ratio)}</td></tr><tr><td>Real-time factor (sim/wall)</td><td>{fmt(perf['time']['real_time_factor'])}</td></tr><tr><td>Average ground speed</td><td>{fmt(avg_speed,' m/s')}</td></tr><tr><td>Distance efficiency</td><td>{fmt(distance_eff)}</td></tr><tr><td>Useful progress / energy</td><td>{fmt(useful_progress_per_wh,' m/Wh')}</td></tr></table>'''
    (mode_dir/'ONLINE_FLIGHT_PERFORMANCE.html').write_text(doc)
    return perf

def _write_online_reports(orch,cfg,records,count,args,report_root, *, prefix='workflow-online_jsbsim-', episodes=None):
    import pandas as pd
    mode_dir=report_root/'online'; mode_dir.mkdir(parents=True,exist_ok=True); artifacts=None; report_error=None; rs={}
    # A held pre-release attempt has no operational transaction.  An empty
    # selection must never expand to the entire (potentially huge) Offline store.
    txids=[str(x['transaction_id']) for x in records if x.get('transaction_id')]
    if txids:
        try:
            rs,art=TDOCReportGenerator(cfg).generate(transaction_ids=txids,output_dir=mode_dir/'visualization',terrain_grid_n=args.terrain_grid_n); artifacts=art.to_dict()
        except Exception as exc:report_error=repr(exc)
    elif records:
        rs={'status':'NO_OPERATIONAL_TRANSACTIONS','cycle_count':0,
            'reason':'pre-release/failed attempts retained in numerical diagnostics; no unfiltered historical report'}
    knowledge=[x.get('knowledge') or {} for x in records]; handoffs=[x.get('offline_knowledge_handoff') or {} for x in records]
    l6_rows=[x.get('layer6_runtime') or {} for x in records]
    ptlib_observation=[x for x in l6_rows if x.get('source')=='Layer6/GUI/MAVLink']
    live_pt=[x for x in ptlib_observation if not x.get('validation_only')]
    l6_good=[x for x in l6_rows if x.get('status') in ('EXECUTED','OBSERVATION_ONLY')]
    l7_good=[x for x in records if x.get('layer7_status')=='OK']
    l7_expected=[x for x in records if x.get('layer7_upstream_execution_eligible') is True]
    l7_upstream_consequence=[x for x in records if x.get('layer7_consequence_only') is True]
    l7_own_blocker=[x for x in records if x.get('layer7_root_cause') is True]
    current_observations=[x for x in records if x.get('current_online_observation_present') is True]
    execution_correlated_observations=[x for x in current_observations if x.get('current_online_observation_execution_correlated') is True]
    fresh_control_observations=[x for x in current_observations if x.get('current_online_observation_fresh_for_control_state') is True]
    post_execution_observations=[x for x in records if x.get('post_execution_observation_present') is True]
    _obs_times=[_num(x.get('current_online_observation_plant_time_s'),None) for x in current_observations]
    _obs_times=[float(x) for x in _obs_times if x is not None]
    observation_plant_time_monotonic=all(_obs_times[i]+1e-9>=_obs_times[i-1] for i in range(1,len(_obs_times)))
    validation_only=bool(records) and all(bool(x.get('validation_only')) for x in l6_good) if l6_good else False
    requested=bool(args.online_training)
    observation_only=bool(getattr(args,'online_observation_only',False))
    training_intent='OBSERVATION_ONLY_VALIDATION' if observation_only else 'CONTROL_TRAINING'
    layer7_trigger_coverage_ok=bool(len(l7_good)==len(l7_expected))
    strict_cycle_coverage=bool(records) and len(records)==int(count) and len(l6_good)==len(records) and layer7_trigger_coverage_ok
    live_cycle_coverage=bool(records) and len(ptlib_observation)==len(records) if observation_only else (bool(records) and len(live_pt)==len(records))
    episode_contract=True
    if episodes is not None:
        episode_contract=(bool(episodes) and len(episodes)==int(args.online_episodes)) if observation_only else (bool(episodes) and all(bool(e.get('terminal_reached')) for e in episodes) and len(episodes)==int(args.online_episodes))
    online_contract={'requested':requested,'offline_knowledge_loaded_all':bool(records) and all(h.get('ok') for h in handoffs) if requested else None,
                     'setd_package_ids':sorted({str(h.get('setd_package_id')) for h in handoffs if h.get('setd_package_id')}),
                     'cycles_requested':int(count),'cycles_completed':len(records),'layer6_good_cycles':len(l6_good),'layer7_ok_cycles':len(l7_good),'layer7_expected_cycles':len(l7_expected),
                     'ptlib_layer6_participated_any':bool(ptlib_observation),'layer7_participated_any':bool(l7_good),
                     'live_ptlib_cycles':len(live_pt),'ptlib_observation_cycles':len(ptlib_observation),
                     'current_online_observation_cycles':len(current_observations),
                     'execution_correlated_observation_cycles':len(execution_correlated_observations),
                     'fresh_control_observation_cycles':len(fresh_control_observations),
                     'post_execution_observation_cycles':len(post_execution_observations),
                     'current_observation_plant_time_monotonic':observation_plant_time_monotonic,
                     'current_observation_coverage_ratio':len(current_observations)/len(records) if records else 0.0,
                     'runtime_coverage_ratio':len(ptlib_observation)/len(records) if records else 0.0,
                     'layer7_coverage_ratio':len(l7_good)/len(records) if records else 0.0,'layer7_trigger_coverage_ratio':(len(l7_good)/len(l7_expected) if l7_expected else 1.0),'layer7_trigger_coverage_ok':layer7_trigger_coverage_ok,'strict_cycle_coverage':strict_cycle_coverage,
                     'episode_contract_ok':episode_contract if episodes is not None else None,'validation_only':validation_only,'training_intent':training_intent,'mission_progress_required':not observation_only}
    online_contract['contract_ok']=True if not requested else bool(online_contract['offline_knowledge_loaded_all'] and strict_cycle_coverage and live_cycle_coverage and episode_contract and (observation_only or not validation_only))
    online_contract['validation_contract_ok']=bool(requested and online_contract['offline_knowledge_loaded_all'] and strict_cycle_coverage and validation_only)
    l6_invoked_rows=[x for x in l6_rows if _layer6_was_invoked(x)]
    episode_termination_reasons=[str(e.get('termination_reason')) for e in (episodes or []) if e.get('termination_reason')]
    _tax_path=mode_dir/'ONLINE_READINESS_TAXONOMY.json'
    readiness_taxonomy=None
    if _tax_path.is_file():
        try: readiness_taxonomy=json.loads(_tax_path.read_text())
        except Exception: readiness_taxonomy=None
    diagnostics={'stage':'ONLINE_RUNTIME' if records else 'ONLINE_NOT_STARTED','cycles_requested':int(count),'cycles_completed':len(records),
                 'ptlib_invoked':bool(ptlib_observation),'layer6_invoked':bool(l6_invoked_rows),'layer6_invocation_count':len(l6_invoked_rows),'layer6_statuses':[x.get('status') for x in l6_rows],
                 'termination_reasons':episode_termination_reasons,'last_termination_reason':(episode_termination_reasons[-1] if episode_termination_reasons else None),
                 'layer7_invoked':bool(l7_good),'layer7_ok_cycles':len(l7_good),'layer7_upstream_consequence_cycles':len(l7_upstream_consequence),'layer7_own_blocker_cycles':len(l7_own_blocker),'layer7_initial_root_cause':bool(l7_own_blocker),'live_runtime_evidence_cycles':len(live_pt),
                 'ptlib_observation_cycles':len(ptlib_observation),'ardupilot_jsbsim_runtime_claimed':bool(ptlib_observation),
                 'current_online_observation_cycles':len(current_observations),'execution_correlated_observation_cycles':len(execution_correlated_observations),
                 'fresh_control_observation_cycles':len(fresh_control_observations),'post_execution_observation_cycles':len(post_execution_observations),
                 'current_observation_plant_time_monotonic':observation_plant_time_monotonic,
                 'current_observation_semantics':'Layer6/GUI/MAVLink current state visibility is separate from Layer6 execution evidence and never MESA navigation authority',
                 'held_cycles':sum(1 for x in l6_rows if x.get('status')=='HELD'),
                 'failed_cycles':sum(1 for x in records if x.get('status')=='FAILED'),'episodes':episodes,'training_intent':training_intent,
                 'online_control_readiness':getattr(args,'online_control_readiness',None),
                 'readiness_taxonomy':readiness_taxonomy,
                 'note':'CONTROL_TRAINING requires a validated Phoenix actuation bridge before cycle 1. Infrastructure readiness is not current-phase release readiness and is not mission certification. OBSERVATION_ONLY_VALIDATION can exercise Layer6/Layer7 without claiming mission progress or actuation.'}
    rows=[]
    for i,x in enumerate(records,1):
        l6=x.get('layer6_runtime') or {}; h=x.get('offline_knowledge_handoff') or {}
        _rt=dict(x.get('readiness_taxonomy') or {})
        _infra=dict(_rt.get('infrastructure') or {}); _cur=dict(_rt.get('current_phase_release') or {}); _mission=dict(_rt.get('mission_certification') or {})
        rows.append({'global_cycle':i,'episode_index':x.get('episode_index'),'cycle_in_episode':x.get('cycle_in_episode'),'transaction_id':x.get('transaction_id'),'tdoc_status':x.get('status'),
                     'infrastructure_status':_infra.get('status'),'current_phase_release_status':_cur.get('status'),'mission_certification_status':_mission.get('status'),
                     'layer6_status':l6.get('status'),'layer6_source':l6.get('source'),'live_runtime_evidence':l6.get('live_runtime_evidence'),'io_ready':l6.get('io_ready'),
                     'current_online_observation_present':x.get('current_online_observation_present'),'current_online_observation_plant_time_s':x.get('current_online_observation_plant_time_s'),
                     'current_online_observation_execution_correlated':x.get('current_online_observation_execution_correlated'),'current_online_observation_fresh_for_control_state':x.get('current_online_observation_fresh_for_control_state'),
                     'post_execution_observation_present':x.get('post_execution_observation_present'),'post_execution_observation_plant_time_s':x.get('post_execution_observation_plant_time_s'),
                     'layer7_status':x.get('layer7_status'),'layer7_reason':x.get('layer7_reason'),'layer7_consequence_only':x.get('layer7_consequence_only'),'layer7_root_cause':x.get('layer7_root_cause'),'offline_setd_ok':h.get('ok'),'setd_package_id':h.get('setd_package_id')})
    pd.DataFrame(rows).to_csv(mode_dir/'ONLINE_CYCLE_TIMELINE.csv',index=False)
    performance=None
    try:
        region_file=ROOT/'region'/'region_v1.geojson'; _,origin,destination=load_region(region_file)
        performance=_write_online_performance_report(records,episodes,report_root,origin,destination,cfg)
    except Exception as exc:
        diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
        diagnostics['performance_report_error']=repr(exc)
    m={'cycle_count_requested':int(count),'cycle_count_completed':len(records),'records':records,
       'episodes':episodes,'knowledge_coverage':{'knowledge_target_reached_any':any(k.get('knowledge_target_reached') for k in knowledge),'observation_only_seen':any(k.get('knowledge_completion_kind')=='observation_only' for k in knowledge),'actuation_executed_any':any(k.get('actuation_executed') is True for k in knowledge)},
       'online_training_contract':online_contract,'orchestration_diagnostics':diagnostics,'readiness_taxonomy':readiness_taxonomy,
       'flight_performance':performance,'visualization_summary':rs,'artifacts':artifacts,'report_error':report_error}
    (mode_dir/'ONLINE_JSBSIM_SUMMARY.json').write_text(json.dumps(m,indent=2,ensure_ascii=False)); (mode_dir/'ONLINE_ORCHESTRATION_DIAGNOSTICS.json').write_text(json.dumps(diagnostics,indent=2,ensure_ascii=False)); return m

def run_cycle_mode(orch,cfg,req0,mode,count,args,report_root,online_forcing=None,online_climate_alignment=None):
    records=[]; print(f"\n=== {mode}: {count} cycle(s) ===",flush=True)
    prefix=f"workflow-{mode.lower()}-"
    for i in range(count):
        req=copy.deepcopy(req0); req['learning_mode']=mode
        if mode=='ONLINE_JSBSIM' and args.online_training:
            req['online_training']=True; req['require_offline_knowledge']=True; req['online_observation_only']=bool(args.online_observation_only)
            if args.offline_knowledge_manifest:req['offline_knowledge_manifest']=args.offline_knowledge_manifest
        if mode=='ONLINE_JSBSIM' and args.online_validation_fixture:req['layer6_validation_observation_fixture']=json.loads(Path(args.online_validation_fixture).read_text())
        dt=float(cfg.get('scheduler.fast_dt_s',1.0)); req['reference_time_s']=float(req0.get('reference_time_s',0.0))+i*dt
        if mode=='ONLINE_JSBSIM':_apply_online_climate_time(req,online_forcing,i*dt,online_climate_alignment)
        req['transaction_id']=f"{prefix}{i+1:04d}"
        wall0=time.perf_counter()
        try:
            r=orch.run_cycle(req).to_dict(); rec=_online_record(r,cycle_in_episode=i+1) if mode=='ONLINE_JSBSIM' else {'transaction_id':r['transaction_id'],'status':r['status']}
            rec['wall_cycle_s']=time.perf_counter()-wall0
            records.append(rec)
            if mode=='ONLINE_JSBSIM':
                h=rec.get('offline_knowledge_handoff') or {}; l6=rec.get('layer6_runtime') or {}
                print(f"[{i+1:04d}/{count:04d}] {r['status']} | offline_SETD={h.get('setd_package_id')} | L6={l6.get('status')} live={l6.get('live_runtime_evidence')} | L7={rec.get('layer7_status')}",flush=True)
                if args.online_training and not h.get('ok'):break
        except Exception as exc:records.append({'status':'FAILED','error':repr(exc),'cycle_in_episode':i+1,'wall_cycle_s':time.perf_counter()-wall0}); break
    if mode=='ONLINE_JSBSIM':return _write_online_reports(orch,cfg,records,count,args,report_root,prefix=prefix)
    return {'cycle_count_requested':count,'cycle_count_completed':len(records),'records':records}

def _next_phase_bootstrap_allowed(phase, layer6_output, layer7_status):
    # on_ground must qualify takeoff before any actuation. Controlled phases
    # must first execute and close their feedback cycle; predicting a future
    # phase early replaces the single current-phase artifact and starves control.
    return phase=='on_ground' or bool(layer6_output.get('status')=='EXECUTED'
        and layer6_output.get('actuation_executed') is True and layer7_status=='OK')


def run_online_episodes(orch,cfg,req0,args,report_root,online_forcing=None,online_climate_alignment=None, *, initial_rtmo_cap=None, rtmo_execution_overrides=None):
    episodes=[]; all_records=[]
    region_file=ROOT/'region'/'region_v1.geojson'
    if not region_file.is_file():raise FileNotFoundError(f'online episode termination requires {region_file}')
    _,origin,destination=load_region(region_file)
    local_seconds=getattr(args,'online_local_hover_seconds',None)
    resolved_cap,budget_meta=_resolve_online_episode_cycle_cap(cfg,args,report_root,origin,destination)
    args.online_max_cycles_per_episode=int(resolved_cap); args.online_cycle_budget_meta=budget_meta
    total_requested=int(args.online_episodes)*int(resolved_cap)
    rtmo_enabled=bool(cfg.get('tdoc.rtmo.enabled',False))
    dt=float(cfg.get('tdoc.rtmo.realtime.max_control_period_s',0.1) if rtmo_enabled else cfg.get('scheduler.fast_dt_s',1.0))
    print(f"\n=== ONLINE_JSBSIM: {args.online_episodes} mission episode(s), max {resolved_cap} cycles/episode ({budget_meta.get('mode')}:{budget_meta.get('source')}) ===",flush=True)
    global_cycle=0
    for ep in range(1,int(args.online_episodes)+1):
        eid=f'online-episode-{ep:04d}'; start_meta=orch.begin_online_episode(eid); ep_records=[]; terminal=None; origin_check={'verified':False,'distance_m':None,'position':None}
        previous_goal=None; previous_sim=None; initial_goal=None; previous_state={}; no_actuation_streak=0; previous_stall_gate=None; bootstrap_hold_streak=0
        rtmo_cap=(initial_rtmo_cap if (rtmo_enabled and initial_rtmo_cap is not None) else
                  build_capability_manifest(report_root, model_dependencies_provider=(lambda: capture_model_dependencies(cfg)) if integrated_native(cfg) else None,layer6_forward_only=False, execution_overrides=(rtmo_execution_overrides or {}),
                    certificate_bank_path=(cfg.path_value('TDDT_LAYER4_ROOT',required=True)/'runtime/verification/ff_certificates.jsonl'),
                    require_certificate_guard=bool(cfg.get('tdoc.rtmo.l4_artifacts.require_live_certificate_guard',True)),
                    capability_registry_path=(ROOT/'runtime'/'rtmo'/'capability_registry.json.gz'),
                    require_live_registry=bool(cfg.get('tdoc.rtmo.capability.require_live_registry',True)))) if rtmo_enabled else None
        phase_mgr=MissionPhaseManager.from_offline_report(report_root/'offline'/'mission'/'PHASE_HISTORY.csv') if rtmo_enabled else None
        live_capability_registry=LiveCapabilityRegistry(ROOT/'runtime'/'rtmo'/'capability_registry.json.gz') if rtmo_enabled else None
        rtclock=RTClock(period_s=dt,max_state_age_s=max(2*dt,0.2)) if rtmo_enabled else None
        goal_sup=GoalSupervisor(float(destination[1]),float(destination[0]),goal_tolerance_m=float(args.online_goal_tolerance_m)) if rtmo_enabled else None
        run_ctx=RunContext.create(eid,config_version=str(cfg.get('package.version','unknown'))) if rtmo_enabled else None
        mission_prior=load_mission_prior(cfg,report_root,origin,destination) if rtmo_enabled else None
        initial_nav=dict(mission_prior['initial_state']) if mission_prior else None
        local_acceptance=None
        if local_seconds is not None:
            from tddt_tdoc.online_mission import LocalHoverAcceptance
            if not initial_nav:raise ValueError('local hover requires surveyed mission initialization')
            phase_mgr=MissionPhaseManager.local_hover()
            destination=list(origin)
            goal_sup=GoalSupervisor(float(origin[1]),float(origin[0]),goal_tolerance_m=5.)
            local_acceptance=LocalHoverAcceptance(origin,initial_nav,duration_s=local_seconds,
                height_m=args.online_local_hover_height_m,max_gap_s=float(cfg.get('tdoc.rtmo.realtime.max_control_period_s',.1)))
        last_navigation=dict(initial_nav or {})
        rtmo_phase_history=[]
        # P0-4: retain only non-actuating phase-entry bootstrap evidence in-memory
        # for this episode.  It is never promoted to durable control proof.
        phase_bootstrap_evidence={}
        # A successful supervised-release handoff changes the Layer6/GUI/MAVLink clock
        # epoch once.  Carry that proved boundary to the next Layer2 transaction;
        # it is not a recurring relaxation of the normal plant-time grant.
        pending_layer2_clock_sync=None
        print(f"\n--- mission {ep}/{args.online_episodes} | Layer6 session={start_meta.get('session_generation')} | cycle_cap={resolved_cap} | origin=({float(origin[1]):.6f},{float(origin[0]):.6f}) destination=({float(destination[1]):.6f},{float(destination[0]):.6f}) ---",flush=True)
        try:
            boot0=time.perf_counter()
            episode_preparation=(orch.prepare_online_transport_session() if args.online_observation_only else orch.prepare_online_control_session())
            diagnostic_event('online.episode.preparation',episode=ep,result=episode_preparation)
            episode_preparation=dict(episode_preparation or {}); episode_preparation['elapsed_s']=time.perf_counter()-boot0
            if episode_preparation.get('ok') and cfg.get('layers.layer6.online.execution_profile')=='closed_loop_sitl':
                from tddt_tdoc.online_mission import validate_native_origin
                native_session=orch.adapters[6]._get_client().native_session_status()
                origin_check=validate_native_origin(orch.adapters[6]._status(),
                    native_session,origin,args.online_origin_tolerance_m)
                episode_preparation['origin_validation']=origin_check
                if not origin_check['verified']:
                    episode_preparation.update(ok=False,status='NOT_READY',reason=origin_check['reason'])
            prep_label='OBSERVATION TRANSPORT' if args.online_observation_only else 'CONTROL / Layer6 DEMO-ALIGNED'
            print(f"[E{ep:03d} PREP] {prep_label} status={episode_preparation.get('status')} ok={episode_preparation.get('ok')} elapsed={episode_preparation.get('elapsed_s',0.0):.3f}s",flush=True)
            if not episode_preparation.get('ok'):
                print(f"[E{ep:03d} PREP] reason={episode_preparation.get('reason') or episode_preparation.get('error') or 'readiness rejected'}",flush=True)
                terminal={'reached':False,'reason':'online_transport_preparation_failed' if args.online_observation_only else 'online_control_preparation_failed','position':None,'goal_distance_m':None}
            else:
                ground_boundary=None
                if rtmo_enabled and phase_mgr is not None and phase_mgr.phase=='on_ground':
                    # Current Online Layer6 state is authoritative for the passive
                    # on_ground boundary.  This refresh can grant or revoke only the
                    # boundary state; it never writes controlled-phase L4/L5 proof.
                    ground_boundary=online_on_ground_boundary_evidence(episode_preparation)
                    LiveCapabilityRegistry(ROOT/'runtime'/'rtmo'/'capability_registry.json.gz').record_boundary_state(
                        phase='on_ground',boundary_evidence=ground_boundary)
                    rtmo_cap=build_capability_manifest(report_root, model_dependencies_provider=(lambda: capture_model_dependencies(cfg)) if integrated_native(cfg) else None,layer6_forward_only=False, execution_overrides=(rtmo_execution_overrides or {}),
                        certificate_bank_path=(cfg.path_value('TDDT_LAYER4_ROOT',required=True)/'runtime/verification/ff_certificates.jsonl'),
                        require_certificate_guard=bool(cfg.get('tdoc.rtmo.l4_artifacts.require_live_certificate_guard',True)),
                        capability_registry_path=(ROOT/'runtime'/'rtmo'/'capability_registry.json.gz'),
                        require_live_registry=bool(cfg.get('tdoc.rtmo.capability.require_live_registry',True)))
                for ci in range(1,int(args.online_max_cycles_per_episode)+1):
                    current_readiness_taxonomy=None
                    global_cycle+=1; req=copy.deepcopy(req0); req['learning_mode']='ONLINE_JSBSIM'; req['online_training']=True; req['require_offline_knowledge']=True; req['online_observation_only']=bool(args.online_observation_only)
                    req['online_episode_id']=eid; req['online_episode_index']=ep; req['online_cycle_in_episode']=ci
                    ctx=dict(req.get('context') or {}); ctx.update({'online_episode_id':eid,'online_episode_index':ep,'online_cycle_in_episode':ci})
                    _clock_rebase_bootstrap=False
                    if pending_layer2_clock_sync is not None:
                        ctx['rtmo_plant_clock_synchronization']=dict(pending_layer2_clock_sync)
                        _clock_rebase_bootstrap=True
                        pending_layer2_clock_sync=None
                    if rtmo_enabled:
                        phase=phase_mgr.phase; req['stage']=phase
                        token=phase_mgr.token(cycle=ci,sim_time_s=rtclock.last_sim_time_s,mission_version='RTMO-11PHASE-v2')
                        ctx.update({'flight_phase':phase,'rtmo_phase_token':token,'rtmo_run_context':run_ctx.cycle(ci,phase),
                                    'rtmo_data_authority':{'navigation':'MESA_STD','simulator_truth':'DIAGNOSTIC_EXECUTION_ONLY','truth_injection_to_mesa':False}})
                        bind_pending_navigation_initialization(ctx,orch.adapters[2],eid,initial_nav)
                        rtmo_phase_history.append({'cycle':ci,'phase':phase,'sim_time_s':rtclock.last_sim_time_s})
                    req['context']=ctx
                    if mission_prior is not None:
                        apply_mission_prior(req,mission_prior,last_navigation)
                    if local_acceptance is not None:
                        from tddt_tdoc.online_mission import bind_local_hover_reference
                        bind_local_hover_reference(req,initial_nav,duration_s=local_seconds,height_m=args.online_local_hover_height_m)
                    if args.offline_knowledge_manifest:req['offline_knowledge_manifest']=args.offline_knowledge_manifest
                    if args.online_validation_fixture:req['layer6_validation_observation_fixture']=json.loads(Path(args.online_validation_fixture).read_text())
                    req['reference_time_s']=(rtclock.reference_time(float(req0.get('reference_time_s',0.0))+(ci-1)*dt) if rtmo_enabled else float(req0.get('reference_time_s',0.0))+(ci-1)*dt)
                    if rtmo_enabled:
                        # P0-2: reference_time_s keeps model/scheduler requests well-formed,
                        # but it is never physical freshness authority.  Only Layer6/GUI/MAVLink
                        # simulation_time may promote Online operational-state freshness.
                        _pre_clock=rtclock.plant_time_state()
                        _ctx=dict(req.get('context') or {})
                        _ctx['rtmo_time_authority']={
                            'plant_time_authority':'Layer6/GUI/MAVLink plant clock',
                            'plant_time_present':bool(_pre_clock.get('plant_time_present')),
                            'operational_state_fresh':bool(_pre_clock.get('state_fresh')),
                            'reference_time_semantics':'SCHEDULER_MODEL_REFERENCE_ONLY',
                            'reference_time_is_physical_freshness_evidence':False,
                        }
                        req['context']=_ctx
                    _elapsed=(global_cycle-1)*dt
                    _env=episode_preparation.get('environment_contract') or {}
                    _native_binding_path=None;_now=None
                    if _env.get('binding_hash') and integrated_native(cfg):
                        _now=rtclock.last_sim_time_s
                        if _now is None:_now=(episode_preparation.get('current_gui_state') or {}).get('plant_time_s')
                        if _now is None:raise RuntimeError('native climate requires observed plant time; no scheduler/zero fallback')
                        _native_binding_path=cfg.path_value('TDDT_LAYER6_ROOT',required=True)/'runtime/tdoc_environment_binding.json'
                        rtclock.observe_sim_time(_now);req['reference_time_s']=float(_now)
                    _apply_online_climate_time(req,online_forcing,_elapsed,online_climate_alignment,
                        native_binding_path=_native_binding_path,plant_time_s=_now)
                    req['transaction_id']=f'workflow-online_jsbsim-e{ep:04d}-c{ci:04d}'
                    pre_release_current=None
                    pre_release_next_entry=None
                    l4_bootstrap_event=None
                    if rtmo_enabled:
                        _next=(phase_mgr.phase_order[phase_mgr.current_index+1] if not phase_mgr.complete else None)
                        # P0-4: cold-start/phase-entry must synchronously create the
                        # first verified artifact before the >=10 Hz reuse lane is allowed
                        # to run.  on_ground is passive, so it bootstraps the imminent
                        # controlled phase (vertical_takeoff) from the current state.
                        _bootstrap_target=(_next if phase=='on_ground' else (None if phase=='touchdown' else phase))
                        if _bootstrap_target:
                            _cached=phase_bootstrap_evidence.get(_bootstrap_target)
                            _bootstrap_required=False
                            if not orch.rtmo_l4_artifacts.phase_seeded(_bootstrap_target):
                                _bootstrap_required=True
                            elif _clock_rebase_bootstrap and _bootstrap_target==phase:
                                _bootstrap_required=True
                            if _bootstrap_required:
                                try:
                                    _pre_release_flush=orch.prepare_synchronous_pre_release()
                                    if not _pre_release_flush.get('ok'):
                                        raise RuntimeError(f"pre_release_audit_flush_failed:{_pre_release_flush}")
                                    _cached,_pre_cycle=_run_pre_release_verify(
                                        orch,req,phase=_bootstrap_target,cycle_id=ci,
                                        reference_time_s=float(req['reference_time_s']),registry=live_capability_registry,rtclock=rtclock)
                                    _cached['pre_release_audit_flush']=_pre_release_flush
                                except Exception as exc:
                                    diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
                                    _cached={'schema':'TDOC-RTMO-pre-release-verify-v1','phase':_bootstrap_target,
                                        'cycle_id':ci,'reference_time_s':float(req['reference_time_s']),
                                        'qualified':False,'non_actuating':True,'control_effect_applied':False,
                                        'reason':'pre_release_current_phase_bootstrap_exception','error':repr(exc),
                                        'synthetic_capability':False,
                                        'l4_artifact_bootstrap':{'ok':False,'reason':'bootstrap_exception','atomic_publish':False,'fast_lane_seeded':False}}
                                phase_bootstrap_evidence[_bootstrap_target]=_cached
                            _bst=dict((_cached or {}).get('l4_artifact_bootstrap') or {})
                            l4_bootstrap_event={
                                'schema':'TDOC-RTMO-pre-release-current-phase-bootstrap-v1',
                                'requested_from_phase':phase,'target_phase':_bootstrap_target,
                                'cold_start_or_phase_entry':True,
                                'qualified':bool((_cached or {}).get('qualified') is True),
                                'artifact_published':bool(_bst.get('ok') is True and _bst.get('fast_lane_seeded') is True),
                                'artifact_publish_reason':_bst.get('reason'),
                                'artifact_gate_status':orch.rtmo_l4_artifacts.status(),
                                'non_actuating':True,'layer6_invoked':False,'layer7_invoked':False,
                            }
                            if not l4_bootstrap_event['artifact_published']:
                                bootstrap_hold_streak+=1
                                _mission_cert=(rtmo_cap.to_dict().get('mission_certification') if rtmo_cap is not None else {})
                                current_readiness_taxonomy=write_taxonomy(report_root/'online'/'ONLINE_READINESS_TAXONOMY.json',
                                    control_readiness=getattr(args,'online_control_readiness',None),phase=phase,release=None,
                                    mission_certification=_mission_cert,hold_reason='WAIT_L4_BOOTSTRAP',bootstrap=l4_bootstrap_event,
                                    current_phase_evaluated=True,mission_evaluated=True,episode_index=ep,cycle_in_episode=ci)
                                rec={'status':'HOLD','episode_index':ep,'cycle_in_episode':ci,'wall_cycle_s':0.0,
                                     'rtmo_phase':phase,'readiness_taxonomy':current_readiness_taxonomy,'rtmo_l4_bootstrap':l4_bootstrap_event,
                                     'rtmo_pre_release_verify_bootstrap':_cached,
                                     'current_online_observation':((_cached or {}).get('current_online_observation') if isinstance(_cached,dict) else None),
                                     'current_online_observation_present':bool(((_cached or {}).get('current_online_observation') or {}).get('plant_time_present')) if isinstance(_cached,dict) else False,
                                     'current_online_observation_plant_time_s':_num((((_cached or {}).get('current_online_observation') or {}).get('plant_time_s') if isinstance(_cached,dict) else None),None),
                                     'current_online_observation_execution_correlated':bool((((_cached or {}).get('current_online_observation') or {}).get('execution_correlated')) if isinstance(_cached,dict) else False),
                                     'current_online_observation_fresh_for_control_state':bool((((_cached or {}).get('current_online_observation') or {}).get('fresh_for_control_state')) if isinstance(_cached,dict) else False),
                                     'post_execution_observation':None,'post_execution_observation_present':False,'post_execution_observation_plant_time_s':None,
                                     'decision_gate_trace':{'first_stop_gate':'RTMO_L4_BOOTSTRAP',
                                         'first_stop_reasons':[str(_bst.get('reason') or (_cached or {}).get('reason') or 'l4_bootstrap_not_published')]},
                                     'layer6_runtime':{'status':'NOT_INVOKED','source':'RTMO_WAIT_L4_BOOTSTRAP','live_runtime_evidence':False},
                                     'layer7_status':'NOT_INVOKED','offline_knowledge_handoff':{'ok':True},
                                     'terminal':{'reached':False,'reason':'WAIT_L4_BOOTSTRAP','position':None,'goal_distance_m':None}}
                                ep_records.append(rec); all_records.append(rec); terminal=rec['terminal']
                                print(f"[E{ep:03d} C{ci:04d}] HOLD | phase={phase} | RTMO WAIT_L4_BOOTSTRAP | target={_bootstrap_target} | reason={_bst.get('reason') or (_cached or {}).get('reason')} | detail={(_cached or {}).get('error') or (_cached or {}).get('l5_hold_reason') or ''} | no actuation",flush=True)
                                limit=max(1,int(cfg.get('layers.online_training.no_actuation_stall_abort_cycles',5) or 5))
                                rec['bootstrap_stall_guard']={'consecutive_cycles':bootstrap_hold_streak,'limit':limit,
                                    'reasons':(_cached or {}).get('l4_rejection_reasons') or []}
                                if bootstrap_hold_streak>=limit:
                                    terminal={**terminal,'reason':'online_l4_bootstrap_stalled',
                                        'first_stop_gate':'RTMO_L4_BOOTSTRAP',
                                        'first_stop_reasons':rec['bootstrap_stall_guard']['reasons']}
                                    rec['terminal']=terminal
                                    break
                                # A phase-entry bootstrap HOLD is an intentional,
                                # non-actuating fail-closed cycle, not an episode
                                # terminal condition.  Retry from a fresh Online
                                # observation on the next scheduler cycle; Layer5
                                # FINAL and artifact publication remain mandatory.
                                continue
                            bootstrap_hold_streak=0
                            if (phase=='on_ground' and _bootstrap_target=='vertical_takeoff'
                                and cfg.get('layers.layer6.online.execution_profile')=='closed_loop_sitl'):
                                from tddt_tdoc.online_mission import passive_native_entry
                                live=orch.adapters[6].current_online_status()
                                entry=passive_native_entry(live,_cached,
                                    expected_session_id=(episode_preparation.get('native_session') or {}).get('session_id'),
                                    environment_ready=orch.adapters[6].validated_environment_status(live),
                                    max_age_s=cfg.get('tdoc.rtmo.l4_artifacts.max_age_s',30.))
                                if entry['ok']:
                                    boundary_release=rtmo_cap.runtime_release(phase,next_phase=_bootstrap_target,
                                        next_pre_release_evidence=_cached)
                                    can,reasons=phase_mgr.can_advance(
                                        sim_time_s=entry['lifecycle_plant_time_s'],
                                        next_capability=bool(boundary_release.get('next_phase_capability')),
                                        current_state_fresh=True,current_state_executable_release=True)
                                    if can:
                                        rec={'status':'PASSIVE_BOUNDARY_QUALIFIED','episode_index':ep,'cycle_in_episode':ci,
                                            'rtmo_phase':phase,'wall_cycle_s':0.,'passive_lifecycle_entry':entry,
                                            'rtmo_pre_release_verify_next':_cached,
                                            'rtmo_phase_transition':{'from':phase,'to':_bootstrap_target,'allowed':True,
                                                'policy':'PASSIVE_LIFECYCLE_ONLY; next cycle retains all current-state control gates'},
                                            'layer6_runtime':{'status':'NOT_INVOKED','live_runtime_evidence':False},
                                            'layer7_status':'NOT_INVOKED','offline_knowledge_handoff':{'ok':True},
                                            'terminal':{'reached':False,'reason':'destination_not_yet_reached'}}
                                        ep_records.append(rec);all_records.append(rec);terminal=rec['terminal']
                                        phase_mgr.advance(sim_time_s=entry['lifecycle_plant_time_s'])
                                        print(f"[E{ep:03d} C{ci:04d}] passive ground entry qualified; no actuation; next={_bootstrap_target}",flush=True)
                                        continue
                        pre_release_current=phase_bootstrap_evidence.get(phase)
                        pre_release_next_entry=phase_bootstrap_evidence.get(_next) if _next else None
                        # P0-3: historical/durable capability is never current
                        # actuation authority.  CurrentRuntimeRelease is a separate,
                        # transaction-scoped gate.  At phase entry it may consume
                        # the P0-4 non-actuating bootstrap evidence, but only together
                        # with authoritative plant freshness and Layer6 readiness.
                        _pre_qualified=bool((pre_release_current or {}).get('qualified') is True)
                        current_release_evidence={
                            'schema':'TDOC-RTMO-current-runtime-release-evidence-v1',
                            'phase':phase,
                            'learning_mode':'ONLINE_JSBSIM',
                            'transaction_id':req.get('transaction_id'),
                            'fresh_operational_state':bool(_pre_clock.get('state_fresh')),
                            'current_l4_artifact_verified':_pre_qualified,
                            'current_l4_artifact_compatible':bool(_pre_qualified and orch.rtmo_l4_artifacts.phase_seeded(phase)),
                            'current_l5_final':_pre_qualified,
                            'clock_freshness_ok':bool(_pre_clock.get('ok') and _pre_clock.get('state_fresh')),
                            'layer6_ready':bool(episode_preparation.get('ok') and episode_preparation.get('status')=='READY'),
                            'source':'P0_4_PRE_RELEASE_CURRENT_PHASE_BOOTSTRAP',
                            'durable_capability_used_as_actuation_authority':False,
                        }
                        release=rtmo_cap.runtime_release(
                            phase,next_phase=_next,pre_release_evidence=pre_release_current,
                            next_pre_release_evidence=pre_release_next_entry,
                            current_runtime_evidence=current_release_evidence
                        ) if rtmo_cap else {'status':'WAIT_CAPABILITY','actuation_release':False,'blockers':['rtmo_capability_manifest_missing']}
                        ctx=dict(req.get('context') or {}); ctx['rtmo_runtime_release']=release
                        ctx['rtmo_current_runtime_release_evidence']=current_release_evidence
                        if l4_bootstrap_event is not None: ctx['rtmo_l4_bootstrap']=l4_bootstrap_event
                        if pre_release_current is not None: ctx['rtmo_pre_release_verify_current']=pre_release_current
                        if pre_release_next_entry is not None: ctx['rtmo_pre_release_verify_next_entry']=pre_release_next_entry
                        req['context']=ctx
                        # Passive boundaries must be allowed to execute one
                        # non-actuating observation transaction; otherwise plant
                        # freshness can never be established and on_ground becomes
                        # a circular WAIT_RUNTIME_RELEASE deadlock. Controlled phases
                        # remain gated exactly as before.
                        if not bool(release.get('actuation_release')) and phase not in ('on_ground','touchdown'):
                            _wait_status=('WAIT_CAPABILITY' if not bool(release.get('current_phase_capability')) else 'WAIT_RUNTIME_RELEASE')
                            _terminal_reason=('WAIT_CAPABILITY' if _wait_status=='WAIT_CAPABILITY' else 'WAIT_CURRENT_RUNTIME_RELEASE')
                            _mission_cert=(rtmo_cap.to_dict().get('mission_certification') if rtmo_cap is not None else {})
                            current_readiness_taxonomy=write_taxonomy(report_root/'online'/'ONLINE_READINESS_TAXONOMY.json',
                                control_readiness=getattr(args,'online_control_readiness',None),phase=phase,release=release,
                                mission_certification=_mission_cert,hold_reason=_wait_status,bootstrap=l4_bootstrap_event,
                                current_phase_evaluated=True,mission_evaluated=True,episode_index=ep,cycle_in_episode=ci)
                            rec={'status':'HOLD','episode_index':ep,'cycle_in_episode':ci,'wall_cycle_s':0.0,
                                 'rtmo_phase':phase,'readiness_taxonomy':current_readiness_taxonomy,'rtmo_runtime_release':release,'rtmo_pre_release_verify':pre_release_current,
                                 'rtmo_current_runtime_release_evidence':current_release_evidence,
                                 'current_online_observation':((pre_release_current or {}).get('current_online_observation') if isinstance(pre_release_current,dict) else None),
                                 'current_online_observation_present':bool(((pre_release_current or {}).get('current_online_observation') or {}).get('plant_time_present')) if isinstance(pre_release_current,dict) else False,
                                 'current_online_observation_plant_time_s':_num((((pre_release_current or {}).get('current_online_observation') or {}).get('plant_time_s') if isinstance(pre_release_current,dict) else None),None),
                                 'current_online_observation_execution_correlated':bool((((pre_release_current or {}).get('current_online_observation') or {}).get('execution_correlated')) if isinstance(pre_release_current,dict) else False),
                                 'current_online_observation_fresh_for_control_state':bool((((pre_release_current or {}).get('current_online_observation') or {}).get('fresh_for_control_state')) if isinstance(pre_release_current,dict) else False),
                                 'post_execution_observation':None,'post_execution_observation_present':False,'post_execution_observation_plant_time_s':None,
                                 'rtmo_l4_bootstrap':l4_bootstrap_event,
                                 'rtmo_l4_artifact_gate_status':orch.rtmo_l4_artifacts.status(),
                                 'decision_gate_trace':{'first_stop_gate':'RTMO_RUNTIME_RELEASE','first_stop_reasons':release.get('blockers') or []},
                                 'layer6_runtime':{'status':'NOT_INVOKED','source':f'RTMO_{_wait_status}','live_runtime_evidence':False},
                                 'layer7_status':'NOT_INVOKED','offline_knowledge_handoff':{'ok':True},
                                 'terminal':{'reached':False,'reason':_terminal_reason,'position':None,'goal_distance_m':None}}
                            ep_records.append(rec); all_records.append(rec)
                            terminal=rec['terminal']
                            print(f"[E{ep:03d} C{ci:04d}] HOLD | phase={phase} | RTMO {_wait_status} | durable={bool(release.get('current_phase_durable_capability'))} current_release={bool(release.get('current_runtime_release'))} | L4bootstrap={bool((l4_bootstrap_event or {}).get('artifact_published'))} | no actuation",flush=True)
                            break
                        _mission_cert=(rtmo_cap.to_dict().get('mission_certification') if rtmo_cap is not None else {})
                        current_readiness_taxonomy=write_taxonomy(report_root/'online'/'ONLINE_READINESS_TAXONOMY.json',
                            control_readiness=getattr(args,'online_control_readiness',None),phase=phase,release=release,
                            mission_certification=_mission_cert,bootstrap=l4_bootstrap_event,current_phase_evaluated=True,
                            mission_evaluated=True,episode_index=ep,cycle_in_episode=ci)
                    wall0=time.perf_counter()
                    try:
                        r=orch.run_cycle(req).to_dict(); rec=_online_record(r,episode_index=ep,cycle_in_episode=ci); rec['wall_cycle_s']=time.perf_counter()-wall0
                        _nav=(r.get('layer2_packets',{}).get('MESA_NAV',{}).get('state') or {})
                        if all(k in _nav for k in ('position_n','position_e','position_d')):
                            last_navigation={**last_navigation,'pn':float(_nav['position_n']),'pe':float(_nav['position_e']),'pd':float(_nav['position_d'])}
                        if current_readiness_taxonomy is not None: rec['readiness_taxonomy']=current_readiness_taxonomy
                    except Exception as exc:
                        diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
                        rec={'status':'FAILED','error':repr(exc),'episode_index':ep,'cycle_in_episode':ci,'wall_cycle_s':time.perf_counter()-wall0}; ep_records.append(rec); all_records.append(rec); break
                    terminal=_online_terminal(r,destination,args.online_goal_tolerance_m,args.online_touchdown_altitude_m); rec['terminal']=terminal
                    if local_acceptance is not None and not local_acceptance.completed:
                        terminal['reached']=False
                        terminal['reason']='local_hover_measured_duration_not_complete'
                    if rtmo_enabled and phase not in ('on_ground','touchdown'):
                        # Promote only *realized* Online evidence to durable capability.
                        # PRE_RELEASE_VERIFY itself never enters joint_control_proof_cycles.
                        _l6out=_l6_output_from_cycle(r); _l6ev=_l6_evidence_from_cycle(r); _ios=dict(_l6ev.get('io_snapshot') or {})
                        _control_effect=bool(_l6out.get('status')=='EXECUTED' and _l6out.get('actuation_executed') is True and _l6ev.get('source')=='Layer6/GUI/MAVLink' and _ios.get('ready') is True)
                        live_capability_registry.record_cycle(phase=phase,cycle_id=ci,reference_time_s=float(req['reference_time_s']),
                            layer4_result=r.get('layer4_result') or {},layer5_result=l5_result(r),control_effect_applied=_control_effect,context=req.get('context'))
                        rec['rtmo_live_capability_record']={'phase':phase,'control_effect_applied':_control_effect,'source':'current Online transaction'}
                    if rtmo_enabled:
                        op=terminal.get('position') or {}
                        # Plant-clock authority is carried by the explicit Layer6/GUI/MAVLink
                        # observation contracts, not by MESA position or scheduler
                        # reference time.  Controlled phases require the observation
                        # captured after Layer6 execution.  Passive contact boundaries
                        # have no actuation by design, so their current Layer6 ingress
                        # is the authoritative sample.
                        _post_obs=dict(rec.get('post_execution_observation') or {})
                        _current_obs=dict(rec.get('current_online_observation') or {})
                        if _post_obs.get('plant_time_present') is True:
                            sim_now=_num(_post_obs.get('plant_time_s'),None)
                            _plant_time_sample='POST_EXECUTION_LAYER6_NATIVE'
                        elif phase in ('on_ground','touchdown') and _current_obs.get('plant_time_present') is True:
                            sim_now=_num(_current_obs.get('plant_time_s'),None)
                            _plant_time_sample='PASSIVE_BOUNDARY_CURRENT_LAYER6'
                        elif (_current_obs.get('plant_time_present') is True
                              and _l6_output_from_cycle(r).get('status')=='HELD'):
                            # A rejected decision has no post-execution observation,
                            # but its real ingress still observes plant time. This
                            # never substitutes for execution/phase-release evidence.
                            sim_now=_num(_current_obs.get('plant_time_s'),None)
                            _plant_time_sample='HELD_CURRENT_LAYER6_NON_ACTUATING'
                        else:
                            sim_now=None
                            _plant_time_sample='MISSING'
                        # Always observe the *current-cycle* plant timestamp. Missing
                        # time must not inherit the previous cycle's freshness.
                        _handoff_sync=False
                        if phase=='vertical_takeoff' and sim_now is not None:
                            _ev_now=_l6_evidence_from_cycle(r)
                            _wr=dict(_ev_now.get('write_result') or {})
                            _stream_last=dict(((_wr.get('stream_status') or {}).get('last_result')) or {})
                            _hc=dict(_wr.get('handoff_confirmation') or _stream_last.get('handoff_confirmation') or {})
                            _tr=dict(_hc.get('transition') or {})
                            _handoff_sync=bool(
                                _hc.get('ready') is True and _hc.get('authorized') is True
                                and _tr.get('complete') is True and _tr.get('handoff_state')=='complete'
                                and not any((x.get('rtmo_clock') or {}).get('clock_synchronization')
                                            for x in ep_records)
                            )
                        _clock_samples=(_l6_evidence_from_cycle(r).get('plant_clock_samples') or [])
                        _native_measured_clock=(_current_obs.get('source')=='Layer6/GUI/MAVLink')
                        if _native_measured_clock:
                            # A segment's tail must not discard the consumed L2
                            # history preceding a slow decision. These are actual
                            # observations from one session, never filled gaps.
                            _handoff_sync=False
                            try:
                                if _plant_time_sample in ('PASSIVE_BOUNDARY_CURRENT_LAYER6','HELD_CURRENT_LAYER6_NON_ACTUATING'):
                                    # Layer2 acquired a newer real observation
                                    # after MicroMet; the pre-L2 diagnostic sample
                                    # must not hide that consumed source endpoint.
                                    sim_now=_consumed_native_imu_clock(r)['endpoint_s']
                                    _plant_time_sample+=':MESA_CONSUMED_MEASURED_ENDPOINT'
                                _clock_samples=_merge_consumed_native_clock_samples(r,_clock_samples)
                                clock_state=rtclock.observe_measured_samples(sim_now,_clock_samples)
                            except (ValueError,TypeError,KeyError) as exc:
                                clock_state={'ok':False,'state_fresh':False,'plant_time_present':sim_now is not None,
                                    'reason':'invalid_consumed_native_clock','error':str(exc)}
                        else:
                            if not _clock_samples:
                                _clock_ctx=((r.get('layer2_packets') or {}).get('MESA_NAV') or {}).get('context') or {}
                                _clock_samples=((_clock_ctx.get('online_state_ingress') or {}).get('ptlib_ingress') or {}).get('imu_samples') or []
                            clock_state=(rtclock.synchronize_after_handoff(sim_now)
                                         if _handoff_sync else rtclock.observe_measured_samples(sim_now,_clock_samples))
                        if (_handoff_sync and
                                clock_state.get('clock_synchronization')=='SUPERVISED_RELEASE_HANDOFF_COMPLETE'):
                            pending_layer2_clock_sync={
                                'schema':'TDOC-RTMO-PlantClockSynchronization-v1',
                                'authorized':True,
                                'reason':'SUPERVISED_RELEASE_HANDOFF_COMPLETE',
                                'source_transaction_id':req.get('transaction_id'),
                                'plant_time_s':float(sim_now),
                                'handoff_evidence':{
                                    'ready':True,'authorized':True,
                                    'transition_complete':True,'handoff_state':'complete',
                                },
                            }
                        clock_state['sample_contract']=_plant_time_sample
                        rec['rtmo_clock']=clock_state
                        rec['rtmo_operational_state_fresh']=bool(clock_state.get('state_fresh'))
                        rec['rtmo_time_authority']={
                            'plant_time_authority':'Layer6/GUI/MAVLink plant clock',
                            'plant_time_present':bool(clock_state.get('plant_time_present')),
                            'plant_time_sample_contract':_plant_time_sample,
                            'reference_time_s':float(req['reference_time_s']),
                            'reference_time_semantics':'SCHEDULER_MODEL_REFERENCE_ONLY',
                            'reference_time_is_physical_freshness_evidence':False,
                        }
                        vehicle_now=rec.get('vehicle_state') or {}; vg=((vehicle_now.get('speed') or {}).get('groundspeed_m_s')) or op.get('groundspeed_m_s')
                        goal_state=goal_sup.update(lat=op.get('lat'),lon=op.get('lon'),groundspeed_m_s=_num(vg,0.0),
                            latency_bound_s=float(cfg.get('tdoc.rtmo.realtime.max_control_period_s',0.1)),transition_bound_s=0.0,
                            ground_referenced=bool(terminal.get('altitude_ok')))
                        rec['rtmo_goal']=goal_state; rec['rtmo_phase']=phase_mgr.phase; rec['rtmo_run_context']=run_ctx.cycle(ci,phase_mgr.phase)
                        # Completion is valid only in the exact final mission phase.
                        if phase_mgr.phase!='touchdown' and terminal.get('reached'):
                            terminal['reached']=False; terminal['reason']='goal_envelope_seen_before_touchdown_phase'
                        max_jump=float(cfg.get('tdoc.rtmo.realtime.max_sim_time_jump_s',dt*1.5))
                        jump=_num(clock_state.get('jump_s'),None)
                        if jump is not None and jump>max_jump:
                            terminal={'reached':False,'reason':'rtmo_simulation_time_grant_exceeded','position':terminal.get('position'),'goal_distance_m':terminal.get('goal_distance_m'),'clock':clock_state}
                            rec['terminal']=terminal
                    if ci==1:
                        p0=terminal.get('position')
                        if p0 and origin_check.get('source')!='Layer6/GUI/MAVLink/GLOBAL_POSITION_INT':
                            od=_haversine_m(p0['lat'],p0['lon'],float(origin[1]),float(origin[0])); origin_check={'verified':bool(od<=float(args.online_origin_tolerance_m)),'distance_m':od,'position':p0}
                        rec['origin_check']=origin_check
                    ep_records.append(rec); all_records.append(rec)
                    l6=rec.get('layer6_runtime') or {}; mpc=rec.get('mpc_participation') or {}; l4=(mpc.get('layer4') or {}); l5=(mpc.get('layer5') or {}); gate_trace=rec.get('decision_gate_trace') or {}
                    goal=_num(terminal.get('goal_distance_m'),None); pos=terminal.get('position') or {}; sim=_num(pos.get('simulation_time_s'),None)
                    if initial_goal is None and goal is not None: initial_goal=goal
                    dgoal=(previous_goal-goal) if previous_goal is not None and goal is not None else None
                    progress=((initial_goal-goal) if initial_goal is not None and goal is not None else None); progress_pct=(100.0*progress/initial_goal if progress is not None and initial_goal and initial_goal>0 else None)
                    dsim=(sim-previous_sim) if sim is not None and previous_sim is not None else None
                    sess=(l6.get('session') or {}); recov=sess.get('recovery_attempt'); l6ms=1000.0*_num(rec.get('layer6_elapsed_s'),0.0)
                    dg,pr,st,ds=_format_online_terminal_metrics(dgoal,progress,progress_pct,sim,dsim)
                    vehicle=rec.get('vehicle_state') or {}; rec['progress_diagnostic']=_online_progress_diagnostic(r,vehicle,progress,dgoal)
                    stop_gate=str(gate_trace.get('first_stop_gate') or 'UNKNOWN'); stop_reason=';'.join(str(x) for x in (gate_trace.get('first_stop_reasons') or []))
                    reason=str(l5.get('decision_reason') or stop_reason or '')[:44]
                    checkpoint={'schema':'TDOC-online-progress-checkpoint-v1','episode_index':ep,'cycle_in_episode':ci,'cycles_completed_total':len(all_records),
                                'transaction_id':r.get('transaction_id'),'status':r.get('status'),'first_stop_gate':stop_gate,'first_stop_reasons':gate_trace.get('first_stop_reasons') or [],
                                'layer6_status':l6.get('status'),'layer7_status':rec.get('layer7_status'),'wall_cycle_s':rec.get('wall_cycle_s'),'updated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
                    cp=report_root/'online'/'ONLINE_PROGRESS_CHECKPOINT.json'; cp.parent.mkdir(parents=True,exist_ok=True); tmp=cp.with_name(cp.name+'.tmp'); tmp.write_text(json.dumps(checkpoint,indent=2,ensure_ascii=False)); os.replace(tmp,cp)
                    state={'tdoc':r['status'],'l4':f"{l4.get('feasible_count')}/{l4.get('candidate_count')}",'l5':f"{l5.get('operational_mode')}:{reason}:gate={stop_gate}",'l6':l6.get('status'),'l7':rec.get('layer7_status')}
                    changes=[]
                    labels={'tdoc':'TDOC','l4':'L4 feasible','l5':'L5','l6':'L6','l7':'L7'}
                    for k,v in state.items():
                        if ci==1 or previous_state.get(k)!=v: changes.append(f"{labels[k]}={v}")
                    if recov: changes.append(f"RECOVERY={recov}")
                    att=vehicle.get('attitude') or {}; spd=vehicle.get('speed') or {}; acts=vehicle.get('actuators') or {}; le=acts.get('left_elevon') or {}; re=acts.get('right_elevon') or {}; lm=acts.get('left_motor') or {}; rm=acts.get('right_motor') or {}
                    def ff(v,d=1):
                        n=_num(v,None); return 'N/A' if n is None else f'{n:.{d}f}'
                    dynamic=(f"progress={pr} Δgoal={dg} motion={(vehicle.get('motion') or {}).get('state','UNKNOWN')} | sim={st} Δsim={ds} | "
                             f"att[R/P/Y]={ff(att.get('roll_deg'))}/{ff(att.get('pitch_deg'))}/{ff(att.get('yaw_deg'))}deg | Vair/Vg={ff(spd.get('airspeed_m_s'))}/{ff(spd.get('groundspeed_m_s'))}m/s | "
                             f"act LE/RE={ff(le.get('estimated_angle_deg'))}/{ff(re.get('estimated_angle_deg'))}deg PWM={ff(le.get('pwm_us'),0)}/{ff(re.get('pwm_us'),0)} LM/RM={ff(lm.get('estimated_speed_rpm'),0)}/{ff(rm.get('estimated_speed_rpm'),0)}rpm PWM={ff(lm.get('pwm_us'),0)}/{ff(rm.get('pwm_us'),0)} | "
                             f"cycle={_num(rec.get('wall_cycle_s'),0.0):.3f}s L6={l6ms:.0f}ms")
                    gate_status=f" | gate={stop_gate}"+(f" reason={stop_reason[:100]}" if stop_reason else '')
                    suffix=(" | "+" | ".join(changes)) if changes else ''
                    print(f"[E{ep:03d} C{ci:04d}] {dynamic}{gate_status}{suffix}",flush=True)
                    previous_state=state
                    motion_rec=((rec.get('vehicle_state') or {}).get('motion') or {})
                    motion=motion_rec.get('state') or (rec.get('terminal') or {}).get('motion_state')
                    propulsion_active=bool(motion_rec.get('propulsion_actuation_active'))
                    translation_active=bool(motion_rec.get('translation_active'))
                    final_authorized=bool(l5.get('final_gate'))
                    current_phase=(phase_mgr.phase if rtmo_enabled else None)
                    stalled=(not args.online_observation_only) and (not propulsion_active) and (not translation_active) and (current_phase not in ('on_ground','touchdown'))
                    if stalled and previous_stall_gate==stop_gate:
                        no_actuation_streak+=1
                    elif stalled:
                        previous_stall_gate=stop_gate; no_actuation_streak=1
                    else:
                        no_actuation_streak=0; previous_stall_gate=None
                    rec['online_stall_guard']={'eligible':bool(stalled),'consecutive_cycles':int(no_actuation_streak),'gate':stop_gate,
                                               'surface_actuation_active':bool(motion_rec.get('surface_actuation_active')),
                                               'propulsion_actuation_active':propulsion_active,'translation_active':translation_active,
                                               'final_authorized':final_authorized}
                    stall_limit=max(1,int(cfg.get('layers.online_training.no_actuation_stall_abort_cycles',5) or 5))
                    if (not args.online_observation_only) and no_actuation_streak>=stall_limit:
                        terminal={'reached':False,'reason':'online_control_actuation_stalled','position':terminal.get('position'),'goal_distance_m':terminal.get('goal_distance_m'),
                                  'first_stop_gate':stop_gate,'first_stop_reasons':gate_trace.get('first_stop_reasons') or []}
                        print(f"[E{ep:03d} C{ci:04d}] ABORT: no authorized propulsion/translation for {no_actuation_streak} consecutive cycles at gate={stop_gate}",flush=True)
                        break
                    if rtmo_enabled:
                        if str(l6.get('status'))=='FAILED':
                            terminal={'reached':False,'reason':'rtmo_execution_failed_fail_stop','position':terminal.get('position'),'goal_distance_m':terminal.get('goal_distance_m')}
                            rec['terminal']=terminal; break
                        if (rec.get('rtmo_goal') or {}).get('overshoot'):
                            terminal={'reached':False,'reason':'rtmo_overshoot_fail_stop','position':terminal.get('position'),'goal_distance_m':terminal.get('goal_distance_m')}
                            rec['terminal']=terminal; break
                        if (rec.get('rtmo_clock') or {}).get('ok') is False and (rec.get('rtmo_clock') or {}).get('jump_s') is not None:
                            terminal={'reached':False,'reason':'rtmo_clock_desynchronization_fail_stop','position':terminal.get('position'),'goal_distance_m':terminal.get('goal_distance_m')}
                            rec['terminal']=terminal; break
                        if not phase_mgr.complete:
                            local_guard=local_acceptance.update(phase_mgr.phase,r) if local_acceptance is not None else None
                            if local_guard is not None:rec['local_hover_acceptance']=local_guard
                            nxt=phase_mgr.phase_order[phase_mgr.current_index+1]
                            # Refresh only at the transition boundary so newly-issued L4 certificates /
                            # same-cycle proof can release an imminent phase without requiring distant
                            # future phases before cycle 1.
                            rtmo_cap=build_capability_manifest(report_root, model_dependencies_provider=(lambda: capture_model_dependencies(cfg)) if integrated_native(cfg) else None,layer6_forward_only=False,execution_overrides=(rtmo_execution_overrides or {}),
                                certificate_bank_path=(cfg.path_value('TDDT_LAYER4_ROOT',required=True)/'runtime/verification/ff_certificates.jsonl'),
                                require_certificate_guard=bool(cfg.get('tdoc.rtmo.l4_artifacts.require_live_certificate_guard',True)),
                                capability_registry_path=(ROOT/'runtime'/'rtmo'/'capability_registry.json.gz'),
                                require_live_registry=bool(cfg.get('tdoc.rtmo.capability.require_live_registry',True)))
                            # Terminal-schedule refresh: keep expensive Full-Fidelity
                            # outside the fast actuation transaction.  The old artifact
                            # remains authoritative unless this non-actuating L2→L5
                            # transaction again proves verified candidates and FINAL.
                            _l4_cycle=dict(r.get('layer4_result') or {})
                            _reuse_meta=dict((_l4_cycle.get('provenance') or {}).get('rtmo_l4_artifact_reuse') or {})
                            _refresh_fraction=float(cfg.get('tdoc.rtmo.l4_artifacts.refresh_envelope_fraction',0.75))
                            _pos_delta=_num(_reuse_meta.get('nearest_position_delta_m'),None)
                            _vel_delta=_num(_reuse_meta.get('nearest_velocity_delta_m_s'),None)
                            _near_artifact_edge=bool(
                                (_pos_delta is not None and _pos_delta >= _refresh_fraction*float(cfg.get('tdoc.rtmo.l4_artifacts.position_tolerance_m',20.0)))
                                or (_vel_delta is not None and _vel_delta >= _refresh_fraction*float(cfg.get('tdoc.rtmo.l4_artifacts.velocity_tolerance_m_s',4.0)))
                            )
                            if _near_artifact_edge and phase_mgr.phase not in ('on_ground','touchdown'):
                                try:
                                    _current_refresh,_current_refresh_cycle=_run_pre_release_verify(
                                        orch,req,phase=phase_mgr.phase,cycle_id=ci,
                                        reference_time_s=float(sim_now),registry=live_capability_registry,rtclock=rtclock)
                                except Exception as exc:
                                    diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
                                    _current_refresh={'schema':'TDOC-RTMO-pre-release-verify-v1','phase':phase_mgr.phase,
                                        'cycle_id':ci,'reference_time_s':float(sim_now),'qualified':False,
                                        'non_actuating':True,'control_effect_applied':False,
                                        'reason':'terminal_schedule_current_refresh_exception','error':repr(exc),
                                        'synthetic_capability':False,
                                        'l4_artifact_bootstrap':{'ok':False,'reason':'refresh_exception','fast_lane_seeded':False}}
                                phase_bootstrap_evidence[phase_mgr.phase]=_current_refresh
                                rec['rtmo_current_artifact_terminal_refresh']={
                                    **_current_refresh,
                                    'trigger':'VERIFIED_CORRIDOR_ENVELOPE_MARGIN',
                                    'envelope_fraction':_refresh_fraction,
                                    'nearest_position_delta_m':_pos_delta,
                                    'nearest_velocity_delta_m_s':_vel_delta,
                                    'fast_transaction':False,
                                }
                            pre_release_next=phase_bootstrap_evidence.get(nxt)
                            durable_next=bool((rtmo_cap.phases.get(nxt) or {}).get('capability'))
                            # P0-4: before the FSM enters an imminent controlled phase,
                            # synchronously seed that phase's reusable L4 artifact even
                            # when durable historical capability already exists.
                            # The artifact TTL and its consumer must use the same
                            # authoritative plant-time domain.  A cold-start artifact
                            # may have been built before the first FMI timestamp and is
                            # therefore re-verified rather than silently rebased.
                            _bootstrap_ref=float(sim_now if sim_now is not None else req['reference_time_s'])
                            _prior_bootstrap_ref=_num((pre_release_next or {}).get('reference_time_s'),None)
                            _bootstrap_clock_aligned=bool(
                                _prior_bootstrap_ref is not None
                                and abs(_bootstrap_ref-_prior_bootstrap_ref)
                                    <= float(cfg.get('tdoc.rtmo.l4_artifacts.max_age_s',30.0))
                            )
                            _phase_dwell_s=float((phase_mgr.offline_min_dwell_s or {}).get(phase_mgr.phase,0.0))
                            _phase_dwell_ready=bool(
                                sim_now is not None and phase_mgr.entry_sim_time_s is not None
                                and float(sim_now)-float(phase_mgr.entry_sim_time_s) >= _phase_dwell_s
                            )
                            if local_guard is not None:_phase_dwell_ready=_phase_dwell_ready and local_guard['phase_ready']
                            if (nxt not in ('on_ground','touchdown') and _phase_dwell_ready
                                and _next_phase_bootstrap_allowed(phase_mgr.phase,_l6_output_from_cycle(r),rec.get('layer7_status')) and (
                                not orch.rtmo_l4_artifacts.phase_seeded(nxt)
                                or not _bootstrap_clock_aligned
                            )):
                                try:
                                    pre_release_next,_pre_next_cycle=_run_pre_release_verify(
                                        orch,req,phase=nxt,cycle_id=ci,
                                        reference_time_s=_bootstrap_ref,
                                        registry=live_capability_registry,rtclock=rtclock)
                                except Exception as exc:
                                    diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
                                    pre_release_next={'schema':'TDOC-RTMO-pre-release-verify-v1','phase':nxt,'cycle_id':ci,'reference_time_s':_bootstrap_ref,'qualified':False,'non_actuating':True,'control_effect_applied':False,'reason':'pre_release_phase_entry_bootstrap_exception','error':repr(exc),'synthetic_capability':False,'l4_artifact_bootstrap':{'ok':False,'reason':'bootstrap_exception','fast_lane_seeded':False}}
                                phase_bootstrap_evidence[nxt]=pre_release_next
                            # P0-1/P0-2/P0-3 current-cycle authority. Durable capability
                            # remains historical evidence only. Build an explicit Online
                            # CurrentRuntimeRelease record from this cycle's compatible
                            # L4/L5 outputs, authoritative plant clock and Layer6 readiness.
                            _l4_valid=bool(l4.get('invoked') and int(l4.get('verified_count') or 0)>0)
                            _l5_final=bool(l5.get('invoked') and l5.get('final_gate') is True)
                            _l6_executed=bool(str(l6.get('status') or '')=='EXECUTED')
                            _l6out_now=_l6_output_from_cycle(r); _l6ev_now=_l6_evidence_from_cycle(r)
                            _ios_now=dict(_l6ev_now.get('io_snapshot') or {})
                            _actuation_ack=bool(
                                _l6_executed and _l6out_now.get('actuation_executed') is True
                                and _l6ev_now.get('source')=='Layer6/GUI/MAVLink' and _ios_now.get('ready') is True
                            )
                            _state_fresh=bool((rec.get('rtmo_clock') or {}).get('state_fresh'))
                            _clock_ok=bool((rec.get('rtmo_clock') or {}).get('ok') and _state_fresh)
                            _layer6_ready=bool(episode_preparation.get('ok') and episode_preparation.get('status')=='READY')
                            current_cycle_release_evidence={
                                'schema':'TDOC-RTMO-current-runtime-release-evidence-v1',
                                'phase':phase_mgr.phase,
                                'learning_mode':'ONLINE_JSBSIM',
                                'transaction_id':r.get('transaction_id') or req.get('transaction_id'),
                                'fresh_operational_state':_state_fresh,
                                'current_l4_artifact_verified':_l4_valid,
                                'current_l4_artifact_compatible':_l4_valid,
                                'current_l5_final':_l5_final,
                                'clock_freshness_ok':_clock_ok,
                                'layer6_ready':_layer6_ready,
                                'source':'CURRENT_ONLINE_CYCLE',
                                'durable_capability_used_as_actuation_authority':False,
                            }
                            rel=rtmo_cap.runtime_release(
                                phase_mgr.phase,next_phase=nxt,
                                next_pre_release_evidence=pre_release_next,
                                current_runtime_evidence=current_cycle_release_evidence
                            )
                            _next_artifact_seeded=(True if nxt in ('on_ground','touchdown') else orch.rtmo_l4_artifacts.phase_seeded(nxt))
                            ncap=bool(rel.get('next_phase_capability') and _next_artifact_seeded)
                            _runtime_release=bool(rel.get('actuation_release'))
                            _realized_state=bool(_actuation_ack and _state_fresh and sim_now is not None)
                            # on_ground remains passive.  The boundary evidence is real,
                            # but leaving it requires a non-actuating current-state
                            # qualification; no historical capability can synthesize it.
                            _ground_ok=bool((ground_boundary or {}).get('supported')) if phase_mgr.phase=='on_ground' else False
                            _current_exec_release=bool(
                                _ground_ok and (pre_release_next or {}).get('qualified') is True
                            ) if phase_mgr.phase=='on_ground' else bool(
                                _l4_valid and _l5_final and _runtime_release and _l6_executed
                                and _actuation_ack and _realized_state
                            )
                            can,reasons=phase_mgr.can_advance(
                                sim_time_s=sim_now,next_capability=ncap,goal_state=rec.get('rtmo_goal') or {},
                                current_state_fresh=_state_fresh,
                                current_state_executable_release=_current_exec_release,
                                l4_artifact_valid=_l4_valid,l5_final=_l5_final,
                                runtime_release=_runtime_release,l6_executed=_l6_executed,
                                actuation_ack=_actuation_ack,realized_state_evidence=_realized_state)
                            rec['rtmo_runtime_release']=rel
                            if local_guard is not None and not local_guard['phase_ready']:
                                can=False;reasons.append('local_hover_measured_hold_not_complete')
                            rec['rtmo_phase_advance_evidence']={
                                'simulation_time_s':sim_now,'current_state_fresh':_state_fresh,
                                'reference_time_s':float(req['reference_time_s']),
                                'reference_time_is_physical_freshness_evidence':False,
                                'l4_artifact_valid':_l4_valid,'l5_final':_l5_final,
                                'runtime_release':_runtime_release,'l6_executed':_l6_executed,
                                'actuation_ack':_actuation_ack,'realized_state_evidence':_realized_state,
                                'current_state_executable_release':_current_exec_release,
                                'next_phase_capability':ncap,
                                'next_phase_artifact_seeded':_next_artifact_seeded,
                            }
                            if pre_release_current is not None: rec['rtmo_pre_release_verify_current']=pre_release_current
                            if pre_release_next is not None: rec['rtmo_pre_release_verify_next']=pre_release_next
                            rec['rtmo_phase_transition']={'from':phase_mgr.phase,'to':nxt,'allowed':can,'reasons':reasons,
                                                          'policy':'PHASE_LOCAL_RUNTIME_RELEASE_WITH_EXECUTION_AND_PLANT_TIME_GUARDS'}
                            if can:
                                phase_mgr.advance(sim_time_s=sim_now)
                            elif 'simulation_time_missing' in reasons or 'current_state_not_fresh' in reasons:
                                rec['rtmo_phase_transition']['status']='WAIT_PLANT_TIME'
                                # Fail closed instead of consuming the complete Online
                                # episode budget on scheduler-only timestamps. A future
                                # invocation may resume only after Layer6/GUI/MAVLink exposes
                                # authoritative plant time.
                                root_gate=(rec.get('decision_gate_trace') or {}).get('first_stop_gate','')
                                terminal={'reached':False,'reason':root_gate.lower() if root_gate.startswith(('L2_','L3_')) else 'rtmo_wait_plant_time',
                                          'position':terminal.get('position'),'goal_distance_m':terminal.get('goal_distance_m'),
                                          'clock':rec.get('rtmo_clock'),'phase':phase_mgr.phase}
                                rec['terminal']=terminal
                                break
                            elif 'next_phase_capability_blocked' in reasons:
                                rec['rtmo_phase_transition']['status']='WAIT_CAPABILITY'
                            else:
                                rec['rtmo_phase_transition']['status']='WAIT_EXECUTION_EVIDENCE'
                    if goal is not None: previous_goal=goal
                    if sim is not None: previous_sim=sim
                    if ci==1 and not origin_check.get('verified'):
                        terminal={'reached':False,'reason':'origin_not_verified','position':terminal.get('position'),'goal_distance_m':terminal.get('goal_distance_m')}; break
                    if not (rec.get('offline_knowledge_handoff') or {}).get('ok'):break
                    if terminal.get('reached'):break
            reached=bool(terminal and terminal.get('reached'))
            term_reason=_resolve_online_termination_reason(terminal,ep_records,int(args.online_max_cycles_per_episode))
            episodes.append({'episode_index':ep,'episode_id':eid,'cycles_completed':len(ep_records),'max_cycles':int(args.online_max_cycles_per_episode),'cycle_budget':dict(budget_meta),'origin_verified':bool(origin_check.get('verified')),'origin_distance_m':origin_check.get('distance_m'),'terminal_reached':reached,
                             'termination_reason':term_reason,'final_goal_distance_m':terminal.get('goal_distance_m') if terminal else None,'final_position':terminal.get('position') if terminal else None,
                             'ptlib_episode_preparation':episode_preparation,'rtmo_phase_history':rtmo_phase_history if rtmo_enabled else None,
                             'rtmo_final_phase':phase_mgr.phase if rtmo_enabled else None,'rtmo_capability':rtmo_cap.to_dict() if rtmo_cap else None})
        finally:orch.end_online_episode(eid)
        if not episodes[-1]['terminal_reached']:
            print(f"episode {ep} did not reach touchdown/destination; stopping further episodes fail-closed",flush=True); break
    # requested cycles for strict coverage are the cycles actually required by the completed episode sequence, not the theoretical maximum.
    required_cycles=sum(int(e.get('cycles_completed') or 0) for e in episodes)
    return _write_online_reports(orch,cfg,all_records,required_cycles,args,report_root,prefix='workflow-online_jsbsim-',episodes=episodes)

def write_online_not_ready(report_root,requested_cycles,reason,offline_finalize=None,control_readiness=None):
    mode_dir=report_root/'online'; mode_dir.mkdir(parents=True,exist_ok=True)
    contract={'requested':True,'offline_knowledge_loaded_all':False,'setd_package_ids':[],
              'cycles_requested':int(requested_cycles),'cycles_completed':0,'layer6_good_cycles':0,'layer7_ok_cycles':0,'live_ptlib_cycles':0,
              'runtime_coverage_ratio':0.0,'layer7_coverage_ratio':0.0,'strict_cycle_coverage':False,'episode_contract_ok':None,
              'ptlib_layer6_participated_any':False,'layer7_participated_any':False,'validation_only':False,'validation_contract_ok':False,'contract_ok':False}
    stage='PRE_ONLINE_CONTROL_READINESS_GATE' if control_readiness is not None else 'PRE_LAYER6_OFFLINE_KNOWLEDGE_GATE'
    diag={'stage':stage,'reason':reason,'ptlib_invoked':False,'layer6_invoked':False,
          'layer7_invoked':False,'ardupilot_jsbsim_runtime_claimed':False,'offline_knowledge_finalization':offline_finalize,'online_control_readiness':control_readiness}
    _cr=(control_readiness or {'ok':False,'status':'NOT_READY','reason':reason,'blockers':[reason]})
    readiness_taxonomy=write_taxonomy(mode_dir/'ONLINE_READINESS_TAXONOMY.json',control_readiness=_cr,
        phase=None,current_phase_evaluated=False,mission_evaluated=False)
    _claim=dict(_cr.get('claim_scope') or {})
    _limits=dict(_cr.get('claim_limits') or (_cr.get('phase_execution_capability') or {}).get('claim_limits') or {})
    claim_scope_report={'schema':'TDOC-online-claim-scope-v1','status':_claim.get('status') or 'CLAIM_SCOPE_UNAVAILABLE','scope_ceiling':_claim.get('scope_ceiling') or 'SITL_SIMULATION_ONLY','current_sitl_infrastructure_ready':bool(_cr.get('ok')),'claim_limits_block_current_sitl_by_themselves':False,'beyond_sitl_claims_allowed':bool(_claim.get('beyond_sitl_claims_allowed',False)),'commissioning_may_upgrade_claim_limits':False,'claim_limits':_limits,'unmet_beyond_sitl_claims':list(_claim.get('unmet_beyond_sitl_claims') or []),'authority':'Layer5 Phoenix source-bound profile + explicit validation evidence'}
    (mode_dir/'ONLINE_CLAIM_SCOPE.json').write_text(json.dumps(claim_scope_report,indent=2,ensure_ascii=False))
    diag['readiness_taxonomy']=readiness_taxonomy
    diag['claim_scope']=claim_scope_report
    m={'status':'NOT_READY','reason':reason,'cycle_count_requested':int(requested_cycles),'cycle_count_completed':0,'records':[],
       'knowledge_coverage':{'knowledge_target_reached_any':False,'observation_only_seen':False,'actuation_executed_any':False},
       'online_training_contract':contract,'orchestration_diagnostics':diag,'offline_knowledge_finalization':offline_finalize,'online_control_readiness':control_readiness,
       'readiness_taxonomy':readiness_taxonomy,'claim_scope':claim_scope_report,'artifacts':None,'report_error':None}
    (mode_dir/'ONLINE_JSBSIM_SUMMARY.json').write_text(json.dumps(m,indent=2,ensure_ascii=False))
    (mode_dir/'ONLINE_ORCHESTRATION_DIAGNOSTICS.json').write_text(json.dumps(diag,indent=2,ensure_ascii=False))
    return m

def _write_workflow_summary(overall:dict, report_root:Path, *, online_training:bool=False)->None:
    """Atomically publish the current invocation so a crashed run never leaves stale evidence."""
    report_root.mkdir(parents=True,exist_ok=True)
    targets=[report_root/'TRAINING_WORKFLOW_SUMMARY.json']
    if online_training:
        od=report_root/'online'; od.mkdir(parents=True,exist_ok=True)
        targets.append(od/'ONLINE_TRAINING_WORKFLOW_SUMMARY.json')
    text=json.dumps(overall,indent=2,ensure_ascii=False)
    for target in targets:
        tmp=target.with_name(target.name+'.tmp')
        tmp.write_text(text)
        os.replace(tmp,target)

def _install_workflow_summary_guard(overall:dict, report_root:Path, *, online_training:bool=False):
    """Persist a partial forensic summary on SIGINT/SIGTERM/unhandled exit.

    Normal completion changes status away from RUNNING, making the guard a no-op.
    """
    state={'armed':True}
    def checkpoint(reason='process_exit_before_normal_finalization'):
        if not state.get('armed') or overall.get('status')!='RUNNING': return
        overall['status']='INTERRUPTED'
        overall['reason']=reason
        overall['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        cp=report_root/'online'/'ONLINE_PROGRESS_CHECKPOINT.json'
        if cp.is_file():
            try: overall['last_online_checkpoint']=json.loads(cp.read_text())
            except Exception as exc: overall['last_online_checkpoint_error']=repr(exc)
        try:_write_workflow_summary(overall,report_root,online_training=online_training)
        except Exception: pass
    def on_signal(signum,frame):
        checkpoint('SIGINT' if signum==signal.SIGINT else 'SIGTERM')
        raise KeyboardInterrupt if signum==signal.SIGINT else SystemExit(128+int(signum))
    atexit.register(checkpoint)
    try: signal.signal(signal.SIGINT,on_signal); signal.signal(signal.SIGTERM,on_signal)
    except Exception: pass
    return state

def main():
    ap=argparse.ArgumentParser(description='TDOC stateful offline origin-to-destination training + online JSBSim training workflow')
    ap.add_argument('--config',default=str(ROOT/'configs/tdoc.yaml')); ap.add_argument('--request',default=str(ROOT/'examples/runtime_request.json'))
    ap.add_argument('--offline',action='store_true',help='Run only the OFFLINE_SE origin-to-destination training episode; preferred replacement for --skip-online.')
    ap.add_argument('--offline-driver',choices=['mission','legacy'],default='mission',help='mission is the default stateful origin-to-destination trainer; legacy retains fixed cycles.')
    ap.add_argument('--offline-cycles',type=int,default=6,help='Used only with --offline-driver legacy.')
    ap.add_argument('--offline-max-cycles',type=int,default=0,help='Hard safety cycle budget. 0 means reference length + terminal-correction budget; successful touchdown stops early.')
    ap.add_argument('--offline-vertical-proof-dwell-cycles',type=int,default=3,help='Priority-6 bounded same-episode dwell at the final vertical_takeoff/hover reference sample while waiting for a real same-cycle L4+L5+Layer2 joint control proof. 0 disables the dwell; no synthetic capability is created.')
    ap.add_argument('--offline-vertical-proof-climate-window',choices=['auto','forcing-start'],default='auto',help='Priority-6 forcing-epoch policy. auto selects the earliest real contiguous forcing window that stays conservatively within the Phoenix hover-wind envelope; forcing-start preserves the raw forcing start. No forcing value or physical constraint is modified.')
    ap.add_argument('--goal-tolerance-m',type=float,default=25.0); ap.add_argument('--initial-energy-wh',type=float,default=None); ap.add_argument('--offline-terminal-correction-cycles',type=int,default=120); ap.add_argument('--offline-terminal-reposition-altitude-m',type=float,default=12.0); ap.add_argument('--offline-terminal-capture-m',type=float,default=8.0)
    ap.add_argument('--online-cycles',type=int,default=1,help='Maximum consecutive ONLINE_JSBSIM iterations in continuous mode; this is not a mission/episode count.')
    ap.add_argument('--online-episodes','--online-missions',dest='online_episodes',type=int,default=0,help='Number of complete origin-to-destination ONLINE_JSBSIM episodes/missions. 0 uses continuous --online-cycles mode.')
    ap.add_argument('--online-max-cycles-per-episode',type=int,default=320,help='Hard safety cycle cap for each online episode/mission.')
    ap.add_argument('--online-max-cycles-per-episode-auto',action='store_true',help='Resolve the per-mission cycle safety cap automatically from the latest offline mission baseline; falls back to route distance, nominal speed and fast_dt.')
    ap.add_argument('--online-local-hover-seconds',type=float)
    ap.add_argument('--online-local-hover-height-m',type=float,default=3.)
    ap.add_argument('--online-origin-tolerance-m',type=float,default=50.0,help='Maximum horizontal offset from the configured origin at the first live cycle of each online episode.')
    ap.add_argument('--online-goal-tolerance-m',type=float,default=25.0,help='Horizontal destination tolerance for online episode completion.')
    ap.add_argument('--online-touchdown-altitude-m',type=float,default=2.5,help='Relative/AGL altitude threshold used with destination tolerance for online touchdown completion.')
    ap.add_argument('--skip-offline',action='store_true',help='Internal/advanced: reuse an existing finalized offline handoff instead of running a fresh offline episode.')
    ap.add_argument('--skip-online',action='store_true',help=argparse.SUPPRESS)
    ap.add_argument('--terrain-grid-n',type=int,default=70)
    ap.add_argument('--online-training',action='store_true',help='Explicit ONLINE_JSBSIM training continuation. Requires verified finalized offline SETD knowledge and routes live Layer6 evidence through Layer7.')
    ap.add_argument('--offline-knowledge-manifest',default=None,help='Optional TDOC offline handoff manifest (.json.gz). With --online-training, default is runtime/knowledge/handoff/offline_latest.json.gz.')
    ap.add_argument('--online-validation-fixture',default=None,help='Validation-only observation fixture; never sends control and never counts as live online-training success.')
    ap.add_argument('--online-observation-only',action='store_true',help='Allow explicit Layer6/Layer7 observation validation when Phoenix actuation readiness is false; control-training otherwise fails before cycle 1.')
    ap.add_argument('--no-online-auto-commission',action='store_true',help='Disable bounded held-SITL Layer6/ArduPilot commissioning when nominal Phoenix control readiness is missing.')
    args=ap.parse_args()
    if args.skip_online:
        print('[deprecated] --skip-online is retained for compatibility; use --offline instead.',flush=True); args.offline=True
    if args.online_training and args.offline:ap.error('--online-training cannot be combined with --offline')
    if args.online_episodes and not args.online_training:ap.error('--online-episodes/--online-missions requires --online-training')
    if args.online_cycles<=0:ap.error('--online-cycles must be > 0')
    if args.online_episodes<0 or args.online_max_cycles_per_episode<=0:ap.error('--online-episodes must be >= 0 and --online-max-cycles-per-episode must be > 0')
    if args.online_max_cycles_per_episode_auto and not args.online_episodes:ap.error('--online-max-cycles-per-episode-auto requires --online-episodes/--online-missions')
    if args.online_local_hover_seconds is not None and (not args.online_episodes or not 1<=args.online_local_hover_seconds<=60 or not 1<=args.online_local_hover_height_m<=10):
        ap.error('local hover requires an Online episode, 1..60 seconds and 1..10 metres')
    if args.online_origin_tolerance_m<=0 or args.online_goal_tolerance_m<=0 or args.online_touchdown_altitude_m<0:ap.error('online origin/goal/touchdown tolerances are invalid')
    cfg=TDOCConfig.from_yaml(args.config); req0=json.loads(Path(args.request).read_text()); report_root=ROOT/'reports'; report_root.mkdir(exist_ok=True)
    run_provenance=_pin_run_provenance(report_root,cfg.runtime_root)
    overall={'schema':'TDOC-training-workflows-v6','package':'TDOC_v20260911_2052','run_id':f"training-{uuid.uuid4().hex[:16]}",
             'started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'status':'RUNNING','modes':{},
             'invocation':{'argv':list(sys.argv[1:]),'cwd':str(Path.cwd()),'config':str(Path(args.config).resolve()),
                           'online_training':bool(args.online_training),'online_observation_only':bool(args.online_observation_only),
                           'skip_offline':bool(args.skip_offline),'online_episodes':int(args.online_episodes),'online_cycles':int(args.online_cycles)},
             'semantics':{'online_cycles':'maximum consecutive ONLINE_JSBSIM iterations in continuous mode','online_episodes':'complete origin-to-destination/touchdown mission repetitions','online_max_cycles_per_episode':'per-episode safety cap','online_max_cycles_per_episode_auto':'automatic safety cap derived from offline mission baseline or route geometry; mission episode is not a Layer5 retraining episode'},
             'run_provenance':run_provenance}
    _write_workflow_summary(overall,report_root,online_training=bool(args.online_training))
    if not run_provenance.get('ok'):
        overall['status']='NOT_READY'; overall['reason']='run_provenance_invalid'; overall['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); _write_workflow_summary(overall,report_root,online_training=bool(args.online_training)); print(json.dumps({'status':'NOT_READY','reason':'run_provenance_invalid','run_provenance':run_provenance},indent=2),flush=True); return 2
    summary_guard=_install_workflow_summary_guard(overall,report_root,online_training=bool(args.online_training))
    rc=0
    with TDOCOrchestrator(cfg) as orch:
        pf=orch.preflight(); overall['preflight']=pf
        need_offline=not args.skip_offline
        need_online=not args.offline
        offline_ready=bool((pf.get('execution_modes') or {}).get('OFFLINE_SE'))
        online_ready=bool((pf.get('execution_modes') or {}).get('ONLINE_JSBSIM'))
        if (need_offline and not offline_ready) or (need_online and not online_ready):
            overall['status']='NOT_READY'; overall['reason']='preflight_execution_mode_not_ready'; overall['required_modes']={'OFFLINE_SE':need_offline,'ONLINE_JSBSIM':need_online}
            diag={}
            if need_online and not online_ready:
                diag=online_preflight_diagnostics(pf)
                overall['online_preflight_diagnostics']=diag
                diagnostic_event('online.preflight.failed',diagnostics=diag)
            overall['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); _write_workflow_summary(overall,report_root,online_training=bool(args.online_training));
            print(json.dumps({'status':'NOT_READY','required_modes':overall['required_modes'],'execution_modes':pf.get('execution_modes'),'online_preflight_diagnostics':diag},indent=2),flush=True); return 2
        offline_finalize=None
        if need_offline:
            if args.offline_driver=='mission':overall['modes']['OFFLINE_SE']=run_offline_mission(orch,cfg,req0,args,report_root)
            else:overall['modes']['OFFLINE_SE']=run_cycle_mode(orch,cfg,req0,'OFFLINE_SE',args.offline_cycles,args,report_root)
            offline_mode=overall['modes'].get('OFFLINE_SE') or {}
            q=dict(offline_mode.get('knowledge_quality') or ((offline_mode.get('mission') or {}).get('knowledge_quality') or {}))
            _capreg_path=ROOT/'runtime'/'rtmo'/'capability_registry.json.gz'
            _capreg=LiveCapabilityRegistry(_capreg_path).load()
            if _capreg:
                q['live_capability_registry']={
                    'schema':_capreg.get('schema'),'run_id':_capreg.get('run_id'),'episode_id':_capreg.get('episode_id'),
                    'mission_version':_capreg.get('mission_version'),'path':str(_capreg_path),'sha256':_sha256_file(_capreg_path),
                    'authority':'runtime transaction results; reports audit-only',
                }
            climate_alignment={}
            try:
                _cr=cfg.path_value('TDDT_CLIMATE_ROOT',required=True); _fw=forcing_window(_cr); _,climate_alignment=_climate_time_for_elapsed(_fw,0.0,return_metadata=True)
                _fp=resolve_climate_forcing(_cr,require_exists=True); climate_alignment['forcing_file']=str(_fp) if _fp else None; climate_alignment['forcing_file_sha256']=_sha256_file(_fp)
            except Exception as _exc:
                climate_alignment={'ok':False,'reason':'offline_climate_alignment_capture_failed','error':repr(_exc)}
            offline_finalize=orch.finalize_offline_knowledge({'tdoc_training_workflow':'run_training_workflows','offline_driver':args.offline_driver,'knowledge_quality':q,'climate_time_alignment':climate_alignment})
            overall['offline_knowledge_finalization']=offline_finalize
            oval=dict(offline_mode.get('offline_validation') or ((offline_mode.get('mission') or {}).get('offline_validation') or {}))
            oval=offline_validation_view(offline_finalize,oval)
            overall['offline_validation']=oval
            odir=report_root/'offline'; odir.mkdir(parents=True,exist_ok=True); (odir/'OFFLINE_VALIDATION.json').write_text(json.dumps(oval,indent=2,ensure_ascii=False))
            if not (offline_finalize or {}).get('ok'):
                overall['status']='NOT_READY'; overall['reason']='offline_setd_finalization_failed'; rc=2
            if args.offline_driver=='mission':
                from tddt_tdoc.release_gate import write_mission_release_gate
                release_gate=write_mission_release_gate(ROOT)
                overall['mission_release_gate']=release_gate
                if not release_gate.get('ok'):
                    overall['status']='NOT_READY'; overall['reason']='offline_mission_release_gate_failed'; rc=2

        # Reject invalid OFFLINE knowledge at OnlineStart, before
        # climate setup, Layer6 commissioning, or RTMO capability construction.
        # RTMO must never be the first component to discover a semantically
        # invalid reusable handoff. Learned policy may be explicitly unavailable.
        if need_online and rc==0 and args.online_training:
            _online_manifest=Path(args.offline_knowledge_manifest).expanduser() if args.offline_knowledge_manifest else (cfg.runtime_root/'knowledge'/'handoff'/'offline_latest.json.gz')
            online_start_gate=orch.offline_handoff.validate_online_start({'offline_knowledge_manifest':str(_online_manifest)})
            online_start_gate['readiness_scope']='ONLINE_KNOWLEDGE_START_ONLY; not flight/control readiness'
            online_start_gate['actuation_authority']=False
            if online_start_gate.get('ok'):
                _loaded_setd=orch.adapters[5].load_setd()
                orch.adapters[6].offline_knowledge_manifest=str(_online_manifest)
                orch.adapters[5].offline_knowledge_manifest=str(_online_manifest)
                _hm=dict(online_start_gate.get('manifest') or {})
                _expected=((_hm.get('setd') or {}).get('package_id'))
                _actual=((_loaded_setd.get('manifest') or {}).get('package_id') if isinstance(_loaded_setd,dict) else None)
                _q=((_loaded_setd.get('manifest') or {}).get('knowledge_quality') or {}) if isinstance(_loaded_setd,dict) else {}
                _pc=dict(_q.get('phase_control_coverage') or {})
                _missing=list(_pc.get('missing_control_phases') or [])
                _phase_consistent=bool(_pc.get('all_required_control_phases') is True and not _missing)
                _setd_checks={
                    'setd_loaded':bool(isinstance(_loaded_setd,dict) and _loaded_setd.get('ok')),
                    'setd_identity_match':bool(_expected and _actual==_expected),
                }
                _setd_blockers=[k for k,v in _setd_checks.items() if not v]
                online_start_gate['setd_precheck']={
                    'checks':_setd_checks,'blockers':_setd_blockers,
                    'expected_package_id':_expected,'actual_package_id':_actual,
                    'control_knowledge_valid':bool(_q.get('control_knowledge_valid') is True),
                    'offline_control_coverage_complete':_phase_consistent,
                    'mission_certification_status':'FULLY_CERTIFIED' if _phase_consistent else 'NOT_FULLY_CERTIFIED',
                    'missing_control_phases':_missing,
                    'phase_local_runtime_authority':'RTMO_CURRENT_AND_NEXT_PHASE',
                }
                if _setd_blockers:
                    online_start_gate['ok']=False
                    online_start_gate['status']='NOT_READY'
                    online_start_gate['reason']='VALID_FINALIZED_SETD_INTEGRITY_REQUIRED'
                    online_start_gate['blockers']=list(online_start_gate.get('blockers') or [])+['setd:'+x for x in _setd_blockers]
                if online_start_gate.get('ok'):
                    _policy_gate=orch.ensure_online_knowledge_policy(handoff_path=str(_online_manifest))
                    online_start_gate['knowledge_policy_gate']=_policy_gate
                    online_start_gate['policy_required_for_startup']=False
                    online_start_gate['policy_available']=bool(_policy_gate.get('policy_available'))
                    if not _policy_gate.get('ok'):
                        online_start_gate['ok']=False
                        online_start_gate['status']='NOT_READY'
                        online_start_gate['reason']='KNOWLEDGE_POLICY_REJECTED'
                        online_start_gate['blockers']=list(online_start_gate.get('blockers') or [])+[
                            'knowledge_policy:'+str((_policy_gate.get('policy_diagnostics') or {}).get('reason')
                                or _policy_gate.get('reason') or _policy_gate.get('status') or 'not_ready')]
            mode_dir=report_root/'online'; mode_dir.mkdir(parents=True,exist_ok=True)
            (mode_dir/'ONLINE_START_KNOWLEDGE_GATE.json').write_text(json.dumps(online_start_gate,indent=2,ensure_ascii=False))
            overall['online_start_knowledge_gate']=online_start_gate
            _write_workflow_summary(overall,report_root,online_training=True)
            if not online_start_gate.get('ok'):
                requested=args.online_episodes*args.online_max_cycles_per_episode if args.online_episodes else args.online_cycles
                reason=str(online_start_gate.get('reason') or 'VALID_FINALIZED_SETD_INTEGRITY_REQUIRED')
                print('\n=== ONLINE START KNOWLEDGE NOT READY ===',flush=True); print(reason,flush=True)
                overall['modes']['ONLINE_JSBSIM']=write_online_not_ready(report_root,requested,reason,offline_finalize,control_readiness={'ok':False,'blockers':online_start_gate.get('blockers') or [online_start_gate.get('handoff_reason') or online_start_gate.get('reason')],'online_start_knowledge_gate':online_start_gate})
                overall['status']='NOT_READY'; overall['reason']=reason; rc=2

        if need_online and rc==0:
            online_forcing=None
            online_climate_alignment=None
            try:
                climate_root=cfg.path_value('TDDT_CLIMATE_ROOT',required=True)
                online_forcing=forcing_window(climate_root)
                if not online_forcing or online_forcing[0] is None:
                    raise RuntimeError('active climate forcing has no valid time rows')
                overall['online_climate_forcing_window']={'start':online_forcing[0].isoformat(),'end':online_forcing[1].isoformat() if online_forcing[1] else None}
                _mp=Path(args.offline_knowledge_manifest).expanduser() if args.offline_knowledge_manifest else (cfg.runtime_root/'knowledge'/'handoff'/'offline_latest.json.gz')
                online_climate_alignment=_load_online_climate_alignment(cfg.runtime_root,_mp,online_forcing,climate_root)
                # Validate the time contract before any expensive Layer6/SITL commissioning.
                _apply_online_climate_time(copy.deepcopy(req0),online_forcing,0.0,online_climate_alignment)
                online_climate_alignment['validated_before_commissioning']=True
                overall['online_climate_alignment']=online_climate_alignment
            except Exception as exc:
                diagnostic_event("workflow.exception",error=repr(exc),traceback=True)
                requested=args.online_episodes*args.online_max_cycles_per_episode if args.online_episodes else args.online_cycles
                overall['modes']['ONLINE_JSBSIM']=write_online_not_ready(report_root,requested,f'online climate forcing window unavailable: {exc!r}',offline_finalize)
                overall['status']='NOT_READY'; overall['reason']='online_climate_forcing_not_ready'; rc=2
            if rc==0 and args.online_training:
                # R6.05: exercise real transport independently of current flight authority.
                # No ARM or control commissioning follows from transport readiness.
                transport_preparation=orch.prepare_online_transport_session()
                overall['online_native_transport_preparation']=transport_preparation
                if not transport_preparation.get('ok'):
                    overall['status']='NOT_READY';overall['reason']='native_transport_not_ready'
                    _write_workflow_summary(overall,report_root,online_training=True)
                    raise RuntimeError('native transport preparation failed: '+str(transport_preparation))
                control_readiness=orch.online_control_readiness()
                mode_dir=report_root/'online'; mode_dir.mkdir(parents=True,exist_ok=True)
                auto_commission=bool(cfg.get('layers.online_training.auto_commission_if_not_ready',True)) and not bool(args.no_online_auto_commission)
                commissioning=None
                if (not control_readiness.get('ok')) and (not args.online_observation_only) and auto_commission:
                    print('\n=== ONLINE CONTROL COMMISSIONING (held SITL, bounded) ===',flush=True)
                    commissioning=orch.commission_online_control()
                    overall['online_control_commissioning']=commissioning
                    (mode_dir/'ONLINE_CONTROL_COMMISSIONING.json').write_text(json.dumps(commissioning,indent=2,ensure_ascii=False))
                    _write_workflow_summary(overall,report_root,online_training=True)
                    preparation=(commissioning or {}).get('preparation') or ((commissioning or {}).get('certificate') or {}).get('preparation')
                    if isinstance(preparation,dict):
                        overall['online_ptlib_preparation']=preparation
                        (mode_dir/'ONLINE_LAYER6_PREPARATION.json').write_text(json.dumps(preparation,indent=2,ensure_ascii=False))
                    cert=(commissioning or {}).get('certificate') or {}
                    if isinstance(cert,dict) and cert:
                        cdir=cfg.runtime_root/'control'; cdir.mkdir(parents=True,exist_ok=True)
                        (cdir/'phoenix_control_commission.json').write_text(json.dumps(cert,indent=2,ensure_ascii=False))
                    control_readiness=orch.online_control_readiness()
                args.online_control_readiness=control_readiness
                overall['online_control_readiness']=control_readiness
                (mode_dir/'ONLINE_CONTROL_READINESS.json').write_text(json.dumps(control_readiness,indent=2,ensure_ascii=False))
                online_claim_scope=dict((control_readiness or {}).get('claim_scope') or {})
                online_claim_limits=dict((control_readiness or {}).get('claim_limits') or ((control_readiness or {}).get('phase_execution_capability') or {}).get('claim_limits') or {})
                claim_scope_report={
                    'schema':'TDOC-online-claim-scope-v1',
                    'status':online_claim_scope.get('status') or 'CLAIM_SCOPE_UNAVAILABLE',
                    'scope_ceiling':online_claim_scope.get('scope_ceiling') or 'SITL_SIMULATION_ONLY',
                    'current_sitl_infrastructure_ready':bool((control_readiness or {}).get('ok')),
                    'claim_limits_block_current_sitl_by_themselves':False,
                    'beyond_sitl_claims_allowed':bool(online_claim_scope.get('beyond_sitl_claims_allowed',False)),
                    'commissioning_may_upgrade_claim_limits':False,
                    'claim_limits':online_claim_limits,
                    'unmet_beyond_sitl_claims':list(online_claim_scope.get('unmet_beyond_sitl_claims') or []),
                    'authority':'Layer5 Phoenix source-bound profile + explicit validation evidence',
                    'note':'Infrastructure/SITL readiness is independent from broader validation claims. Successful commissioning must not promote autonomous-VTOL, full-envelope-aero, or real-aircraft claims.',
                }
                overall['online_claim_scope']=claim_scope_report
                (mode_dir/'ONLINE_CLAIM_SCOPE.json').write_text(json.dumps(claim_scope_report,indent=2,ensure_ascii=False))
                readiness_taxonomy=write_taxonomy(mode_dir/'ONLINE_READINESS_TAXONOMY.json',control_readiness=control_readiness,
                    phase=None,current_phase_evaluated=False,mission_evaluated=False)
                overall['online_readiness_taxonomy']=readiness_taxonomy
                l6_diag=getattr((orch.adapters or {}).get(6),'_startup_log_diagnostics',None)
                startup_diag=l6_diag() if callable(l6_diag) else {
                    'classification':'DIAGNOSTIC_ONLY','readiness_authority':False,
                    'known_benign_messages':['Loaded defaults from ...','validate_structures:...: Validating structures','Skipping port (null)'],
                    'operational_gate_sources':['Layer6/GUI/MAVLink structured readiness','Layer5 control readiness','RTMO phase capability']}
                startup_diag['control_readiness_snapshot']={'ok':bool(control_readiness.get('ok')),'status':control_readiness.get('status'),'reason':control_readiness.get('reason'),'claim_scope_status':claim_scope_report.get('status'),'scope_ceiling':claim_scope_report.get('scope_ceiling'),'beyond_sitl_claims_allowed':claim_scope_report.get('beyond_sitl_claims_allowed')}
                startup_diag['claim_scope']=claim_scope_report
                overall['online_startup_diagnostics']=startup_diag
                (mode_dir/'ONLINE_STARTUP_DIAGNOSTICS.json').write_text(json.dumps(startup_diag,indent=2,ensure_ascii=False))
                print('Layer6 native startup text: diagnostic-only; readiness authority = structured Layer6/GUI/MAVLink + Layer5 + RTMO gates',flush=True)
                _write_workflow_summary(overall,report_root,online_training=True)
                if (not control_readiness.get('ok') or control_readiness.get('nominal_control_ready') is False or control_readiness.get('flight_release_blockers')) and not args.online_observation_only:
                    requested=args.online_episodes*args.online_max_cycles_per_episode if args.online_episodes else args.online_cycles
                    blockers=', '.join(control_readiness.get('flight_release_blockers') or control_readiness.get('blockers') or ['nominal_control_not_ready'])
                    reason=f'online control-training blocked before cycle 1 after bounded commissioning: {blockers}; use --online-observation-only only for telemetry/Layer7 validation'
                    print('\n=== ONLINE CONTROL NOT READY ===',flush=True); print(reason,flush=True)
                    overall['modes']['ONLINE_JSBSIM']=write_online_not_ready(report_root,requested,reason,offline_finalize,control_readiness=control_readiness)
                    overall['status']='NOT_READY'; overall['reason']='online_control_actuation_not_ready'; rc=2
            if rc==0 and args.online_training and bool(cfg.get('tdoc.rtmo.enabled',False)) and args.online_episodes:
                # Capability is evaluated only after bounded Online commissioning, so
                # L6 evidence reflects the active Layer6/ArduPilot execution path.
                # This never promotes L4/L5 phases lacking their own evidence.
                mode_dir=report_root/'online'; mode_dir.mkdir(parents=True,exist_ok=True)
                ms=dict((control_readiness or {}).get('mapping_status') or {})
                phase_exec=dict((control_readiness or {}).get('phase_execution_capability') or ms.get('phase_execution_capability') or {})
                phase_supported=dict(phase_exec.get('phase_supported') or {})
                # on_ground/touchdown are passive execution states; their L4/L5 semantic
                # readiness is handled separately (problem #10).  Controlled VTOL phases
                # are authorized only by phase-scoped execution evidence, never by a
                # blanket autonomous-ArduPilot claim.
                execution_overrides={'on_ground':True,'touchdown':True}
                for _p in ('vertical_takeoff','hover','transition_forward','transition_back','vertical_landing'):
                    if _p in phase_supported:
                        execution_overrides[_p]=bool(phase_supported[_p])
                vtol_exec_ready=all(bool(phase_supported.get(_p,False)) for _p in ('vertical_takeoff','hover','transition_back','vertical_landing'))
                rtmo_cap=build_capability_manifest(report_root, model_dependencies_provider=(lambda: capture_model_dependencies(cfg)) if integrated_native(cfg) else None,layer6_forward_only=not vtol_exec_ready,execution_overrides=execution_overrides, certificate_bank_path=(cfg.path_value('TDDT_LAYER4_ROOT',required=True)/'runtime/verification/ff_certificates.jsonl'), require_certificate_guard=bool(cfg.get('tdoc.rtmo.l4_artifacts.require_live_certificate_guard',True)), capability_registry_path=(ROOT/'runtime'/'rtmo'/'capability_registry.json.gz'), require_live_registry=bool(cfg.get('tdoc.rtmo.capability.require_live_registry',True)))
                rtmo_cap_dict=rtmo_cap.to_dict()
                rtmo_cap_dict['execution_profile']={
                    'forward_only':bool(ms.get('forward_only',True)),
                    'vtol_force_model_validated':bool(ms.get('vtol_force_model_validated',False)),
                    'vtol_autopilot_controller_validated':bool(ms.get('vtol_autopilot_controller_validated',False)),
                    'phase_execution_capability':phase_exec,
                    'claim_scope':dict((control_readiness or {}).get('claim_scope') or phase_exec.get('claim_scope') or {}),
                    'claim_limits':dict((control_readiness or {}).get('claim_limits') or phase_exec.get('claim_limits') or {}),
                    'commissioned_nominal_control':bool((control_readiness or {}).get('nominal_control_ready',False)),
                    'authority_semantics':'Layer5 external controller -> Layer6 -> ArduPilot authoritative mixer -> JSBSim; direct PWM forbidden',
                    'claim_semantics':'SITL commissioning does not upgrade autonomous-ArduPilot, full-envelope-aerodynamics, or real-aircraft validation claims',
                }
                (mode_dir/'RTMO_PHASE_CAPABILITY.json').write_text(json.dumps(rtmo_cap_dict,indent=2,ensure_ascii=False))
                overall['rtmo_phase_capability']=rtmo_cap_dict
                _write_workflow_summary(overall,report_root,online_training=True)
                # Solution 16: mission-wide certification is a trust/audit signal, not
                # an admission gate before cycle 1. Distant missing phases are warnings;
                # current-phase actuation and imminent transitions are gated locally in
                # run_online_episodes().
                mission_cert=rtmo_cap_dict.get('mission_certification') or {}
                (mode_dir/'RTMO_MISSION_CERTIFICATION.json').write_text(json.dumps(mission_cert,indent=2,ensure_ascii=False))
                readiness_taxonomy=write_taxonomy(mode_dir/'ONLINE_READINESS_TAXONOMY.json',control_readiness=(control_readiness or {}),
                    phase='on_ground',current_phase_evaluated=False,mission_certification=mission_cert,mission_evaluated=True)
                overall['online_readiness_taxonomy']=readiness_taxonomy
                if not rtmo_cap.ready_11phase:
                    overall['rtmo_mission_certification_warning']={
                        'status':'NOT_FULLY_CERTIFIED','blocked_phases':rtmo_cap.blocked_phases,
                        'admission_blocked':False,'policy':'PHASE_LOCAL_RUNTIME_RELEASE'}
                    print('\n=== RTMO MISSION NOT FULLY CERTIFIED (non-blocking) ===',flush=True)
                    print('Distant phase gaps: '+','.join(rtmo_cap.blocked_phases),flush=True)
            if rc==0 and args.online_training and need_offline and not (offline_finalize or {}).get('ok'):
                requested=args.online_episodes*args.online_max_cycles_per_episode if args.online_episodes else args.online_cycles
                overall['modes']['ONLINE_JSBSIM']=write_online_not_ready(report_root,requested,'valid finalized offline SETD-KStore required before online training',offline_finalize)
                rc=2
            elif rc==0:
                if args.online_training and args.online_episodes:
                    overall['modes']['ONLINE_JSBSIM']=run_online_episodes(orch,cfg,req0,args,report_root,online_forcing=online_forcing,online_climate_alignment=online_climate_alignment,
                        initial_rtmo_cap=(rtmo_cap if 'rtmo_cap' in locals() else None), rtmo_execution_overrides=(execution_overrides if 'execution_overrides' in locals() else None))
                    _write_workflow_summary(overall,report_root,online_training=True)
                else:
                    overall['modes']['ONLINE_JSBSIM']=run_cycle_mode(orch,cfg,req0,'ONLINE_JSBSIM',args.online_cycles,args,report_root,online_forcing=online_forcing,online_climate_alignment=online_climate_alignment)
                    _write_workflow_summary(overall,report_root,online_training=bool(args.online_training))
    if args.online_training:
        oc=((overall.get('modes') or {}).get('ONLINE_JSBSIM') or {}).get('online_training_contract') or {}
        if not oc.get('contract_ok'):
            rc=2
            if overall.get('status')=='RUNNING': overall['status']='NOT_READY'
            overall.setdefault('reason','online_training_contract_not_ok')
    overall['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    if overall.get('status')=='RUNNING':
        overall['status']='OK' if rc==0 else 'FAILED'
    _write_workflow_summary(overall,report_root,online_training=bool(args.online_training))
    summary_guard['armed']=False
    print(f"\nSummary: {report_root/'TRAINING_WORKFLOW_SUMMARY.json'}")
    return rc
if __name__=='__main__': raise SystemExit(main())
