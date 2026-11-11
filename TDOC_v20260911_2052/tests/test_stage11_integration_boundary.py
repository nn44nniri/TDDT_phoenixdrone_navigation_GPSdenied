"""Stage11 source and cross-library boundary tests; no runtime, network or SITL."""
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
import pytest

from tddt_tdoc.integration_audit import ROOTS,audit
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from tddt_tdoc.layer6_bridge import Layer6Bridge,Layer6BridgeError


def write(base, rel, content=b'example'):
    p=base/rel
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_bytes(content)
    return p


def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()


def fixtures(tmp_path):
    root=tmp_path
    for name in ROOTS.values(): (root/name).mkdir()
    for rel in [
        'layer5_v20260911_1510/src/layer5/native_pilot_boundary.py',
        'layer7_v20260901_0921/src/tddt_layer7/native_pair.py',
        'TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914/src/tdoc_unit_converter/boundary.py',
        'climate_twin_v20260911_0922/scripts/validate_stage8_binding.py',
        'boulder_multilayer_map_builder_v20260814_1215/scripts/validate_stage9_site_map.py',
        'SARG_v20260821_0945/scripts/validate_stage10_site_sarg.py']:
        write(root,rel,b'patch marker')
    (root/'TDOC_v20260911_2052/src/tddt_tdoc').mkdir(parents=True)
    (root/'reports/stage8').mkdir(parents=True)
    (root/'reports/stage9').mkdir(parents=True)
    (root/'reports/stage10').mkdir(parents=True)
    forcing=write(root,'internet_datasets/climate_twin/openmeteo_24h.csv')
    climate=write(root,'climate_twin_v20260911_0922/fmu/MicroMetTwin.fmu')
    s8={'status':'FORCING_EPOCH_BOUND_DIAGNOSTIC_ONLY','schema':'TDDT-Climate-ForcingBinding-v1',
        'forcing_file':str(forcing),'fmu_file':str(climate),'forcing_sha256':sha(forcing),'fmu_sha256':sha(climate),
        'mission_epoch_utc':'2026-10-09T12:00:00+00:00','plant_elapsed_s':0,
        'mapped_forecast_time_utc':'2026-10-09T12:00:00+00:00',
        'common_start_utc':'2026-10-09T12:00:00+00:00','common_end_utc':'2026-10-10T11:00:00+00:00',
        'wind_frame':'NED','wind_bearing_convention':'METEOROLOGICAL_FROM_DEG_CLOCKWISE_FROM_NORTH',
        'flight_authorized':False,'native_controller_certified':False,
        'native_environment_confirmed':False,'temperature_applied_in_jsbsim':False,'humidity_applied_in_jsbsim':False}
    b=root/'boulder_multilayer_map_builder_v20260814_1215';dataset=b/'outputs/region_v1_map'
    region=write(b,'region/region_v1.geojson',b'{"type":"FeatureCollection","features":[]}')
    rf={}
    for rel in ['terrain_obstacles.tif','climate_contexts.tif','magnetic_anomaly.tif','CRP/crp_cluster_map.tif']:
        f=write(dataset,rel)
        rf[rel]={'sha256':sha(f),'crs':'EPSG:32613','dimensions':[343,342],
                 'affine':[10,0,0,0,-10,0,0,0,1]}
    s9={'status':'MAP_CRP_SPATIAL_DIAGNOSTIC_PASS_UNCERTIFIED','schema':'TDDT-Boulder-SpatialBinding-v1',
        'region_file':str(region),'region_sha256':sha(region),'crs':'EPSG:32613',
        'grid_width_height':[343,342],'raster_files':rf,'crp':{'cluster_count':32},
        'points':{'origin':{'terrain_elevation_m':1800,'terrain_vertical_datum_verified':False},
                  'destination':{'terrain_elevation_m':1636,'terrain_vertical_datum_verified':False}},
        'flight_authorized':False,'native_controller_certified':False,
        'native_agl_verified':False,'vertical_datum_authenticated':False}
    sd=root/'SARG_v20260821_0945/generated/SARG'
    files={}
    for name in ['sarg_records.csv','sarg_lookup_index.csv','sarg_patterns.csv','metadata.json','status_codes.csv','region_v1.geojson']:
        f=write(sd,name);files[name]=sha(f)
    twins=root/'twins_layer2_v20260820_1233'
    mesa=write(twins,'fmus/MESA_NAV.fmu');wing=write(twins,'fmus/WingMPET.fmu')
    s10={'status':'SARG_REFERENCE_CONTRACT_DIAGNOSTIC_PASS_UNCERTIFIED',
         'schema':'TDDT-SARG-ReferenceBoundary-v1','dataset_dir':str(sd),'dataset_files_sha256':files,
         'record_count':1920,'pattern_count':1920,'index_validated':True,'phase_coverage_exact':True,
         'regime_count':10,'phase_count':6,'crp_count':32,'crp_catalog_count':32,
         'region_file_sha256':sha(region),'source_fmu_hash_binding_checked':True,
         'source_fmu_hash_checks':{
             'mesa_nav_fmu':{'sha256':sha(mesa),'verified_against_frozen_sarg_metadata':True},
             'wingmpet_fmu':{'sha256':sha(wing),'verified_against_frozen_sarg_metadata':True},
             'drone_config':{'sha256':'a'*64,'verified_against_frozen_sarg_metadata':True}},
         'data_mutated':False,'sarg_rebuilt':False,'flight_authorized':False,'native_controller_certified':False,
         'reference_is_current_navigation_state':False}
    for k,rep in [('stage8',s8),('stage9',s9),('stage10',s10)]:
        write(root,f'reports/{k}/audit.json',json.dumps(rep).encode())
    return root, s8,s9,s10


