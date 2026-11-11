"""Audit a finished OFFLINE mission without granting execution authority.

A finalized SETD prior is not a flight-completion certificate. Every invocation
writes an atomic positive or negative report, bound to source-file hashes.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd


def _truth(value):
    if isinstance(value, str):
        return value.strip().lower() in {'true','1','yes'}
    return bool(value) if pd.notna(value) else False


def write_mission_release_gate(root: Path | str) -> dict:
    root=Path(root)
    directory=root/'reports/offline/mission'
    paths={'timeline':directory/'MISSION_TIMELINE.csv','summary':directory/'MISSION_SUMMARY.json'}
    report={'schema':'TDOC-mission-fix-release-gate-v2','generated_at':datetime.now(timezone.utc).isoformat(),
            'scope':'OFFLINE_SIMULATION_AUDIT_ONLY','online_actuation_authority':False,
            'source_files':{},'checks':{},'ok':False}
    try:
        for key,path in paths.items():
            report['source_files'][key]={'path':str(path.relative_to(root)),
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        df=pd.read_csv(paths['timeline']); summary=json.loads(paths['summary'].read_text())
        if df.empty: raise ValueError('empty_mission_timeline')
        for column in ('realized_x_m','realized_y_m','agl_m','goal_distance_m','wind_counterfactual_displacement_m'):
            numeric=pd.to_numeric(df[column],errors='raise')
            if not numeric.map(math.isfinite).all():
                raise ValueError(f'non_finite_mission_measurement:{column}')
        required={'on_ground','vertical_takeoff','hover','transition_forward','climb','cruise','turn','approach','transition_back','vertical_landing','touchdown'}
        phases=set(df.phase.astype(str))
        x=df.realized_x_m.astype(float).to_numpy(); y=df.realized_y_m.astype(float).to_numpy()
        dx=x[-1]-x[0]; dy=y[-1]-y[0]; den=math.hypot(dx,dy) or 1.0
        cross=max(abs(dy*(xx-x[0])-dx*(yy-y[0]))/den for xx,yy in zip(x,y))
        coverage=(summary.get('knowledge_quality') or {}).get('phase_control_coverage') or {}
        checks={
            'starts_on_ground':bool(df.iloc[0].agl_m<=1.0 and df.iloc[0].phase=='on_ground'),
            'required_phases':required.issubset(phases),
            'touchdown_final':bool(df.iloc[-1].phase=='touchdown' and df.iloc[-1].agl_m<=1.0),
            'goal_reached':bool(df.iloc[-1].goal_distance_m<=float(summary.get('goal_tolerance_m',25.0))),
            'no_teacher_forcing':not df.teacher_forced.map(_truth).any(),
            'sarg_reference_only':not df.sarg_used_as_next_state.map(_truth).any(),
            'wind_causal_effect':bool(df.wind_counterfactual_displacement_m.max()>0.1),
            'realized_not_straight_chord':bool(cross>0.1),
            'layer4_advisory_participation':bool(df.l4_advisory_applied.map(_truth).any()),
            'fast_meso_slow':all(df[c].map(_truth).any() for c in ('fast_event','meso_event','slow_event')),
            'mission_summary_complete':summary.get('mission_complete') is True,
            'summary_matches_timeline':summary.get('cycles_completed')==len(df),
            'control_ready':summary.get('offline_control_ready') is True,
            'all_required_control_phases':coverage.get('all_required_control_phases') is True and not coverage.get('missing_control_phases'),
        }
        report.update(checks={k:bool(v) for k,v in checks.items()},ok=all(checks.values()),
            cycles_completed=len(df),final_goal_distance_m=float(df.iloc[-1].goal_distance_m),
            final_agl_m=float(df.iloc[-1].agl_m),max_cross_track_from_chord_m=float(cross),
            phases_seen=list(dict.fromkeys(df.phase.astype(str))),
            max_wind_counterfactual_displacement_m=float(df.wind_counterfactual_displacement_m.max()),
            missing_control_phases=list(coverage.get('missing_control_phases') or []))
    except Exception as exc:
        report['checks']['complete_parseable_evidence']=False
        report['error']=f'{type(exc).__name__}: {exc}'
    report['failed_checks']=[k for k,v in report['checks'].items() if not v]
    target=root/'reports/validation/MISSION_FIX_RELEASE_GATE.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False))
    temporary.replace(target)
    return report
