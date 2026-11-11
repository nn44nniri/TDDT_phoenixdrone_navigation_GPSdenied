from tddt_layer7.tdoc_adapter import Layer7SideModule
from tddt_layer7.cli import demo_payload

def test_manifest_is_side_module():
    m=Layer7SideModule.registration_descriptor(); assert m['trigger_owner']=='TDOC' and m['side_module'] and not m['direct_actuation']

def test_pipeline_demo_runs():
    out=Layer7SideModule(state_root='.test_state').process(demo_payload())
    assert out['evidence_ok'] and out['provenance']['trigger_owner']=='TDOC' and out['compliance']['Compliance_7']

def test_version_mismatch_rejected():
    p=demo_payload(); p['PredictionEvidence']['version']='8'
    out=Layer7SideModule(state_root='.test_state2').process(p)
    assert out['mode']=='REJECT' and not out['evidence_ok']

def test_no_direct_actuation_fields():
    out=Layer7SideModule(state_root='.test_state3').process(demo_payload())
    text=str(out).lower(); assert 'pwm' not in text and 'direct_actuation' in text

def test_commit_callback_is_only_commit_owner():
    versions=[]
    def cb(rec): versions.append(rec['candidate_version']); return rec['candidate_version']
    # A single demo does not hit Slow structural trigger; callback therefore must not be called.
    Layer7SideModule(tdoc_commit_callback=cb,state_root='.test_state4').process(demo_payload())
    assert versions==[]


def test_offline_packet_never_operational_commit(tmp_path):
    m=Layer7SideModule(state_root=tmp_path/'state')
    out=m.process_offline({'version':'o1','evidence_type':'simulation','delta_sarg':1,'coc_cost':2,'constraint_violations':0,'simulation_residual':3})
    assert out['stored'] and not out['operational_commit_allowed']

def test_typed_alignment_and_sync_feedback_passthrough(tmp_path):
    p=demo_payload(); p['AdaptationHints'].update({'alignment_error':{'x':2.0},'synchronization_error':{'x':2.0},
        'alignment_unit_delta':{'x':'m->cm'},'fast_period_delta':0.1})
    out=Layer7SideModule(state_root=tmp_path/'state2').process(p)
    assert out['route']['layer31'] and out['route']['layer33']
    assert out['feedback_layer31']['DeltaT_unit']=={'x':'m->cm'}
    assert out['feedback_layer33']['DeltaFastPeriod']==0.1
