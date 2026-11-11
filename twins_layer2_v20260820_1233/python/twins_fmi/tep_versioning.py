from __future__ import annotations
import csv, json, shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

class TEPVersionManager:
    """Slow, versioned, rollback-capable external TEP posterior manager.

    It never mutates the embedded FMU TEP. A posterior release requires records
    explicitly labeled as field evidence. The active version is selected through
    an external pointer and can be passed to DomainTwin(tep_root=...).
    """
    def __init__(self,store:str|Path,twin_id:str):
        self.store=Path(store); self.twin_id=twin_id; (self.store/'versions').mkdir(parents=True,exist_ok=True)
        self.pointer=self.store/'active.json'
    def _versions(self): return sorted([p for p in (self.store/'versions').iterdir() if p.is_dir()])
    def bootstrap(self,source_tddt_root:str|Path):
        if self.pointer.exists(): return self.active_root()
        dst=self.store/'versions'/'v000_sim_prior'; shutil.copytree(Path(source_tddt_root),dst)
        self.pointer.write_text(json.dumps({'active':dst.name,'history':[dst.name]},indent=2),encoding='utf-8'); return dst
    def active_root(self):
        d=json.loads(self.pointer.read_text(encoding='utf-8')); return self.store/'versions'/d['active']
    def create_posterior(self,field_evidence:Iterable[Mapping[str,Any]],*,source:str,provenance:Mapping[str,Any]|None=None):
        ev=list(field_evidence)
        if not ev or any(str(x.get('evidence_type','')).lower() not in {'field','field_evidence','operational_field'} for x in ev):
            raise ValueError('Posterior TEP update requires explicit field evidence for every record')
        active=self.active_root(); idx=len(self._versions()); name=f'v{idx:03d}_field_posterior'; dst=self.store/'versions'/name; shutil.copytree(active,dst)
        # update Beta posterior by cell using accepted/pass evidence
        groups={}
        for x in ev: groups.setdefault(str(x['cell_id']),[]).append(x)
        cells={}
        with (dst/'tep'/'tep.csv').open(newline='',encoding='utf-8') as f:
            rows=list(csv.DictReader(f))
        for r in rows:
            cid=r['cell_id']
            if cid not in groups: continue
            prior=float(r['reliability_prior']); n0=max(2,int(float(r['evidence_count']))+2); a0=prior*n0; b0=(1-prior)*n0
            s=sum(1 for x in groups[cid] if bool(x.get('passed',x.get('accepted',False)))); n=len(groups[cid]); post=(a0+s)/(a0+b0+n)
            cells[cid]={'reliability_posterior':post,'field_evidence_count':n,'source':source,'updated_at':datetime.now(timezone.utc).isoformat()}
        override={'schema_version':'1.0','twin_id':self.twin_id,'source':source,'provenance':dict(provenance or {}),'cells':cells}
        (dst/'tep'/'posterior_overrides.json').write_text(json.dumps(override,indent=2,ensure_ascii=False),encoding='utf-8')
        man=json.loads((dst/'tep'/'manifest.json').read_text(encoding='utf-8')); man['tep_version']=name; man['maturity']='TEP_hybrid'; man['field_evidence_present']=True; man['field_evidence']=True; man['evidence_type']='hybrid_simulation_plus_field'; man['parent_version']=json.loads((active/'tep'/'manifest.json').read_text())['tep_version']; man['update_provenance']={'source':source,**dict(provenance or {})}
        (dst/'tep'/'manifest.json').write_text(json.dumps(man,indent=2,ensure_ascii=False),encoding='utf-8'); (dst/'tep'/'VERSION').write_text(name+'\n',encoding='utf-8')
        p=json.loads(self.pointer.read_text()); p.setdefault('history',[]).append(name); p['active']=name; self.pointer.write_text(json.dumps(p,indent=2),encoding='utf-8'); return dst
    def rollback(self,version_name:str):
        target=self.store/'versions'/version_name
        if not target.exists(): raise FileNotFoundError(target)
        p=json.loads(self.pointer.read_text()); p['active']=version_name; p.setdefault('rollback_events',[]).append({'to':version_name,'at':datetime.now(timezone.utc).isoformat()}); self.pointer.write_text(json.dumps(p,indent=2),encoding='utf-8'); return target
