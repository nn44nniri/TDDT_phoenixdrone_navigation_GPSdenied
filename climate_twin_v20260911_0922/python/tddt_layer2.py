#!/usr/bin/env python3
"""Dynamic N-Twin Layer-2 adapter for the independent MicroMet climate twin.

This module intentionally implements only Layer-2 epistemic packaging. It does not
compute Layer-3 topology, coupling, trust weights, STD, Center of Consistency, or control.
"""
from __future__ import annotations
import csv, json, math, shutil, hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
RES=ROOT/'resources'/'tddt_layer2'
FEATURES=['temperature_local','relative_humidity_local','wind_speed_terrain','surface_pressure_pa','altitude_agl_m']
UOUT=['temperature_local', 'relative_humidity_local', 'wind_speed_terrain', 'wind_direction_terrain_deg', 'surface_pressure_pa', 'shortwave_radiation', 'wind_u_3dw', 'wind_v_3dw', 'diagnostic_vertical_wind', 'wind_speed_3d', 'air_density']
STATE_DEFAULT=['temperature_local','relative_humidity_local','wind_speed_terrain','wind_direction_terrain_deg','surface_pressure_pa','shortwave_radiation','longwave_radiation','precipitation_local','wind_u_3dw','wind_v_3dw','diagnostic_vertical_wind','wind_speed_3d','air_density']


def _finite(x):
    try:return math.isfinite(float(x))
    except:return False

def _load_json(p): return json.loads(Path(p).read_text(encoding='utf-8'))

