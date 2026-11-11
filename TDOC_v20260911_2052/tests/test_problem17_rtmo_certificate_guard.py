import csv,gzip,json,time
from pathlib import Path
from tddt_tdoc.rtmo.capability_gate import build_capability_manifest


def _reports(root:Path):
    m=root/'offline'/'mission'; m.mkdir(parents=True)
    with (m/'PHASE_HISTORY.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['cycle','reference_time_s','phase']); w.writeheader(); w.writerow({'cycle':1,'reference_time_s':10.0,'phase':'transition_forward'})
    with (m/'LAYER4_CANDIDATES.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['cycle','reference_time_s','verified','feasible']); w.writeheader(); w.writerow({'cycle':1,'reference_time_s':10.0,'verified':'true','feasible':'true'})
    with gzip.open(m/'DECISION_POINT_DETAILS.json.gz','wt') as f:
        json.dump([{'cycle':1,'mission':{'phase':'transition_forward'},'layer5_decision':{'gates':{'FINAL':True}}}],f)
    (m/'VTOL_PHASE_METRICS.json').write_text(json.dumps({'starts_on_ground':False,'touchdown_seen':False,'final_agl_m':999,'final_goal_distance_m':999}))


def _cert(ref=10.0,with_env=True):
    c={'schema':'Layer4-FF-certificate-v4','origin':'FF_CURRENT','verified':True,'phase':'transition_forward',
       'model_hash':'m','parameter_hash':'p','context_signature':'ctx','scenario_semantics_hash':'sem','ff_evidence_hash':'ff',
       'freshness_clock':'mission_reference','reference_time_s':ref,'freshness_s':300.0}
    if with_env:
        c['state_envelope']={'lower':[0.0],'upper':[1.0]}; c['action_envelope']={'lower':[[0.0]],'upper':[[1.0]]}
    return c


def test_rtmo_requires_live_v4_certificate_contract_for_control_phase(tmp_path):
    _reports(tmp_path); bank=tmp_path/'certs.jsonl'; bank.write_text(json.dumps(_cert())+'\n')
    cap=build_capability_manifest(tmp_path,layer6_forward_only=False,certificate_bank_path=bank,require_certificate_guard=True)
    p=cap.phases['transition_forward']
    assert p['capability'] is True and p['certificate_guard']['certificate_contract_ready'] is True


def test_rtmo_rejects_stale_or_envelope_incomplete_certificate(tmp_path):
    _reports(tmp_path); bank=tmp_path/'certs.jsonl'
    bank.write_text(json.dumps(_cert(ref=1000.0))+'\n')
    cap=build_capability_manifest(tmp_path,layer6_forward_only=False,certificate_bank_path=bank,require_certificate_guard=True)
    assert cap.phases['transition_forward']['capability'] is False
    assert 'layer4_ff_certificate_not_reusable_for_phase' in cap.phases['transition_forward']['blockers']
    bank.write_text(json.dumps(_cert(with_env=False))+'\n')
    cap=build_capability_manifest(tmp_path,layer6_forward_only=False,certificate_bank_path=bank,require_certificate_guard=True)
    assert cap.phases['transition_forward']['certificate_guard']['certificate_contract_ready'] is False


def test_rtmo_does_not_promote_legacy_point_certificate(tmp_path):
    _reports(tmp_path); bank=tmp_path/'certs.jsonl'
    legacy={'schema':'Layer4-FF-certificate-v3','origin':'FF_CURRENT','verified':True,'phase':'transition_forward','created_unix_s':time.time(),'freshness_s':300.0,'model_hash':'m','parameter_hash':'p','context_signature':'ctx','x0':[0.0],'actions':[[0.0]],'x_tolerance':0.05,'action_tolerance':0.01}
    bank.write_text(json.dumps(legacy)+'\n')
    cap=build_capability_manifest(tmp_path,layer6_forward_only=False,certificate_bank_path=bank,require_certificate_guard=True)
    g=cap.phases['transition_forward']['certificate_guard']
    assert g['certificate_contract_ready'] is False
    assert 'legacy_certificate_missing_v4_envelope_contract' in g['reasons']
