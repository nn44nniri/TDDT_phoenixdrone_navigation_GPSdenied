import json, numpy as np
from pathlib import Path
from scipy.spatial.transform import Rotation
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from twins_fmi.realtime import RealtimeTwins,rotation
root=Path(__file__).parent;rows=[json.loads(l) for l in (root/'attempt02/telemetry_wire.jsonl').read_text().splitlines()]
imu=[r for r in rows if r['type']=='HIGHRES_IMU'];start=imu[0]['plant_time_s'];cal=[r for r in imu if r['plant_time_s']<=start+3.]
cfg=TDOCConfig.from_yaml(root.parent/'TDOC_v20260911_2052/configs/tdoc.yaml')
climate={'wind_ned_m_s':[0.,0.,0.],'air_density_kg_m3':1.}
twins=RealtimeTwins(Layer2Adapter(cfg),{'latitude_deg':40.01,'longitude_deg':-105.3,'heading_deg':45.6},cal,climate)
result={'calibration':twins.calibration,'replayed':[]};latest={}
for row in rows:
 latest[row['type']]=row
 if row['type']!='HIGHRES_IMU' or row['plant_time_s']<=twins.last:continue
 t=row['plant_time_s'];nav=twins.step({'HIGHRES_IMU':row,'ULTRASONIC_WIND':{'present':False}},[row])
 if len(result['replayed'])==0 or t-result['replayed'][-1]['time_s']>.5:
  actual=latest.get('ATTITUDE_QUATERNION',{}).get('data',{});q=[actual.get('q'+str(i)) for i in range(1,5)]
  if None in q:continue
  error=(rotation(q).inv()*rotation(nav['q_wxyz'])).magnitude()*180/np.pi
  result['replayed'].append({'time_s':t,'attitude_error_deg':error,'nav_vtol_euler_deg':(rotation(nav['q_wxyz'])*Rotation.from_euler('y',-90,degrees=True)).as_euler('xyz',degrees=True).tolist()})
(root/'evidence/attitude_sensor_replay.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
