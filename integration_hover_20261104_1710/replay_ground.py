"""Held-out measured IMU through the unmodified MESA operation 5."""
import gzip,json,math,statistics,time
from pathlib import Path
from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.online_imu import CHANNELS
ROOT=Path(__file__).resolve().parent.parent;OUT=Path(__file__).resolve().parent/'evidence'
data=json.load(gzip.open(OUT/'ground_imu.json.gz','rt'));samples=data['samples'];t0=samples[0]['plant_time_s']
cal=[s for s in samples if s['plant_time_s']<=t0+5.]
hold=[s for s in samples if s['plant_time_s']>cal[-1]['plant_time_s']]
mean={k:statistics.mean(s[k] for s in cal) for k in CHANNELS.values()}
p=next((ROOT/'integration_hover_20261104_1710/before/TDOC_v20260911_2052/runtime/cycles').glob('*pre-release*.gz'))
old=json.load(gzip.open(p,'rt'));init=old['layer2_packets']['MESA_NAV']['context']['rtmo_navigation_initial_state']
w,x,y,z=[init[k] for k in ('qw','qx','qy','qz')];yaw=math.atan2(2*(w*z+x*y),1-2*(y*y+z*z))
def quat(v):
 ax,ay,az=[v[k] for k in ('accel_x_m_s2','accel_y_m_s2','accel_z_m_s2')]
 roll=math.atan2(-ay,-az);pitch=math.atan2(ax,math.hypot(ay,az))
 cr,sr=math.cos(roll/2),math.sin(roll/2);cp,sp=math.cos(pitch/2),math.sin(pitch/2);cy,sy=math.cos(yaw/2),math.sin(yaw/2)
 return dict(zip(('qw','qx','qy','qz'),(cr*cp*cy+sr*sp*sy,sr*cp*cy-cr*sp*sy,cr*sp*cy+sr*cp*sy,cr*cp*sy-sr*sp*cy)))
cfg=TDOCConfig.from_yaml(ROOT/'TDOC_v20260911_2052/configs/tdoc.yaml');a=Layer2Adapter(cfg);results=[]
try:
 for name in ('single_gravity','mean_gravity','measured_static_bias'):
  session=a.open_isolated_session();m=session['mesa'];values={'rN':init['pn'],'rE':init['pe'],'rD':init['pd'],'vN':0.,'vE':0.,'vD':0.,**quat(cal[-1] if name=='single_gravity' else mean)}
  if name=='measured_static_bias':
   for dest,src in zip(('bgx','bgy','bgz'),('gyro_x_rad_s','gyro_y_rad_s','gyro_z_rad_s')):values[dest]=mean[src]
   norm=math.sqrt(sum(mean[k]**2 for k in ('accel_x_m_s2','accel_y_m_s2','accel_z_m_s2')))
   for dest,src in zip(('bfx','bfy','bfz'),('accel_x_m_s2','accel_y_m_s2','accel_z_m_s2')):values[dest]=mean[src]*(1.-9.80665/norm)
  m.fmu.set(**values);last=cal[-1]['plant_time_s'];checkpoints=[]
  try:
   for s in hold:
    dt=s['plant_time_s']-last
    m.fmu.set(**{dest:s[src] for dest,src in CHANNELS.items()})
    packet=m.evaluate_packet(5,inputs={'dt':dt},context={},communication_step=dt)
    last=s['plant_time_s']
    if not checkpoints or last-t0>=checkpoints[-1]['time_s']+10.:
     st=packet.state;checkpoints.append({'time_s':last-t0,'horizontal_error_m':math.hypot(st['position_n']-init['pn'],st['position_e']-init['pe']),
       'vertical_error_m':st['position_d']-init['pd']})
   st=packet.state;row={'case':name,'calibration_duration_s':cal[-1]['plant_time_s']-t0,'held_out_duration_s':last-cal[-1]['plant_time_s'],
     'initial_values':values,'horizontal_error_m':math.hypot(st['position_n']-init['pn'],st['position_e']-init['pe']),
     'vertical_error_m':st['position_d']-init['pd'],'checkpoints':checkpoints,'raw_samples_changed':False,'FCU_pose_used':False}
   results.append(row);print(name,row['horizontal_error_m'],row['vertical_error_m'],flush=True)
  finally:a.close_isolated_session(session)
finally:a.close()
(OUT/'ground_bias_replay.json').write_text(json.dumps(results,indent=2))
