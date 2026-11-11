#!/usr/bin/env python3
from __future__ import annotations
import csv, json, math, tempfile, copy, sys
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from tddt_layer2 import ClimateDomainTwin, TEPVersionManager, UOUT

NEW=['wind_u_3dw','wind_v_3dw','diagnostic_vertical_wind','wind_speed_3d','air_density']
EXPECTED=['temperature_local','relative_humidity_local','wind_speed_terrain','wind_direction_terrain_deg','surface_pressure_pa','shortwave_radiation']+NEW

def load_rows():
    with (ROOT/'resources/tddt_layer2/tep/tep.csv').open(newline='',encoding='utf-8') as f:
        return list(csv.DictReader(f))

def main():
    rows=load_rows(); manifest=json.loads((ROOT/'resources/tddt_layer2/tep/manifest.json').read_text())
    assert UOUT==EXPECTED, (UOUT,EXPECTED)
    assert manifest['covariance_variable_order']==EXPECTED
    assert manifest['held_out_validation']['pass'] is True
    min_eig=1e9
    for r in rows:
        std=json.loads(r['uncertainty_std_json']); var=json.loads(r['variance_json']); cov=json.loads(r['covariance_json'])
        assert cov['variables']==EXPECTED
        for k in EXPECTED:
            assert k in std and math.isfinite(float(std[k])) and float(std[k])>=0
            assert k in var and math.isfinite(float(var[k])) and float(var[k])>=0
            assert abs(float(var[k])-float(std[k])**2) <= max(1e-10,1e-8*max(1.0,float(var[k])))
        a=np.asarray(cov['matrix'],dtype=float)
        assert a.shape==(len(EXPECTED),len(EXPECTED))
        assert np.all(np.isfinite(a))
        assert np.max(np.abs(a-a.T))<1e-8
        e=float(np.linalg.eigvalsh((a+a.T)/2).min()); min_eig=min(min_eig,e)
        assert e>=-1e-7, e
        rho=json.loads(r['rho_uncertainty_check_json'])
        assert rho['valid'] and rho['propagated_variance']>=0 and rho['ensemble_variance']>=0
        assert rho['relative_difference']<0.05
    assert all(any(float(json.loads(r['uncertainty_std_json'])[k])>0 for r in rows) for k in NEW)

    # Runtime packet, novelty inflation and wind_3dw_valid gate.
    scenario=json.loads((ROOT/'tests/micromet_all_formulas_scenario_air_density.json').read_text())
    result={'ok':True,'request':{'time':scenario['configuration']['request']['time'],'altitude_agl_m':scenario['configuration']['request']['altitude_agl_m'],'terrain_source':'scenario-explicit'},'fmu_outputs':scenario['outputs'],'source_count':5}
    twin=ClimateDomainTwin()
    p0=twin.packet_from_estimate(result)
    for k in NEW:
        assert k in p0['uncertainty']['std'] and k not in p0['uncertainty']['unquantified_outputs']
    assert all(p0['uncertainty']['availability'][k] for k in NEW)
    far=copy.deepcopy(result); far['fmu_outputs']['temperature_local']=1e6
    p1=twin.packet_from_estimate(far)
    assert p1['uncertainty']['novelty_distance']>p0['uncertainty']['novelty_distance']
    assert p1['uncertainty']['std']['air_density']>=p0['uncertainty']['std']['air_density']
    bad=copy.deepcopy(result); bad['fmu_outputs']['wind_3dw_valid']=0.0
    pb=twin.packet_from_estimate(bad)
    for k in NEW[:4]: assert pb['uncertainty']['availability'][k] is False
    assert pb['uncertainty']['availability']['air_density'] is True

    # Field posterior must accept residuals for all new outputs and update full 11x11 covariance.
    cell=rows[0]['tep_cell_id']
    with tempfile.TemporaryDirectory() as td:
        ep=Path(td)/'evidence.jsonl'; out=Path(td)/'post'
        with ep.open('w',encoding='utf-8') as f:
            for i,sgn in enumerate((1.0,-1.0)):
                residual={k:sgn*(i+1)*1e-3*(j+1) for j,k in enumerate(EXPECTED)}
                ev={'evidence_type':'field_validated','tep_cell_id':cell,'pass':True,'timestamp':f'2026-08-21T10:3{i}:00-04:00','source':'test','validation_status':'accepted','validation_id':f'VAL-{i}','provenance':{'test':True},'residual':residual}
                f.write(json.dumps(ev)+'\n')
        meta=TEPVersionManager().create_field_posterior(ep,out,version='test-field-v1',min_evidence=2)
        assert meta['posterior_uncertainty_updated'] is True
        rr=list(csv.DictReader((out/'tep.csv').open()))
        ur=next(x for x in rr if x['tep_cell_id']==cell)
        c=json.loads(ur['covariance_posterior_json'])
        assert c['variables']==EXPECTED and len(c['matrix'])==len(EXPECTED)
        assert all(k in json.loads(ur['uncertainty_std_posterior_json']) for k in NEW)

    print(json.dumps({'ok':True,'cells':len(rows),'covariance_order':EXPECTED,'min_covariance_eigenvalue':min_eig,'rho_relative_difference_max':manifest['rho_uncertainty_consistency']['cell_relative_difference_max'],'heldout_error':manifest['held_out_validation']['E_TEP_mean_profile_distance']},indent=2))

if __name__=='__main__': main()
