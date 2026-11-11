"""Read-only SARG v2 provenance/lookup/phase audit for existing generated data.

Never rebuild SARG/CRP, never copy current observations into references, and never
produce an authorization credential. A verified hash binds to a caller-supplied
independent input, not to a hash computed from the same untrusted file.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from .region import load_region, transform_point_between, WGS84
from .selector import PRIMARY_KEY, SARGIndexError, SARGSelector

class SARGSiteAuditError(ValueError):
    pass


def _sha(p: Path) -> str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        for blk in iter(lambda:f.read(1024*1024),b''):
            h.update(blk)
    return h.hexdigest()


def _check(flag: bool, reason: str) -> None:
    if not flag: raise SARGSiteAuditError(reason)


def _ints(data:pd.Series, field:str) -> set[int]:
    n=pd.to_numeric(data,errors='coerce')
    _check(n.notna().all(),f'{field}:missing_or_invalid')
    vals=n.to_numpy(dtype=float)
    _check(bool(np.isfinite(vals).all()) and bool((vals==np.floor(vals)).all()), f'{field}:non_integral')
    return set(n.astype(int))


def audit_site_dataset(dataset_dir: str|Path, *, region_file: str|Path,
                       crp_catalog: str|Path, twins_root: str|Path|None = None,
                       expected_region_sha256: str|None = None,
                       expected_records:int|None = None) -> dict[str,Any]:
    d=Path(dataset_dir).resolve(strict=True)
    required=['sarg_records.csv','sarg_lookup_index.csv','sarg_patterns.csv',
              'metadata.json','region_v1.geojson','status_codes.csv']
    for name in required: _check((d/name).is_file(), f'dataset_file_missing:{name}')
    m=json.loads((d/'metadata.json').read_text(encoding='utf-8'))
    _check(m.get('dataset')=='SARG' and m.get('version')==2,'metadata:unsupported_version_or_dataset')
    _check(m.get('generation_mode')=='FMI_ONLY_STRICT','metadata:non_native_fmi_generation')
    _check(m.get('primary_key')==list(PRIMARY_KEY),'metadata:primary_key_mismatch')
    _check(m.get('architecture_version')=='SARG_Architecture_v2','metadata:architecture_mismatch')
    selector=SARGSelector(d)  # validates each index row against target and local CRP map
    df=selector.records
    index=selector.index
    pat=pd.read_csv(d/'sarg_patterns.csv')
    crp=pd.read_csv(crp_catalog)
    _check('cluster_id' in crp.columns,'crp:cluster_id_missing')
    crps=_ints(crp['cluster_id'],'crp.cluster_id')
    _check(len(crps)==len(crp),'crp:duplicate_cluster_id')
    actual_crps=_ints(df['crp_id'],'sarg.crp_id')
    _check(actual_crps<=crps,'sarg:unknown_crp_id')
    _check(len(actual_crps)==int(m['crp_cluster_count_in_region']),'metadata:regional_crp_count_mismatch')
    _check(m.get('crp_cardinality_contract')=='N_CRP = N_CRP,in-region','metadata:crp_region_policy_mismatch')
    _check(_ints(df['regime_id'],'regime_id')==set(range(1,11)),'sarg:regime_coverage_invalid')
    _check(_ints(df['phase_id'],'phase_id')==set(range(1,7)),'sarg:phase_coverage_invalid')
    _check(len(df)==60*len(actual_crps),'sarg:cardinality_mismatch')
    _check(len(pat)==len(df),'patterns:cardinality_mismatch')
    _check(int(m['record_count'])==len(df)==int(m['expected_record_count']), 'metadata:record_count_mismatch')
    if expected_records is not None: _check(int(expected_records)==len(df),'site:expected_count_mismatch')
    # Exact all-cross-product uniqueness (not just matching cardinality).
    _check(len(index)==len(df) and not df.duplicated(list(PRIMARY_KEY)).any(),'sarg:duplicate_key')
    if set(['regime_id','phase_id','crp_cluster_id'])-set(pat.columns):
        raise SARGSiteAuditError('patterns:missing_key_fields')
    _check(not pat.duplicated(['regime_id','phase_id','crp_cluster_id']).any(), 'patterns:duplicate_key')
    keycols=['regime_id','phase_id','crp_id']
    expected_keys=set((r,p,c) for r in range(1,11) for p in range(1,7) for c in actual_crps)
    actual_keys=set(map(tuple,df[keycols].astype(int).itertuples(index=False,name=None)))
    _check(actual_keys==expected_keys,'sarg:canonical_cross_product_incomplete')
    patt_keys=set((int(r),int(p),int(c)) for r,p,c in pat[['regime_id','phase_id','crp_cluster_id']].itertuples(index=False,name=None))
    _check(patt_keys==expected_keys,'patterns:canonical_key_mismatch')
    if 'status_label' in pat:
        _check(set(pat['status_label'].dropna().unique())<={'FMI_VALID','FMI_CONDITIONAL'},'patterns:status_label_unsupported')
    needed=['record_status','wing_status','mesa_status','phase_used','flight_ok','nav_ok','record_confidence']
    _check(not(set(needed)-set(df.columns)),'sarg:admission_fields_missing')
    codes=_ints(df['record_status'],'record_status')
    _check(codes <=set(range(11)),'sarg:record_status_unknown')
    for field, allowed in [('wing_status',{0,5}),('mesa_status',{0,4}),('phase_used',{0,1}),('flight_ok',{0,1}),('nav_ok',{0,1})]:
        _check(_ints(df[field],field)<=allowed,f'sarg:{field}_invalid')
    # Code 0 or 1 implies both FMU paths valid. Never promote NOT_TRIGGERED,
    # NAV_ONLY or FLIGHT_ONLY because of a high confidence scalar.
    good=df['record_status'].astype(int).isin([0,1])
    coherence=((df['wing_status']==0)&(df['mesa_status']==0)&
               (df['flight_ok']==1)&(df['nav_ok']==1)&(df['phase_used']==1))
    _check(bool((~good | coherence).all()),'sarg:record_status_valid_contradicts_fmu_evidence')
    _check(bool(((df['phase_used']!=0)|(df['record_status']==4)).all()),'sarg:nontriggered_phase_marked_valid')
    # Non-triggered phase cannot become valid via numeric sentinel/score.
    confidences=pd.to_numeric(df['record_confidence'], errors='coerce')
    good_conf=confidences[good].to_numpy(dtype=float)
    _check(bool(np.isfinite(good_conf).all()) and bool(((good_conf>=0)&(good_conf<=1)).all()),'sarg:valid_record_confidence_invalid')
    # SARG's WGS84 origin/destination must be grounded to BOTH supplied region
    # and the region actually copied at build time, using geometry and coordinates.
    declared=load_region(region_file)
    saved=load_region(d/'region_v1.geojson')
    rmeta=m.get('region') or {}
    for label, region in [('site',declared),('dataset',saved)]:
        _check(region.region_id==rmeta.get('region_id'),f'region:{label}_identity_mismatch')
        for key, position in [('origin_wgs84',region.origin_wgs84),('destination_wgs84',region.destination_wgs84)]:
            coords=rmeta.get(key)
            _check(isinstance(coords,list) and len(coords)==2 and
                   all(abs(float(a)-float(b))<=1e-8 for a,b in zip(coords,position)),
                   f'region:{label}_{key}_mismatch')
    # Geometry must match in canonical JSON, even if original file formatting differs.
    _check(saved.geometry_wgs84==declared.geometry_wgs84,'region:geometry_mismatch')
    map_crs=(m.get('coordinate_reference') or {}).get('map_crs')
    _check(isinstance(map_crs,str) and map_crs.startswith('EPSG:'), 'metadata:map_crs_missing')
    for key,lonlat in [('origin',declared.origin_wgs84),('destination',declared.destination_wgs84)]:
        src=m.get(key)
        _check(isinstance(src,list) and len(src)>=3,'metadata:'+key+':projected_coordinate_missing')
        x,y=transform_point_between(lonlat,WGS84,map_crs)
        _check(math.isfinite(float(src[0])) and math.isfinite(float(src[1])) and
               math.hypot(x-float(src[0]),y-float(src[1]))<=.1,
               'metadata:'+key+':WGS84_projected_origin_destination_mismatch')
    sha=_sha(Path(region_file))
    if expected_region_sha256 is not None:
        _check(sha==expected_region_sha256,'region:independent_sha256_mismatch')
    _check(m.get('coordinate_reference',{}).get('local_frame')=='ENU','metadata:local_frame_mismatch')
    _check(m.get('coordinate_reference',{}).get('vertical_reference')=='MSL','metadata:height_frame_mismatch')
    native_files={}
    if twins_root is not None:
        rt=Path(twins_root).resolve(strict=True)
        fields={'wingmpet_fmu':rt/'fmus/WingMPET.fmu',
                'mesa_nav_fmu':rt/'fmus/MESA_NAV.fmu',
                'drone_config':rt/'config/phoenix_drone_v3.yaml'}
        for key,file in fields.items():
            _check(file.is_file(), f'native_source_missing:{key}')
            got=_sha(file)
            _check(got==m.get('sha256',{}).get(key), f'native_source_hash_mismatch:{key}')
            native_files[key]={'sha256':got,'verified_against_frozen_sarg_metadata':True}
    # The audit checks prior provenance, NOT whether source-produced model trajectories
    # are measured current navigation/execution states.
    samples=[]
    for code in (0,1,2,3,4,5,6,7,8,9,10):
        one=df[df['record_status']==code].head(1)
        if not one.empty:
            r=one.iloc[0]
            a=selector.select_reference(int(r['regime_id']),int(r['phase_id']),int(r['crp_id']))
            _check(a['reference_admissible'] == (code in (0,1)),f'admission:status_{code}_wrong')
            samples.append({'key':a['canonical_key'],'status':code,'reference_admissible':a['reference_admissible'], 'blockers':a['blockers']})
    return {
      'test':'STAGE10_SARG_SITE_READ_ONLY_AUDIT','schema':'TDDT-SARG-ReferenceBoundary-v1',
      'status':'SARG_REFERENCE_CONTRACT_DIAGNOSTIC_PASS_UNCERTIFIED',
      'dataset_dir':str(d),'record_count':len(df),'pattern_count':len(pat),
      'crp_count':len(actual_crps),'crp_catalog_count':len(crps),
      'phase_count':6,'regime_count':10,'index_validated':True,
      'phase_coverage_exact':True,'region_file_sha256':sha,
      'dataset_files_sha256':{x:_sha(d/x) for x in required},
      'source_fmu_hash_binding_checked':twins_root is not None,
      'source_fmu_hash_checks':native_files,'status_samples':samples,
      'data_mutated':False,'sarg_rebuilt':False,'reference_is_current_navigation_state':False,
      'native_controller_certified':False,'flight_authorized':False,
      'scope':'SARG_DATASET_REFERENCE_AND_PROVENANCE_ONLY_NOT_EXECUTION_AUTHORITY',
    }