def perform(root,*,require_native=False):
    return audit(root,stage8_report=root/'reports/stage8/audit.json',
                 stage9_report=root/'reports/stage9/audit.json',
                 stage10_report=root/'reports/stage10/audit.json',
                 environ={},require_native=require_native)


def report(root,key,data):
    write(root,f'reports/{key}/audit.json',json.dumps(data).encode())


def test_good_reference_binding_does_not_certify_native(tmp_path):
    root,*_=fixtures(tmp_path)
    d=perform(root)
    assert d['status']=='CROSS_LAYER_REFERENCE_BINDING_PASS_UNCERTIFIED', d['blockers']
    assert d['diagnostic_binding_comparable'] and not d['flight_authorized']
    assert not d['operational_commit_allowed'] and not d['native_controller_certified']


def test_native_request_rejects_even_valid_reference(tmp_path):
    root,*_=fixtures(tmp_path)
    d=perform(root,require_native=True)
    assert d['status']=='BLOCKED_NATIVE_CONTROLLER_AND_ENVIRONMENT_CERTIFICATION_UNAVAILABLE'
    assert d['diagnostic_binding_comparable'] and not d['flight_authorized']


@pytest.mark.parametrize('target', ['forcing','fmu','terrain','sarg','mesa'])
def test_source_hash_drift_detected(tmp_path,target):
    root,*_=fixtures(tmp_path)
    files={'forcing':'internet_datasets/climate_twin/openmeteo_24h.csv',
           'fmu':'climate_twin_v20260911_0922/fmu/MicroMetTwin.fmu',
           'terrain':'boulder_multilayer_map_builder_v20260814_1215/outputs/region_v1_map/terrain_obstacles.tif',
           'sarg':'SARG_v20260821_0945/generated/SARG/sarg_records.csv',
           'mesa':'twins_layer2_v20260820_1233/fmus/MESA_NAV.fmu'}
    (root/files[target]).write_bytes(b'altered')
    d=perform(root)
    assert d['status']=='BLOCKED_CROSS_LAYER_CONTRACT'
    assert any('sha256_mismatch' in x for x in d['blockers'])


def test_region_mismatch_between_stage9_and_sarg(tmp_path):
    root,s8,s9,s10=fixtures(tmp_path)
    s10['region_file_sha256']='0'*64;report(root,'stage10',s10)
    d=perform(root)
    assert not d['checks']['stage9_stage10_region_binding']['ok']


