from pathlib import Path
import json, math, sys, zipfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from twins_fmi import DomainTwin, TwinsLayer2

cfg=ROOT/'config'/'phoenix_drone_v3.yaml'
required=['twin_contract.json','twin_output_schema.json','metadata/variables.json','metadata/uncertainty_model.json','metadata/quality_rules.json','metadata/validity_envelope.json','metadata/reliability_profile.json','tep/tep.csv','tep/manifest.json']
for twin in ['MESA_NAV','WingMPET']:
    with zipfile.ZipFile(ROOT/'fmus'/f'{twin}.fmu') as z:
        names=set(z.namelist())
        for rel in required:
            assert f'resources/tddt_layer2/{rel}' in names, (twin,rel)

with DomainTwin(ROOT/'fmus'/'MESA_NAV.fmu',config=cfg) as mesa:
    p=mesa.evaluate_packet(7,inputs={'air_density':1.1,'pitot_pressure':220.0,'predicted_airspeed':20.0,'airspeed_variance':1.0},context_id='C001',stage_id='cruise')
    d=p.to_dict()
    assert d['twin_id']=='MESA_NAV'
    assert set(['state','uncertainty','quality','validity','reliability','context','timestamp','provenance']).issubset(d)
    assert d['reliability']['kind']=='verification_prior_no_field_evidence'
    assert all(math.isfinite(float(v)) for v in d['state'].values())

with DomainTwin(ROOT/'fmus'/'WingMPET.fmu',config=cfg) as wing:
    p=wing.evaluate_packet(5,inputs={'altitude':800.0,'external_temperature':0.0,'external_pressure':0.0,'external_density':0.0,'relative_humidity':0.4,'airspeed':13.0},context_id='C001',stage_id='cruise')
    d=p.to_dict()
    assert d['twin_id']=='WingMPET'
    assert d['validity']['score'] > 0
    assert all(math.isfinite(float(v)) for v in d['state'].values())

with TwinsLayer2.tddt(ROOT/'fmus'/'MESA_NAV.fmu',ROOT/'fmus'/'WingMPET.fmu',config=cfg) as twins:
    assert twins.mesa.fmu.component != twins.wingmpet.fmu.component
    w0=twins.wingmpet.fmu.t
    twins.mesa.evaluate_packet(7,inputs={'air_density':1.1,'pitot_pressure':220.0,'predicted_airspeed':20.0,'airspeed_variance':1.0})
    assert twins.wingmpet.fmu.t == w0
    twins.mesa.reset()
    assert twins.wingmpet.fmu.t == w0

print('ALL_TDDT_LAYER2_ALIGNMENT_TESTS_PASSED')
