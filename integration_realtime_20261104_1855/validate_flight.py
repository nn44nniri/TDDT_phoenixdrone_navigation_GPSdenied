"""Independent, read-only evaluation. Simulator pose is never fed to control."""
import argparse,hashlib,json,math
from pathlib import Path
from pyproj import Geod
from scipy.spatial.transform import Rotation
p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--hover-seconds',type=float,default=0);a=p.parse_args()
root=a.directory;result=json.loads((root/'MISSION_RESULT.json').read_text());sid=result['native_session_id'];geod=Geod(ellps='WGS84')
rows=[json.loads(l) for l in (root/'cycles.jsonl').read_text().splitlines()];wire=[json.loads(l) for l in (root/'telemetry_wire.jsonl').read_text().splitlines()]
def physical(row):
 v=row['external_validation_only'];s=(v.get('SIM_STATE') or {}).get('data')
 if s:
  return {'lat':s.get('lat_int') or s['lat'],'lon':s.get('lon_int') or s['lon'],
          'alt':s['alt']*1000.,'vx':s['vn']*100.,'vy':s['ve']*100.,'vz':s['vd']*100.}
 if 'AP_AHRS_SIM' not in result.get('inner_FCU_attitude_backend',''):
  raise ValueError('independent SIM_STATE required for sensor EKF evaluation')
 return v['GLOBAL_POSITION_INT']['data']
first=physical(rows[0]);origin=(first['lon']*1e-7,first['lat']*1e-7);initial_alt=first['alt']*.001
qualified_start=None;previous_time=None;longest=0.;peak_height=-math.inf;peak_radius=0.;consumed=set();accepted=set();healthy=True;restricted=True;trace_valid=True;pose_rows=[]
for r in rows:
 n=r['L2']['navigation'];t=n['time_s'];pose=physical(r);h=pose['alt']*.001-initial_alt
 bearing,_,distance=geod.inv(*origin,pose['lon']*1e-7,pose['lat']*1e-7);speed=math.sqrt(sum(pose[k]**2 for k in ('vx','vy','vz')))*.01
 peak_height=max(peak_height,h);peak_radius=max(peak_radius,distance)
 qualified=r['phase']=='hover' and abs(h-result['cruise_height_above_launch_m'])<=1 and distance<=5 and speed<1
 if not qualified or previous_time is not None and t-previous_time>.3:qualified_start=None
 if qualified:
  if qualified_start is None:qualified_start=t
  longest=max(longest,t-qualified_start)
 previous_time=t
 healthy &= r['health']['ok'] and r['health']['session_id']==sid and bool(r['health']['panels_ready'])
 restricted &= all(n.get(k) is False for k in ('truth_used','GNSS_used','odometry_used','vision_used'))
 trace_valid &= r['L3']['snapshot_hash']==r['L5']['snapshot_hash']==r['L4']['snapshot_hash'] and r['L3']['transaction_id']==r['transaction_id']
 if n.get('feedback_consumed'):consumed.add(n['feedback_consumed'])
 if r['L7'].get('ok'):accepted.add(r['L7']['route_hash'])
 pose_rows.append({'time_s':t,'phase':r['phase'],'actual_height_above_launch_CG_m':h,'actual_radius_m':distance,'actual_speed_m_s':speed,'estimated_position_ned_m':n['position_ned_m']})
hb=[r for r in wire if r['type']=='HEARTBEAT'];disarmed=bool(hb and not hb[-1]['data']['base_mode']&128)
final=rows[-1]['external_validation_only'];landed=(final['EXTENDED_SYS_STATE'] or {}).get('data',{}).get('landed_state')==1
physical_contact=abs(pose_rows[-1]['actual_height_above_launch_CG_m'])<.5 and pose_rows[-1]['actual_speed_m_s']<.4
physical_packets=[r['data'] for r in wire if r['type']=='SIM_STATE']
final_physical_lean=None
if physical_packets:
 s=physical_packets[-1]
 rotation=Rotation.from_quat([s['q2'],s['q3'],s['q4'],s['q1']])*Rotation.from_euler('y',-90,degrees=True)
 final_physical_lean=float(max(abs(v) for v in rotation.as_euler('xyz',degrees=True)[:2]))
images=[]
for m in (root.parent/'evidence').glob('*/manifest.json'):
 data=json.loads(m.read_text())
 if data.get('native_session_id')==sid and data.get('all_six_captured'):
  images.append({'manifest':str(m),'sha256':hashlib.sha256(m.read_bytes()).hexdigest()})
checks={'all_cycles_physical_map':all(r['health'].get('physical_map',{}).get('ready') for r in rows) if rows[0]['external_validation_only'].get('SIM_STATE') else True,'mission_driver_pass':result['status']=='PASS','approved_navigation_sensors':bool(restricted),
 'same_cycle_snapshot_trace':bool(trace_valid),'all_cycle_panel_health':bool(healthy),'all_six_real_images':bool(images),
 'at_least_two_feedbacks_consumed':len(consumed&accepted)>=2,'actual_takeoff':peak_height>1.,
 'actual_hover_duration':longest>=a.hover_seconds if a.hover_seconds else True,
 'actual_contact':physical_contact,'physical_upright_after_landing':final_physical_lean is not None and final_physical_lean<=25.,'FCU_on_ground':landed,'FCU_disarmed':disarmed,
 'actual_destination_error':result.get('actual_goal_error_m',math.inf)<=25.,
 'GPS_disabled_readbacks':all(result['parameter_readbacks'][k]['value']==0 for k in ('SIM_GPS1_ENABLE','SIM_GPS2_ENABLE','SIM_GPS3_ENABLE','SIM_GPS4_ENABLE','GPS1_TYPE','GPS2_TYPE')),
 'no_GPS_fix':all(r['data'].get('fix_type',0)<2 for r in wire if r['type']=='GPS_RAW_INT')}
summary={'status':'PASS' if all(checks.values()) else 'FAIL','native_session_id':sid,'checks':checks,
 'actual_hover_continuous_seconds':longest,'peak_actual_height_m':peak_height,'peak_actual_radius_m':peak_radius,
 'accepted_feedbacks':len(accepted),'consumed_feedbacks':len(consumed&accepted),'actual_goal_error_m':result.get('actual_goal_error_m'),'final_physical_VTOL_lean_deg':final_physical_lean,
 'real_window_evidence':images,'validation_only_pose_used_for_control':False,'physical_validation_source':'SIM_STATE' if rows[0]['external_validation_only'].get('SIM_STATE') else 'AP_AHRS_SIM-only/GLOBAL_POSITION_INT'}
(root/'independent_validation.json').write_text(json.dumps(summary,indent=2));(root/'physical_trajectory.json').write_text(json.dumps(pose_rows,separators=(',',':')))
print(json.dumps(summary,indent=2))
