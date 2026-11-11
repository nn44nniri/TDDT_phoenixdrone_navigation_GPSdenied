import copy, json, time
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.multifidelity import CertifiedMultiFidelityManager
from layer4.models import Candidate, Scenario


def _snap(root,cfg):
    return Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))


def _scenario(sid):
    return Scenario(
        scenario_id=sid, probability=1.0, topology_mode='G_nominal', topology={'nodes':['MESA_NAV','WingMPET','Climate']},
        action_sequence=[[0.1,0.0,0.0,0.0,0.0]], disturbance_sequence=[[0.0]*6], topology_sequence=[],
        parameters={'action_name':'a','disturbance_name':'nominal','degradation_name':'none','coupling_mode':'nominal','coupling_delta':{},'objective_profile':'balanced','model_parameters':{}},
        constraints={'safe':True}, objectives={'mission':1.0}, degradation={}
    )


def _candidate(s,sid):
    return Candidate('C-'+sid,sid,s.version,s.hash,1.0,[[0.0]*19],[[[0.0]*19]*19],[{}],[[0.1,0.0,0.0,0.0,0.0]],{'mission':0})


def test_problem16_regenerated_scenario_id_can_reuse_same_ff_certificate(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg)
    c1=_candidate(s,'S00001'); sc1=_scenario('S00001')
    cert=mf.build(c1,s,sc1,'model','param',{'verified':True,'candidate_verified':True,'executed':True,'reason':'verified'})
    assert cert and cert['scenario_id_provenance']=='S00001'
    c2=_candidate(s,'S00999'); sc2=_scenario('S00999')
    hit=mf.find(c2,s,sc2,'model','param')
    assert hit and hit['certificate_id']==cert['certificate_id']
    stats=mf.reuse_stats(); assert stats['attempts']==1 and stats['hits']==1


def test_problem16_scenario_semantics_still_gate_reuse(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg)
    c1=_candidate(s,'S1'); sc1=_scenario('S1')
    mf.build(c1,s,sc1,'model','param',{'verified':True,'candidate_verified':True,'executed':True})
    sc2=_scenario('S2'); sc2.disturbance_sequence=[[2.0]*6]
    assert mf.find(_candidate(s,'S2'),s,sc2,'model','param') is None
    assert mf.reuse_stats()['misses'].get('context_mismatch')==1


def test_problem16_problem17_contract_is_now_mission_freshness_with_envelope_floors(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg); sc=_scenario('S1'); c=_candidate(s,'S1')
    cert=mf.build(c,s,sc,'model','param',{'verified':True,'candidate_verified':True,'executed':True})
    assert cert['freshness_s']==float(cfg.get('verification.certificate_freshness_s',300.0))
    assert cert['freshness_clock']=='mission_reference'
    assert cert['x_tolerance']==float(cfg.get('verification.certificate_state_tolerance',0.05))
    assert cert['action_tolerance']==float(cfg.get('verification.certificate_action_tolerance',0.01))
    assert cert['state_envelope'] and cert['action_envelope']
    # Wall-clock delay no longer invalidates v4 when mission reference remains aligned.
    rows=[json.loads(x) for x in (tmp_path/'certs.jsonl').read_text().splitlines()]
    rows[0]['created_unix_s']=time.time()-10000.0
    (tmp_path/'certs.jsonl').write_text('\n'.join(json.dumps(x) for x in rows)+'\n'); mf._cache=None
    assert mf.find(_candidate(s,'S2'),s,_scenario('S2'),'model','param') is not None


def test_problem16_legacy_certificates_keep_exact_id_compatibility(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg); sc=_scenario('S1'); c=_candidate(s,'S1')
    # Emulate pre-problem16 certificate schema/signature exactly.
    h=mf._context_hashes(s)
    from layer4.hashing import sha256_obj
    legacy_sig=sha256_obj({'phase':mf.phase(s),'model_hash':'model','parameter_hash':'param','climate_hash':h['climate_hash'],'map_hash':h['map_hash'],'scenario_id':'S1','scenario_parameters':sc.parameters})
    row={'certificate_id':'legacy','origin':'FF_CURRENT','verified':True,'created_unix_s':time.time(),'freshness_s':300.0,'phase':mf.phase(s),'model_hash':'model','parameter_hash':'param','context_signature':legacy_sig,'x0':c.trajectory[0],'actions':c.actions,'x_tolerance':0.05,'action_tolerance':0.01}
    (tmp_path/'certs.jsonl').write_text(json.dumps(row)+'\n')
    assert mf.find(c,s,sc,'model','param')['certificate_id']=='legacy'
    # Different regenerated ID must not broaden a legacy certificate.
    assert mf.find(_candidate(s,'S2'),s,_scenario('S2'),'model','param') is None


def test_exact_negative_certificate_reuses_only_identical_rejected_request(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg); sc=_scenario('S1'); c=_candidate(s,'S1')
    rejection={'verified':False,'candidate_verified':False,'attempted':True,'executed':True,
               'outcome_status':'ATTEMPTED_AND_REJECTED','reason_codes':['chance_constraint_gate_failed']}
    cert=mf.build_rejection(c,s,sc,'model','param',rejection)
    assert cert and cert['authority']=='reject_only_never_verified'
    assert cert['schema']=='Layer4-FF-rejection-certificate-v2'
    assert cert['exact_identity_schema']=='Layer4-semantic-exact-request-v3'
    hit=mf.find_rejection(c,s,sc,'model','param')
    assert hit and hit['certificate_id']==cert['certificate_id'] and hit['verified'] is False
    rerun=_candidate(s,'S1'); rerun.snapshot_hash='different-run-provenance-hash'
    assert mf.find_rejection(rerun,s,sc,'model','param')['certificate_id']==cert['certificate_id']
    changed=_candidate(s,'S1'); changed.actions[0][0]+=1e-12
    assert mf.find_rejection(changed,s,sc,'model','param') is None


def test_exact_negative_certificate_is_bound_to_physical_initial_state(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg); sc=_scenario('S1'); c=_candidate(s,'S1')
    s.context['online_execution_state']={'omega_left':240.0,'omega_right':240.0,'soc':1.0}
    rejection={'verified':False,'candidate_verified':False,'attempted':True,'executed':True,
               'outcome_status':'ATTEMPTED_AND_REJECTED','reason_codes':['physical_gate_failed']}
    cert=mf.build_rejection(c,s,sc,'model','param',rejection)
    assert cert and mf.find_rejection(c,s,sc,'model','param') is not None
    s.context['online_execution_state']['omega_left']=241.0
    assert mf.find_rejection(c,s,sc,'model','param') is None


def test_negative_certificate_never_enters_positive_reuse_lane(root,cfg,tmp_path):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'certs.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg); sc=_scenario('S1'); c=_candidate(s,'S1')
    rejection={'verified':False,'attempted':True,'executed':True,
               'outcome_status':'ATTEMPTED_AND_REJECTED','reason_codes':['physical_gate_failed']}
    assert mf.build_rejection(c,s,sc,'model','param',rejection)
    assert mf.find(c,s,sc,'model','param') is None
