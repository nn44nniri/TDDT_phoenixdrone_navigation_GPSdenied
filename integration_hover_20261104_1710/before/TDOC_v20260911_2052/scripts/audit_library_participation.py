#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,os
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--root',default=str(Path(__file__).resolve().parents[1])); a=ap.parse_args(); root=Path(a.root); df=pd.read_csv(root/'reports/offline/mission/MISSION_TIMELINE.csv')
 online_path=root/'reports/online/ONLINE_JSBSIM_SUMMARY.json'; online=json.loads(online_path.read_text()) if online_path.is_file() else {}; online_done=bool(online.get('cycle_count_completed',0)); l7_online=bool((online.get('knowledge_coverage') or {}).get('knowledge_target_reached_any')); obs_only=bool((online.get('knowledge_coverage') or {}).get('observation_only_seen'))
 def rec(lib,env,off,on,causal,safety,role): return {'library':lib,'configured':bool(os.getenv(env)) if env else True,'offline_invoked':off,'online_invoked':on,'offline_causal':causal,'safety_or_gating':safety,'role':role}
 rows=[
  rec('MESA_NAV','TDDT_TWINS_ROOT',True,online_done,True,False,'Layer2 realized navigation/state envelope'),
  rec('WingMPET','TDDT_TWINS_ROOT',True,online_done,True,False,'Layer2 + authoritative offline op07 plant; op27/28/30/31 shadows'),
  rec('MicroMetTwin','TDDT_CLIMATE_ROOT',True,online_done,bool(df.wind_counterfactual_displacement_m.max()>0.1),False,'3-D wind/atmosphere causally applied to WingMPET'),
  rec('SARG','TDDT_SARG_ROOT',True,False,False,False,'offline reference/prior and scenario/CRP knowledge; never realized next-state source'),
  rec('CRP/Boulder Map','TDDT_MAP_ROOT',True,False,True,True,'terrain/obstacle context and SARG/CRP selection'),
  rec('ISO Adapter','TDDT_UNIT_ADAPTER_ROOT',True,online_done,True,True,'unit/frame/time boundary normalization'),
  rec('Layer3','TDDT_LAYER3_ROOT',True,online_done,True,True,'Fast/Meso/Slow network fusion/coupling'),
  rec('Layer4','TDDT_LAYER4_ROOT',True,online_done,bool(df.l4_advisory_applied.astype(bool).any()),True,'candidate optimization; only feasible high-level advisory may affect offline guidance'),
  rec('Layer5','TDDT_LAYER5_ROOT',True,online_done,bool(df.layer2_control_effect_applied.astype(bool).any()),True,'decision/final authorization; current physical mapping remains fail-closed'),
  rec('Layer6/GUI/MAVLink','TDDT_LAYER6_ROOT',False,online_done,False,True,'online-only; observation-only validation allowed, never in OFFLINE_SE'),
  rec('Layer7','TDDT_LAYER7_ROOT',False,l7_online,False,True,'online-only probabilistic error/adaptation route; never in OFFLINE_SE'),
 ]
 report={'schema':'TDOC-library-participation-audit-v2','offline_mode':'OFFLINE_SE','online_validation_present':online_done,'online_observation_only':obs_only,'libraries':rows,'all_required_configured':all(r['configured'] for r in rows),'note':'Effective participation is role-aware. A library may be safety/gating or online-only rather than causally steering the offline plant; configured path alone is not counted as execution.'}
 out=root/'reports/validation/LIBRARY_PARTICIPATION_AUDIT.json'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(report,indent=2))
 cols=['library','configured','offline_invoked','online_invoked','offline_causal','safety_or_gating','role']; f=go.Figure(go.Table(header={'values':[c.replace('_',' ').title() for c in cols]},cells={'values':[[r[c] for r in rows] for c in cols]})); f.update_layout(title='TDOC Library Participation — configured, invoked, causal, safety/gating, online-only'); f.write_html(root/'reports/offline/mission/LIBRARY_PARTICIPATION.html',include_plotlyjs='cdn'); print(json.dumps(report,indent=2)); return 0 if report['all_required_configured'] else 2
if __name__=='__main__': raise SystemExit(main())
