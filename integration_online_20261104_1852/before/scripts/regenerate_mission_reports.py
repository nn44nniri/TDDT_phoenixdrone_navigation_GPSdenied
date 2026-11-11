#!/usr/bin/env python3
from __future__ import annotations
import argparse,gzip,importlib.util,json,math
from pathlib import Path
import pandas as pd
from tddt_tdoc.storage import read_json_auto, write_json_gz


def _finite(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except Exception:return None


def _reconstruct_details(root:Path, df:pd.DataFrame):
    details=[]
    cycles=root/'runtime/cycles'
    for _,row in df.iterrows():
        tx=str(row.get('transaction_id'))
        p=cycles/f'{tx}.json.gz'
        cyc=read_json_auto(p) if p.is_file() else {}
        ds=cyc.get('downstream_layers') or {}; l5=((ds.get('5') or {}).get('output') or {}).get('result') or {}
        l4=cyc.get('layer4_result') or {}; best={}
        cid=row.get('l4_best_candidate_id')
        for c in l4.get('candidates') or []:
            if str(c.get('candidate_id'))==str(cid): best=c; break
        climate=(((cyc.get('layer2_packets') or {}).get('MicroMetTwin') or {}).get('state') or {})
        details.append({
            'cycle':int(row['cycle']),'transaction_id':tx,
            'time_location':{'reference_time_s':_finite(row.get('reference_time_s')),'latitude_deg':_finite(row.get('latitude_deg')),'longitude_deg':_finite(row.get('longitude_deg')),'realized_x_m':_finite(row.get('realized_x_m')),'realized_y_m':_finite(row.get('realized_y_m')),'altitude_msl_m':_finite(row.get('realized_z_msl_m')),'agl_m':_finite(row.get('agl_m'))},
            'mission':{'phase':row.get('phase'),'goal_distance_m':_finite(row.get('goal_distance_m')),'energy_remaining_wh':_finite(row.get('energy_remaining_wh')),'state_source':row.get('state_source'),'teacher_forced':bool(row.get('teacher_forced')),'sarg_used_as_next_state':bool(row.get('sarg_used_as_next_state'))},
            'uav_navigation':(((cyc.get('layer2_packets') or {}).get('MESA_NAV') or {}).get('state') or {}),
            'flight_state':(((cyc.get('layer2_packets') or {}).get('WingMPET') or {}).get('state') or {}),
            'sensors':{name:{'state':(packet or {}).get('state'),'quality':(packet or {}).get('quality'),'uncertainty':(packet or {}).get('uncertainty'),'validity':(packet or {}).get('validity')} for name,packet in (cyc.get('layer2_packets') or {}).items()},
            'climate':climate,'layer4_selected_candidate':best,
            'layer5_decision':{'decision':l5.get('decision'),'gates':l5.get('gates'),'fallback':l5.get('fallback')},
            'offline_plant':{'realized_state':{'x':_finite(row.get('realized_x_m')),'y':_finite(row.get('realized_y_m')),'h':_finite(row.get('realized_z_msl_m'))},'counterfactual_state':{'x':_finite(row.get('counterfactual_x_m')),'y':_finite(row.get('counterfactual_y_m')),'h':_finite(row.get('counterfactual_z_msl_m'))},'wind_counterfactual_displacement_m':_finite(row.get('wind_counterfactual_displacement_m')),'simulation_reference_actuators':{'left_motor_cmd':_finite(row.get('simulation_reference_left_motor_cmd')),'right_motor_cmd':_finite(row.get('simulation_reference_right_motor_cmd')),'left_elevon_rad':_finite(row.get('simulation_reference_left_elevon_rad')),'right_elevon_rad':_finite(row.get('simulation_reference_right_elevon_rad'))},'shadow_6dof_op27':{'flight_mode_code':_finite(row.get('shadow_flight_mode_code')),'vtol_feasible':row.get('shadow_vtol_feasible')},'envelope_op30':{'vtol_envelope_valid':row.get('vtol_envelope_valid')},'power_op31':{'selected_power':_finite(row.get('predicted_power_w'))}}
        })
    return details


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default=str(Path(__file__).resolve().parents[1])); a=ap.parse_args(); root=Path(a.root).resolve(); out=root/'reports/offline/mission'
    spec=importlib.util.spec_from_file_location('wf_report',root/'scripts/run_training_workflows.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    df=pd.read_csv(out/'MISSION_TIMELINE.csv'); cdf=pd.read_csv(out/'LAYER4_CANDIDATES.csv')
    # AGL is local terrain clearance. Older v3 rows accidentally used destination-ground elevation.
    if 'terrain_clearance_m' in df.columns: df['agl_m']=pd.to_numeric(df['terrain_clearance_m'],errors='coerce').clip(lower=0.0)
    # Full run may have completed dynamics but failed while serializing a NaN-rich detail payload.
    # Reconstruct the immutable reports from authoritative cycle gzip snapshots + timeline.
    summary_path=out/'MISSION_SUMMARY.json'
    if summary_path.is_file(): summary=json.loads(summary_path.read_text())
    else:
        last=df.iloc[-1]; loops={'fast':int(len(df)),'meso':int(df['meso_event'].fillna(False).astype(bool).sum()),'slow':int(df['slow_event'].fillna(False).astype(bool).sum())}
        reached=bool(str(last.get('phase'))=='touchdown' and float(last.get('goal_distance_m',1e9))<=25.0 and float(last.get('agl_m',1e9))<=1.0)
        summary={'schema':'TDOC-offline-realized-wingmpet-v3','package':'TDOC_v20260911_2052','mode':'OFFLINE_SE','reference_route':{'scenario_id':3,'crp_id':14,'route':'S03_C014.csv','shortlist':[]},'initial_energy_wh':float(df['energy_remaining_wh'].iloc[0]),'remaining_energy_wh':float(last['energy_remaining_wh']),'goal_tolerance_m':25.0,'goal_reference_reached':reached,'touchdown_reached':reached,'cycles_completed':int(len(df)),'loop_counts':loops,'state_sources':{'closed_loop_cycles':int(df['layer2_control_effect_applied'].fillna(False).astype(bool).sum()),'teacher_forced_reference_cycles':int(df['teacher_forced'].fillna(False).astype(bool).sum()),'wingmpet_simulation_cycles':int(len(df)-df['layer2_control_effect_applied'].fillna(False).astype(bool).sum())},'sarg_used_as_next_state':bool(df['sarg_used_as_next_state'].fillna(False).astype(bool).any()),'l4_advisory_cycles':int(df['l4_advisory_applied'].fillna(False).astype(bool).sum()),'semantic_note':'SARG is reference/prior only. Realized state is propagated by external WingMPET op07 with MicroMet 3-D wind; zero-wind same-command counterfactual is recorded. Layer5 physical authorization remains fail-closed.'}
    ref=summary.get('reference_route') or {}; sarg=m.envpath('TDDT_SARG_ROOT'); route_path=Path(ref.get('route') or '')
    if not route_path.is_file(): route_path=sarg/'routes'/f"S{int(ref.get('scenario_id',3)):02d}_C{int(ref.get('crp_id',14)):03d}.csv"
    route=pd.read_csv(route_path).sort_values('point_id').reset_index(drop=True); wrapped=m.build_vtol_reference(route)
    summary['schema']='TDOC-offline-realized-wingmpet-v4'; summary['package']='TDOC_v20260909_1144'
    summary.setdefault('reference_cycle_count',int(len(wrapped)))
    summary.setdefault('terminal_correction_cycle_budget',120)
    summary.setdefault('cycle_budget_max',int(len(wrapped))+int(summary['terminal_correction_cycle_budget']))
    summary.setdefault('unused_cycle_budget',max(0,int(summary['cycle_budget_max'])-int(len(df))))
    summary.setdefault('termination_reason','touchdown_goal_reached' if bool(summary.get('touchdown_reached')) else 'historical_runtime_regenerated')
    summary.setdefault('cycle_budget_semantics','cycle_budget_max is a safety upper bound, not a required run length; successful touchdown terminates the mission immediately')
    details_path=out/'DECISION_POINT_DETAILS.json.gz'
    try:
        with gzip.open(details_path,'rt',encoding='utf-8') as f: details=json.load(f)
        if len(details)!=len(df): raise ValueError('detail count mismatch')
    except Exception:
        details=_reconstruct_details(root,df); write_json_gz(details_path,details,indent=2)
    choice={k:v for k,v in ref.items() if k!='shortlist'}
    m.mission_reports(out,df.to_dict('records'),cdf.to_dict('records'),choice,wrapped,summary,details)
    m.postprocess_mission_diagnostics(out)
    print('regenerated',out,'cycles',len(df),'details',len(details)); return 0
if __name__=='__main__': raise SystemExit(main())
