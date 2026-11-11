from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from twins_fmi import DomainTwin

cfg=ROOT/'config'/'phoenix_drone_v3.yaml'
with DomainTwin(ROOT/'fmus'/'MESA_NAV.fmu', config=cfg) as mesa:
    p=mesa.evaluate_packet(7, inputs={'air_density':1.1,'pitot_pressure':220.0,'predicted_airspeed':20.0,'airspeed_variance':1.0}, context_id='CRP_demo', stage_id='cruise')
    print('MESA',p.to_dict())

with DomainTwin(ROOT/'fmus'/'WingMPET.fmu', config=cfg) as wing:
    p=wing.evaluate_packet(5, inputs={'altitude':800.0,'external_temperature':0.0,'external_pressure':0.0,'external_density':0.0,'relative_humidity':0.4,'airspeed':13.0}, context_id='CRP_demo', stage_id='cruise')
    print('WingMPET',p.to_dict())
