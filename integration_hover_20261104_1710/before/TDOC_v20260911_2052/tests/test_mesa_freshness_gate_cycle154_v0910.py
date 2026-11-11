import os
import sys
from pathlib import Path

from tddt_tdoc.adapters.layer2 import Layer2Adapter


def _layer3_alignment_types():
    root=Path(os.environ['TDDT_LAYER3_ROOT']).resolve()
    src=root/'src'
    if str(src) not in sys.path:
        sys.path.insert(0,str(src))
    from layer3 import Layer3Config
    from layer3.alignment import STDAlignment
    return Layer3Config, STDAlignment, root


def _mesa_packet(timestamp):
    return {
        'twin_id':'MESA_NAV',
        'timestamp':float(timestamp),
        'state':{'velocity_n':6.8515,'velocity_e':7.7928,'velocity_d':3.1129},
        'quality':{'score':1.0},
        'validity':{'score':1.0},
        'reliability':{'prior':1.0},
        'uncertainty':{},
        'context':{},
        'provenance':{'source':'MESA_NAV.fmu'},
    }


def test_cycle154_mesa_freshness_no_longer_falls_below_layer3_gate():
    Layer3Config, STDAlignment, root=_layer3_alignment_types()
    cfg=Layer3Config.default(root)
    align=STDAlignment(cfg,tdoc=object())

    # Reproduce problem #2 from the 0910 evidence: local FMI time at cycle 154
    # is about 150 s behind mission time and therefore crosses min_freshness=0.05.
    local=_mesa_packet(2.906)
    old=align.align_packet(local,153.001)
    assert old.freshness < cfg.alignment.min_freshness['default']
    assert old.gate == 0

    # Cumulative problem-01 fix binds the simulation-backed packet to the cycle's
    # mission reference before Layer 3 computes age/freshness.  Problem #2 is the
    # downstream gate regression that this test freezes closed.
    fixed=Layer2Adapter._bind_mesa_simulation_packet_to_mission_time(
        local,
        reference_time_s=153.001,
        alignment={'applied':True,'schema':'TDOC-MESA-state-alignment-regression'},
        learning_mode='OFFLINE_SE',
    )
    current=align.align_packet(fixed,153.001)
    assert current.source_time_s == 153.001
    assert current.age_s == 0.0
    assert current.freshness == 1.0
    assert current.gate == 1
    assert current.canonical_state['ground_velocity_ned_m_s'] == [6.8515,7.7928,3.1129]
    rec=fixed['provenance']['tdoc_mesa_mission_time_alignment']
    assert rec['local_fmi_timestamp_s'] == 2.906
    assert rec['mission_reference_time_s'] == 153.001
    assert rec['not_field_evidence'] is True
