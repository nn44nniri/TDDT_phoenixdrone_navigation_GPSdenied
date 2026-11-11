import json
from tddt_tdoc.rtmo.capability_gate import _certificate_bank_guard


def _l4(phase):
    base={p:{} for p in ['on_ground','vertical_takeoff','hover','transition_forward','climb','cruise','turn','approach','transition_back','vertical_landing','touchdown']}
    base[phase]={
        'verified_reference_times':[10.0],
        'model_hashes':['model-s12'],
        'parameter_hashes':['param-s12'],
        'effective_climate_fingerprints':['climate-s12'],
    }
    return base


def _cert(phase,physical=True):
    c={
        'schema':'Layer4-FF-certificate-v4','origin':'FF_CURRENT','verified':True,'phase':phase,
        'model_hash':'model-s12','parameter_hash':'param-s12','climate_hash':'climate-s12',
        'context_signature':'ctx-s12','scenario_semantics_hash':'sem-s12','ff_evidence_hash':'ff-s12',
        'freshness_clock':'mission_reference','reference_time_s':10.0,'freshness_s':300.0,
        'state_envelope':{'lower':[0.0],'upper':[1.0]},
        'action_envelope':{'lower':[[0.0]],'upper':[[1.0]]},
    }
    if physical:
        c.update({
            'bank_persisted':True,
            'issuance_contract':'Layer4-FF-vertical-certificate-issuance-v1',
            'physical_verdict_hash':'pv-s12',
            'physical_issuance_evidence':{
                'verification_origin':'FF_CURRENT','physical_actions_consumed':True,
                'physical_runtime_safe':True,'physical_mission_safe':True,
                'verdict_authoritative':True,'verdict_non_bypassable':True,
                'all_rows_vtol_feasible':True,'verdict_sources':['WingMPET/op27-substepped'],
            },
        })
    return c


def test_takeoff_hover_bank_requires_persisted_physical_v4_contract(tmp_path):
    for phase in ('vertical_takeoff','hover','vertical_landing'):
        bank=tmp_path/f'{phase}.jsonl'
        bank.write_text(json.dumps(_cert(phase,physical=False))+'\n')
        g=_certificate_bank_guard(bank,_l4(phase))[phase]
        assert g['certificate_contract_ready'] is False
        assert g['reusable_certificate_count']==0

        bank.write_text(json.dumps(_cert(phase,physical=True))+'\n')
        g=_certificate_bank_guard(bank,_l4(phase))[phase]
        assert g['certificate_contract_ready'] is True
        assert g['reusable_certificate_count']==1
        assert g['bank_entries_for_phase']==1


def test_tdoc_ff_path_fails_closed_when_vertical_certificate_cannot_be_issued():
    # Source-level invariant protects against a future refactor accidentally keeping
    # verified=true after certificate persistence fails for vertical takeoff/hover/landing.
    from pathlib import Path
    import tddt_tdoc.adapters.layer4 as mod
    src=Path(mod.__file__).read_text(encoding='utf-8')
    assert 'require_vertical_certificate=phase_name in {"vertical_takeoff","hover","vertical_landing"}' in src
    assert 'ff_certificate_issuance_failed' in src
    assert 'vr["verified"]=False' in src
    assert 'C_FF_certificate_issuance_integrity' in src
