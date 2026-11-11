"""Temporary synthetic fixtures are NEVER mounted as accepted site datasets."""
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from sarg_builder.region import load_region, transform_point_between
from sarg_builder.selector import SARGSelector, SARGIndexError, SARGSelectionError, build_lookup_index
from sarg_builder.site_audit import audit_site_dataset, SARGSiteAuditError

ROOT=Path(__file__).resolve().parents[1]


def mk(tmp_path):
    data=tmp_path/'generated'/'SARG'; data.mkdir(parents=True)
    crp=tmp_path/'CRP';crp.mkdir()
    reg=ROOT/'region/region_v1.geojson'
    shutil.copyfile(reg,data/'region_v1.geojson')
    twin=tmp_path/'twins'
    for path in ['fmus/WingMPET.fmu','fmus/MESA_NAV.fmu','config/phoenix_drone_v3.yaml']:
        loc=twin/path;loc.parent.mkdir(parents=True,exist_ok=True);loc.write_bytes(b'DIAGNOSTIC_FIXTURE_'+path.encode())
    rows=[];patterns=[];n=1
    for regime in range(1,11):
        for crp_id in (4,7):
            for phase in range(1,7):
                status=0 if phase in (1,3) else (1 if phase==2 else (2 if phase==4 else (3 if phase==5 else 4)))
                wing_ok= status in (0,1,2)
                mesa_ok= status in (0,1,3)
                rows.append({'record_id':n,'regime_id':regime,'scenario_id':regime,
                             'phase_id':phase,'crp_id':crp_id,'record_status':status,
                             'wing_status':0 if wing_ok else 5,'mesa_status':0 if mesa_ok else 4,
                             'flight_ok':int(wing_ok),'nav_ok':int(mesa_ok),'phase_used':0 if status==4 else 1,
                             'record_confidence':.65 if status in (0,1) else -9999})
                patterns.append({'regime_id':regime,'phase_id':phase,'crp_cluster_id':crp_id,
                                 'status_label':'FMI_VALID' if status in (0,1) else 'FMI_CONDITIONAL',
                                 'sarg_id':f'EXAMPLE-{n}','mission_stage':str(phase),'pattern_confidence':.3})
                n+=1
    r=pd.DataFrame(rows); r.to_csv(data/'sarg_records.csv',index=False)
    build_lookup_index(r,[4,7]).to_csv(data/'sarg_lookup_index.csv',index=False)
    pd.DataFrame(patterns).to_csv(data/'sarg_patterns.csv',index=False)
    pd.DataFrame([{'cluster_id':4},{'cluster_id':7}]).to_csv(crp/'crp_cluster_catalog.csv',index=False)
    pd.DataFrame([{'group':'record','code':0,'name':'VALID'}]).to_csv(data/'status_codes.csv',index=False)
    region=load_region(reg)
    projected=[transform_point_between(t,'EPSG:4326','EPSG:32613') for t in (region.origin_wgs84,region.destination_wgs84)]
    metadata={'dataset':'SARG','version':2,'architecture_version':'SARG_Architecture_v2',
              'generation_mode':'FMI_ONLY_STRICT','origin':[*projected[0],1800.0],
              'destination':[*projected[1],1700.0], 'primary_key':['regime_id','phase_id','crp_id'],
              'record_count':120,'expected_record_count':120,'crp_cluster_count_in_region':2,
              'crp_cardinality_contract':'N_CRP = N_CRP,in-region',
              'region':{'region_id':region.region_id,'origin_wgs84':list(region.origin_wgs84),
                        'destination_wgs84':list(region.destination_wgs84)},
              'coordinate_reference':{'map_crs':'EPSG:32613','local_frame':'ENU','vertical_reference':'MSL'},
              'sha256':{k:hashlib.sha256((twin/v).read_bytes()).hexdigest() for k,v in {
                'wingmpet_fmu':'fmus/WingMPET.fmu','mesa_nav_fmu':'fmus/MESA_NAV.fmu',
                'drone_config':'config/phoenix_drone_v3.yaml'}.items()}}
    (data/'metadata.json').write_text(json.dumps(metadata))
    return data,crp/'crp_cluster_catalog.csv',reg,twin


def audit(q):
    d,crp,reg,tw=q
    return audit_site_dataset(d,region_file=reg,crp_catalog=crp,twins_root=tw,expected_records=120)


def test_site_fixture_read_only_success(tmp_path):
    q=mk(tmp_path)
    a=audit(q)
    assert a['record_count']==120 and a['crp_count']==2
    assert a['phase_coverage_exact'] and a['index_validated']
    assert a['source_fmu_hash_binding_checked']
    assert a['flight_authorized'] is False
    assert a['reference_is_current_navigation_state'] is False


def test_guard_status_admits_only_valid_low_conf(tmp_path):
    q=mk(tmp_path);s=SARGSelector(q[0]);assert s.select_reference(3,1,4)['reference_admissible']
    assert s.select_reference(3,2,4)['reference_admissible']
    for phase in (4,5,6):
        a=s.select_reference(3,phase,4)
        assert a['reference_admissible'] is False and a['reference_mode']=='diagnostic_only'
        assert a['flight_authorized'] is False
        assert a['no_implicit_cross_regime_fallback'] is True


def test_unknown_regime_cannot_implicitly_choose(tmp_path):
    q=mk(tmp_path);s=SARGSelector(q[0])
    with pytest.raises(SARGSelectionError,match='explicit_regime_required'):s.select_reference(None,1,4)
    assert len(s.select(None,1,4))==10
    with pytest.raises(KeyError):s.select_reference(123,1,4)


