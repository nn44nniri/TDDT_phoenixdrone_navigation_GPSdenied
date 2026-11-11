import copy, json
import numpy as np
import pytest
from layer4.snapshot import Layer3SnapshotAdapter
from layer4.multifidelity import CertifiedMultiFidelityManager
from layer4.models import Candidate, Scenario


def _snap(root,cfg,phase):
    s=Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    s=copy.deepcopy(s)
    s.context=dict(s.context or {})
    s.context['flight_phase']=phase
    s.context['effective_climate_fingerprint']='climate-s12'
    return s


def _candidate(s):
    cov=(np.eye(19)*0.01).tolist()
    return Candidate('C-S12','S-S12',s.version,s.hash,1.0,[[0.0]*19],[cov],[{}],[[0.0]*5],{'mission':0.0})


def _scenario():
    return Scenario('S-S12',1.0,'G_nominal',{'nodes':['MESA_NAV','WingMPET','Climate']},[[0.0]*5],[[0.0]*6],[],
                    {'disturbance_name':'nominal','degradation_name':'none','coupling_mode':'nominal','coupling_delta':{},'model_parameters':{}},
                    {'safe':True},{'mission':1.0},{})


def _physical_verification():
    return {
        'verified':True,'candidate_verified':True,'executed':True,'verification_origin':'FF_CURRENT',
        'physical_actions_consumed':True,'physical_runtime_safe':True,'physical_mission_safe':True,
        'wingmpet_physical_verdict':{
            'authoritative':True,'non_bypassable':True,'override_allowed':False,'all_rows_vtol_feasible':True,
            'rows':[{'ok':True,'source':'WingMPET/op27-substepped','action_consumed':True,
                     'replay_vtol_feasible':True,'output_vtol_feasible':True,'substeps_all_vtol_feasible':True,'substep_count':100}],
        },
    }


@pytest.mark.parametrize('phase',['vertical_takeoff','hover','vertical_landing'])
def test_vertical_certificate_requires_real_physical_verdict_and_persists(root,cfg,tmp_path,phase):
    cfg.data['verification']['certificate_bank_path']=str(tmp_path/'ff_certificates.jsonl')
    mf=CertifiedMultiFidelityManager(cfg); s=_snap(root,cfg,phase); c=_candidate(s); sc=_scenario()

    # A boolean-only "verified" claim is not enough for vertical takeoff/hover/landing issuance.
    assert mf.build(c,s,sc,'model-s12','param-s12',
                    {'verified':True,'candidate_verified':True,'executed':True,'verification_origin':'FF_CURRENT'},
                    require_physical_verdict=True) is None

    cert=mf.build(c,s,sc,'model-s12','param-s12',_physical_verification(),require_physical_verdict=True)
    assert cert is not None
    assert cert['schema']=='Layer4-FF-certificate-v4'
    assert cert['phase']==phase
    assert cert['bank_persisted'] is True
    assert cert['issuance_contract']=='Layer4-FF-vertical-certificate-issuance-v1'
    assert cert['physical_issuance_evidence']['verdict_authoritative'] is True
    assert cert['physical_issuance_evidence']['all_rows_vtol_feasible'] is True
    assert cert['physical_issuance_evidence']['verdict_sources']==['WingMPET/op27-substepped']
    assert cert['physical_verdict_hash']
    assert cert['state_envelope'] and cert['action_envelope']

    rows=[json.loads(x) for x in (tmp_path/'ff_certificates.jsonl').read_text().splitlines() if x.strip()]
    assert len(rows)==1 and rows[0]['certificate_id']==cert['certificate_id']
    assert mf.find(c,s,sc,'model-s12','param-s12')['certificate_id']==cert['certificate_id']
