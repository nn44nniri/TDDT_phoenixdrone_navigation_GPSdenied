"""Read-only cross-layer provenance audit, independent of mission driver PASS."""
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);known_feedback={};used=set();sources=set();cycles=0
checks=dict.fromkeys(('atomic_snapshot_hash','all_layer_transaction_ids','current_climate_is_wing_input',
    'climate_source_age_within_3s','no_forbidden_navigation_inputs','next_cycle_feedback_is_prior_L7',
    'feedback_updates_WingMPET','unique_feedback_consumption','original_prediction_time',
    'all_cycles_physical_map'),True)
max_climate_age=0.;max_cycle_wall=0.;max_source_gap=0.;last_time=None;previous_route=None;previous_version=0
for line in (root/'cycles.jsonl').open():
    r=json.loads(line);cycles+=1;n=r['L2']['navigation'];w=r['L2']['wing'];c=r['L2']['climate'];s=r['L3']
    raw={k:v for k,v in s.items() if k!='snapshot_hash'}
    digest=hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    checks['atomic_snapshot_hash'] &= digest==s['snapshot_hash']==r['L4']['snapshot_hash']==r['L5']['snapshot_hash']
    checks['all_layer_transaction_ids'] &= s['transaction_id']==r['transaction_id']==r['L5']['transaction_id']
    checks['current_climate_is_wing_input'] &= w['climate_input_time_s']==c['time_s'] and w['climate_input_forcing_sha256']==c['forcing_sha256']
    age=n['time_s']-c['time_s'];max_climate_age=max(max_climate_age,age)
    checks['climate_source_age_within_3s'] &= 0<=age<=3.
    checks['no_forbidden_navigation_inputs'] &= all(n.get(k) is False for k in ('truth_used','GNSS_used','odometry_used','vision_used'))
    if n['feedback_consumed']:
        route=n['feedback_consumed'];f=known_feedback.get(route)
        checks['next_cycle_feedback_is_prior_L7'] &= f is not None
        # Navigation publishes the last consumed route until a newer one arrives.
        # Consumption means a route/version transition, not repeated publication.
        version=w['reduced_inner_loop']['feedback_version']
        if route!=previous_route:
            checks['unique_feedback_consumption'] &= route not in used and version==previous_version+1
            used.add(route)
        else:checks['unique_feedback_consumption'] &= version==previous_version
        previous_route=route;previous_version=version
        checks['feedback_updates_WingMPET'] &= f is not None and all(w['reduced_inner_loop'].get(k)==v for k,v in f['model_update'].items())
    if r['L7'].get('ok'):
        f=r['L7'];known_feedback[f['route_hash']]=f
        checks['original_prediction_time'] &= f['prediction_retimed'] is False and f['prediction_source_time_s']<f['observation_time_s']
    sources.add(n['source_wire_sha256'])
    checks['all_cycles_physical_map'] &= bool(r['health']['physical_map']['ready'])
    max_cycle_wall=max(max_cycle_wall,r['cycle_wall_s'])
    if last_time is not None:max_source_gap=max(max_source_gap,n['time_s']-last_time)
    last_time=n['time_s']
wire_sources=set()
for line in (root/'telemetry_wire.jsonl').open():
    p=json.loads(line)
    if p['type']=='HIGHRES_IMU':wire_sources.add(p['wire_sha256'])
checks['navigation_IMU_hashes_match_raw_wire']=sources<=wire_sources
out={'status':'PASS' if all(checks.values()) else 'FAIL','cycles':cycles,'checks':checks,
    'unique_L7_routes':len(known_feedback),'unique_feedbacks_consumed':len(used),
    'max_climate_model_age_s':max_climate_age,'max_cycle_wall_s':max_cycle_wall,'max_navigation_cycle_source_gap_s':max_source_gap}
(root/'cross_layer_trace_validation.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