@pytest.mark.parametrize('changed,reason',[
    ('offset', 'row_offset'),('record_id','record_id'),('phase','phase_id'),
    ('fractional_offset','invalid_integer'),('crp_local','CRP local mapping'),
    ('duplicate_local','CRP local mapping')
])
def test_corrupt_index_fails_selector(tmp_path,changed,reason):
    d,*_=mk(tmp_path)
    p=d/'sarg_lookup_index.csv';x=pd.read_csv(p)
    if changed=='offset':x.loc[0,'row_offset']=x.loc[1,'row_offset'];x.loc[1,'row_offset']=0
    if changed=='record_id':x.loc[0,'record_id']=999
    if changed=='phase':x.loc[0,'phase_id']=6
    if changed=='fractional_offset':x['row_offset']=x['row_offset'].astype(float);x.loc[0,'row_offset']=.5
    if changed=='crp_local':x.loc[x.crp_id==7,'crp_local_index']=2
    if changed=='duplicate_local':x.loc[x.crp_id==7,'crp_local_index']=0
    x.to_csv(p,index=False)
    with pytest.raises(SARGIndexError): SARGSelector(d)


def test_incorrect_status_cannot_be_upgraded_by_confidence(tmp_path):
    d,*_=mk(tmp_path)
    p=d/'sarg_records.csv';x=pd.read_csv(p)
    chosen=x.index[x.record_status==4][0]
    x.loc[chosen,'record_confidence']=.99
    x.to_csv(p,index=False)
    assert not SARGSelector(d).select_reference(int(x.loc[chosen,'regime_id']),6,int(x.loc[chosen,'crp_id']))['reference_admissible']


def test_forged_valid_with_fmu_rejected_blocked(tmp_path):
    q=mk(tmp_path);p=q[0]/'sarg_records.csv';x=pd.read_csv(p)
    x.loc[x.index[0],'wing_status']=5
    x.to_csv(p,index=False)
    with pytest.raises(SARGSiteAuditError,match='contradicts_fmu_evidence'):audit(q)


def test_incomplete_phase_cartesian_set_blocked(tmp_path):
    q=mk(tmp_path);p=q[0]/'sarg_records.csv';x=pd.read_csv(p)
    x=x.iloc[:-1];x.to_csv(p,index=False)
    with pytest.raises((SARGIndexError,SARGSiteAuditError)):audit(q)


def test_fmu_identity_change_blocked(tmp_path):
    q=mk(tmp_path); p=q[3]/'fmus/WingMPET.fmu';p.write_bytes(b'changed')
    with pytest.raises(SARGSiteAuditError,match='native_source_hash_mismatch:wingmpet_fmu'):audit(q)


def test_region_hash_fail_closed(tmp_path):
    q=mk(tmp_path)
    with pytest.raises(SARGSiteAuditError,match='independent_sha256_mismatch'):
        audit_site_dataset(q[0],region_file=q[2],crp_catalog=q[1],expected_region_sha256='0'*64)


def test_region_identity_mutated_blocked(tmp_path):
    q=mk(tmp_path);p=q[0]/'metadata.json';d=json.loads(p.read_text())
    d['region']['origin_wgs84']=[0,0];p.write_text(json.dumps(d))
    with pytest.raises(SARGSiteAuditError,match='origin_wgs84_mismatch'):audit(q)


def test_crp_catalog_missing_cluster_blocked(tmp_path):
    q=mk(tmp_path);pd.DataFrame([{'cluster_id':4}]).to_csv(q[1],index=False)
    with pytest.raises(SARGSiteAuditError,match='unknown_crp_id'):audit(q)


def test_schema_matches_builder_emitted_labels():
    d=json.loads((ROOT/'schemas/sarg_pattern.schema.json').read_text())
    assert d['properties']['status_label']['enum']==['FMI_VALID','FMI_CONDITIONAL']


def test_cli_no_overwrite_and_no_rebuild(tmp_path):
    import subprocess,sys
    q=mk(tmp_path);p=ROOT/'scripts/validate_stage10_site_sarg.py';out=tmp_path/'report.json'
    args=[sys.executable,str(p),'--dataset-dir',str(q[0]),'--region',str(q[2]),
          '--crp-catalog',str(q[1]),'--twins-root',str(q[3]),'--expected-records','120','--output',str(out)]
    done=subprocess.run(args,text=True,capture_output=True)
    assert done.returncode==0,done.stdout+done.stderr
    assert json.loads(out.read_text())['status']=='SARG_REFERENCE_CONTRACT_DIAGNOSTIC_PASS_UNCERTIFIED'
    prev=out.read_bytes()
    assert subprocess.run(args,text=True,capture_output=True).returncode==2
    assert out.read_bytes()==prev


def test_cli_wrong_hash_reports_blocker(tmp_path):
    import subprocess,sys
    q=mk(tmp_path);p=ROOT/'scripts/validate_stage10_site_sarg.py';out=tmp_path/'wrong.json'
    args=[sys.executable,str(p),'--dataset-dir',str(q[0]),'--region',str(q[2]),
          '--crp-catalog',str(q[1]),'--expected-region-sha256','0'*64,'--output',str(out)]
    done=subprocess.run(args,text=True,capture_output=True)
    assert done.returncode==2
    report=json.loads(out.read_text());assert report['status']=='BLOCKED_SARG_REFERENCE_CONTRACT'
    assert report['flight_authorized'] is False


def test_reject_inconsistent_projected_goal(tmp_path):
    q=mk(tmp_path);p=q[0]/'metadata.json';d=json.loads(p.read_text())
    d['destination'][0]+=10
    p.write_text(json.dumps(d))
    with pytest.raises(SARGSiteAuditError,match='WGS84_projected_origin_destination_mismatch'):audit(q)
