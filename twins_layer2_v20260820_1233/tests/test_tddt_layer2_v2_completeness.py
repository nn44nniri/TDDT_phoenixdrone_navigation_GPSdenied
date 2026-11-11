from pathlib import Path
import json, math, sys, tempfile, zipfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from twins_fmi import DomainTwin, TEPVersionManager

REQ_MANIFEST=['tep_name','tep_version','created_at','twin_id','twin_model_version','formalism_version','simulation_version','generation_method','sampling_method','perturbation_model','parameter_ranges','coverage','cell_count','min_evidence_per_cell','resolution','evidence_type','field_evidence_present','uncertainty_representation','quality_definition','validity_definition','reliability_definition','held_out_validation','limitations','provenance']
EXPECTED={'MESA_NAV':(72,24,3),'WingMPET':(330,22,15)}
for twin,(cells,ops,cells_per_op) in EXPECTED.items():
    with zipfile.ZipFile(ROOT/'fmus'/f'{twin}.fmu') as z:
        names=set(z.namelist()); base='resources/tddt_layer2/'
        for rel in ['twin_contract.json','metadata/uncertainty_model.json','metadata/quality_rules.json','metadata/validity_envelope.json','metadata/reliability_profile.json','tep/tep.csv','tep/manifest.json','tep/heldout_validation.json','tep/VERSION','tep/quality_report.json','validation/heldout_validation.json']:
            assert base+rel in names,(twin,rel)
        m=json.loads(z.read(base+'tep/manifest.json'))
        assert all(k in m for k in REQ_MANIFEST)
        assert m['cell_count']==cells and m['operation_count']==ops and m['cells_per_operation']==cells_per_op
        assert m['evidence_type']=='simulation_prior' and not m['field_evidence_present'] and m['maturity']=='TEP_sim'
        assert m['update_policy']['runtime_read_only'] and m['update_policy']['rollback_capable']
        # Source TEP manifest is authoritative: never rewrite its raw-trial policy to pass a stale test.
        if m['raw_trials_archived']:
            assert m['raw_trials_file'] is not None
            assert base + 'tep/' + m['raw_trials_file'] in names
        else:
            assert m['raw_trials_file'] is None
        c=json.loads(z.read(base+'twin_contract.json'))
        assert c['independent_fmu'] and c['feedback_contract']['fast_tick_rewrite_allowed'] is False
        assert 'Center_of_Consistency' in c['responsibility_boundary']['layer2_does_not_own']

cfg=ROOT/'config'/'phoenix_drone_v3.yaml'
with DomainTwin(ROOT/'fmus'/'WingMPET.fmu',config=cfg) as w:
    nominal=w.evaluate_packet(5,inputs={'altitude':800.0,'external_temperature':0.0,'external_pressure':0.0,'external_density':0.0,'relative_humidity':0.4,'airspeed':13.0},reset_before=True)
    novel=w.evaluate_packet(5,inputs={'altitude':100000.0,'external_temperature':0.0,'external_pressure':0.0,'external_density':0.0,'relative_humidity':0.4,'airspeed':130.0},freshness_age_s=1000,reset_before=True)
    assert novel.context['tep_novelty']
    assert novel.quality['score'] <= nominal.quality['score']
    assert novel.validity['score'] <= nominal.validity['score']
    if nominal.reliability['prior'] is not None and novel.reliability['prior'] is not None:
        assert novel.reliability['prior'] <= nominal.reliability['prior']
    nu=novel.uncertainty['by_output']; ou=nominal.uncertainty['by_output']
    common=[k for k in nu if k in ou and float(ou[k])>0]
    if common: assert any(float(nu[k])>=float(ou[k]) for k in common)
    try:
        w.evaluate_packet(5,inputs={'altitude':800.0,'external_temperature':0.0,'external_pressure':0.0,'external_density':0.0,'relative_humidity':0.4,'airspeed':13.0},prior_profile={'evidence_type':'field_evidence'})
        raise AssertionError('field evidence masquerade accepted')
    except ValueError: pass

# Versioned/rollback posterior path is external and field-only.
with tempfile.TemporaryDirectory() as td:
    td=Path(td); ex=td/'ex'; ex.mkdir()
    with zipfile.ZipFile(ROOT/'fmus'/'MESA_NAV.fmu') as z: z.extractall(ex)
    mgr=TEPVersionManager(td/'store','MESA_NAV'); prior=mgr.bootstrap(ex/'resources'/'tddt_layer2')
    try:
        mgr.create_posterior([{'cell_id':'MESA_NAV_OP07_E002','evidence_type':'simulation','passed':True}],source='bad')
        raise AssertionError('non-field posterior accepted')
    except ValueError: pass
    post=mgr.create_posterior([{'cell_id':'MESA_NAV_OP07_E002','evidence_type':'field_evidence','passed':True}],source='flight_test_001',provenance={'episode':'E001'})
    assert json.loads((post/'tep'/'manifest.json').read_text())['maturity']=='TEP_hybrid'
    assert (post/'tep'/'posterior_overrides.json').exists()
    rolled=mgr.rollback('v000_sim_prior'); assert rolled.name=='v000_sim_prior'

print('ALL_TDDT_LAYER2_V3_VTOL_COMPLETENESS_TESTS_PASSED')
