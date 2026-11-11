from __future__ import annotations

import csv, hashlib, json, math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Mapping

from .formula_fmi import FormulaFMU
from .drone_config import DroneConfig
from .vtol_phase import phase_family, TEP_NOVELTY_EXCLUDED_FEATURES

@dataclass(frozen=True)
class TwinPacket:
    twin_id: str
    state: dict[str, Any]
    uncertainty: dict[str, Any]
    quality: dict[str, Any]
    validity: dict[str, Any]
    reliability: dict[str, Any]
    context: dict[str, Any]
    timestamp: float
    provenance: dict[str, Any]
    operation_code: int
    context_id: str | None = None
    stage_id: str | None = None
    tep_cell_id: str | None = None
    def to_dict(self) -> dict[str, Any]: return asdict(self)

class TwinEpistemicProfile:
    """Read-mostly runtime TEP for one independent Layer-2 twin."""
    JSON_FIELDS=("bounds_json","center_json","scale_json","mean_json","uncertainty_json","variance_json","quality_components_json")
    def __init__(self, root: Path):
        self.root=Path(root)
        self.manifest=json.loads((self.root/'tep'/'manifest.json').read_text(encoding='utf-8'))
        self.contract=json.loads((self.root/'twin_contract.json').read_text(encoding='utf-8'))
        metadata=self.root/'metadata'
        def mf(name):
            p=metadata/name; return p if p.exists() else self.root/name
        self.quality_rules=json.loads(mf('quality_rules.json').read_text(encoding='utf-8'))
        self.validity_envelope=json.loads(mf('validity_envelope.json').read_text(encoding='utf-8'))
        self.reliability_profile=json.loads(mf('reliability_profile.json').read_text(encoding='utf-8'))
        self.uncertainty_model=json.loads(mf('uncertainty_model.json').read_text(encoding='utf-8'))
        self.records=[]
        with (self.root/'tep'/'tep.csv').open(newline='',encoding='utf-8') as f:
            for row in csv.DictReader(f):
                rec=dict(row)
                for key in ('operation_code','quality_prior','validity_prior','reliability_prior','evidence_count'):
                    if key in rec and rec[key]!='': rec[key]=int(float(rec[key])) if key in {'operation_code','evidence_count'} else float(rec[key])
                for key in self.JSON_FIELDS:
                    if key in rec: rec[key]=json.loads(rec.get(key) or '{}')
                self.records.append(rec)
        self.posterior={}
        pp=self.root/'tep'/'posterior_overrides.json'
        if pp.exists(): self.posterior=json.loads(pp.read_text(encoding='utf-8')).get('cells',{})
    @property
    def version(self): return str(self.manifest.get('tep_version','unknown'))
    @property
    def maturity(self): return str(self.manifest.get('maturity','TEP_sim'))
    def lookup(self, operation_code:int, operating:Mapping[str,Any]|None=None):
        operating=dict(operating or {})
        requested_family=phase_family(operating.get('flight_phase') or operating.get('stage') or operating.get('route_stage'))
        cand=[r for r in self.records if int(r.get('operation_code',-1))==int(operation_code)]
        if requested_family:
            exact=[r for r in cand if str(r.get('phase_family') or '').strip()==requested_family]
            generic=[r for r in cand if str(r.get('phase_family') or '').strip() in {'','generic'}]
            cand=exact or generic or cand
        if not cand:
            return {'cell_id':None,'quality_prior':None,'validity_prior':None,'reliability_prior':None,'evidence_count':0,'uncertainty_json':{},'variance_json':{},'source':'TEP_no_matching_cell','novelty':True,'distance':math.inf}
        def distance(rec):
            c,s=rec.get('center_json',{}),rec.get('scale_json',{}); terms=[]
            excluded=set(self.manifest.get('novelty_excluded_features') or []) | TEP_NOVELTY_EXCLUDED_FEATURES
            for name,center in c.items():
                if name in excluded or name not in operating: continue
                try: val=float(operating[name]); cen=float(center); sc=max(abs(float(s.get(name,1.0))),1e-12)
                except (TypeError,ValueError): continue
                terms.append(((val-cen)/sc)**2)
            return math.sqrt(sum(terms)) if terms else 0.0
        best=min(cand,key=distance); result=dict(best); result['distance']=distance(best)
        result['novelty']=bool(result['distance']>float(self.manifest.get('novelty_radius',3.0)))
        ov=self.posterior.get(str(result.get('cell_id')),{})
        if ov:
            result['reliability_posterior']=ov.get('reliability_posterior',result.get('reliability_prior'))
            result['posterior_evidence_count']=ov.get('field_evidence_count',0)
            result['posterior_source']=ov.get('source','field_evidence')
        return result
    def validity(self, values:Mapping[str,Any], *, stage:Any=None):
        reasons=[]; checked=0; passed=0
        family=phase_family(stage)
        mode_overrides=(self.validity_envelope.get('mode_ranges',{}).get(family,{}) if family else {})
        rules=dict(self.validity_envelope.get('ranges',{})); rules.update(mode_overrides)
        for name,rule in rules.items():
            if rule is None or (isinstance(rule,dict) and rule.get('enabled') is False): continue
            if name not in values: continue
            checked+=1
            try: v=float(values[name])
            except (TypeError,ValueError): reasons.append(f'{name}:non_numeric'); continue
            lo,hi=rule.get('min'),rule.get('max'); ok=math.isfinite(v) and (lo is None or v>=float(lo)) and (hi is None or v<=float(hi))
            passed+=int(ok)
            if not ok: reasons.append(f'{name}:outside[{lo},{hi}]')
        if checked==0: return 1.0,['no_applicable_runtime_range_rule']
        return passed/checked,reasons or ['within_validity_envelope']

