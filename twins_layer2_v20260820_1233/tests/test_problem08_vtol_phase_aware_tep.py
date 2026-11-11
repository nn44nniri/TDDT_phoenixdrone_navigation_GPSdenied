import csv, json, math
from pathlib import Path

from twins_fmi.domain_twin import TwinEpistemicProfile


def _fake_root(tmp_path: Path):
    root=tmp_path/'WingMPET'; (root/'tep').mkdir(parents=True); (root/'metadata').mkdir()
    env={
        'ranges':{'flightpath_angle':{'min':-math.radians(20),'max':math.radians(20),'unit':'rad'}},
        'mode_ranges':{
            'vertical_up':{'flightpath_angle':{'enabled':False}},
            'transition':{'flightpath_angle':{'enabled':False}},
            'wing_borne':{'flightpath_angle':{'min':-math.radians(20),'max':math.radians(20),'unit':'rad'}},
            'vertical_down':{'flightpath_angle':{'enabled':False}},
        },
    }
    (root/'metadata'/'validity_envelope.json').write_text(json.dumps(env))
    for n in ('quality_rules','reliability_profile','uncertainty_model'):
        (root/'metadata'/f'{n}.json').write_text('{}')
    (root/'twin_contract.json').write_text(json.dumps({'twin_id':'WingMPET'}))
    man={'tep_version':'test','maturity':'TEP_sim','novelty_radius':3.0,'novelty_excluded_features':['state_x','state_y','state_h']}
    (root/'tep'/'manifest.json').write_text(json.dumps(man))
    fields=['operation_code','cell_id','phase_family','center_json','scale_json','quality_prior','validity_prior','reliability_prior','evidence_count','uncertainty_json','variance_json','bounds_json','mean_json','quality_components_json']
    rows=[
        {'operation_code':4,'cell_id':'up','phase_family':'vertical_up','center_json':json.dumps({'state_x':0,'state_y':0,'state_h':1000,'air_velocity_x':0,'air_velocity_y':0,'air_velocity_h':2}),'scale_json':json.dumps({'state_x':1,'state_y':1,'state_h':1,'air_velocity_x':2,'air_velocity_y':2,'air_velocity_h':2}),'quality_prior':1,'validity_prior':1,'reliability_prior':1,'evidence_count':8,'uncertainty_json':'{}','variance_json':'{}','bounds_json':'{}','mean_json':'{}','quality_components_json':'{}'},
        {'operation_code':4,'cell_id':'wing','phase_family':'wing_borne','center_json':json.dumps({'state_x':0,'state_y':0,'state_h':1000,'air_velocity_x':13,'air_velocity_y':0.5,'air_velocity_h':0}),'scale_json':json.dumps({'state_x':1,'state_y':1,'state_h':1,'air_velocity_x':2,'air_velocity_y':2,'air_velocity_h':2}),'quality_prior':1,'validity_prior':1,'reliability_prior':1,'evidence_count':8,'uncertainty_json':'{}','variance_json':'{}','bounds_json':'{}','mean_json':'{}','quality_components_json':'{}'},
    ]
    with (root/'tep'/'tep.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    return root


def test_vertical_phase_does_not_apply_fixed_wing_flightpath_bound(tmp_path):
    tep=TwinEpistemicProfile(_fake_root(tmp_path))
    score,reasons=tep.validity({'flightpath_angle':math.pi/2},stage='vertical_takeoff')
    assert score==1.0 and 'flightpath_angle' not in ' '.join(reasons)
    score2,reasons2=tep.validity({'flightpath_angle':math.pi/2},stage='cruise')
    assert score2==0.0 and any('flightpath_angle:outside' in r for r in reasons2)


def test_phase_family_lookup_uses_vtol_cell_and_ignores_absolute_route_position(tmp_path):
    tep=TwinEpistemicProfile(_fake_root(tmp_path))
    r=tep.lookup(4,{'flight_phase':'vertical_takeoff','state_x':474000,'state_y':4428000,'state_h':1800,'air_velocity_x':0,'air_velocity_y':0,'air_velocity_h':2})
    assert r['cell_id']=='up'
    assert r['novelty'] is False
    assert r['distance'] < 1e-9