def _sha256(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()

@dataclass
class TEPMatch:
    cell: dict[str,Any]
    distance: float
    novel: bool

class ClimateDomainTwin:
    def __init__(self, resources: Path|str=RES):
        self.resources=Path(resources)
        self.contract=_load_json(self.resources/'twin_contract.json')
        self.output_schema=_load_json(self.resources/'twin_output_schema.json')
        self.manifest=_load_json(self.resources/'tep/manifest.json')
        with (self.resources/'tep/tep.csv').open(newline='',encoding='utf-8') as f:
            self.cells=list(csv.DictReader(f))
        self.ranges=self.manifest.get('validity_ranges',{})
        schema_candidates=[ROOT/'docs'/'fmi_semantic_schema.json', ROOT/'build'/'fmu_stage'/'resources'/'fmi_semantic_schema.json']
        self.semantic_schema={}
        for sp in schema_candidates:
            if sp.exists():
                self.semantic_schema=_load_json(sp).get('variables',{})
                break

    def _feature_vector(self, result):
        o=result.get('fmu_outputs',{}); q=result.get('request',{})
        return {
            'temperature_local':o.get('temperature_local'),
            'relative_humidity_local':o.get('relative_humidity_local'),
            'wind_speed_terrain':o.get('wind_speed_terrain'),
            'surface_pressure_pa':o.get('surface_pressure_pa'),
            'altitude_agl_m':q.get('altitude_agl_m'),
        }

    def match_tep(self,result)->TEPMatch:
        x=self._feature_vector(result)
        best=None
        for row in self.cells:
            c=json.loads(row['centroid_json']); ss=0.0; n=0
            for k in FEATURES:
                if not _finite(x.get(k)) or k not in c: continue
                rr=self.ranges.get(k,{})
                scale=max(1e-12,float(rr.get('max',1))-float(rr.get('min',0)))
                z=(float(x[k])-float(c[k]))/scale
                ss+=z*z; n+=1
            d=math.sqrt(ss/max(1,n)) if n else float('inf')
            if best is None or d<best[0]: best=(d,row)
        radius=float((self.manifest.get('runtime_lookup') or {}).get('novelty_radius_standardized',self.manifest.get('novelty_radius_standardized',3.0)))
        return TEPMatch(best[1],best[0],best[0]>radius)

    def packet_from_estimate(self,result:dict,*,context_id='UNSPECIFIED',stage_id='UNSPECIFIED',operation_code='climate_estimate',evidence_log=None,data_age_s=0.0)->dict:
        ts=(result.get('request') or {}).get('time') or datetime.now(timezone.utc).isoformat()
        if result.get('ok') is False:
            return {'twin_id':'MicroMetTwin','state':{},'uncertainty':{},'quality':{'score':0.0,'flag':'INVALID','reason':result.get('error')},
                    'validity':{'score':0.0,'reason':result.get('error')},'reliability':{'prior':0.0,'posterior':None,'evidence_count':0},
                    'context':{'context_id':context_id,'stage_id':stage_id,'operation_code':operation_code},'timestamp':ts,
                    'provenance':{'model':'MicroMetTwin.fmu','formalism':'micromet_formalism_v2.md','error':result}}
        m=self.match_tep(result); c=m.cell; o=result['fmu_outputs']
        state={k:o.get(k) for k in STATE_DEFAULT if k in o}
        state_semantics={}
        for k in state:
            md=self.semantic_schema.get(k,{})
            state_semantics[k]={'unit':md.get('unit','1'),'frame':md.get('frame','unspecified'),'source':md.get('source','MicroMetTwin_FMU'),'timestamp':ts,'quality':md.get('quality','model_derived'),'validity_rule':md.get('validity','finite')}
        wind_ok=float(o.get('wind_3dw_valid',0))>=0.5
        density_ok=_finite(o.get('air_density')) and float(o.get('air_density',0))>0
        std=json.loads(c['uncertainty_std_json']); var=json.loads(c['variance_json'])
        cov=json.loads(c['covariance_json'])
        uncertainty_source='TEP_sim prior'
        if c.get('uncertainty_std_posterior_json') not in (None,'') and c.get('variance_posterior_json') not in (None,''):
            try:
                std=json.loads(c['uncertainty_std_posterior_json']); var=json.loads(c['variance_posterior_json'])
                if c.get('covariance_posterior_json') not in (None,''): cov=json.loads(c['covariance_posterior_json'])
                uncertainty_source='field-backed TEP posterior'
            except Exception: pass
        novelty=max(0.0,m.distance-float(self.manifest.get('novelty_radius_standardized',3.0)))
        inflate=1.0+novelty
        cov_scaled={'variables':cov.get('variables',UOUT),'matrix':[[float(v)*inflate*inflate for v in row] for row in cov.get('matrix',[])]}
        uncertainty={'std':{k:v*inflate for k,v in std.items()},'variance':{k:v*inflate*inflate for k,v in var.items()},'covariance':cov_scaled,
                     'type':uncertainty_source,'source':'nearest epistemic operating cell','novelty_distance':m.distance,
                     'unquantified_outputs':['longwave_radiation','precipitation_local','velocity_gradient_3dw','material_derivative_3dw'],
                     'availability':{k:(wind_ok if k in ['wind_u_3dw','wind_v_3dw','diagnostic_vertical_wind','wind_speed_3d'] else (density_ok if k=='air_density' else True)) for k in UOUT},
                     'runtime_gates':{'wind_u_3dw':'wind_3dw_valid==1','wind_v_3dw':'wind_3dw_valid==1','diagnostic_vertical_wind':'wind_3dw_valid==1','wind_speed_3d':'wind_3dw_valid==1','air_density':'formula_valid==1 && air_density>0'}}
        finite_ratio=sum(_finite(v) for v in state.values())/max(1,len(state))
        formula_ok=float(o.get('formula_valid',0))>=0.5
        phys=(0<=float(o.get('relative_humidity_local',-1))<=100 and float(o.get('wind_speed_terrain',-1))>=0 and float(o.get('surface_pressure_pa',-1))>0 and density_ok)
        qp=float(c['quality_prior']); vp=float(c['validity_prior']); rp=float(c['reliability_prior'])
        novelty_factor=math.exp(-novelty)
        fres=float((self.manifest.get('runtime_lookup') or {}).get('freshness_lambda_per_s',0.0))
        freshness=math.exp(-max(0.0,float(data_age_s))*max(0.0,fres))
        q=max(0.0,min(1.0,qp*finite_ratio*(1.0 if formula_ok else 0.0)*novelty_factor*freshness))
        v=max(0.0,min(1.0,vp*(1.0 if phys and formula_ok else 0.0)*novelty_factor*freshness))
        r=max(0.0,min(1.0,rp*novelty_factor*freshness))
        if v<=0: flag='INVALID'
        elif freshness<0.5: flag='STALE'
        elif m.novel: flag='EXTRAPOLATED'
        elif q>=0.8 and v>=0.8: flag='GOOD'
        else: flag='DEGRADED'
        posterior=None
        if c.get('reliability_posterior') not in (None,''):
            try:posterior=float(c['reliability_posterior'])
            except:pass
        packet={
            'twin_id':'MicroMetTwin','layer':2,'state':state,'state_semantics':state_semantics,'uncertainty':uncertainty,
            'quality':{'score':q,'flag':flag,'prior':qp,'reason':'finite+physical+support+TEP novelty',
                       'wind_3dw_current_valid':wind_ok},
            'validity':{'score':v,'prior':vp,'reason':'validity envelope + current physical checks + TEP support'},
            'reliability':{'prior':r,'posterior':posterior,'evidence_count':int(float(c['evidence_count'])),
                           'field_evidence_count':int(float(c.get('field_evidence_count') or 0)),'claim':'simulation prior unless posterior is field-backed'},
            'context':{'context_id':context_id,'stage_id':stage_id,'operation_code':operation_code,'tep_cell_id':c['tep_cell_id'],
                       'novelty':m.novel,'novelty_distance':m.distance,'data_age_s':float(data_age_s),'freshness_factor':freshness},
            'timestamp':ts,
            'provenance':{'model':'MicroMetTwin.fmu','model_version':self.manifest.get('twin_model_version','MicroMetTwin.fmu'),'formalism':'micromet_formalism_v2.md','semantic_schema_version':'1.0','tep_version':self.manifest['tep_version'],
                          'tep_maturity':self.manifest['maturity'],'terrain_source':(result.get('request') or {}).get('terrain_source'),
                          'source_count':result.get('source_count'),'architectural_boundary':'Layer 2 only; no topology/coupling/CoC'}
        }
        if evidence_log:
            p=Path(evidence_log); p.parent.mkdir(parents=True,exist_ok=True)
            with p.open('a',encoding='utf-8') as f:f.write(json.dumps(packet,separators=(',',':'),ensure_ascii=False)+'\n')
        return packet

class TEPVersionManager:
    """Slow, explicit field-backed posterior update with lineage, validation and rollback.

    No update is performed during fast/meso evaluation. Field evidence must be explicitly
    accepted by an external validation process and must retain provenance.
    """
    REQUIRED_EVIDENCE=('evidence_type','tep_cell_id','pass','timestamp','source','validation_status','validation_id','provenance','residual')
    UOUT=['temperature_local', 'relative_humidity_local', 'wind_speed_terrain', 'wind_direction_terrain_deg', 'surface_pressure_pa', 'shortwave_radiation', 'wind_u_3dw', 'wind_v_3dw', 'diagnostic_vertical_wind', 'wind_speed_3d', 'air_density']
    def __init__(self, resources: Path|str=RES): self.resources=Path(resources)

    @staticmethod
    def _cov(vectors):
        n=len(vectors); d=len(vectors[0])
        mu=[sum(v[j] for v in vectors)/n for j in range(d)]
        return [[sum((v[i]-mu[i])*(v[j]-mu[j]) for v in vectors)/(n-1) for j in range(d)] for i in range(d)]

    def _validate_evidence(self,e):
        missing=[k for k in self.REQUIRED_EVIDENCE if k not in e]
        if missing: raise ValueError('field evidence missing required fields: '+','.join(missing))
        if e.get('evidence_type')!='field_validated': raise ValueError('posterior update requires evidence_type=field_validated')
        if e.get('validation_status')!='accepted': raise ValueError('field evidence validation_status must be accepted')
        if not isinstance(e.get('provenance'),dict) or not e['provenance']: raise ValueError('field evidence provenance must be a non-empty object')
        r=e.get('residual')
        if not isinstance(r,dict) or not all(k in r and _finite(r[k]) for k in UOUT): raise ValueError('field evidence residual must contain finite residuals for all posterior uncertainty outputs')

    def create_field_posterior(self, evidence_jsonl, output_dir, *, version, min_evidence=2):
        ev=[]
        with open(evidence_jsonl,encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    e=json.loads(line); self._validate_evidence(e); ev.append(e)
        if len(ev)<min_evidence: raise ValueError('insufficient validated field evidence')
        out=Path(output_dir); out.mkdir(parents=True,exist_ok=True)
        src=self.resources/'tep/tep.csv'; rows=list(csv.DictReader(src.open(newline='',encoding='utf-8')))
        by={r['tep_cell_id']:[] for r in rows}
        for e in ev:
            if e['tep_cell_id'] in by: by[e['tep_cell_id']].append(e)
        old=_load_json(self.resources/'tep/manifest.json')
        cap=int((old.get('posterior_policy') or {}).get('prior_evidence_cap',20))
        updated_cells=0
        for r in rows:
            ee=by[r['tep_cell_id']]; n=len(ee)
            if not n: continue
            updated_cells+=1
            s_count=sum(1 for e in ee if bool(e['pass'])); f_count=n-s_count
            prior=float(r['reliability_prior']); neff=min(int(float(r['evidence_count'])),cap)
            a0=prior*neff; b0=(1-prior)*neff
            r['reliability_posterior']=f"{(a0+s_count)/(a0+b0+n):.12g}"; r['field_evidence_count']=n
            if n>=2:
                vectors=[[float(e['residual'][k]) for k in UOUT] for e in ee]
                cf=self._cov(vectors)
                cp=json.loads(r['covariance_json'])['matrix']
                omega=n/(n+neff) if n+neff else 1.0
                post=[[(1-omega)*float(cp[i][j])+omega*float(cf[i][j]) for j in range(len(UOUT))] for i in range(len(UOUT))]
                var={k:max(0.0,post[i][i]) for i,k in enumerate(UOUT)}; std={k:math.sqrt(v) for k,v in var.items()}
                r['covariance_posterior_json']=json.dumps({'variables':UOUT,'matrix':post,'field_weight':omega},separators=(',',':'))
                r['variance_posterior_json']=json.dumps(var,separators=(',',':')); r['uncertainty_std_posterior_json']=json.dumps(std,separators=(',',':'))
        with (out/'tep.csv').open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        new=dict(old); new.update({'tep_version':version,'maturity':'TEP_hybrid','field_evidence':True,'evidence_type':'hybrid_field_posterior','parent_tep_version':old['tep_version'],'field_evidence_count':len(ev),'updated_cells':updated_cells,'created_at':datetime.now(timezone.utc).isoformat(),'rollback_to':old['tep_version'],'validation_status':'accepted','evidence_validation_ids':sorted({str(e['validation_id']) for e in ev}),'prior_evidence_cap':cap,'posterior_uncertainty_updated':True,'scientific_claim':'hybrid prior/posterior; operational maturity requires explicit field-validation policy and sufficient coverage'})
        (out/'manifest.json').write_text(json.dumps(new,indent=2,ensure_ascii=False),encoding='utf-8')
        (out/'VERSION').write_text(str(version)+'\n',encoding='utf-8')
        (out/'field_evidence_provenance.json').write_text(json.dumps({'source_file':str(evidence_jsonl),'count':len(ev),'validation_ids':new['evidence_validation_ids'],'created_at':new['created_at']},indent=2),encoding='utf-8')
        return new

if __name__=='__main__':
    print(json.dumps({'ok':True,'twin':'MicroMetTwin','layer':2,'resources':str(RES)},indent=2))
