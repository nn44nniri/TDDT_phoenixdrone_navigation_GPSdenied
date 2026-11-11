from pathlib import Path
from tddt_layer7.tdoc_adapter import Layer7SideModule
from tddt_layer7.cli import demo_payload


def test_formalism_documents_bundled():
    root=Path(__file__).resolve().parents[1]
    assert (root/'docs/Formalism_layer7_v2.md').exists()
    assert (root/'docs/TDDT_Dynamic_NTwin_Layer7_details_FA_v2.md').exists()
    assert (root/'docs/TDDT_Dynamic_NTwin_TDOC_details_FA_v4.md').exists()


def test_runtime_compliance_is_computed_not_hardcoded(tmp_path):
    out=Layer7SideModule(state_root=tmp_path/'s').process(demo_payload())
    required=['real_offline_separation','error_decomposition','deterministic_route','evidence_sufficiency','bounded_WKG','shadow_validation','safe_commit','rollback','version_provenance','typed_feedback']
    assert all(out['compliance'][x] for x in required) and out['compliance']['Compliance_7']
    bad=demo_payload(); bad['PredictionEvidence']['version']='bad'
    out2=Layer7SideModule(state_root=tmp_path/'s2').process(bad)
    assert not out2['compliance']['Compliance_7']


def test_route_precedes_trigger_in_pipeline_source():
    import inspect, tddt_layer7.pipeline as p
    s=inspect.getsource(p.Layer7Pipeline.process)
    assert s.index('route = self.route.route') < s.index('trig = self.triggers.decide')
