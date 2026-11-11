"""Diagnostic replay; archived IMU only, no connection to flight controller."""
import gzip,json,math,os,time
from pathlib import Path
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.online_imu import CHANNELS

audit=Path(__file__).resolve().parent
os.chdir(audit.parent/'TDOC_v20260911_2052')
paths=sorted((audit/'after_first_runtime/cycles').glob('workflow-online_jsbsim-*.json.gz'))
samples={};initial=None;origin=None;initial_time=None
for p in paths:
    d=json.load(gzip.open(p,'rt'))
    packet=(d.get('layer2_packets') or {}).get('MESA_NAV') or {}
    ctx=packet.get('context') or {}
    if not ctx:continue
    initial=initial or ctx.get('rtmo_navigation_initial_state')
    pt=(packet.get('provenance') or {}).get('online_imu_ingress',{}).get('plant_time_s')
    if pt is not None:initial_time=min(initial_time,pt) if initial_time is not None else pt
    ingress=(ctx.get('online_state_ingress') or {}).get('ptlib_ingress') or {}
    for s in ingress.get('imu_samples') or []:samples[s['plant_time_s']]=s
ordered=[samples[k] for k in sorted(samples) if initial_time is None or k>=initial_time]
print('samples',len(ordered),'initial',initial,flush=True)
first=ordered[0];ax,ay,az=[first[k] for k in ('accel_x_m_s2','accel_y_m_s2','accel_z_m_s2')]
w,x,y,z=[initial[k] for k in ('qw','qx','qy','qz')]
yaw=math.atan2(2*(w*z+x*y),1-2*(y*y+z*z))
roll=math.atan2(-ay,-az);pitch=math.atan2(ax,math.hypot(ay,az))
cr,sr=math.cos(roll/2),math.sin(roll/2);cp,sp=math.cos(pitch/2),math.sin(pitch/2);cy,sy=math.cos(yaw/2),math.sin(yaw/2)
q=(cr*cp*cy+sr*sp*sy,sr*cp*cy-cr*sp*sy,cr*sp*cy+sr*cp*sy,cr*cp*sy-sr*sp*cy)
cfg=TDOCConfig.from_yaml('configs/tdoc.yaml');adapter=Layer2Adapter(cfg)
results=[]
try:
    for label,quat in [('configured',(w,x,y,z)),('IMU_gravity',q)]:
        session=adapter.open_isolated_session();mesa=session['mesa']
        try:
            mesa.fmu.set(rN=initial['pn'],rE=initial['pe'],rD=initial['pd'],vN=0.,vE=0.,vD=0.,**dict(zip(('qw','qx','qy','qz'),quat)))
            last=ordered[0]['plant_time_s'];start=time.perf_counter();checkpoints=[];target=1
            for sample in ordered[1:]:
                now=sample['plant_time_s'];dt=now-last
                if dt>.15:raise ValueError('archived evidence has gap')
                mesa.fmu.set(**{target:sample[source] for target,source in CHANNELS.items()})
                packet=mesa.evaluate_packet(5,inputs={'dt':dt},context={},communication_step=dt)
                last=now
                if now-ordered[0]['plant_time_s']>=target:
                    state=packet.state
                    checkpoints.append({'elapsed_s':now-ordered[0]['plant_time_s'],
                        'horizontal_drift_m':math.hypot(state['position_n']-initial['pn'],state['position_e']-initial['pe'])})
                    target={1:5,5:10,10:20,20:1000}.get(target,1000)
            state=packet.to_dict()['state']
            results.append({'case':label,'elapsed_s':time.perf_counter()-start,'integrated_s':last-ordered[0]['plant_time_s'],
                'first_sample':first,'quaternion':quat,'state':state,'checkpoints':checkpoints,
                'horizontal_drift_m':math.hypot(state['position_n']-initial['pn'],state['position_e']-initial['pe'])})
        finally:adapter.close_isolated_session(session)
finally:adapter.close()
(audit/'evidence/attitude_replay.json').write_text(json.dumps(results,indent=2))
for r in results:print(r['case'],r['horizontal_drift_m'],r['integrated_s'],flush=True)
