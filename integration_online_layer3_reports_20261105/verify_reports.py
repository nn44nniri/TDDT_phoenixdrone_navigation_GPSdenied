import csv,gzip,hashlib,json,sys
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import unquote
sys.path.insert(0,str(Path('TDOC_v20260911_2052/scripts').resolve()))
from online_reports.provenance import snapshot,compare,file_hash,write_json
from tddt_tdoc.config import TDOCConfig
A=Path('integration_online_layer3_reports_20261105');base=Path('TDOC_v20260911_2052/reports/online');run=base/'runs/20261105_095929_eac3f73b';out=run/'mission';native=run/'evidence/native';m=json.loads((out/'REPORT_MANIFEST.json').read_text())
assert all(file_hash(out/f['path'])==f['sha256'] for f in m['files'])
assert all(file_hash(v['path'])==v['sha256'] for v in m['source_files'].values())
for name,value in m['report_generator_sources'].items():
 assert file_hash(Path('TDOC_v20260911_2052/scripts/online_reports')/name)==value,name
expected=[json.loads(x) for x in (native/'cycles.jsonl').open()]
with (out/'MISSION_TIMELINE.csv').open() as f:timeline=list(csv.DictReader(f))
with (out/'LAYER4_CANDIDATES.csv').open() as f:candidates=list(csv.DictReader(f))
assert len(timeline)==len(expected)==4458
assert len(candidates)==sum(len(r['L4']['candidates']) for r in expected)
for a,b in zip(timeline,expected):
 assert a['transaction_id']==b['transaction_id']
 for i in range(4):
  assert abs(float(a['l5_pilot_cmd_'+str(i)])-b['L5']['u_cmd'][i])<1e-12
with gzip.open(out/'DECISION_POINT_DETAILS.json.gz','rt') as f:
 assert len(json.load(f))==len(expected)
h=hashlib.sha256()
with gzip.open(out/'RAW_CYCLE_RECORDS.jsonl.gz','rb') as f:
 for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
assert h.hexdigest()==file_hash(native/'cycles.jsonl')
missing=[];external=[]
class Links(HTMLParser):
 def __init__(self,p):super().__init__();self.p=p
 def handle_starttag(self,tag,attrs):
  for k,v in attrs:
   if k in ('src','href') and v and not v.startswith('#'):
    if v.startswith(('http:','https:','//')):external.append((str(self.p),v))
    else:
     q=unquote(v.split('#')[0]);target=self.p.parent/q
     if not target.exists():missing.append((str(self.p),q))
