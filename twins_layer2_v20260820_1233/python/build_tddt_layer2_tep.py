from __future__ import annotations
import csv, gzip, json, math, sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from twins_fmi import FormulaFMU, DroneConfig
from twins_fmi.scenario_defaults import scenario_value
from twins_fmi.vtol_phase import WING_TEP_PHASE_FAMILIES, apply_wing_phase_overrides, TEP_NOVELTY_EXCLUDED_FEATURES, WING_TEP_SCALE_FLOORS

def sample_mesa(name, index=1): return scenario_value("MESA_NAV", name, index)
def sample_wing(name, index=1): return scenario_value("WingMPET", name, index)

DISCRETE_TOKENS=('count','index','valid','flag','mode','store_','operation','accepted','candidate_count','code')
QUALITY_WEIGHTS={
    'convergence':0.20,'completeness':0.15,'physical':0.20,'numerical':0.15,
    'support':0.10,'repeatability':0.10,'metamorphic':0.10,
}
DOE_FACTORS=(-0.05,0.0,0.05)  # deterministic stratified centers
LOCAL_PERTURB=0.01
TRIALS=8
HELDOUT_FACTORS=(-0.025,0.025)

NONNEG_TOKENS=('density','pressure','energy','power','current','voltage','mass','radius','distance','margin','q_dyn','groundspeed','airspeed','path_length','segment_time','load_factor')
BOUNDED_01_TOKENS=('soc','relative_humidity','efficiency','quality','validity','reliability','score')

def perturbable(name: str) -> bool:
    n=name.lower()
    return not any(t in n for t in DISCRETE_TOKENS)

def clamp_semantic(name: str, x: float) -> float:
    n=name.lower()
    if any(k in n for k in BOUNDED_01_TOKENS):
        return min(1.0,max(0.0,x))
    return x

def shifted(inputs, factor: float):
    out={}
    for idx,(name,val) in enumerate(inputs.items(),1):
        try: x=float(val)
        except Exception:
            out[name]=val; continue
        if not perturbable(name):
            out[name]=x; continue
        # deterministic alternating direction avoids all dimensions moving identically
        sign=-1.0 if idx%2==0 else 1.0
        if abs(x)>1e-12: y=x*(1.0+sign*factor)
        else: y=sign*factor*0.1
        out[name]=clamp_semantic(name,y)
    return out

def local_perturb(center, trial):
    out={}
    for idx,(name,val) in enumerate(center.items(),1):
        try: x=float(val)
        except Exception:
            out[name]=val; continue
        if not perturbable(name): out[name]=x; continue
        phase=((idx*37 + trial*19) % 17)-8
        frac=(phase/8.0)*LOCAL_PERTURB
        if abs(x)>1e-12: y=x*(1.0+frac)
        else: y=frac*0.1
        out[name]=clamp_semantic(name,y)
    return out

def finite_dict(res):
    d={}
    for k,v in res.items():
        try:
            fv=float(v)
            if math.isfinite(fv): d[k]=fv
        except Exception: pass
    return d

def physical_check(outputs):
    reasons=[]
    for name,v in outputs.items():
        n=name.lower(); fv=float(v)
        # flags are allowed to be 0/1; margins may be negative by design, so don't constrain all margins.
        if any(tok in n for tok in NONNEG_TOKENS) and 'margin' not in n and not (
            ('residual' in n) and not any(t in n for t in ('variance', 'sigma', 'std', 'norm', 'squared', 'magnitude'))
        ):
            if fv < -1e-9: reasons.append(f'{name}:negative')
        if any(tok in n for tok in BOUNDED_01_TOKENS):
            if not (-1e-9 <= fv <= 1.0+1e-9): reasons.append(f'{name}:outside_0_1')
    return len(reasons)==0,reasons

def range_valid(values, envelope, phase_family=None):
    reasons=[]; checked=0
    rules=dict(envelope.get('ranges',{}))
    if phase_family:
        rules.update(envelope.get('mode_ranges',{}).get(phase_family,{}))
    for name,rule in rules.items():
        if rule is None or (isinstance(rule,dict) and rule.get('enabled') is False): continue
        if name not in values: continue
        checked+=1
        try: v=float(values[name])
        except Exception: reasons.append(f'{name}:non_numeric'); continue
        lo,hi=rule.get('min'),rule.get('max')
        if not math.isfinite(v) or (lo is not None and v<float(lo)) or (hi is not None and v>float(hi)):
            reasons.append(f'{name}:outside[{lo},{hi}]')
    return len(reasons)==0,reasons,checked

