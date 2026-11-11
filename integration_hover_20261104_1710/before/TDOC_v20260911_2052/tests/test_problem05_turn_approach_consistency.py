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


def _packet(twin_id,timestamp,state):
    return {
        'twin_id':twin_id,'timestamp':float(timestamp),'state':dict(state),
        'quality':{'score':1.0},'validity':{'score':1.0},
        'reliability':{'prior':1.0},'uncertainty':{},'context':{},'provenance':{},
    }


def _assess_phase(stage, reference_time_s, local_mesa_time_s):
    Layer3Config, STDAlignment, ConsistencyManager, TDOCAdapter, root=_layer3_types()
    cfg=Layer3Config.default(root)
    # Integrated TDOC is the authority for all external roots.  Bind the Layer-3
    # config exactly as production does after `source configs/environment.sh`.
    from tddt_tdoc.config import TDOCConfig
    tdoc_root=Path(__file__).parents[1]
    TDOCConfig.from_yaml(tdoc_root/'configs/tdoc.yaml').bind_layer3_external_paths(cfg,required=True)
    align=STDAlignment(cfg,tdoc=TDOCAdapter(cfg))

    # Reproduce the historical post-coupling local-FMI overwrite, then apply the
    # TDOC simulation-time authority fixed in problems #1-#4.
    mesa=_packet('MESA_NAV',local_mesa_time_s,{
        'velocity_n':6.8515,'velocity_e':7.7928,'velocity_d':3.1129,
    })
    mesa=Layer3Adapter._rebind_mesa_after_layer3_couplings(
        {'MESA_NAV':mesa},reference_time_s=reference_time_s,
        alignment={'applied':True,'schema':'TDOC-MESA-simulation-state-alignment-v2'},
        learning_mode='OFFLINE_SE',layer2_adapter=Layer2Adapter)['MESA_NAV']
    wing=_packet('WingMPET',reference_time_s,{
        'air_velocity_n':5.8515,'air_velocity_e':5.7928,'air_velocity_d':2.1129,
    })
    climate=_packet('MicroMetTwin',reference_time_s,{
        'wind_u_3dw':2.0,'wind_v_3dw':1.0,'diagnostic_vertical_wind':-1.0,
    })
    std=align.build_std({'MESA_NAV':mesa,'WingMPET':wing,'MicroMetTwin':climate},reference_time_s)
    active=['MicroMetTwin->WingMPET','MicroMetTwin->MESA_NAV','MESA_NAV->WingMPET']
    coc=ConsistencyManager(cfg).evaluate(
        std,{'MESA_NAV':1/3,'WingMPET':1/3,'MicroMetTwin':1/3},
        {'active_edges':active},{e:1.0 for e in active})
    assert mesa['timestamp'] == reference_time_s, stage
    assert 'MESA_NAV:ground_velocity_ned_m_s' not in (coc.get('missing_expected_observations') or []), stage
    assert 'velocity_triangle_ned' in (coc.get('constraint_residuals') or {}), stage
    assert not (coc.get('uncompiled_constraints') or []), stage
    assert not ((coc.get('consistency_region') or {}).get('unassessed_edges') or []), stage
    assert coc.get('status') == 'solved', stage


def test_turn_replay_consistency_remains_fully_assessed_after_cycle154_boundary():
    # Historical turn failure started when MESA local time lagged the mission clock.
    for step in range(4,7):
        _assess_phase('turn',153.001+step,2.906+0.012*step)


def test_approach_replay_consistency_is_complete_from_first_step():
    # Historical approach was already incomplete at step 1.  The same mission-time
    # authority must keep every representative replay step fully assessed.
    for step in range(1,7):
        _assess_phase('approach',168.001+step,3.086+0.012*step)