for p in out.glob('*.html'):Links(p).feed(p.read_text())
assert not missing,missing
assert not external,external
for p in (base/'mission').glob('*.html'):Links(p).feed(p.read_text())
assert not missing,missing
cfg=TDOCConfig.from_yaml(Path('TDOC_v20260911_2052/configs/tdoc.yaml').resolve());pres=compare(json.loads((run/'evidence/preservation_before.json').read_text()),snapshot(cfg));assert pres['status']=='PASS'
old=json.loads(Path('integration_realtime_20261104_1855/source_manifest.json').read_text());assert all(file_hash(f['path'])==f['after_sha256'] for f in old['files'])
eq=json.loads((out/'REPORT_EQUIVALENCE.json').read_text());assert len(eq['artifacts'])==28 and eq['all_offline_artifact_names_covered']
q=json.loads((out/'NATIVE_QUALIFICATION.json').read_text());assert q['status']=='PASS'
result={'status':'PASS','native_session_id':q['native_session_id'],'same_model_id':pres['model_id'],'cycles':len(timeline),'candidate_rows':len(candidates),'offline_artifacts_covered':28,'html_reports':len(list(out.glob('*.html'))),'all_report_hashes_verified':True,'control_commands_match_raw_cycles':True,'compressed_raw_archive_byte_exact':True,'all_local_links_work_including_latest_aliases':True,'external_browser_assets':False,'historical_36_changed_files_unchanged':True,'preservation_after_reporting':pres,'actual_goal_error_m':q['actual_goal_error_m_independent']}
write_json(A/'FINAL_VERIFICATION.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))

import math
perf=json.loads((out/'LANDING_ENERGY_TIME_METRICS.json').read_text())
terrain=json.loads((out/'TERRAIN_CONTEXT.json').read_text())
grid=json.loads((out/'TERRAIN_SURFACE.json').read_text())
assert terrain['available'] and terrain['binding_sha256_verified']
assert file_hash(terrain['source_raster'])==terrain['source_sha256']
assert terrain['used_for_control'] is False
assert terrain['route_draped_or_shifted_to_terrain'] is False
assert grid['height_offset_m']==0 and grid['height_exaggeration']==1
assert len(grid['elevation_msl_m'])==90 and all(len(row)==90 for row in grid['elevation_msl_m'])
assert terrain['grid_max_elevation_msl_m']>terrain['grid_min_elevation_msl_m']+1
assert math.isclose(sum(x['duration_s'] for x in perf['phases']),perf['time']['control_duration_s'])
assert math.isclose(sum(x['energy_used_wh'] for x in perf['phases']),perf['energy']['consumed_wh'])
assert perf['landing_accuracy']['GNSS_disabled_verified'] is True
assert perf['landing_accuracy']['physical_truth_used_for_navigation'] is False
assert math.isclose(perf['landing_accuracy']['actual_horizontal_error_m'],q['actual_goal_error_m_independent'])
with (out/'TERRAIN_ROUTE_HISTORY.csv').open() as f: terrain_rows=list(csv.DictReader(f))
assert len(terrain_rows)==len(expected)
for row, raw in zip(terrain_rows,expected):
 assert float(row['physical_altitude_msl_m'])==raw['external_validation_only']['SIM_STATE']['data']['alt']
 assert abs(float(row['navigation_altitude_msl_m'])-(raw['L3']['origin']['altitude_msl_m']-raw['L2']['navigation']['position_ned_m'][2]))<1e-10
result.update(terrain_hash_verified=True,terrain_grid_shape=terrain['grid_shape'],terrain_route_unshifted=True,
 time_s=perf['time']['control_duration_s'],energy_wh=perf['energy']['consumed_wh'],phase_totals_verified=True)
write_json(A/'FINAL_VERIFICATION.json',result)
print('Terrain and landing/energy/time extensions: PASS')

import numpy as np
with (out/'ROUTE_WIND_HISTORY.csv').open() as f: wind_rows=list(csv.DictReader(f))
with (out/'ROUTE_PHASE_DETAILS.csv').open() as f: phase_rows=list(csv.DictReader(f))
assert len(wind_rows)==len(expected)
assert [int(x['sample_count']) for x in phase_rows]==[102,4083,141,79,53]
assert sum(int(x['sample_count']) for x in phase_rows)==len(expected)
for w,r in zip(wind_rows,expected):
 n,e,d=r['L2']['climate']['wind_ned_m_s']
 assert all(abs(float(w[k])-v)<1e-12 for k,v in [('wind_n_m_s',n),('wind_e_m_s',e),('wind_d_m_s',d)])
 assert math.isclose(float(w['display_up_m_s']),-d)
 if math.hypot(n,e)>1e-6:
  to=math.degrees(math.atan2(e,n))%360
  assert abs(float(w['flow_to_bearing_deg'])-to)<1e-10
  assert abs(float(w['meteorological_from_bearing_deg'])-(to+180)%360)<1e-10
context=json.loads((out/'ROUTE_DETAIL_CONTEXT.json').read_text())
assert context['phase_segments']==5 and context['wind_rows']==4458
assert context['control_state_changed'] is False
result.update(phase_detail_cycle_counts=context['phase_cycle_counts'],wind_rows_verified=len(wind_rows),
 wind_direction_and_NED_transform_verified=True)
write_json(A/'FINAL_VERIFICATION.json',result)
print('Full phase and wind CSV verification: PASS')

network=json.loads((out/'LAYER3_NETWORK_SUMMARY.json').read_text())
assert network['report_integrity']=='PASS'
assert network['snapshot_count']==network['distinct_snapshots']==len(expected)
assert network['topology_count']==1 and network['topology_changes']==0
assert network['unique_feedback_routes_applied']==network['accepted_feedback_routes']==1665
assert all(v['mismatched']==v['unavailable']==0 for v in network['check_counts'].values())
with (out/'LAYER3_NETWORK_TIMELINE.csv').open() as f: network_timeline=list(csv.DictReader(f))
with (out/'LAYER3_NETWORK_EDGES.csv').open() as f: network_edges=list(csv.DictReader(f))
assert len(network_timeline)==4458 and len(network_edges)==4458*5
assert sum(x['evidence_status']=='INITIAL_PRIOR_ONLY' for x in network_edges)==4458
with gzip.open(out/'LAYER3_SNAPSHOTS.jsonl.gz','rt') as f:
 snapshots=[json.loads(line) for line in f]
assert snapshots==[r['L3'] for r in expected]
for n,r in zip(network_timeline,expected):
 assert n['snapshot_hash']==r['L3']['snapshot_hash']
 assert n['transaction_id']==r['transaction_id']
 assert int(n['declared_coupling_count'])==len(r['L3']['couplings'])
result.update(layer3_snapshots_verified=len(snapshots),layer3_edge_rows_verified=len(network_edges),
 layer3_checks_verified=network['check_counts'],layer3_topology_changes=network['topology_changes'])
write_json(A/'FINAL_VERIFICATION.json',result)
print('Layer3 snapshots, edges, clocks and feedback: PASS')
