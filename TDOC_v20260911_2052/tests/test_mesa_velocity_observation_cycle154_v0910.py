import os
import sys
from pathlib import Path

from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer3 import Layer3Adapter


def _layer3_types():
    root=Path(os.environ['TDDT_LAYER3_ROOT']).resolve()
    src=root/'src'
    if str(src) not in sys.path:
        sys.path.insert(0,str(src))
    from layer3 import Layer3Config
    from layer3.alignment import STDAlignment
    from layer3.consistency import ConsistencyManager
    from layer3.tdoc_adapter import TDOCAdapter
    return Layer3Config, STDAlignment, ConsistencyManager, TDOCAdapter, root


def _base_packet(twin_id, timestamp, state):
    return {
        'twin_id':twin_id,
        'timestamp':float(timestamp),
        'state':dict(state),
        'quality':{'score':1.0},
        'validity':{'score':1.0},
        'reliability':{'prior':1.0},
        'uncertainty':{},
        'context':{},
        'provenance':{},
    }


def test_cycle154_coupled_mesa_velocity_is_not_dropped_from_layer3_coc():
    Layer3Config, STDAlignment, ConsistencyManager, TDOCAdapter, root=_layer3_types()
    cfg=Layer3Config.default(root)
    # Integrated TDOC is the authority for all external roots.  Bind the Layer-3
    # config exactly as production does after `source configs/environment.sh`.
    from tddt_tdoc.config import TDOCConfig
    tdoc_root=Path(__file__).parents[1]
    TDOCConfig.from_yaml(tdoc_root/'configs/tdoc.yaml').bind_layer3_external_paths(cfg,required=True)
    align=STDAlignment(cfg,tdoc=TDOCAdapter(cfg))

    # The original Layer-2 packet has already been bound by problem #1/#2.
    mesa=_base_packet('MESA_NAV',153.001,{
        'velocity_n':6.8515,'velocity_e':7.7928,'velocity_d':3.1129,
    })
    mesa=Layer2Adapter._bind_mesa_simulation_packet_to_mission_time(
        mesa,reference_time_s=153.001,
        alignment={'applied':True,'schema':'TDOC-MESA-simulation-state-alignment-v2'},
        learning_mode='OFFLINE_SE')

    # Reproduce the independent problem #3 mechanism: a Layer-3 destination
    # coupling evaluates MESA again and _merge_packet adopts that local FMI time.
    coupled_update=_base_packet('MESA_NAV',2.906,{
        'terrain_flow_update_valid':1.0,
    })
    merged_state=dict(mesa['state']); merged_state.update(coupled_update['state'])
    coupled=dict(mesa); coupled['state']=merged_state; coupled['timestamp']=coupled_update['timestamp']

    old=align.align_packet(coupled,153.001)
    assert old.gate == 0
    assert old.freshness < cfg.alignment.min_freshness['default']
    # The actual velocity is still present even though freshness rejects it.
    assert old.canonical_state['ground_velocity_ned_m_s'] == [6.8515,7.7928,3.1129]

    packets=Layer3Adapter._rebind_mesa_after_layer3_couplings(
        {'MESA_NAV':coupled},reference_time_s=153.001,
        alignment={'applied':True,'schema':'TDOC-MESA-simulation-state-alignment-v2'},
        learning_mode='OFFLINE_SE',layer2_adapter=Layer2Adapter)
    fixed=packets['MESA_NAV']
    current=align.align_packet(fixed,153.001)
    assert current.source_time_s == 153.001
    assert current.age_s == 0.0
    assert current.freshness == 1.0
    assert current.gate == 1
    assert current.canonical_state['ground_velocity_ned_m_s'] == [6.8515,7.7928,3.1129]

    # Feed all three expected velocity observations into the real Layer-3 CoC.
    wing=_base_packet('WingMPET',153.001,{
        'air_velocity_n':5.8515,'air_velocity_e':5.7928,'air_velocity_d':2.1129,
    })
    climate=_base_packet('MicroMetTwin',153.001,{
        'wind_u_3dw':2.0,'wind_v_3dw':1.0,'diagnostic_vertical_wind':-1.0,
    })
    std=align.build_std({'MESA_NAV':fixed,'WingMPET':wing,'MicroMetTwin':climate},153.001)
    active=['MicroMetTwin->WingMPET','MicroMetTwin->MESA_NAV','MESA_NAV->WingMPET']
    coc=ConsistencyManager(cfg).evaluate(
        std,{'MESA_NAV':1/3,'WingMPET':1/3,'MicroMetTwin':1/3},
        {'active_edges':active},{e:1.0 for e in active})
    assert 'MESA_NAV:ground_velocity_ned_m_s' not in (coc.get('missing_expected_observations') or [])
    assert 'velocity_triangle_ned' in (coc.get('constraint_residuals') or {})
    assert not (coc.get('uncompiled_constraints') or [])
    assert coc.get('network_residual_raw_ned_m_s') == [0.0,0.0,0.0]
    assert coc.get('status') == 'solved'

    prov=fixed['provenance']['tdoc_post_layer3_coupling_time_rebind']
    assert prov['simulation_only'] is True
    assert prov['not_field_evidence'] is True


def test_post_coupling_rebind_does_not_relabel_non_simulation_packet():
    packet=_base_packet('MESA_NAV',2.906,{'velocity_n':1.0,'velocity_e':2.0,'velocity_d':3.0})
    out=Layer3Adapter._rebind_mesa_after_layer3_couplings(
        {'MESA_NAV':packet},reference_time_s=153.001,
        alignment={'applied':False},learning_mode='ONLINE_JSBSIM',layer2_adapter=Layer2Adapter)
    assert out['MESA_NAV']['timestamp'] == 2.906
    assert 'tdoc_post_layer3_coupling_time_rebind' not in out['MESA_NAV'].get('provenance',{})