def test_missing_stage_report_is_fail_closed(tmp_path):
    root,*_=fixtures(tmp_path)
    (root/'reports/stage8/audit.json').unlink()
    d=perform(root)
    assert d['status']=='BLOCKED_CROSS_LAYER_CONTRACT'
    assert not d['checks']['stage8_report']['ok']


def test_reference_report_cannot_claim_native_flight(tmp_path):
    root,s8,*_=fixtures(tmp_path)
    s8['flight_authorized']=True;report(root,'stage8',s8)
    d=perform(root)
    assert d['status']=='BLOCKED_CROSS_LAYER_CONTRACT'


def test_retired_fmu_root_rejected(tmp_path):
    root,*_=fixtures(tmp_path)
    d=audit(root,stage8_report=root/'reports/stage8/audit.json',stage9_report=root/'reports/stage9/audit.json',
            stage10_report=root/'reports/stage10/audit.json',environ={'EXT_FMU_SIM_ROOT':'/obsolete'})
    assert not d['checks']['retired_fmu_not_active']['ok']


def test_false_source_fmu_frozen_binding_rejected(tmp_path):
    root,s8,s9,s10=fixtures(tmp_path)
    s10['source_fmu_hash_checks']['wingmpet_fmu']['verified_against_frozen_sarg_metadata']=False
    report(root,'stage10',s10)
    assert not perform(root)['checks']['stage10_source_wingmpet_fmu_frozen']['ok']


def test_lookup_false_completeness_rejected(tmp_path):
    root,s8,s9,s10=fixtures(tmp_path)
    s10['phase_coverage_exact']=False;report(root,'stage10',s10)
    assert not perform(root)['checks']['stage10_complete_index']['ok']


class FakeLayer5:
    def __init__(self,readiness, verifier=None):
        self.readiness=readiness
        self.verify_native_execution_evidence=verifier if verifier else None
    def online_control_readiness(self): return self.readiness


def layer6_run(layer5, package):
    config=SimpleNamespace(get=lambda key,default=None:default)
    adapter=Layer6OnlineAdapter(config,layer5)
    adapter._cancel_active=lambda *a,**kw:None
    adapter._module=lambda: (_ for _ in ()).throw(AssertionError('native module must not be loaded'))
    result={'gates':{'FINAL':True},'layer6_package':package,'decision':{'candidate_id':'selected'}}
    return adapter.invoke({'learning_mode':'ONLINE_JSBSIM','layer5_output':{'result':result},'transaction_id':'txn'})


def test_layer6_rejects_infrastructure_ok_without_native_verifier():
    l5=FakeLayer5({'ok':True,'nominal_control_ready':True})
    r=layer6_run(l5,{'transaction_id':'txn'})
    assert not r['ok'] and not r['actuation_executed']
    assert 'native_execution_model_not_verified' in r['error']


def test_layer6_rejects_spoofed_readiness_flags():
    l5=FakeLayer5({'ok':True,'native_pilot_controller_prediction_certified':True,'execution_mapping_certified':True})
    r=layer6_run(l5,{'transaction_id':'txn'})
    assert r['status']=='FAILED' and not r['actuation_executed']


def test_layer6_does_not_overwrite_mismatched_package_transaction():
    l5=FakeLayer5({'ok':True},lambda *a,**k:{'ok':True,'native_execution_model_matched':True})
    r=layer6_run(l5,{'transaction_id':'another'})
    assert r['status']=='FAILED' and 'transaction identity mismatch' in r['error']


def test_layer6_rejects_mismatched_candidate():
    l5=FakeLayer5({'ok':True},lambda *a,**k:{'ok':True,'native_execution_model_matched':True})
    r=layer6_run(l5,{'transaction_id':'txn','candidate_id':'other'})
    assert r['status']=='FAILED' and 'candidate identity mismatch' in r['error']


def test_bridge_rejects_nonfinite_deadline_without_submit():
    sent=[]
    client=SimpleNamespace(submit=lambda *a,**k:sent.append(1))
    b=Layer6Bridge(client,fresh=lambda before:True,confirmed=lambda *a:True)
    with pytest.raises(Layer6BridgeError,match='non-finite'):
        b.exchange({'transaction_id':'t','layer6_package':{'transaction_id':'t','tau_deadline':float('inf')}},
                   before={},timeout_s=1,segment_s=.1)
    assert not sent