def normalized_repeatability(samples, names):
    scores=[]
    for name in names:
        vals=[s[name] for s in samples if name in s]
        if len(vals)<2: continue
        mu=mean(vals); sd=pstdev(vals); scale=max(abs(mu),1.0)
        scores.append(max(0.0,1.0-min(1.0,sd/scale)))
    return mean(scores) if scores else 1.0

def metamorphic_continuity(samples, names):
    # Local +/- perturbations should not create non-finite/discontinuous blow-ups.
    scores=[]
    for name in names:
        vals=[s[name] for s in samples if name in s]
        if len(vals)<2: continue
        med=sorted(vals)[len(vals)//2]
        span=max(vals)-min(vals); denom=max(abs(med),1.0)
        scores.append(max(0.0,1.0-min(1.0,span/(10.0*denom))))
    return mean(scores) if scores else 1.0

def center_bounds(center):
    bounds={}; scales={}
    for name,val in center.items():
        if not isinstance(val,(int,float)) or not perturbable(name): continue
        x=float(val); s=max(abs(x)*0.075,0.01)
        lo=clamp_semantic(name,x-s); hi=clamp_semantic(name,x+s)
        if lo>hi: lo,hi=hi,lo
        bounds[name]=[lo,hi]; scales[name]=max((hi-lo)/2.0,1e-9)
    return bounds,scales

def write_metadata(model, catalog, cfg, outroot):
    meta=outroot/'metadata'; meta.mkdir(parents=True,exist_ok=True)
    old_contract=json.loads((outroot/'twin_contract.json').read_text(encoding='utf-8')) if (outroot/'twin_contract.json').exists() else {}
    all_inputs=sorted({x for op in catalog['operations'].values() for x in op.get('inputs',[])})
    all_outputs=sorted({x for op in catalog['operations'].values() for x in op.get('outputs',[])})
    contract={**old_contract,
      'schema_version':'3.0',
      'scientific_contract':{
        'inputs':all_inputs,'outputs':all_outputs,
        'parameters_source':'config/phoenix_drone_v3.yaml + operation runtime inputs',
        'units_frames_source':'modelDescription.xml + resources/signal_schema.json',
        'ranges_source':'metadata/validity_envelope.json',
        'physical_constraints':['finite numeric outputs','formalism-specific feasibility flags','nonnegative physical magnitudes where applicable'],
        'numerical_constraints':['FMI status OK','finite Real outputs','deterministic resettable evaluation'],
        'uncertainty_sources':['input','parameter','model','numerical','context'],
        'quality_rules_source':'metadata/quality_rules.json',
        'reliability_pass_rule':'FMI success AND complete finite output AND physical check AND validity-envelope check',
        'tolerance_policy':'operation/formalism-specific; runtime envelope is authoritative',
        'provenance_required':True,
      },
      'feedback_contract':{
        'accepts_domain_specific_layer7_feedback':True,
        'tep_update_policy':'slow, field-evidence-only, versioned, provenance-preserving, rollback-capable',
        'fast_tick_rewrite_allowed':False,
      }
    }
    (outroot/'twin_contract.json').write_text(json.dumps(contract,indent=2,ensure_ascii=False),encoding='utf-8')
    uncertainty={
      'schema_version':'3.0','twin_id':model,
      'sources':['input','parameter','model','numerical','context'],
      'offline_method':['deterministic stratified DOE','local perturbation ensemble'],
      'representation':['per-output standard deviation','per-output diagonal variance'],
      'field_covariance_claim':False,'maturity':'TEP_sim',
    }
    quality={
      'schema_version':'3.0','twin_id':model,'components':QUALITY_WEIGHTS,
      'flags':{'good_min':0.9,'degraded_min':0.5},
      'components_meaning':['convergence','completeness','physical consistency','numerical health','support','repeatability','metamorphic continuity'],
      'truth_requirement':'none for simulation/verification quality; not operational accuracy',
    }
    reliability={
      'schema_version':'3.0','twin_id':model,'prior_type':'Beta-regularized verification reliability',
      'alpha0':1.0,'beta0':1.0,'pass_rule':'FMI+finite+complete+physical+validity',
      'operational_reliability_available':False,'field_evidence_count':0,
      'maturity_path':['TEP_sim','TEP_hybrid','TEP_operational'],
    }
    for name,obj in [('uncertainty_model.json',uncertainty),('quality_rules.json',quality),('reliability_profile.json',reliability)]:
        (meta/name).write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding='utf-8')
        (outroot/name).write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding='utf-8')
    # ensure validity envelope is versioned and declares novelty modifier policy
    vp=meta/'validity_envelope.json'
    env=json.loads(vp.read_text(encoding='utf-8'))
    if model=='WingMPET':
        env['schema_version']='4.0'; env['envelope_version']='2026-09-15-v4-vtol-phase-aware'
        env['novelty_excluded_features']=sorted(TEP_NOVELTY_EXCLUDED_FEATURES)
    else:
        env['schema_version']='3.0'; env['envelope_version']='2026-08-20-v3-vtol'
    env['maturity']='simulation_prior'
    env['novelty_policy']={'uncertainty_multiplier':2.0,'quality_multiplier':0.6,'validity_cap':0.5,'reliability_multiplier':0.6}
    env['freshness_policy']={'lambda_per_second':0.001,'minimum_multiplier':0.25}
    vp.write_text(json.dumps(env,indent=2,ensure_ascii=False),encoding='utf-8')
    (outroot/'validity_envelope.json').write_text(json.dumps(env,indent=2,ensure_ascii=False),encoding='utf-8')
    return env

