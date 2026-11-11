"""Native observations remain strict; isolated real FMUs use explicit forecasts."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest
from pyproj import Transformer

from test_r4_02_binding_consumers import bound, explicit_readonly_reference, TDOC
from test_r4_04_clock_alignment import native_request, layer3_boundary
from layer3.alignment import TimeSynchronizer
from layer4.state import StateCodec
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer3 import Layer3Adapter
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_time import stamp_climate_time


def replay_case(bound,tmp_path):
    path,binding=bound
    config=TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml')
    config.data['runtime']['root']=str(tmp_path/'runtime')
    layer2=Layer2Adapter(config)
    adapter=Layer3Adapter(config,layer2_adapter=layer2)
    request=json.loads((TDOC/'examples/runtime_request.json').read_text())
    observed=native_request(bound,plant=10.)
    request['climate']['time']=observed['climate']['time']
    request['context'].update(observed['context'])
    request.update(learning_mode='ONLINE_JSBSIM',stage='hover',reference_time_s=10.,transaction_id='clock-ff-test')
    home=binding['mission_origin']
    east,north=Transformer.from_crs('EPSG:4326',home['map_crs'],always_xy=True).transform(home['longitude_deg'],home['latitude_deg'])
    altitude=home['terrain_elevation_msl_m']+100.
    request['climate']['input']=binding['forcing_file']
    request['climate']['dem']=binding['origin']['terrain_file']['path']
    for key,value in [('latitude',home['latitude_deg']),('longitude',home['longitude_deg'])]:
        request['climate'][key]['value']=value
    request['climate']['elevation_ground_level']['value']=100.
    request['context']['online_execution_state']={'pn':north,'pe':east,'pd':-altitude,
        'vn':0.,'ve':0.,'vd':0.,'qw':math.sqrt(.5),'qx':0.,'qy':math.sqrt(.5),'qz':0.,
        'p_rate':0.,'q_rate':0.,'r_rate':0.,'omega_left':650.,'omega_right':650.,'soc':1.}
    codec=StateCodec(config.path_value('TDDT_LAYER4_ROOT')/'resources/state_schema.json')
    state=[0.]*codec.dim
    for key,value in {'mesa_position_ned_m_0':north,'mesa_position_ned_m_1':east,
        'mesa_position_ned_m_2':-altitude,'air_density_kg_m3':1.18}.items():state[codec.index[key]]=value
    candidate={'candidate_id':'native-clock-model-only','trajectory':[state,deepcopy(state)],'actions':[[0.]*5]}
    snapshot={'reference_time_s':10.,'context':{'altitude_agl_m':100.,
        'agl_authority':{'valid':True,'simulator_truth_used':False}},
        'std':{'representations':{'WingMPET':{'validity':{'score':1.}}}}}
    action={'left_motor_cmd':.82,'right_motor_cmd':.82,'left_elevon_rad':0.,'right_elevon_rad':0.}
    return adapter,layer2,request,candidate,snapshot,codec,action


@pytest.mark.parametrize('step_s,lane',[(.1,'COMMIT_SEGMENT'),(1.,'FORECAST_HORIZON')])
def test_actual_isolated_ff_queries_future_time_without_advancing_observed_clock(bound,tmp_path,step_s,lane):
    adapter,layer2,request,candidate,snapshot,codec,action=replay_case(bound,tmp_path)
    before=deepcopy(request)
    try:
        rows=adapter.full_fidelity_replay(candidate,snapshot,request,codec,1.,physical_step_s=step_s,
            physical_actions=[action],bridge_record={'ok':True})
    finally:
        adapter.close();layer2.close()
    assert request==before and len(rows)==1
    row=rows[0]
    assert row['physical_action_consumed'] and row['physical_replay']['source'].startswith('WingMPET/op27')
    assert row['physical_replay']['timing_contract']['replay_lane']==lane
    context=row['snapshot']['context']
    assert 'native_climate_time_projection' not in context
    clock=context['tdoc_full_fidelity_prediction_clock']
    assert clock['observed_anchor_projection']==before['context']['native_climate_time_projection']
    assert clock['observed_anchor_projection']['plant_time_s']==10.
    assert clock['forecast_reference_time_s']==10.+step_s
    assert clock['forecast_offset_from_observed_anchor_s']==pytest.approx(step_s)
    assert clock['dataset_timestamp']==('2026-10-28T16:00:10.100000' if step_s==.1 else '2026-10-28T16:00:11')
    assert clock['role']=='ISOLATED_MODEL_PREDICTION' and clock['observed_source_time'] is False
    assert clock['first_packet_rebase'] is False and clock['UTC_conversion_performed'] is False
    assert clock['actuation_authority'] is False


def test_live_alignment_still_rejects_observed_projection_plus_forecast_override(bound):
    config,contracts=layer3_boundary();request=native_request(bound,plant=10.)
    packet=stamp_climate_time({'twin_id':'MicroMetTwin','timestamp':'2026-10-28T16:00:10'},request['context'])
    request['context']['tdoc_full_fidelity_reference_time_s']=10.
    with pytest.raises(ValueError,match='native observed climate clock cannot use a replay clock override'):
        TimeSynchronizer(config,contracts.tdoc).align(packet,10.,context=request['context'])


@pytest.mark.parametrize('fault',['profile','changed_projection','changed_observed_time','forecast_before_anchor','forcing_window'])
def test_isolated_forecast_requires_real_anchor_and_explicit_profile(bound,tmp_path,fault):
    adapter,layer2,request,candidate,snapshot,codec,action=replay_case(bound,tmp_path)
    if fault=='profile':adapter.config.data['layers']['layer6']['online']['execution_profile']='qualified_ground'
    elif fault=='changed_projection':request['context']['native_climate_time_projection']['plant_time_s']=999.
    elif fault=='changed_observed_time':request['climate']['time']['value']='2026-10-28T16:00:11'
    elif fault=='forcing_window':snapshot['reference_time_s']=1e6
    else:snapshot['reference_time_s']=9.
    try:
        with pytest.raises(ValueError):
            adapter.full_fidelity_replay(candidate,snapshot,request,codec,.1,
                physical_actions=[action],bridge_record={'ok':True})
    finally:
        adapter.close();layer2.close()
