from pathlib import Path
from layer3 import Layer3Config
from layer3.alignment import STDAlignment


def _cfg():
    root=Path(__file__).resolve().parents[1]
    cfg=Layer3Config.from_yaml(root/'configs/configuration_boulder.yaml',package_root=root)
    cfg.alignment.strict_units=False; cfg.alignment.strict_frames=False; cfg.alignment.strict_semantics=False
    return cfg


def _packet(ts):
    return {
        'twin_id':'WingMPET','timestamp':ts,
        'state':{'airspeed_norm':13.0,'flightpath_angle':0.0,'air_heading':0.0},
        'quality':{'score':1.0},'validity':{'score':1.0},
        'reliability':{'prior':1.0},'uncertainty':{'by_output':{}},'provenance':{}
    }


def test_large_future_timestamp_is_not_fresh():
    cfg=_cfg(); a=STDAlignment(cfg)
    out=a.align_packet(_packet(227.619),153.001)
    assert out.source_time_s == 227.619
    assert out.age_s == 0.0
    assert out.freshness == 0.0
    assert out.gate == 0
    assert out.provenance['future_time_violation'] is True
    assert out.provenance['future_skew_s'] > 74.0


def test_small_bounded_clock_skew_is_tolerated():
    cfg=_cfg(); a=STDAlignment(cfg)
    out=a.align_packet(_packet(153.021),153.001)
    assert out.gate == 1
    assert out.freshness == 1.0
    assert out.provenance['future_time_violation'] is False
