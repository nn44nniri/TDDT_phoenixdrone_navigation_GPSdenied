"""Offline sensor replay; physical pose is used only to score each hypothesis."""
from pathlib import Path
import json,math,bisect,re,numpy as np
from pyproj import Geod
from twins_fmi.realtime import RealtimeTwins,rotation
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.config import TDOCConfig
A=Path(__file__).parent;P=A/'attempt17_sensor_ekf_full_route';rows=[json.loads(l) for l in (P/'cycles.jsonl').read_text().splitlines()];wire=[json.loads(l) for l in (P/'telemetry_wire.jsonl').read_text().splitlines()];imu=[r for r in wire if r['type']=='HIGHRES_IMU'];times=[r['plant_time_s'] for r in imu];cal=json.loads((P/'startup.json').read_text())['calibration'];cfg=TDOCConfig.from_yaml(A.parent/'TDOC_v20260911_2052/configs/tdoc.yaml');mapfile=A.parent/'layer6_v20261007_1032/FlightStack/ardupilot/libraries/AP_Declination/tables.cpp';text=mapfile.read_text();tables=[]
for name in ('intensity','declination','inclination'):
 body=re.search(name+r'_table\[LAT_TABLE_SIZE\]\[LON_TABLE_SIZE\]\s*=\s*\{(.*?)\n\};',text,re.S).group(1);tables.append(np.array([float(v) for v in re.findall(r'([-+]?\d+\.\d+)f',body)]).reshape(19,37))
def magnetic(lat,lon):
 i=int(math.floor((lat+90)/10));j=int(math.floor((lon+180)/10));fy=(lat+90)/10-i;fx=(lon+180)/10-j;vals=[(1-fy)*((1-fx)*a[i,j]+fx*a[i,j+1])+fy*((1-fx)*a[i+1,j]+fx*a[i+1,j+1]) for a in tables];s,d,inc=vals;d,inc=math.radians(d),math.radians(inc);return np.array([s*math.cos(inc)*math.cos(d),s*math.cos(inc)*math.sin(d),s*math.sin(inc)])
def nav_geometry(x):
 lat=math.radians(40.01)+x[0]/6362000.;e2=.00669437999014;a=6378137.;s=math.sin(lat);rn=a/math.sqrt(1-e2*s*s);rm=a*(1-e2)/(1-e2*s*s)**1.5;h=1798.64-x[2];omega_en=np.array([x[4]/(rn+h),-x[3]/(rm+h),-x[4]*math.tan(lat)/(rn+h)]);omega_ie=7.292115e-5*np.array([math.cos(lat),0.,-math.sin(lat)]);return lat,math.radians(-105.3)+x[1]/((rn+h)*math.cos(lat)),omega_en,omega_ie
geo=Geod(ellps='WGS84');summary={};first=rows[0]['L2']['navigation'];seed=next(r for r in imu if r['wire_sha256']==first['source_wire_sha256'])
for variant in ('baseline','field','earth','field_earth','field_factory_zero'):
 adapter=Layer2Adapter(cfg);twin=RealtimeTwins(adapter,{'latitude_deg':40.01,'longitude_deg':-105.3,'heading_deg':45.6},imu[:301],rows[0]['L2']['climate']);twin.last=first['time_s'];twin.initial_ground_support=False;twin.takeoff_requested=True;twin.acc_bias=np.array(cal['accel_bias_m_s2']);twin.baro0=cal['baro_initial_m'];twin.P=np.array(first['covariance']);twin.gyro_bias=np.array([seed['data'][k]-first['body_rates_rad_s'][i] for i,k in enumerate(('xgyro','ygyro','zgyro'))]);
 if variant=='field_factory_zero':twin.gyro_bias[:]=0.
 values=dict(zip(('rN','rE','rD','vN','vE','vD'),first['position_ned_m']+first['velocity_ned_m_s']));values.update(dict(zip(('qw','qx','qy','qz'),first['q_wxyz'])));values.update(dict(zip(('bgx','bgy','bgz'),twin.gyro_bias)));values.update(dict(zip(('bfx','bfy','bfz'),twin.acc_bias)));twin.mesa.fmu.set(**values)
 original=twin.mesa.fmu.evaluate_operation
 if variant in ('earth','field_earth'):
  def evaluate(op,*,inputs,communication_step,original=original,twin=twin):
   x,q=twin.state();lat,lon,en,ie=nav_geometry(x);d=twin.mesa.fmu.get('imu_gx','imu_gy','imu_gz');g=np.array([d[k] for k in ('imu_gx','imu_gy','imu_gz')])-rotation(q).inv().apply(en);twin.mesa.fmu.set(imu_gx=g[0],imu_gy=g[1],imu_gz=g[2]);out=original(op,inputs=inputs,communication_step=communication_step);xx,qq=twin.state();acc=-np.cross(2*ie+en,x[3:]);xx[:3]+=.5*communication_step**2*acc;xx[3:]+=communication_step*acc;twin.mesa.fmu.set(**dict(zip(('rN','rE','rD','vN','vE','vD'),xx)));return out
  twin.mesa.fmu.evaluate_operation=evaluate
 metrics=[]
 for index,r in enumerate(rows[1:]):
  t=r['L2']['navigation']['time_s'];samples=imu[bisect.bisect_right(times,twin.last):bisect.bisect_right(times,t)]
  if not samples:continue
  if variant.startswith('field'):
   x,q=twin.state();lat,lon,_,_=nav_geometry(x);twin.mag0=magnetic(math.degrees(lat),math.degrees(lon))
  n=twin.step({'HIGHRES_IMU':samples[-1],'ULTRASONIC_WIND':{'present':False}},samples)
  if index%100==0 or index==len(rows)-2 or r['phase']=='vertical_landing':
   s=r['external_validation_only']['SIM_STATE']['data'];b,_,dist=geo.inv(-105.3,40.01,(s.get('lon_int') or s['lon'])*1e-7,(s.get('lat_int') or s['lat'])*1e-7);xy=dist*np.array([math.cos(math.radians(b)),math.sin(math.radians(b))]);metrics.append({'time_s':t,'phase':r['phase'],'estimated_position':n['position_ned_m'],'physical_xy':xy.tolist(),'error_m':float(np.linalg.norm(xy-np.array(n['position_ned_m'][:2])))})
 summary[variant]={'final':metrics[-1],'before_landing':next(m for m in metrics if m['phase']=='vertical_landing'),'rows':metrics};adapter.close();print(variant,summary[variant]['before_landing'],flush=True)
(A/'evidence/geophysical_replay.json').write_text(json.dumps(summary,indent=2))
