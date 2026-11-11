from layer3.runtime import Layer3FMURuntime

class _Twin:
    def evaluate_packet(self, operation, **kw):
        return {"twin_id":"MESA_NAV","timestamp":0.018,"state":{"velocity_n":1.0,"velocity_e":2.0,"velocity_d":3.0},"provenance":{}}
class _Contracts:
    def output_envelopes(self,*a,**k): return {}

def _runtime():
    rt=Layer3FMURuntime.__new__(Layer3FMURuntime)
    rt.contracts=_Contracts()
    return rt

def test_isolated_full_fidelity_time_origin_is_bound_to_mission_reference():
    rt=_runtime()
    p=rt._evaluate_domain_packet("MESA_NAV",_Twin(),5,context={"tdoc_full_fidelity_reference_time_s":158.001},inputs={})
    assert p["timestamp"] == 158.001
    rec=p["provenance"]["tdoc_isolated_fmi_time_alignment"]
    assert rec["simulation_only"] is True
    assert rec["not_field_evidence"] is True
    assert rec["local_fmi_timestamp_s"] == 0.018

def test_operational_packet_timestamp_is_not_overridden_without_explicit_ff_key():
    rt=_runtime()
    p=rt._evaluate_domain_packet("MESA_NAV",_Twin(),5,context={"reference_time_s":158.001},inputs={})
    assert p["timestamp"] == 0.018
    assert "tdoc_isolated_fmi_time_alignment" not in p["provenance"]

from pathlib import Path
from layer3 import Layer3Config
from layer3.alignment import STDAlignment


def _alignment():
    root=Path(__file__).resolve().parents[1]
    cfg=Layer3Config.from_yaml(root/'configs/configuration_boulder.yaml',package_root=root)
    cfg.alignment.strict_units=False; cfg.alignment.strict_frames=False; cfg.alignment.strict_semantics=False
    return STDAlignment(cfg)


def _ff_packet(ts=0.018):
    return {
        'twin_id':'MESA_NAV','timestamp':ts,
        'state':{'velocity_n':1.0,'velocity_e':2.0,'velocity_d':3.0},
        'quality':{'score':1.0},'validity':{'score':1.0},
        'reliability':{'prior':1.0},'uncertainty':{'by_output':{}},'provenance':{}
    }


def test_alignment_sublayer_consumes_tdoc_full_fidelity_reference_context_directly():
    a=_alignment()
    out=a.align_packet(_ff_packet(0.018),158.001,context={'tdoc_full_fidelity_reference_time_s':158.001})
    assert out.source_time_s == 158.001
    assert out.age_s == 0.0
    assert out.freshness == 1.0
    assert out.gate == 1
    rec=out.provenance['tdoc_full_fidelity_time_origin_alignment']
    assert rec['simulation_only'] is True
    assert rec['not_field_evidence'] is True
    assert rec['local_fmi_timestamp_s'] == 0.018
    assert rec['mission_reference_time_s'] == 158.001


def test_build_std_propagates_full_fidelity_time_context_into_alignment():
    a=_alignment()
    std=a.build_std({'MESA_NAV':_ff_packet(0.018)},158.001,
        context={'tdoc_full_fidelity_reference_time_s':158.001})
    rep=std.representations['MESA_NAV']
    assert rep.source_time_s == 158.001
    assert rep.gate == 1
    assert rep.provenance['tdoc_full_fidelity_time_origin_alignment']['local_fmi_timestamp_s'] == 0.018


def test_mismatched_full_fidelity_reference_context_does_not_override_packet_time():
    a=_alignment()
    out=a.align_packet(_ff_packet(0.018),158.001,context={'tdoc_full_fidelity_reference_time_s':159.001})
    assert out.source_time_s == 0.018
    assert out.gate == 0
    assert 'tdoc_full_fidelity_time_origin_alignment' not in out.provenance
    assert out.provenance['time_alignment_warning'] == 'tdoc_full_fidelity_reference_time_mismatch_or_invalid'
