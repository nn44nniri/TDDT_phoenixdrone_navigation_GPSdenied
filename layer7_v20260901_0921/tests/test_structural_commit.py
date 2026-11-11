from tddt_layer7.tdoc_adapter import Layer7SideModule
from tddt_layer7.cli import demo_payload


def validator(req):
    return {'base_version':req['base_version'],'candidate_hash':req['candidate_hash'],'stability_ok':True,
            'active_metrics':{'error':2.0},'candidate_metrics':{'error':1.0},'violation_count':0,
            'coverage_ok':True,'margin':0.5,'sample_count':6}


def structural_payload(i=0,version='7'):
    p=demo_payload(); p['transaction_id']=f't{i}'; p['v']=version
    for sec in ['ExecutionEvidence','PredictionEvidence','NetworkSnapshot']:
        p[sec]['timestamp']=100+i; p[sec]['version']=version
    p['DecisionTrace']['version']=version; p['DecisionTrace']['timestamp']=100+i
    p['NetworkSnapshot']['coc_cost']=2.0
    p['AdaptationHints'].update({
      'structural_error':2.0,'maturity':1.0,'authorized':True,'identity_ok':True,'policy_ok':True,'trace_ok':True,
      'coupling_error':{'MESA->WingMPET':2.0},'coupling_sensitivity':{'MESA->WingMPET':1.0},
      'coupling_variance':{'MESA->WingMPET':0.2},'coupling_prior_variance':{'MESA->WingMPET':1.0},
      'coupling_bounds':{'MESA->WingMPET':[0.0,1.0]},
      'weight_bounds':{'MESA':[0,1],'WingMPET':[0,1],'MicroMet':[0,1]},
      'tep_centroids':{'MESA':[[10.0]]},'tep_descriptor':{'MESA':[10.1]},'tep_max_distance':{'MESA':2.0}
    })
    return p


def test_slow_structural_commit_via_candidate_specific_tdoc_validation(tmp_path):
    committed=[]
    def cb(rec): committed.append(rec); return rec['candidate_version']
    m=Layer7SideModule(tdoc_commit_callback=cb,tdoc_validation_callback=validator,state_root=tmp_path/'state')
    last=None
    for i in range(6): last=m.process(structural_payload(i))
    assert last['trigger']['slow']
    assert committed and last['commit']['status']=='COMMITTED' and last['compliance']['Compliance_7']


def test_slow_boundary_is_distinct_from_slow_trigger(tmp_path):
    m=Layer7SideModule(state_root=tmp_path/'state2'); last=None
    for i in range(6):
        p=demo_payload(); p['transaction_id']=f'b{i}'
        for sec in ['ExecutionEvidence','PredictionEvidence','NetworkSnapshot']: p[sec]['timestamp']=200+i
        last=m.process(p)
    assert last['trigger']['slow_boundary'] and not last['trigger']['slow']


def test_post_commit_monitor_requests_and_applies_rollback(tmp_path):
    ops=[]
    def cb(rec):
        ops.append(rec['operation'])
        return rec.get('candidate_version',rec.get('target_version'))
    m=Layer7SideModule(tdoc_commit_callback=cb,tdoc_validation_callback=validator,tdoc_rollback_callback=cb,state_root=tmp_path/'state3')
    last=None
    for i in range(6): last=m.process(structural_payload(i))
    assert last['commit']['status']=='COMMITTED'
    p=demo_payload(); p['v']='8'
    for sec in ['ExecutionEvidence','PredictionEvidence','NetworkSnapshot']: p[sec]['version']='8'; p[sec]['timestamp']=200
    p['DecisionTrace']['version']='8'; p['DecisionTrace']['timestamp']=200; p['DecisionTrace']['constraint_violations']=1
    p['NetworkSnapshot']['uncertainty']=100.0
    out=m.process(p)
    assert out['commit']['status']=='ROLLED_BACK'
    assert out['mode']=='DEGRADED' and 'rollback' in ops