class DomainTwin:
    """Independent Dynamic-N-Twin Layer-2 wrapper. No Layer-3 coupling/CoC is computed here."""
    def __init__(self,fmu_path:str|Path,*,config:str|Path|DroneConfig|None=None,instance_name:str|None=None,tep_root:str|Path|None=None,evidence_log:str|Path|None=None,**fmu_options:Any):
        self.fmu_path=Path(fmu_path).resolve(); self.fmu=FormulaFMU(self.fmu_path,config=config,instance_name=instance_name,**fmu_options)
        troot=Path(tep_root).resolve() if tep_root else self.fmu.tmp/'resources'/'tddt_layer2'
        if not troot.exists(): self.fmu.close(suppress_errors=True); raise RuntimeError(f'FMU has no Layer-2 TDDT profile: {troot}')
        self.tep=TwinEpistemicProfile(troot); self.twin_id=str(self.tep.contract.get('twin_id',self.fmu.name)); self.model_version=str(self.tep.contract.get('model_version','unknown'))
        self._sha256=hashlib.sha256(self.fmu_path.read_bytes()).hexdigest(); self.evidence_log=Path(evidence_log) if evidence_log else None
    def close(self): self.fmu.close(suppress_errors=True)
    def reset(self): self.fmu.reset()
    def __enter__(self): return self
    def __exit__(self,exc_type,exc,tb): self.close()
    @staticmethod
    def _finite_fraction(values):
        # A declared zero-output operation is a valid interface/metadata
        # operation.  Its output set is vacuously finite rather than invalid.
        if not values: return 1.0
        good=0
        for v in values.values():
            try: good+=int(math.isfinite(float(v)))
            except (TypeError,ValueError): pass
        return good/len(values)
    @staticmethod
    def _mul_map(d:Mapping[str,Any],factor:float):
        out={}
        for k,v in d.items():
            try: out[k]=float(v)*factor
            except Exception: out[k]=v
        return out
    def _append_evidence(self,packet:TwinPacket):
        if not self.evidence_log: return
        self.evidence_log.parent.mkdir(parents=True,exist_ok=True)
        rec={'kind':'runtime_layer2_evidence','note':'retained for possible Layer-7 review; does not update TEP','packet':packet.to_dict()}
        with self.evidence_log.open('a',encoding='utf-8') as f: f.write(json.dumps(rec,separators=(',',':'),ensure_ascii=False)+'\n')
    def evaluate_packet(self,operation_code:int,*,context:Mapping[str,Any]|None=None,context_id:str|None=None,stage_id:str|None=None,source:str='runtime_fmi',communication_step:float=1e-3,use_config:bool=True,require_all_inputs:bool=False,reset_before:bool=False,inputs:Mapping[str,Any]|None=None,freshness_age_s:float=0.0,prior_profile:Mapping[str,Any]|None=None,**named_inputs:Any)->TwinPacket:
        supplied=dict(inputs or {}); supplied.update(named_inputs)
        signature=self.fmu.operation_signature(operation_code); resolved=self.fmu.configuration_inputs(operation_code) if use_config else {}; resolved.update(supplied)
        outputs=self.fmu.evaluate_operation_enveloped(operation_code,communication_step=communication_step,use_config=use_config,require_all_inputs=require_all_inputs,reset_before=reset_before,inputs=supplied)
        state={k:v['value'] for k,v in outputs.items()}; finite_fraction=self._finite_fraction(state); expected_outputs=len(signature.get('outputs',[])); completeness=1.0 if expected_outputs==0 else len(state)/expected_outputs
        operating={**resolved, **dict(context or {})}
        if stage_id is not None: operating.setdefault('flight_phase',stage_id)
        tep=self.tep.lookup(operation_code,operating); validity_score,validity_reasons=self.tep.validity({**resolved,**state},stage=stage_id or operating.get('flight_phase'))
        d=float(tep.get('distance',0.0) or 0.0); npol=dict(self.tep.manifest.get('novelty_policy',{})); fpol=dict(self.tep.manifest.get('freshness_policy',{}))
        lambda_r=float(npol.get('lambda_R',0.15)); gamma_u=float(npol.get('gamma_U',0.25)); q_novel=float(npol.get('quality_multiplier',0.6)); v_cap=float(npol.get('validity_cap',0.5)); r_novel=float(npol.get('reliability_multiplier',1.0))
        F=max(float(fpol.get('minimum_multiplier',0.25)),math.exp(-float(fpol.get('lambda_per_second',0.001))*max(0.0,float(freshness_age_s))))
        prior_quality=float(tep.get('quality_prior') if tep.get('quality_prior') is not None else 0.0)
        quality_score=(finite_fraction+completeness+prior_quality)/3.0; quality_reasons=['finite_numeric','output_completeness','TEP_prior']
        u_factor=1.0+gamma_u*d; rel_prior=tep.get('reliability_prior'); rel_adj=(float(rel_prior)*math.exp(-lambda_r*d)) if rel_prior is not None and math.isfinite(d) else rel_prior
        if tep.get('novelty'):
            quality_score*=q_novel; validity_score=min(validity_score,v_cap); rel_adj=(rel_adj*r_novel if rel_adj is not None else None); validity_reasons=list(validity_reasons)+['outside_TEP_support']; quality_reasons.append('novelty_penalty')
        # freshness is a runtime data-quality modifier, not a rewrite of TEP.
        quality_score*=F; quality_reasons.append(f'freshness_multiplier={F:.6g}')
        # Optional CRP/SARG-conditioned prior modifiers are not field evidence and cannot create operational claims.
        pp=dict(prior_profile or {})
        if pp:
            et=str(pp.get('evidence_type','reference_conditioning'))
            if 'field' in et.lower(): raise ValueError('prior_profile cannot masquerade as field evidence; use Layer-7 TEPVersionManager for field evidence')
            u_factor*=float(pp.get('uncertainty_multiplier',1.0)); quality_score*=float(pp.get('quality_multiplier',1.0)); validity_score*=float(pp.get('validity_multiplier',1.0))
            if rel_adj is not None: rel_adj*=float(pp.get('reliability_multiplier',1.0))
            quality_reasons.append(f'conditioned_prior:{et}')
        quality_score=min(1.0,max(0.0,quality_score)); validity_score=min(1.0,max(0.0,validity_score));
        if rel_adj is not None: rel_adj=min(1.0,max(0.0,float(rel_adj)))
        qflag='good' if quality_score>=0.9 else ('degraded' if quality_score>=0.5 else 'invalid')
        uncertainty={
          'type':'simulation_prior_local_ensemble','representation':'std+diagonal_variance',
          'by_output':self._mul_map(tep.get('uncertainty_json',{}),u_factor),
          'variance_by_output':self._mul_map(tep.get('variance_json',{}),u_factor*u_factor),
          'source':tep.get('source','TEP'),'tep_cell_id':tep.get('cell_id'),'novelty_distance':d,'adjustment_factor':u_factor,
          'claim':'simulation-derived prior; not field-calibrated uncertainty',
        }
        posterior=tep.get('reliability_posterior',None); kind='operational_posterior' if posterior is not None else 'verification_prior_no_field_evidence'
        reliability={
          'prior':rel_adj,'posterior':posterior if posterior is not None else rel_adj,'kind':kind,
          'evidence_count':int(tep.get('evidence_count',0) or 0),'field_evidence_count':int(tep.get('posterior_evidence_count',0) or 0),
          'source':tep.get('posterior_source',tep.get('source','TEP')),'novelty_distance':d,
        }
        cp=dict(context or {}); cp.setdefault('operation_code',int(operation_code)); cp['freshness_age_s']=float(freshness_age_s); cp['tep_novelty']=bool(tep.get('novelty')); cp['tep_distance']=d
        provenance={'source':source,'fmu':self.fmu_path.name,'fmu_sha256':self._sha256,'model_version':self.model_version,'tep_version':self.tep.version,'tep_maturity':self.tep.maturity,'formula_catalog_version':self.fmu.catalog.get('catalog_version','unknown'),'reliability_claim':'operational only when a field-evidence posterior override is active; otherwise prior/simulation only','layer2_boundary':'no STD/topology/coupling/network weight/Center of Consistency'}
        packet=TwinPacket(twin_id=self.twin_id,state=state,uncertainty=uncertainty,quality={'score':quality_score,'flag':qflag,'reason':quality_reasons},validity={'score':validity_score,'reason':validity_reasons},reliability=reliability,context=cp,timestamp=float(self.fmu.t),provenance=provenance,operation_code=int(operation_code),context_id=context_id,stage_id=stage_id,tep_cell_id=tep.get('cell_id'))
        self._append_evidence(packet); return packet
    def feedback_contract(self): return dict(self.tep.contract.get('feedback_contract',{}))
