#!/usr/bin/env python3
from __future__ import annotations
import csv, gzip, json, math, random, statistics, hashlib, zipfile
from pathlib import Path
from datetime import datetime, timezone, datetime as DT
from copy import deepcopy

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'climate_dataset'/'micromet_grid_24h.csv'
FORECAST=ROOT/'climate_dataset'/'openmeteo_24h.csv'
FMU=ROOT/'fmu'/'MicroMetTwin.fmu'
OUT=ROOT/'resources'/'tddt_layer2'/'tep'

import sys
sys.path.insert(0,str(ROOT/'python'))
from climate_io import read_forecast, sources_at_time, build_fmi_inputs
from fmu_client import MicroMetFMU

FEATURES=['temperature_local','relative_humidity_local','wind_speed_terrain','surface_pressure_pa','altitude_agl_m']
UOUT=['temperature_local', 'relative_humidity_local', 'wind_speed_terrain', 'wind_direction_terrain_deg', 'surface_pressure_pa', 'shortwave_radiation', 'wind_u_3dw', 'wind_v_3dw', 'diagnostic_vertical_wind', 'wind_speed_3d', 'air_density']
QMIN=0.80
CONV_TOL=0.30
REPEAT_TOL=1e-10
META_TOL=1e-7
ALPHA=BETA=1.0
BUILD_BASES=3
HOLDOUT_BASES=1
ENSEMBLE_N=8
SEED=20260812
SEMANTIC_SCHEMA=json.loads((ROOT/'docs'/'fmi_semantic_schema.json').read_text(encoding='utf-8')).get('variables',{})
METADATA_SCORE=sum(1 for k in UOUT if k in SEMANTIC_SCHEMA and SEMANTIC_SCHEMA[k].get('unit') and SEMANTIC_SCHEMA[k].get('frame') and SEMANTIC_SCHEMA[k].get('source'))/len(UOUT)


def f(x,default=float('nan')):
    try:return float(x)
    except:return default

def finite(x): return math.isfinite(f(x))

def band(v,edges):
    for i,e in enumerate(edges):
        if v<=e:return i
    return len(edges)

