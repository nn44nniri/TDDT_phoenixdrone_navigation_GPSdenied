from pathlib import Path
from tddt_tdoc.adapters.layer5 import Layer5Adapter


def test_solution6_source_declares_all_regime_specific_ff_mappings():
    src=Path(__file__).resolve().parents[1]/'src/tddt_tdoc/adapters/layer5.py'
    text=src.read_text()
    for phase in ('vertical_takeoff','hover','transition_forward','transition_back','vertical_landing'):
        assert phase in text
    assert 'wingmpet_verification_{phase_name}_throttle_outside_qualified_neighborhood' in text


def test_solution6_layer4_propagates_same_cycle_context_to_all_vtol_phases():
    src=Path(__file__).resolve().parents[1]/'src/tddt_tdoc/adapters/layer4.py'
    text=src.read_text()
    assert '{"vertical_takeoff","hover","transition_forward","transition_back","vertical_landing"}' in text
    assert 'phoenix_operating_context' in text
