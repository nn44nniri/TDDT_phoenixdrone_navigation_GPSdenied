import json,numpy as np
from pathlib import Path
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from twins_fmi.realtime import RealtimeTwins
from pyproj import Geod
root=Path(__file__).parent;folder=root/'attempt06';records=[json.loads(l) for l in (folder/'cycles.jsonl').read_text().splitlines()];wire=[json.loads(l) for l in (folder/'telemetry_wire.jsonl').read_text().splitlines()]
imu=[r for r in wire if r['type']=='HIGHRES_IMU'];cal=[r for r in imu if r['plant_time_s']<=imu[0]['plant_time_s']+3.];pitot=[r for r in wire if r['type']=='VFR_HUD'];cfg=TDOCConfig.from_yaml(root.parent/'TDOC_v20260911_2052/configs/tdoc.yaml');out={};g=Geod(ellps='WGS84')
for with_pitot in (True,False):
 adapter=Layer2Adapter(cfg);twins=RealtimeTwins(adapter,{'latitude_deg':40.01,'longitude_deg':-105.3,'heading_deg':45.6},cal,records[0]['L2']['climate']);twins.takeoff_requested=True;result=[]
 for r in records:
  t=r['L2']['navigation']['time_s'];samples=[s for s in imu if twins.last<s['plant_time_s']<=t]
  if not samples:continue
  h=samples[-1];m={'HIGHRES_IMU':h,'ULTRASONIC_WIND':{'present':False}}
  pv=[s for s in pitot if s['received_monotonic']<=h['received_monotonic']]
  if with_pitot and pv:
   s=pv[-1];m['AIRSPEED']={'wire_sha256':s['wire_sha256'],'airspeed_m_s':s['data']['airspeed']}
  n=twins.step(m,samples)
  actual=r['external_validation_only']['GLOBAL_POSITION_INT']['data'];bearing,_,dist=g.inv(-105.3,40.01,actual['lon']*1e-7,actual['lat']*1e-7);xy=dist*np.array([np.cos(np.radians(bearing)),np.sin(np.radians(bearing))]);vp=np.array([actual[k]*.01 for k in ('vx','vy','vz')]);result.append({'t':t,'p':n['position_ned_m'],'true_xy':xy.tolist(),'v':n['velocity_ned_m_s'],'true_v':vp.tolist(),'position_error_m':float(np.linalg.norm(xy-np.array(n['position_ned_m'][:2]))),'velocity_error_m_s':float(np.linalg.norm(vp-np.array(n['velocity_ned_m_s'])))})
 out[str(with_pitot)]={'final':result[-1],'rows':result};adapter.close()
(root/'evidence/pitot_ablation.json').write_text(json.dumps(out,indent=2));print({k:v['final'] for k,v in out.items()})