MESA_FULL_STATE_OUTPUTS=('rN','rE','rD','vN','vE','vD','qw','qx','qy','qz',
                         'Ppos_N','Ppos_E','Ppos_D','obs_score','nis','robust_weight',
                         'agl','airspeed','heading','flightpath')


def evaluate_tep_operation(fmu, model, op, inputs):
    """Operation 22 updates native MESA state, not formula output slots.

    Read the published state explicitly rather than counting an empty dict as
    numerical verification of a complete estimator step.
    """
    result=fmu.evaluate_operation(op,inputs=inputs,use_config=False,reset_before=True)
    if model=='MESA_NAV' and int(op)==22:
        return fmu.get(*MESA_FULL_STATE_OUTPUTS)
    return result

def build(model: str, sampler, trials=TRIALS):
    cfg=DroneConfig(ROOT/'config'/'phoenix_drone_v3.yaml')
    fmu_path=ROOT/'fmus'/f'{model}.fmu'
    outroot=ROOT/'resources'/'tddt_layer2'/model
    tepdir=outroot/'tep'; valdir=outroot/'validation'
    tepdir.mkdir(parents=True,exist_ok=True); valdir.mkdir(parents=True,exist_ok=True)
    rows=[]; raw=[]; heldout=[]; all_parameter_ranges={}
    with FormulaFMU(fmu_path, config=cfg) as fmu:
        envelope=write_metadata(model,fmu.catalog,cfg,outroot)
        for op in sorted(fmu.operations):
            sig=fmu.operation_signature(op); names=list(MESA_FULL_STATE_OUTPUTS) if model=='MESA_NAV' and op==22 else list(sig.get('outputs',[]))
            nominal=cfg.overlay(sig.get('inputs',[]), sampler)
            families=WING_TEP_PHASE_FAMILIES if model=='WingMPET' else ('generic',)
            family_centers=[]
            for family in families:
                base=apply_wing_phase_overrides(nominal,family) if model=='WingMPET' else dict(nominal)
                for factor in DOE_FACTORS:
                    family_centers.append((family,shifted(base,factor)))
            for ci,(family,center) in enumerate(family_centers,1):
                bounds,scales=center_bounds(center)
                if model=='WingMPET':
                    for name,floor in WING_TEP_SCALE_FLOORS.items():
                        if name in center and isinstance(center[name],(int,float)):
                            f=float(floor); scales[name]=max(float(scales.get(name,0.0)),f); bounds[name]=[float(center[name])-f,float(center[name])+f]
                for k,b in bounds.items():
                    cur=all_parameter_ranges.setdefault(k,[b[0],b[1]])
                    cur[0]=min(cur[0],b[0]); cur[1]=max(cur[1],b[1])
                samples=[]; pass_count=0; conv=0; complete=0; physical_ok_count=0; numerical_ok_count=0; validity_ok_count=0
                for trial in range(trials):
                    inp=local_perturb(center,trial)
                    record={'operation_code':op,'cell_index':ci,'trial':trial,'inputs':inp}
                    try:
                        res=evaluate_tep_operation(fmu,model,op,inp)
                        conv+=1; fd=finite_dict(res); samples.append(fd)
                        is_complete=(len(fd)==len(res)==len(names)); complete+=int(is_complete)
                        numerical_ok=is_complete and all(math.isfinite(v) for v in fd.values()); numerical_ok_count+=int(numerical_ok)
                        phys_ok,phys_reasons=physical_check(fd); physical_ok_count+=int(phys_ok)
                        val_ok,val_reasons,_=range_valid({**inp,**fd},envelope,family if model=='WingMPET' else None); validity_ok_count+=int(val_ok)
                        passed=bool(numerical_ok and phys_ok and val_ok); pass_count+=int(passed)
                        record.update({'outputs':fd,'passed':passed,'physical_reasons':phys_reasons,'validity_reasons':val_reasons})
                    except Exception as exc:
                        samples.append({}); record.update({'outputs':{},'passed':False,'error':repr(exc)})
                    raw.append(record)
                uncertainty={}; variance={}; means={}
                for name in names:
                    vals=[s[name] for s in samples if name in s]
                    means[name]=mean(vals) if vals else None
                    sd=pstdev(vals) if len(vals)>1 else 0.0
                    uncertainty[name]=sd; variance[name]=sd*sd
                repeat=normalized_repeatability(samples,names); meta=metamorphic_continuity(samples,names)
                components={
                  'convergence':conv/trials,'completeness':complete/trials,'physical':physical_ok_count/trials,
                  'numerical':numerical_ok_count/trials,'support':1.0,'repeatability':repeat,'metamorphic':meta,
                }
                quality=sum(QUALITY_WEIGHTS[k]*components[k] for k in QUALITY_WEIGHTS)
                validity=validity_ok_count/trials
                reliability=(pass_count+1)/(trials+2)
                rows.append({
                  'twin_id':model,'cell_id':f'{model}_OP{op:02d}_{family}_E{ci:03d}','operation_code':op,'phase_family':family,
                  'bounds_json':json.dumps(bounds,separators=(',',':')),
                  'center_json':json.dumps(center,separators=(',',':')),'scale_json':json.dumps(scales,separators=(',',':')),
                  'mean_json':json.dumps(means,separators=(',',':')),'uncertainty_json':json.dumps(uncertainty,separators=(',',':')),
                  'variance_json':json.dumps(variance,separators=(',',':')),'quality_components_json':json.dumps(components,separators=(',',':')),
                  'quality_prior':quality,'validity_prior':validity,'reliability_prior':reliability,
                  'evidence_count':trials,'generation_method':'stratified_DOE+local_perturbation+metamorphic_checks',
                  'source':'simulation_prior_verification','provenance':f'{model}:formalism+FMI+phoenix_config'
                })
            # held-out points distinct from build centers
            for hf in HELDOUT_FACTORS:
                inp=shifted(nominal,hf)
                try:
                    res=evaluate_tep_operation(fmu,model,op,inp); fd=finite_dict(res)
                    phys,_=physical_check(fd); val,_,_=range_valid({**inp,**fd},envelope)
                    heldout.append({'operation_code':op,'factor':hf,'finite_complete':len(fd)==len(res)==len(names),'physical':phys,'validity':val,'passed':bool(len(fd)==len(res)==len(names) and phys and val)})
                except Exception as exc:
                    heldout.append({'operation_code':op,'factor':hf,'passed':False,'error':repr(exc)})
    fields=list(rows[0])
    with (tepdir/'tep.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    with gzip.open(tepdir/'raw_trials.jsonl.gz','wt',encoding='utf-8') as f:
        for r in raw: f.write(json.dumps(r,separators=(',',':'),ensure_ascii=False)+'\n')
    held_pass=sum(1 for x in heldout if x.get('passed'))
    held_report={'schema_version':'1.0','twin_id':model,'heldout_count':len(heldout),'pass_count':held_pass,'pass_rate':held_pass/max(1,len(heldout)),'method':'two deterministic held-out stratified points per operation, disjoint from build centers','records':heldout}
    (tepdir/'heldout_validation.json').write_text(json.dumps(held_report,indent=2,ensure_ascii=False),encoding='utf-8')
    (valdir/'heldout_validation.json').write_text(json.dumps(held_report,indent=2,ensure_ascii=False),encoding='utf-8')
    created=datetime.now(timezone.utc).isoformat()
    cell_diams=[]
    for r in rows:
        scales=json.loads(r['scale_json']); cell_diams.append(2.0*math.sqrt(sum(1.0 for _ in scales)) if scales else 0.0)
    manifest={
      'schema_version':'4.0','tep_name':f'{model}_TEP','tep_version':('2026-09-15-sim-v4-vtol-phase-aware' if model=='WingMPET' else '2026-08-20-sim-v3-vtol'),'created_at':created,
      'twin_id':model,'twin_model_version':json.loads((outroot/'twin_contract.json').read_text())['model_version'],
      'formalism_version':json.loads((outroot/'twin_contract.json').read_text())['model_version'],'simulation_version':'native-C++ FMI2 package 20260820_1202',
      'generation_method':'3-center deterministic stratified DOE per operation + 8 local perturbation trials + physical/numerical/metamorphic checks',
      'sampling_method':'deterministic_stratified','perturbation_model':{'local_fraction':LOCAL_PERTURB,'distribution':'deterministic zero-mean phase sequence'},
      'parameter_ranges':all_parameter_ranges,'coverage':1.0,'coverage_definition':('all declared operation codes have phase-family epistemic cells for Phoenix VTOL and held-out checks' if model=='WingMPET' else 'all declared operation codes have 3 epistemic operating cells and held-out checks'),
      'cell_count':len(rows),'operation_count':len(set(r['operation_code'] for r in rows)),'cells_per_operation':(len(DOE_FACTORS)*len(WING_TEP_PHASE_FAMILIES) if model=='WingMPET' else len(DOE_FACTORS)),
      'phase_families':(list(WING_TEP_PHASE_FAMILIES) if model=='WingMPET' else ['generic']),
      'novelty_excluded_features':(sorted(TEP_NOVELTY_EXCLUDED_FEATURES) if model=='WingMPET' else []),
      'phase_scale_floors':(WING_TEP_SCALE_FLOORS if model=='WingMPET' else {}),
      'min_evidence_per_cell':min(int(r['evidence_count']) for r in rows),'resolution':{'average_normalized_cell_diameter':mean(cell_diams) if cell_diams else 0.0,'definition':'diameter in per-cell normalized coordinates'},
      'evidence_type':'simulation_prior','evidence_kind':'simulation/verification prior only','field_evidence_present':False,'field_evidence':False,'maturity':'TEP_sim',
      'uncertainty_representation':'per-output standard deviation + diagonal variance from local ensemble',
      'quality_definition':'weighted convergence/completeness/physical/numerical/support/repeatability/metamorphic score',
      'validity_definition':'runtime validity envelope + local pass fraction','reliability_definition':'Beta(1,1)-regularized simulation verification pass rate',
      'held_out_validation':{'file':'heldout_validation.json','pass_rate':held_report['pass_rate'],'count':held_report['heldout_count']},
      'novelty_radius':3.0,'novelty_policy':{'uncertainty_multiplier':2.0,'quality_multiplier':0.6,'validity_cap':0.5,'reliability_multiplier':0.6},
      'freshness_policy':{'lambda_per_second':0.001,'minimum_multiplier':0.25},
      'raw_trials_archived':True,'raw_trials_file':'raw_trials.jsonl.gz','raw_trials_release_policy':'retained reproducible simulation-prior audit; no field evidence','generator_revision':'2026-09-20-v3-audited-state-outputs','reliability_regularization':'Beta(1,1)',
      'update_policy':{'runtime_read_only':True,'field_evidence_required_for_posterior':True,'slow_versioned':True,'rollback_capable':True,'no_field_evidence_action':'keep_prior'},
      'limitations':['simulation prior only','no field calibration','deterministic DOE is verification-oriented, not a substitute for operational validation'],
      'provenance':{'source_documents':[model+'_formalism','TDDT_Dynamic_NTwin_Layer2_details_FA_v2.md','TDDT_Dynamic_NTwin_TEP_details_FA_v2.md'],'generator':'python/build_tddt_layer2_tep.py'},
    }
    (tepdir/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    (tepdir/'VERSION').write_text(manifest['tep_version']+'\n',encoding='utf-8')
    quality_report={'twin_id':model,'cell_count':len(rows),'coverage':manifest['coverage'],'min_evidence_per_cell':manifest['min_evidence_per_cell'],'heldout_pass_rate':held_report['pass_rate'],'quality_min':min(float(r['quality_prior']) for r in rows),'validity_min':min(float(r['validity_prior']) for r in rows),'reliability_min':min(float(r['reliability_prior']) for r in rows)}
    (tepdir/'quality_report.json').write_text(json.dumps(quality_report,indent=2),encoding='utf-8')
    return rows,manifest,held_report

if __name__=='__main__':
    for model,sampler in [('MESA_NAV',sample_mesa),('WingMPET',sample_wing)]:
        rows,manifest,held=build(model,sampler)
        print(model,'cells',len(rows),'coverage',manifest['coverage'],'heldout',held['pass_rate'],'quality_min',min(float(r['quality_prior']) for r in rows),'reliability_min',min(float(r['reliability_prior']) for r in rows))
