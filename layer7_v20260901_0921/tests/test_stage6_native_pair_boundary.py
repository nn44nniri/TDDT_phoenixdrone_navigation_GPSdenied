"""Stage6 guard: diagnostic physical pairs never become flight authority."""
from __future__ import annotations
import copy,json,math,subprocess,sys
from pathlib import Path
import pytest
from tddt_layer7.cli import demo_payload
from tddt_layer7.native_pair import check_pair, is_native_request
from tddt_layer7.evidence import EvidenceGateway
from tddt_layer7.config import Layer7Config
from tddt_layer7.tdoc_adapter import Layer7SideModule


@pytest.fixture
def native():
    p=Path(__file__).resolve().parents[1]/'examples/stage6_native_pair_EXAMPLE_NOT_SITE_EVIDENCE.json'
    return json.loads(p.read_text())


def inp(p):return Layer7SideModule.input_from_dict(p)


def test_example_native_pair_is_comparable_not_certified(native):
    result=check_pair(inp(native));assert result.comparable and result.required
    assert result.flight_authorized is False and result.native_controller_certified is False
    assert result.state_residual['x']==pytest.approx(.7)
    assert result.normalized_output_residual==pytest.approx(2.0)
    gate=EvidenceGateway(Layer7Config()).check(inp(native))
    assert gate.native_pair['comparable'] is True
    assert not gate.ok and 'native_pair_matched_diagnostic_only_not_certified' in gate.reasons


@pytest.mark.parametrize('kind,field,value,expected',[ 
    ('ExecutionEvidence','candidate_id','DIFFERENT','binding_mismatch:candidate_id'),
    ('PredictionEvidence','snapshot_hash','d'*64,'binding_mismatch:snapshot_hash'),
    ('ExecutionEvidence','flight_mode','QSTABILIZE','binding_mismatch:flight_mode'),
    ('PredictionEvidence','command_id','wrong','binding_mismatch:command_id'),
    ('ExecutionEvidence','transaction_id','DIFFERENT','binding_mismatch:transaction_id'),
    ('PredictionEvidence','plant_time_s',102.0,'plant_time_mismatch_or_missing'),
    ('PredictionEvidence','target_plant_timestamp',102.0,'target_plant_time_mismatch_or_missing'),
    ('PredictionEvidence','prediction_kind','model_only','state_pair_source_kind_mismatch'),
    ('ExecutionEvidence','observation_kind','pilot_control_effort','state_pair_source_kind_mismatch'),
])
def test_wrong_identity_clock_or_origin_rejected(native,kind,field,value,expected):
    native[kind]['provenance'][field]=value
    result=check_pair(inp(native));assert not result.comparable
    assert expected in result.reasons


def test_outer_tx_and_snapshot_hash_are_not_trusted(native):
    native['transaction_id']='other'
    native['NetworkSnapshot']['content_hash']='f'*64
    v=check_pair(inp(native))
    assert 'outer_transaction_mismatch' in v.reasons
    assert 'network_snapshot_hash_mismatch' in v.reasons


def test_dropped_observation_vs_prediction_rejected(native):
    native['ExecutionEvidence']['x_obs']={}
    assert 'missing_or_partial_state_pair' in check_pair(inp(native)).reasons


def test_state_unit_and_frame_are_strict(native):
    native['PredictionEvidence']['provenance']['state_semantics']['x']['frame']='ENU'
    assert 'state_semantic_mismatch:x' in check_pair(inp(native)).reasons
    native['PredictionEvidence']['provenance']['state_semantics']['x']['frame']='NED'
    native['PredictionEvidence']['provenance']['state_semantics']['x']['unit']='ft'
    assert 'state_semantic_mismatch:x' in check_pair(inp(native)).reasons


def test_motor_rpm_unmeasured_never_inferred_from_pwm(native):
    native['ExecutionEvidence']['x_obs']={'motor_rpm':2000}
    native['PredictionEvidence']['x_hat_td']={'motor_rpm':2100}
    for scope in ('ExecutionEvidence','PredictionEvidence'):
        native[scope]['provenance']['state_semantics']={'motor_rpm':{'quantity':'rotation_speed','unit':'rpm','frame':'SCALAR'}}
    assert 'motor_speed_not_independently_observed:motor_rpm' in check_pair(inp(native)).reasons
    native['ExecutionEvidence']['provenance']['motor_speed_source']='independent_sensor'
    assert check_pair(inp(native)).comparable
    assert check_pair(inp(native)).native_controller_certified is False


def test_pilot_effort_fallback_is_not_physical_validation(native):
    native['ExecutionEvidence']['y_real']={'pilot_control_effort':.5}
    native['PredictionEvidence']['y_hat_td']={'pilot_control_effort':.4}
    both={'pilot_control_effort':{'quantity':'pilot_control_effort','unit':'1','frame':'SCALAR'}}
    native['ExecutionEvidence']['output_semantics']=both
    native['PredictionEvidence']['output_semantics']=both
    native['PredictionEvidence']['sigma_pred']={'pilot_control_effort':.2}
    assert 'pilot_effort_not_physical_state:pilot_control_effort' in check_pair(inp(native)).reasons