def bands_key(r):
    return (band(f(r['altitude_agl_m']),[300,700]), min(2,int(f(r.get('time_index',0),0))//8), band(f(r.get('slope_deg',0),0),[10]))

def quantile(a,p):
    a=sorted(a)
    if not a:return 0.0
    q=(len(a)-1)*p; lo=int(math.floor(q)); hi=int(math.ceil(q))
    return a[lo] if lo==hi else a[lo]*(hi-q)+a[hi]*(q-lo)

def cov_matrix(rows,names):
    vals=[[f(r.get(k)) for k in names] for r in rows]
    vals=[v for v in vals if all(math.isfinite(x) for x in v)]
    if len(vals)<2:return [[0.0]*len(names) for _ in names]
    mu=[statistics.fmean(v[j] for v in vals) for j in range(len(names))]
    return [[sum((v[i]-mu[i])*(v[j]-mu[j]) for v in vals)/(len(vals)-1) for j in range(len(names))] for i in range(len(names))]

def perturb_sources(base_sources,rng):
    out=[]
    for s in base_sources:
        z=dict(s)
        # input uncertainty/perturbation model: conservative local synthetic perturbations.
        z['temperature_2m_C']=f(s['temperature_2m_C'])+rng.gauss(0,0.35)
        z['relative_humidity_2m_pct']=max(0,min(100,f(s['relative_humidity_2m_pct'])+rng.gauss(0,1.5)))
        z['wind_speed_10m_ms']=max(0,f(s['wind_speed_10m_ms'])*(1+rng.gauss(0,0.06)))
        z['wind_direction_10m_deg']=(f(s['wind_direction_10m_deg'])+rng.gauss(0,4.0))%360
        z['surface_pressure_hPa']=max(1,f(s['surface_pressure_hPa'])+rng.gauss(0,0.7))
        z['precipitation_mm']=max(0,f(s['precipitation_mm'])+rng.gauss(0,0.02))
        if 'shortwave_radiation_Wm2' in s:
            z['shortwave_radiation_Wm2']=max(0,f(s['shortwave_radiation_Wm2'])*(1+rng.gauss(0,0.03)))
        out.append(z)
    return out

def _rel(a,b,scale=1.0):
    return abs(float(a)-float(b))/max(float(scale),abs(float(a)),abs(float(b)),1e-12)

def _metamorphic_score(o):
    tests=[]
    try:
        tests.append(_rel(o['wind_speed_terrain'], math.hypot(o['wind_u_terrain'],o['wind_v_terrain']),1.0) <= META_TOL)
        tests.append(_rel(o['wind_speed_3d'], math.sqrt(o['wind_u_3dw']**2+o['wind_v_3dw']**2+o['diagnostic_vertical_wind']**2),1.0) <= META_TOL)
        div=o['du_dx']+o['dv_dy']+o['dw_dz']
        tests.append(_rel(o['continuity_residual'],div,1e-3) <= 1e-5)
    except Exception:
        return 0.0
    return sum(bool(x) for x in tests)/len(tests)

def _repeatability_score(a,b):
    names=UOUT
    scores=[]
    for k in names:
        try:scores.append(_rel(a[k],b[k],1.0)<=REPEAT_TOL)
        except:scores.append(False)
    return sum(scores)/len(scores)

def _convergence_score(a,b):
    names=['diagnostic_vertical_wind','du_dx','du_dy','du_dz','dv_dx','dv_dy','dv_dz','dw_dx','dw_dy','dw_dz','continuity_residual']
    vals=[]
    for k in names:
        try:
            if math.isfinite(float(a[k])) and math.isfinite(float(b[k])): vals.append(_rel(a[k],b[k],1e-3))
        except: pass
    if not vals:return 0.0
    return max(0.0,1.0-statistics.fmean(min(1.0,v/CONV_TOL) for v in vals))

def run_ensemble(fmu, base, forecast_rows, rng, n=ENSEMBLE_N):
    requested=DT.fromisoformat(base['time'])
    src0=sources_at_time(forecast_rows,requested)
    runs=[]
    for m in range(n):
        src=perturb_sources(src0,rng)
        slope=f(base['slope_deg'])+rng.gauss(0,0.15)
        aspect=(f(base['aspect_deg'])+rng.gauss(0,0.5))%360
        curvature=f(base['curvature_scaled'])+rng.gauss(0,0.0005)
        altitude=f(base['altitude_msl_m'])+rng.gauss(0,0.25)
        inp=build_fmi_inputs(src, latitude=f(base['latitude']), longitude=f(base['longitude']), elevation_m=altitude, requested_time=requested,slope_deg=slope,aspect_deg=aspect,curvature=curvature,barnes_filter_km=20.0,lapse_rate_c_per_m=0.0065+rng.gauss(0,0.00008),vapor_coefficient_per_m=max(1e-6,0.00040+rng.gauss(0,0.00001)),gamma_s=max(0,min(1,0.58+rng.gauss(0,0.015))),gamma_c=max(0,min(1,0.42+rng.gauss(0,0.015))),precipitation_chi_km=max(0.01,0.25+rng.gauss(0,0.01)))
        inp.update({'wind_3dw_enabled':1.0,'altitude_agl_m':f(base['altitude_agl_m']),'fd_dx_m':100.0,'fd_dy_m':100.0,'fd_dz_m':10.0,'wind_profile_power':0.143})
        o=fmu.step(inp); o_repeat=fmu.step(inp)
        refined=dict(inp); refined.update({'fd_dx_m':50.0,'fd_dy_m':50.0,'fd_dz_m':5.0}); o_refined=fmu.step(refined)
        phys=(0<=o['relative_humidity_local']<=100 and o['wind_speed_terrain']>=0 and o['surface_pressure_pa']>0 and o.get('air_density',float('nan'))>0 and o['formula_valid']>=0.5)
        finite_all=all(math.isfinite(o[k]) for k in UOUT)
        numerical=(o.get('formula_valid',0)>=0.5 and o.get('wind_3dw_valid',0)>=0.5 and all(math.isfinite(float(o.get(k,float('nan')))) for k in ['continuity_residual','wind_u_3dw','wind_v_3dw','diagnostic_vertical_wind','wind_speed_3d','air_density']))
        qc=1.0 if finite_all else 0.0; qp=1.0 if phys else 0.0; qn=1.0 if numerical else 0.0
        qm=_metamorphic_score(o); qr=_repeatability_score(o,o_repeat); qv=_convergence_score(o,o_refined) if numerical else 0.0
        qs=1.0 if (0<=f(src[0].get('relative_humidity_2m_pct',50))<=100 and f(src[0].get('surface_pressure_hPa',1000))>0) else 0.0; qmd=METADATA_SCORE
        q=0.15*qv+0.15*qc+0.20*qp+0.15*qn+0.15*qm+0.10*qr+0.05*qs+0.05*qmd
        valid=1.0 if phys and finite_all and numerical else 0.0
        residual=abs(f(o.get('continuity_residual',0.0),0.0)) if o.get('wind_3dw_valid',0)>=0.5 else float('inf')
        passed=(valid>=1 and q>=QMIN and residual<=0.05 and qm>=1.0 and qr>=1.0 and qv>=0.5)
        rec={k:o[k] for k in UOUT}
        rec.update({'quality':q,'validity':valid,'pass':int(passed),'physical_residual':residual,'run_index':m,'q_convergence':qv,'q_complete':qc,'q_physical':qp,'q_numerical':qn,'q_metamorphic':qm,'q_repeatability':qr,'q_support':qs,'q_metadata':qmd,
                    'perturbation_snapshot':{'temperature_2m_C':f(src[0].get('temperature_2m_C')),'relative_humidity_2m_pct':f(src[0].get('relative_humidity_2m_pct')),'wind_speed_10m_ms':f(src[0].get('wind_speed_10m_ms')),'wind_direction_10m_deg':f(src[0].get('wind_direction_10m_deg')),'surface_pressure_hPa':f(src[0].get('surface_pressure_hPa')),'slope_deg':slope,'aspect_deg':aspect,'curvature_scaled':curvature,'altitude_msl_m':altitude,'lapse_rate_c_per_m':inp.get('lapse_rate_c_per_m'),'vapor_coefficient_per_m':inp.get('vapor_coefficient_per_m'),'gamma_s':inp.get('gamma_s'),'gamma_c':inp.get('gamma_c'),'precipitation_chi_km':inp.get('precipitation_chi_km')}})
        runs.append(rec)
    return runs

def profile(runs):
    std={}; var={}; means={}
    for k in UOUT:
        a=[f(x[k]) for x in runs if finite(x[k])]
        s=statistics.stdev(a) if len(a)>1 else 0.0; std[k]=s; var[k]=s*s; means[k]=statistics.fmean(a) if a else float('nan')
    q=statistics.fmean(x['quality'] for x in runs)
    v=statistics.fmean(x['validity'] for x in runs)
    s=sum(x['pass'] for x in runs); fail=len(runs)-s
    rel=(s+ALPHA)/(s+fail+ALPHA+BETA)
    return {'std':std,'variance':var,'means':means,'covariance':cov_matrix(runs,UOUT),'quality':q,'validity':v,'reliability':rel,'success':s,'failure':fail,'n':len(runs)}

def p_dist(a,b):
    # Normalized scalar profile distance for held-out validation.
    eps=1e-12
    ua=math.sqrt(sum(v*v for v in a['std'].values())); ub=math.sqrt(sum(v*v for v in b['std'].values()))
    du=abs(ua-ub)/max(eps,ua,ub,1.0)
    return (du+abs(a['quality']-b['quality'])+abs(a['validity']-b['validity'])+abs(a['reliability']-b['reliability']))/4.0

def rho_jacobian(temperature_c, relative_humidity_pct, pressure_pa):
    """Analytical Jacobian d(rho)/d[T_C,RH_pct,p_Pa] for operational Eq. (43).

    This check is valid away from the RH clamp boundaries and the dry-pressure clamp,
    which is the case for the packaged DOE support.
    """
    a=611.21; b=17.502; c=240.97; rd=287.05; rv=461.495
    tc=float(temperature_c); rh=float(relative_humidity_pct); p=float(pressure_pa)
    tk=tc+273.15
    es=a*math.exp(b*tc/(c+tc))
    des_dtc=es*b*c/((c+tc)*(c+tc))
    h=max(0.0,min(100.0,rh))/100.0
    aa=(1.0/rv-1.0/rd)
    dr_dp=1.0/(rd*tk)
    dr_drh=(es/100.0)*aa/tk
    dr_dtc=-p/(rd*tk*tk) + h*aa*(des_dtc/tk-es/(tk*tk))
    return [dr_dtc,dr_drh,dr_dp]

def rho_delta_variance(profile):
    """Jacobian covariance propagation P_rho = J P_[T,RH,p] J^T."""
    names=UOUT
    cov=profile['covariance']
    idx={k:i for i,k in enumerate(names)}
    mu=profile.get('means',{})
    required=['temperature_local','relative_humidity_local','surface_pressure_pa']
    if not all(k in idx and k in mu for k in required): return {'valid':False,'reason':'missing_state'}
    sub=[[cov[idx[a]][idx[b]] for b in required] for a in required]
    j=rho_jacobian(mu['temperature_local'],mu['relative_humidity_local'],mu['surface_pressure_pa'])
    propagated=sum(j[i]*sub[i][k]*j[k] for i in range(3) for k in range(3))
    ensemble=float(profile['variance'].get('air_density',float('nan')))
    rel=abs(propagated-ensemble)/max(abs(propagated),abs(ensemble),1e-18) if math.isfinite(ensemble) else float('inf')
    return {'valid':math.isfinite(propagated) and propagated>=0 and math.isfinite(ensemble) and ensemble>=0,
            'jacobian_order':required,'jacobian':j,'propagated_variance':max(0.0,propagated),
            'ensemble_variance':ensemble,'relative_difference':rel}

def fmu_binary_sha256(p):
    with zipfile.ZipFile(p) as zf:
        b=zf.read('binaries/linux64/MicroMet.so')
    return hashlib.sha256(b).hexdigest()

def sha256(p):
    h=hashlib.sha256();
    with open(p,'rb') as fh:
        for b in iter(lambda:fh.read(1<<20),b''):h.update(b)
    return h.hexdigest()

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    rows=list(csv.DictReader(DATA.open(newline='',encoding='utf-8')))
    forecast=read_forecast(FORECAST)
    groups={}
    for r in rows:groups.setdefault(bands_key(r),[]).append(r)
    rng=random.Random(SEED)
    cells=[]; raw=[]; held=[]; held_errors=[]
    with MicroMetFMU(FMU) as fmu:
        for idx,(key,gr) in enumerate(sorted(groups.items())):
            ordered=sorted(gr,key=lambda r:(r['time'],r['grid_cell_id'],f(r['altitude_agl_m'])))
            # deterministic spread across cell support
            choose=lambda count,offset:[ordered[min(len(ordered)-1, round((j+offset)*(len(ordered)-1)/(count+HOLDOUT_BASES+1)))] for j in range(count)]
            build_bases=choose(BUILD_BASES,1)
            hold_bases=choose(HOLDOUT_BASES,BUILD_BASES+1)
            build_runs=[]
            for bi,b in enumerate(build_bases):
                rr=run_ensemble(fmu,b,forecast,rng); build_runs.extend(rr)
                for x in rr: raw.append({'phase':'build','tep_cell_id':f'MM_{idx:03d}','base_id':bi,'operating_point':{k:b[k] for k in ['grid_cell_id','time','latitude','longitude','altitude_agl_m','slope_deg','aspect_deg','curvature_scaled']},**x})
            pr=profile(build_runs)
            rho_check=rho_delta_variance(pr)
            desc_names=['altitude_agl_m','time_index','slope_deg','aspect_deg','curvature_scaled']
            bounds={k:{'min':min(f(r.get(k)) for r in gr),'max':max(f(r.get(k)) for r in gr)} for k in desc_names}
            centroid={k:statistics.fmean(f(r.get(k)) for r in gr) for k in desc_names}
            centroid.update({k:statistics.fmean(f(r.get(k)) for r in gr) for k in FEATURES if k not in centroid})
            # quality components are explicit and separate
            qconv=statistics.fmean(x['q_convergence'] for x in build_runs)
            qfinite=statistics.fmean(x['q_complete'] for x in build_runs)
            qphys=statistics.fmean(x['q_physical'] for x in build_runs)
            qnum=statistics.fmean(x['q_numerical'] for x in build_runs)
            qmeta=statistics.fmean(x['q_metamorphic'] for x in build_runs)
            qrepeat=statistics.fmean(x['q_repeatability'] for x in build_runs)
            qsupport=statistics.fmean(x['q_support'] for x in build_runs)
            qmd=statistics.fmean(x['q_metadata'] for x in build_runs)
            q=0.15*qconv+0.15*qfinite+0.20*qphys+0.15*qnum+0.15*qmeta+0.10*qrepeat+0.05*qsupport+0.05*qmd
            success=pr['success']; failure=pr['failure']; rel=(success+ALPHA)/(success+failure+ALPHA+BETA)
            provenance={'evidence_type':'simulation_prior','sampling_method':'deterministic_stratified + local Gaussian perturbation ensemble','seed':SEED,'fmu_sha256':fmu_binary_sha256(FMU),'fmu_sha256_scope':'binaries/linux64/MicroMet.so','dataset':str(DATA.relative_to(ROOT))}
            row={
                'twin_id':'MicroMetTwin','cell_id':f'MM_{idx:03d}','tep_cell_id':f'MM_{idx:03d}',
                'bounds_json':json.dumps(bounds,separators=(',',':')),'centroid_json':json.dumps(centroid,separators=(',',':')),
                'uncertainty_std_json':json.dumps(pr['std'],separators=(',',':')),'variance_json':json.dumps(pr['variance'],separators=(',',':')),
                'covariance_json':json.dumps({'variables':UOUT,'matrix':pr['covariance']},separators=(',',':')),
                'rho_uncertainty_check_json':json.dumps(rho_check,separators=(',',':')),
                'quality_prior':f'{q:.12g}','validity_prior':f'{pr["validity"]:.12g}','reliability_prior':f'{rel:.12g}',
                'reliability_posterior':'','uncertainty_std_posterior_json':'','variance_posterior_json':'','covariance_posterior_json':'',
                'success_count':success,'failure_count':failure,'field_evidence_count':0,'evidence_count':len(build_runs),
                'q_convergence':f'{qconv:.12g}','q_finite':f'{qfinite:.12g}','q_physical':f'{qphys:.12g}','q_numerical':f'{qnum:.12g}','q_metamorphic':f'{qmeta:.12g}','q_support':f'{qsupport:.12g}','q_repeatability':f'{qrepeat:.12g}','q_metadata':f'{qmd:.12g}',
                'generation_method':'stratified DOE + local perturbation ensemble + executed convergence/repeatability/physical/numerical/metamorphic tests',
                'provenance_json':json.dumps(provenance,separators=(',',':')),
                'source':'simulation_prior:local_perturbation_FMU_ensemble'
            }
            cells.append(row)
            for hi,b in enumerate(hold_bases):
                rr=run_ensemble(fmu,b,forecast,rng); syn=profile(rr); err=p_dist(pr,syn); held_errors.append(err)
                held.append({'tep_cell_id':f'MM_{idx:03d}','base_id':hi,'profile_distance':err,'lookup_profile':{'std':pr['std'],'quality':q,'validity':pr['validity'],'reliability':rel},'synthetic_profile':syn})
                for x in rr: raw.append({'phase':'heldout','tep_cell_id':f'MM_{idx:03d}','base_id':hi,'operating_point':{k:b[k] for k in ['grid_cell_id','time','latitude','longitude','altitude_agl_m','slope_deg','aspect_deg','curvature_scaled']},**x})
    fields=list(cells[0])
    for name in ('MicroMet_tep.csv','tep.csv'):
        with (OUT/name).open('w',newline='',encoding='utf-8') as fh:
            w=csv.DictWriter(fh,fieldnames=fields);w.writeheader();w.writerows(cells)
    with gzip.open(OUT/'raw_trials.jsonl.gz','wt',encoding='utf-8') as fh:
        for r in raw:fh.write(json.dumps(r,separators=(',',':'),ensure_ascii=False)+'\n')
    # TEP metrics from formalism: coverage, resolution, Nmin, lookup error, out-rate.
    # Dimensionless resolution: Euclidean cell diameter after normalizing each descriptor
    # by the global DOE support. This avoids mixing metres, degrees and time indices.
    desc_names=['altitude_agl_m','time_index','slope_deg','aspect_deg','curvature_scaled']
    global_span={k:max(1e-12,max(f(r.get(k)) for r in rows)-min(f(r.get(k)) for r in rows)) for k in desc_names}
    diam=[]
    for c in cells:
        b=json.loads(c['bounds_json']); diam.append(math.sqrt(sum(((v['max']-v['min'])/global_span[k])**2 for k,v in b.items())))
    heldout={
        'validation_type':'held-out synthetic local perturbation ensembles','heldout_samples':len(held),
        'coverage':1.0,'resolution_mean_cell_diameter':statistics.fmean(diam),'resolution_definition':'mean Euclidean cell diameter in globally normalized descriptor space','min_evidence_per_cell':min(int(c['evidence_count']) for c in cells),
        'E_TEP_mean_profile_distance':statistics.fmean(held_errors),'E_TEP_max_profile_distance':max(held_errors),'out_of_tep_rate':0.0,
        'distance_definition':'mean of normalized uncertainty-norm error and absolute Q/V/R errors','acceptance_threshold':0.30,'pass':statistics.fmean(held_errors)<0.30,
        'samples':held
    }
    (OUT/'heldout_validation.json').write_text(json.dumps(heldout,indent=2,ensure_ascii=False),encoding='utf-8')
    version='2026-08-21-climate-sim-prior-v4'
    (OUT/'VERSION').write_text(version+'\n',encoding='utf-8')
    ranges={}
    for x in FEATURES+['slope_deg','aspect_deg','curvature_scaled']:
        a=[f(r.get(x,'')) for r in rows if finite(r.get(x,''))]
        if a:ranges[x]={'min':min(a),'max':max(a)}
    manifest={
        'tep_name':'MicroMetTwin_TEP','schema_version':'2.2','tep_version':version,'created_at':datetime.now(timezone.utc).isoformat(),
        'twin_id':'MicroMetTwin','twin_model_version':'MicroMetTwin.fmu','formalism_version':'micromet_formalism_v2','simulation_version':'MicroMetTwin.fmu',
        'generation_method':'Twin Contract -> stratified DOE -> local perturbation ensembles -> physical/numerical/metamorphic checks -> U/Q/V/R -> epistemic cells -> held-out validation',
        'sampling_method':'deterministic stratified operating-domain DOE with disjoint build/held-out base points',
        'perturbation_model':{'temperature_C_sigma':0.35,'RH_pct_sigma':1.5,'wind_speed_relative_sigma':0.06,'wind_direction_deg_sigma':4.0,'pressure_hPa_sigma':0.7,'terrain_slope_deg_sigma':0.15,'terrain_aspect_deg_sigma':0.5,'terrain_curvature_sigma':0.0005,'altitude_m_sigma':0.25,'parameter_perturbations':['lapse_rate','vapor_coefficient','gamma_s','gamma_c','precipitation_chi_km']},
        'parameter_ranges':{'source':'twin_contract.json + local perturbation model'},
        'coverage':{'fraction':1.0,'descriptor':['altitude_agl_m','time_index','slope_deg','aspect_deg','curvature_scaled'],'validity_ranges':ranges},'validity_ranges':ranges,
        'cell_count':len(cells),'min_evidence_per_cell':min(int(c['evidence_count']) for c in cells),'resolution':heldout['resolution_mean_cell_diameter'],
        'evidence_type':'simulation_prior','field_evidence_present':False,'maturity':'TEP_sim',
        'uncertainty_representation':'per-cell full covariance + std + diagonal variance from local perturbed FMU ensembles',
        'covariance_variable_order':UOUT,
        'quantified_state_outputs':UOUT,
        'rho_uncertainty_consistency':{'method':'Jacobian propagation P_rho = J P_[T,RH,p] J^T vs ensemble variance','cell_relative_difference_max':max(json.loads(c['rho_uncertainty_check_json'])['relative_difference'] for c in cells),'cell_relative_difference_mean':statistics.fmean(json.loads(c['rho_uncertainty_check_json'])['relative_difference'] for c in cells)},
        'quality_definition':'measured weighted convergence, completeness, physical, numerical, metamorphic, repeatability, support and metadata checks',
        'validity_definition':'applicability/physical envelope and finite numerical outputs',
        'reliability_definition':'simulation pass/fail with Beta(1,1) regularization; not operational reliability',
        'held_out_validation':{'file':'heldout_validation.json','pass':heldout['pass'],'E_TEP_mean_profile_distance':heldout['E_TEP_mean_profile_distance'],'out_of_tep_rate':heldout['out_of_tep_rate']},
        'limitations':['simulation_prior only','no field-calibrated uncertainty','longwave and precipitation state uncertainty are not quantified in this TEP','velocity-gradient and material-derivative aggregate uncertainty are not quantified in this TEP','3DW remains diagnostic and is not CFD/turbulence/gust prediction','held-out validation is synthetic, not field validation'],
        'provenance':{'dataset':str(DATA.relative_to(ROOT)),'forecast':str(FORECAST.relative_to(ROOT)),'fmu_sha256':fmu_binary_sha256(FMU),'fmu_sha256_scope':'binaries/linux64/MicroMet.so','seed':SEED,'source_documents':['TDDT_Dynamic_NTwin_TEP_details_FA_v2.md','micromet_formalism_v2.md']},
        'runtime_lookup':{'method':'nearest epistemic operating cell','novelty_modifier':True,'freshness_modifier_supported':True,'freshness_lambda_per_s':0.0,'novelty_radius_standardized':3.0},
        'posterior_policy':{'layer':7,'field_evidence_only':True,'slow':True,'versioned':True,'provenance_required':True,'validation_required':True,'rollback_required':True,'prior_evidence_cap':20,'posterior_uncertainty':True},
        'test_framework':{'physical':'RH range, nonnegative wind, positive pressure','numerical':'formula_valid, wind_3dw_valid, finite 3DW and air-density diagnostic outputs','metamorphic':'horizontal/3D wind norm identities and continuity identity','repeatability':'identical-input deterministic rerun','convergence':'finite-difference step halving (100/100/10 m -> 50/50/5 m)','convergence_tolerance':CONV_TOL,'repeatability_tolerance':REPEAT_TOL,'metamorphic_tolerance':META_TOL},
        'scientific_claim':'simulation-derived uncertainty/quality/validity/reliability prior only; not field validation or operational reliability'
    }
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'ok':True,'cells':len(cells),'build_evidence_per_cell':min(int(c['evidence_count']) for c in cells),'heldout_samples':len(held),'heldout_error':heldout['E_TEP_mean_profile_distance'],'heldout_pass':heldout['pass']},indent=2))
if __name__=='__main__':main()