def test_bridge_rejects_precommand_plant_regression():
    calls={'cancel':0}
    client=SimpleNamespace(submit=lambda *a,**k:SimpleNamespace(ok=True,transaction_id='t'),
                           gui_status=lambda **k:{'command_first_sent_plant_time_s':9.9,'plant_time_s':10.0},
                           cancel=lambda **k:calls.__setitem__('cancel',calls['cancel']+1))
    b=Layer6Bridge(client,fresh=lambda *a:True,confirmed=lambda *a:True)
    with pytest.raises(Layer6BridgeError,match='post-command'):
        b.exchange({'transaction_id':'t','layer6_package':{'transaction_id':'t','tau_deadline':time.time()+2}},
                   before={'plant_time_s':10.1},timeout_s=.003,segment_s=.05)
    assert calls['cancel']>0


def test_patch_absence_rejects_old_source(tmp_path):
    root,*_=fixtures(tmp_path)
    (root/'layer7_v20260901_0921/src/tddt_layer7/native_pair.py').unlink()
    d=perform(root)
    assert not d['checks']['repair_present_stage6_native_pair']['ok']


def test_report_outside_authoritative_reports_rejected(tmp_path):
    root,*_=fixtures(tmp_path)
    external=tmp_path.parent/('outside_'+tmp_path.name+'.json')
    external.write_text((root/'reports/stage8/audit.json').read_text())
    try:
        d=audit(root,stage8_report=external,
                stage9_report=root/'reports/stage9/audit.json',
                stage10_report=root/'reports/stage10/audit.json',environ={})
        assert not d['checks']['stage8_report']['ok']
    finally:
        external.unlink(missing_ok=True)


def test_real_tdoc_cli_audit_does_not_initialize_runtime(tmp_path,capsys,monkeypatch):
    from tddt_tdoc.cli import main
    root,*_=fixtures(tmp_path)
    for key,dirname in ROOTS.items():
        monkeypatch.setenv(key,str(root/dirname))
    monkeypatch.delenv('TDDT_PTLIB_ROOT',raising=False)
    monkeypatch.delenv('EXT_FMU_SIM_ROOT',raising=False)
    cfg=root/'TDOC_v20260911_2052/configs/tdoc.yaml'
    write(root,'TDOC_v20260911_2052/configs/tdoc.yaml',b'package: {version: STAGE11_TEST}\n')
    resultfile=root/'reports/stage11/result.json'
    exitcode=main(['--config',str(cfg),'audit-integration',
        '--stage8-report',str(root/'reports/stage8/audit.json'),
        '--stage9-report',str(root/'reports/stage9/audit.json'),
        '--stage10-report',str(root/'reports/stage10/audit.json'),
        '--output',str(resultfile)])
    assert exitcode==0
    assert json.loads(resultfile.read_text())['status']=='CROSS_LAYER_REFERENCE_BINDING_PASS_UNCERTIFIED'
    assert not (root/'TDOC_v20260911_2052/runtime').exists()


def test_real_tdoc_cli_native_guard_stays_closed(tmp_path,capsys,monkeypatch):
    from tddt_tdoc.cli import main
    root,*_=fixtures(tmp_path)
    for key,dirname in ROOTS.items():
        monkeypatch.setenv(key,str(root/dirname))
    monkeypatch.delenv('TDDT_PTLIB_ROOT',raising=False)
    monkeypatch.delenv('EXT_FMU_SIM_ROOT',raising=False)
    cfg=root/'TDOC_v20260911_2052/configs/tdoc.yaml'
    write(root,'TDOC_v20260911_2052/configs/tdoc.yaml',b'package: {version: STAGE11_TEST}\n')
    code=main(['--config',str(cfg),'audit-integration',
        '--stage8-report',str(root/'reports/stage8/audit.json'),
        '--stage9-report',str(root/'reports/stage9/audit.json'),
        '--stage10-report',str(root/'reports/stage10/audit.json'),
        '--require-native'])
    assert code==2
    assert not (root/'TDOC_v20260911_2052/runtime').exists()