def test_nonfinite_values_and_uncertainty_cannot_pass_gateway(native):
    legacy=demo_payload()
    legacy['ExecutionEvidence']['y_real']['x']=float('nan')
    g=EvidenceGateway(Layer7Config()).check(inp(legacy))
    assert not g.ok and 'invalid_numeric:y_real.x' in g.reasons
    legacy=demo_payload();legacy['PredictionEvidence']['sigma_pred']['x']=0
    g=EvidenceGateway(Layer7Config()).check(inp(legacy))
    assert not g.ok and 'invalid_numeric:sigma_pred.x' in g.reasons
    legacy=demo_payload();legacy['ExecutionEvidence']['timestamp']=float('inf')
    assert not EvidenceGateway(Layer7Config()).check(inp(legacy)).freshness_ok


def test_native_pipeline_rejection_has_no_feedback_or_mutation(native,tmp_path,monkeypatch):
    monkeypatch.setenv('TDDT_KSTORE_ROOT',str(tmp_path/'kstore'))
    cb_calls=[]
    def cb(arg): cb_calls.append(arg);return 'approved'
    m=Layer7SideModule(tdoc_commit_callback=cb,state_root=tmp_path/'state')
    history_before=(len(m.pipeline.drift.history) if hasattr(m.pipeline.drift,'history') else repr(m.pipeline.drift.run_probs))
    out=m.process(native)
    assert out['mode']=='REJECT' and not out['evidence_ok']
    assert out['residuals']['state_residual']=={} and out['residuals']['output_residual']=={}
    assert out['feedback_layer2']=={} and out['feedback_layer31']=={}
    assert out['feedback_layer32']=={} and out['feedback_layer33']=={}
    assert out['commit']['status']=='REJECTED' and not cb_calls
    assert out['provenance']['native_pair']['comparable'] is True
    assert out['provenance']['dynamic_validation_certified'] is False
    assert out['provenance']['flight_authorized'] is False
    history_after=(len(m.pipeline.drift.history) if hasattr(m.pipeline.drift,'history') else repr(m.pipeline.drift.run_probs))
    assert history_before==history_after
    assert not list((tmp_path/'kstore').rglob('*.jsonl'))


def test_non_native_demo_is_diagnostic_not_certified(tmp_path):
    m=Layer7SideModule(state_root=tmp_path/'state')
    p=demo_payload();v=m.process(p)
    assert v['evidence_ok']
    assert v['provenance']['evidence_scope']=='LEGACY_MODEL_OR_DIAGNOSTIC_ONLY'
    assert v['provenance']['dynamic_validation_certified'] is False
    assert v['provenance']['flight_authorized'] is False


def test_cli_positive_and_negative_are_read_only(native,tmp_path):
    root=Path(__file__).resolve().parents[1]
    source=root/'examples/stage6_native_pair_EXAMPLE_NOT_SITE_EVIDENCE.json'
    cmd=[sys.executable,str(root/'scripts/validate_stage6_native_pair.py'),'--input',str(source),'--output',str(tmp_path/'good.json')]
    c=subprocess.run(cmd,capture_output=True,text=True)
    assert c.returncode==0, c.stderr
    data=json.loads((tmp_path/'good.json').read_text());assert data['status']=='PAIR_COMPARABLE_DIAGNOSTIC_ONLY'
    assert not data['operational_commit_allowed']
    assert subprocess.run(cmd,capture_output=True).returncode==3
    native['PredictionEvidence']['provenance']['candidate_id']='DIFFERENT'
    file=tmp_path/'bad_input.json';file.write_text(json.dumps(native))
    bad=subprocess.run([sys.executable,str(root/'scripts/validate_stage6_native_pair.py'),'--input',str(file),'--output',str(tmp_path/'bad.json')],capture_output=True,text=True)
    assert bad.returncode==2
    assert json.loads((tmp_path/'bad.json').read_text())['status']=='BLOCKED_UNPAIRED_NATIVE_EVIDENCE'


def test_no_native_markers_does_not_implicitly_upgrade(native):
    p=demo_payload();assert not is_native_request(inp(p))
    result=check_pair(inp(p));assert not result.required
    assert result.native_controller_certified is False


def test_malformed_semantic_map_fails_closed(native):
    native['PredictionEvidence']['provenance']['state_semantics']='fake'
    result=check_pair(inp(native))
    assert not result.comparable and 'invalid_prediction_state_semantic_container' in result.reasons


def test_whitespace_actuator_feedback_id_is_missing(native):
    native['ExecutionEvidence']['actuator_feedback_id']='  '
    assert 'missing_actual_actuator_feedback_id' in check_pair(inp(native)).reasons
