from __future__ import annotations
import json, os, shutil, sqlite3, time, uuid
from collections import deque, OrderedDict
from pathlib import Path
from typing import Any
from .config import Layer5Config
from .utils import atomic_write_json, atomic_write_text, sha256_file, sha256_obj

class KnowledgeReferenceResolver:
    """Version-aware, read-only, bounded reference resolver for CRP/SARG/TEP (§24B).

    Layer 5 consumes resolved metadata/derivatives.  It never full-scans or rewrites the
    raw datasets. Optional guidance/TEP derivatives travel with the resolved reference.
    """
    def __init__(self,config:Layer5Config):
        self.config=config; self.cache:OrderedDict[str,dict[str,Any]]=OrderedDict()
        self.cache_max=int(config.get("knowledge.reference_cache_max",32))

    def resolve(self,refs:dict[str,Any]|None,snapshot_version:str|None=None,snapshot_hash:str|None=None)->dict[str,Any]:
        refs=refs or {}; out={}; errors=[]; warnings=[]; low_coverage=[]
        for kind in ("CRP","SARG"):
            rec=refs.get(kind) or refs.get(kind.lower())
            if not rec: continue
            rr=self._one(kind,rec,snapshot_version,snapshot_hash)
            if rr.get("error"): errors.append(rr["error"])
            else: out[kind]=rr
        tep=refs.get("TEP") or refs.get("tep")
        if tep:
            entries=[]
            if isinstance(tep,list): entries=tep
            elif isinstance(tep,dict) and any(k in tep for k in ("id","ref_id","version","hash","sha256")): entries=[tep]
            elif isinstance(tep,dict):
                for twin,rec in tep.items():
                    if isinstance(rec,dict): entries.append({"twin":twin,**rec})
            else: errors.append("TEP_reference_not_mapping_or_list")
            resolved=[]
            for rec in entries:
                rr=self._one("TEP",rec,snapshot_version,snapshot_hash)
                if rr.get("error"): errors.append(rr["error"]); continue
                cov=rec.get("coverage")
                if cov is not None:
                    try:
                        rr["coverage"]=float(cov)
                        if rr["coverage"]<float(self.config.get("knowledge.tep_coverage_min",0.0)): low_coverage.append(rr.get("twin") or rr.get("id"))
                    except Exception: warnings.append("TEP_coverage_not_numeric")
                rr["twin"]=rec.get("twin")
                rr["derivatives"]=dict(rec.get("derivatives") or {})
                rr["source_layer"]=rec.get("source_layer") or ((rec.get("provenance") or {}).get("source_layer") if isinstance(rec.get("provenance"),dict) else None)
                if self.config.get("knowledge.require_tep_layer2_provenance",True) and rr.get("source_layer") not in ("Layer2","Layer 2","L2"):
                    errors.append(f"TEP_reference_not_provenanced_from_Layer2:{rr.get('twin') or rr.get('id')}")
                    continue
                resolved.append(rr)
            if resolved: out["TEP"]=resolved
        composite={"references":out,"snapshot_version":snapshot_version,"snapshot_hash":snapshot_hash}
        version=sha256_obj({k:self._version_view(v) for k,v in out.items()})[:16] if out else None
        h=sha256_obj(composite) if out else None
        coherent=not any("snapshot_" in e for e in errors)
        use=bool(not errors and coherent and not low_coverage)
        evidence=[]
        if low_coverage: evidence.append({"kind":"TEP_LOW_COVERAGE","twins":low_coverage,"route":"Layer7"})
        if not coherent: evidence.append({"kind":"KNOWLEDGE_SNAPSHOT_INCOHERENT","route":"Layer7"})
        result={"ok":not errors,"coherent":coherent,"use_for_normal_decision":use,"knowledge_version":version,"knowledge_hash":h,
                "references":out,"errors":errors,"warnings":warnings,"low_coverage":low_coverage,"layer7_evidence":evidence,
                "lookup_method":"ID/Index/NearestRefCache","full_scan_fast":False,"cache_entries":len(self.cache),
                "runtime_write":{"CRP":False,"SARG":False,"TEP":False},"raw_rewrite_TEP":False}
        return result

    def _one(self,kind:str,rec:Any,snapshot_version:str|None,snapshot_hash:str|None)->dict[str,Any]:
        if not isinstance(rec,dict): return {"error":f"{kind}_reference_not_mapping"}
        rid=rec.get("id") or rec.get("ref_id"); ver=rec.get("version"); h=rec.get("hash") or rec.get("sha256")
        if self.config.get("knowledge.require_hashed_references",True) and (not rid or not ver or not h): return {"error":f"{kind}_reference_missing_id_version_hash"}
        if rec.get("snapshot_version") not in (None,"",snapshot_version): return {"error":f"{kind}_snapshot_version_incompatible"}
        if rec.get("snapshot_hash") not in (None,"",snapshot_hash): return {"error":f"{kind}_snapshot_hash_incompatible"}
        key=f"{kind}:{rid}:{ver}:{h}"; rr=self.cache.get(key)
        if rr is None:
            rr={"id":rid,"version":ver,"hash":h,"provenance":rec.get("provenance"),"read_only":True,
                "guidance":dict(rec.get("guidance") or {}),"lookup":str(rec.get("lookup") or "ID"),"dataset_timestamp":rec.get("timestamp"),
                "evidence_type":rec.get("evidence_type")}
            self.cache[key]=rr; self.cache.move_to_end(key)
            while len(self.cache)>max(1,self.cache_max): self.cache.popitem(last=False)
        return dict(rr)
    def _version_view(self,v):
        if isinstance(v,list): return [{"id":x.get("id"),"version":x.get("version"),"hash":x.get("hash")} for x in v]
        return {"id":v.get("id"),"version":v.get("version"),"hash":v.get("hash")}

class KStore:
    """SS/SETD KStore implementing Formalism_layer5_v4 §24A/§24B.

    FILE layout v2 deliberately avoids a single ever-growing file:
      * SS-KStore is append-only segmented JSONL under ``ss/segments``.
      * SETD-KStore is a versioned multi-file package under ``setd/packages``.
      * root manifests/pointers are small atomic metadata files only.

    The environment variable ``TDDT_KSTORE_ROOT`` has precedence over the YAML
    ``knowledge.directory`` fallback.  The public load_setd() API reconstructs the
    logical SETD snapshot so existing Layer-5 decision code does not need to know
    the physical sharding layout.
    """
    SCHEMA="KStore-record-v1"
    SS_MANIFEST_SCHEMA="SS-KStore-manifest-v2"
    SETD_MANIFEST_SCHEMA="SETD-package-manifest-v2"
    ROOT_MANIFEST_SCHEMA="KStore-manifest-v2"
    POINTER_SCHEMA="SETD-KStore-pointer-v2"

    def __init__(self,config:Layer5Config):
        self.config=config
        self.backend=str(config.get("knowledge.backend","FILE")).upper()
        env_root=config.env_path("TDDT_KSTORE_ROOT")
        configured=config.resolve(config.get("knowledge.directory","runtime/kstore"))
        self.root=(env_root or configured)
        if self.root is None:
            raise ValueError("KStore root is not configured")
        self.root_source="TDDT_KSTORE_ROOT" if env_root is not None else "knowledge.directory"
        self.root.mkdir(parents=True,exist_ok=True)
        self.buf=deque(maxlen=max(1,int(config.get("edge.knowledge_ram_records",128))))
        self.persist_queue_max=max(1,int(config.get("knowledge.persist_queue_max",64)))
        self.queue=deque()
        self.episode_id=None; self.sequence=0; self.critical_loss=False; self.last_hash=None; self.faults=[]
        self.recovery_status={"performed":False,"ok":True,"action":"none"}
        if self.backend not in {"FILE","EMBEDDED_DB"}: raise ValueError("KStore backend must be FILE or EMBEDDED_DB")
        if self.backend=="EMBEDDED_DB": self._init_db()
        else:
            (self.root/"ss"/"segments").mkdir(parents=True,exist_ok=True)
            (self.root/"ss"/"checkpoints").mkdir(parents=True,exist_ok=True)
            (self.root/"setd"/"staging").mkdir(parents=True,exist_ok=True)
            (self.root/"setd"/"packages").mkdir(parents=True,exist_ok=True)
        self._quarantine_staging(); self._resume_active(); self._recover_file_tail()

    # logical interface: Open/Append/Checkpoint/AtomicCommit/Load/Verify/Recover/Compact
    def open(self)->dict[str,Any]: return self.status()

    def _init_db(self):
        db=self.root/"kstore.sqlite3"; con=sqlite3.connect(db)
        con.execute("pragma journal_mode=WAL")
        con.execute("create table if not exists records(seq integer, episode text, kind text, payload text, record_hash text, prev_hash text, state text, committed_at real, primary key(episode,seq))")
        con.execute("create table if not exists checkpoints(id integer primary key autoincrement, episode text, payload text, sha256 text, committed_at real)")
        con.execute("create table if not exists manifests(name text primary key, payload text, committed_at real)")
        con.commit(); con.close()

    def _resume_active(self):
        active=self.root/"ACTIVE_EPISODE.json"
        if active.exists():
            try:
                a=json.loads(active.read_text(encoding="utf-8")); self.episode_id=a.get("episode_id"); self.sequence=int(a.get("sequence",0)); self.critical_loss=bool(a.get("critical_loss",False)); self.last_hash=a.get("last_hash")
            except Exception as e: self.critical_loss=True; self.faults.append(f"active_episode_parse:{e!r}")

    def start_episode(self,episode_id:str|None=None):
        if self.episode_id is not None: raise RuntimeError("single offline episode already active")
        marker=self.root/"OFFLINE_EPISODE_COMPLETED.json"
        if marker.exists(): raise RuntimeError("offline episode limit=1 already consumed")
        self.episode_id=episode_id or ("E-"+uuid.uuid4().hex[:16]); self.sequence=0; self.last_hash=None; self.critical_loss=False
        self._write_active(); return self.episode_id

    def _write_active(self):
        atomic_write_json(self.root/"ACTIVE_EPISODE.json",{"episode_id":self.episode_id,"sequence":self.sequence,"critical_loss":self.critical_loss,"last_hash":self.last_hash,"layout":"segmented-v2"})

    def append(self,record:dict[str,Any],kind:str="FAST_EXPERIENCE",cycle:int|None=None,provenance:dict[str,Any]|None=None,data_version:str|None=None):
        if self.episode_id is None: raise RuntimeError("no offline episode active")
        if len(self.queue)>=self.persist_queue_max:
            self.critical_loss=True; self.faults.append("persist_queue_overflow"); self._write_active(); raise RuntimeError("KStore persist queue overflow; critical experience not accepted")
        self.sequence+=1
        env={"seq":self.sequence,"id_E":self.episode_id,"k":cycle if cycle is not None else self.sequence,"t":time.time(),"kind":kind,
             "payload":record,"schema_version":self.SCHEMA,"model_version":str(self.config.get("governance.model_set_id")),"data_version":data_version,
             "h_prev":self.last_hash,"provenance":provenance or {},"state_commit":"STAGED"}
        env["h_payload"]=sha256_obj(record); env["record_hash"]=sha256_obj({k:v for k,v in env.items() if k not in {"record_hash","state_commit"}}); env["state_commit"]="COMMITTED"
        self.buf.append(env); self.queue.append(env)
        try:
            self._persist_record(env); self.last_hash=env["record_hash"]; self.critical_loss=False; self._write_active()
        except Exception as e:
            self.critical_loss=True; self.faults.append(f"append:{e!r}"); self._write_active(); raise
        finally:
            if self.queue and self.queue[0] is env: self.queue.popleft()
            elif env in self.queue:
                try: self.queue.remove(env)
                except ValueError: pass
        return env

    def _persist_record(self,env):
        if self.backend=="FILE":
            # FILE backend: one bounded append-only segment only; no global monolithic stream.
            line=json.dumps(env,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
            line_bytes=len(line.encode("utf-8"))
            segment_max=max(1024,int(self.config.get("knowledge.file_segment_max_bytes",1048576)))
            record_max=max(1024,int(self.config.get("knowledge.file_record_max_bytes",262144)))
            hard_max=min(segment_max,record_max)
            if line_bytes>hard_max:
                raise RuntimeError(f"KStore record exceeds FILE chunk budget: {line_bytes}>{hard_max} bytes")
            p=self._segment_path_for_append(line_bytes)
            p.parent.mkdir(parents=True,exist_ok=True)
            with p.open("a",encoding="utf-8") as f: f.write(line); f.flush(); os.fsync(f.fileno())
            self._update_ss_manifest(p,env)
        else:
            con=sqlite3.connect(self.root/"kstore.sqlite3")
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("insert into records values(?,?,?,?,?,?,?,?)",(env["seq"],env["id_E"],env["kind"],json.dumps(env,sort_keys=True),env["record_hash"],env["h_prev"],"COMMITTED",time.time()))
                con.commit()
            except Exception: con.rollback(); raise
            finally: con.close()

    def _ss_manifest_path(self)->Path: return self.root/"ss"/"SS_MANIFEST.json"
    def _load_ss_manifest(self)->dict[str,Any]:
        p=self._ss_manifest_path()
        if p.exists():
            try:
                d=json.loads(p.read_text(encoding="utf-8"))
                if d.get("schema")==self.SS_MANIFEST_SCHEMA and isinstance(d.get("segments"),list): return d
            except Exception: pass
        return {"schema":self.SS_MANIFEST_SCHEMA,"episode_id":self.episode_id,"last_sequence":0,"last_hash":None,"segments":[],"checkpoint":None,"committed":True}

    def _segment_path_for_append(self,line_bytes:int)->Path:
        m=self._load_ss_manifest(); segs=m.get("segments") or []
        max_records=max(1,int(self.config.get("knowledge.file_segment_records",256)))
        max_bytes=max(1024,int(self.config.get("knowledge.file_segment_max_bytes",1048576)))
        idx=0
        if segs:
            cur=segs[-1]; idx=int(cur.get("index",len(segs)-1))
            if int(cur.get("records",0))>=max_records or int(cur.get("bytes",0))+line_bytes>max_bytes: idx+=1
        return self.root/"ss"/"segments"/f"segment_{idx:06d}.jsonl"

    def _update_ss_manifest(self,segment_path:Path,env:dict[str,Any]):
        m=self._load_ss_manifest(); rel=str(segment_path.relative_to(self.root)); segs=m.setdefault("segments",[])
        found=None
        for s in segs:
            if s.get("file")==rel: found=s; break
        if found is None:
            found={"index":len(segs),"file":rel,"first_seq":env["seq"],"last_seq":env["seq"],"records":0,"bytes":0,"sha256":None,"prev_segment_sha256":segs[-1].get("sha256") if segs else None}
            segs.append(found)
        found["records"]=int(found.get("records",0))+1; found["last_seq"]=env["seq"]; found["bytes"]=segment_path.stat().st_size; found["sha256"]=sha256_file(segment_path)
        m.update({"episode_id":self.episode_id,"last_sequence":self.sequence,"last_hash":env["record_hash"],"segment_records_max":max(1,int(self.config.get("knowledge.file_segment_records",256))),"segment_bytes_max":max(1024,int(self.config.get("knowledge.file_segment_max_bytes",1048576))),"committed":True})
        atomic_write_json(self._ss_manifest_path(),m)

    def checkpoint(self,summary:dict[str,Any],kind:str="EPISODE_SUMMARY")->dict[str,Any]:
        if self.episode_id is None: raise RuntimeError("no offline episode active")
        payload={"schema":"KStore-checkpoint-v1","episode_id":self.episode_id,"sequence":self.sequence,"kind":kind,"summary":summary,"created_at":time.time()}; payload["sha256"]=sha256_obj({k:v for k,v in payload.items() if k!="sha256"})
        if self.backend=="FILE":
            p=self.root/"ss"/"checkpoints"/f"checkpoint_{self.sequence:08d}.json"; atomic_write_json(p,payload)
            m=self._load_ss_manifest(); m["checkpoint"]={"file":str(p.relative_to(self.root)),"sequence":self.sequence,"sha256":sha256_file(p)}; atomic_write_json(self._ss_manifest_path(),m)
        else:
            con=sqlite3.connect(self.root/"kstore.sqlite3")
            try:
                con.execute("BEGIN IMMEDIATE"); con.execute("insert into checkpoints(episode,payload,sha256,committed_at) values(?,?,?,?)",(self.episode_id,json.dumps(payload,sort_keys=True),payload["sha256"],time.time())); con.commit()
            except Exception: con.rollback(); raise
            finally: con.close()
        return payload

    def _slug(self,key:str)->str:
        s="".join(c if c.isalnum() or c in "-_" else "_" for c in str(key)).strip("_")
        return s[:96] or "component"

    def _write_json_component(self,base:Path,key:str,value:Any)->dict[str,Any]:
        """Write one logical knowledge component using strictly bounded physical chunks.

        Lists and dictionaries are split semantically where possible.  If one element,
        entry, scalar or wrapper would still exceed the byte budget, the canonical JSON
        byte stream is split into raw byte chunks and reconstructed only after all hashes
        verify.  This keeps every data part bounded without changing the logical object.
        """
        slug=self._slug(key)
        max_items=max(1,int(self.config.get("knowledge.setd_component_items",128)))
        max_bytes=max(4096,int(self.config.get("knowledge.setd_component_max_bytes",1048576)))
        logical_hash=sha256_obj(value); files=[]

        def json_bytes(obj:Any)->bytes:
            return (json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n").encode("utf-8")

        def emit_json(path:Path,obj:Any,count:int|None=None):
            raw=json_bytes(obj)
            if len(raw)>max_bytes:
                raise ValueError("component_part_exceeds_byte_budget")
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(raw)
            files.append({"file":str(path.relative_to(base.parent)),"sha256":sha256_file(path),"bytes":path.stat().st_size,"items":count})

        def emit_raw(path:Path,raw:bytes):
            if len(raw)>max_bytes:
                raise ValueError("raw_component_part_exceeds_byte_budget")
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(raw)
            files.append({"file":str(path.relative_to(base.parent)),"sha256":sha256_file(path),"bytes":path.stat().st_size,"items":None})

        def raw_fallback()->dict[str,Any]:
            files.clear(); d=base/slug; d.mkdir(parents=True,exist_ok=True)
            raw=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8")
            for idx,off in enumerate(range(0,len(raw),max_bytes)):
                emit_raw(d/f"part_{idx:06d}.bin",raw[off:off+max_bytes])
            if not files: emit_raw(d/"part_000000.bin",b"null")
            return {"key":key,"kind":"json_bytes_sharded","logical_sha256":logical_hash,
                    "item_count":len(value) if isinstance(value,(list,dict)) else None,
                    "encoding":"utf-8-json","files":files}

        # Small components remain one JSON file only if the physical wrapper also fits.
        single={"schema":"SETD-component-v1","key":key,"value":value}
        if (not isinstance(value,(list,dict)) or len(value)<=max_items) and len(json_bytes(single))<=max_bytes:
            p=base/f"{slug}.json"; emit_json(p,single,len(value) if isinstance(value,(list,dict)) else None)
            return {"key":key,"kind":"single","logical_sha256":logical_hash,"item_count":len(value) if isinstance(value,(list,dict)) else None,"files":files}

        if isinstance(value,list):
            d=base/slug; d.mkdir(parents=True,exist_ok=True); part=[]; part_idx=0
            def flush_list()->bool:
                nonlocal part,part_idx
                if not part: return True
                obj={"schema":"SETD-list-part-v1","key":key,"part":part_idx,"items":part}
                try: emit_json(d/f"part_{part_idx:06d}.json",obj,len(part))
                except ValueError: return False
                part=[]; part_idx+=1; return True
            for item in value:
                trial=part+[item]
                trial_obj={"schema":"SETD-list-part-v1","key":key,"part":part_idx,"items":trial}
                if part and (len(trial)>max_items or len(json_bytes(trial_obj))>max_bytes):
                    if not flush_list(): return raw_fallback()
                    trial=[item]; trial_obj={"schema":"SETD-list-part-v1","key":key,"part":part_idx,"items":trial}
                if len(json_bytes(trial_obj))>max_bytes: return raw_fallback()
                part=trial
            if not flush_list(): return raw_fallback()
            return {"key":key,"kind":"list_sharded","logical_sha256":logical_hash,"item_count":len(value),"files":files}

        if isinstance(value,dict):
            d=base/slug; d.mkdir(parents=True,exist_ok=True); part={}; part_idx=0
            def flush_dict()->bool:
                nonlocal part,part_idx
                if not part: return True
                obj={"schema":"SETD-dict-part-v1","key":key,"part":part_idx,"entries":part}
                try: emit_json(d/f"part_{part_idx:06d}.json",obj,len(part))
                except ValueError: return False
                part={}; part_idx+=1; return True
            for k,v in value.items():
                trial=dict(part); trial[k]=v
                trial_obj={"schema":"SETD-dict-part-v1","key":key,"part":part_idx,"entries":trial}
                if part and (len(trial)>max_items or len(json_bytes(trial_obj))>max_bytes):
                    if not flush_dict(): return raw_fallback()
                    trial={k:v}; trial_obj={"schema":"SETD-dict-part-v1","key":key,"part":part_idx,"entries":trial}
                if len(json_bytes(trial_obj))>max_bytes: return raw_fallback()
                part=trial
            if not flush_dict(): return raw_fallback()
            return {"key":key,"kind":"dict_sharded","logical_sha256":logical_hash,"item_count":len(value),"files":files}

        return raw_fallback()

    def _read_component(self,package_dir:Path,desc:dict[str,Any])->Any:
        kind=desc.get("kind")
        if kind=="json_bytes_sharded":
            parts=[]
            for f in desc.get("files") or []:
                p=package_dir/f["file"]
                if not p.exists() or sha256_file(p)!=f.get("sha256"): raise ValueError(f"component_hash:{p}")
                parts.append(p.read_bytes())
            try: value=json.loads(b"".join(parts).decode("utf-8"))
            except Exception as e: raise ValueError("component_json_bytes_decode") from e
        else:
            vals=[]
            for f in desc.get("files") or []:
                p=package_dir/f["file"]
                if not p.exists() or sha256_file(p)!=f.get("sha256"): raise ValueError(f"component_hash:{p}")
                vals.append(json.loads(p.read_text(encoding="utf-8")))
            if kind=="single": value=vals[0].get("value") if vals else None
            elif kind=="list_sharded":
                value=[]
                for x in vals: value.extend(x.get("items") or [])
            elif kind=="dict_sharded":
                value={}
                for x in vals: value.update(x.get("entries") or {})
            else: raise ValueError("unknown_component_kind")
        if sha256_obj(value)!=desc.get("logical_sha256"): raise ValueError("component_logical_hash")
        return value

    def _fsync_tree(self,root:Path):
        for p in sorted(root.rglob("*")):
            if p.is_file():
                with p.open("rb") as f: os.fsync(f.fileno())
        # durable directory metadata on POSIX when supported
        for p in [x for x in root.rglob("*") if x.is_dir()]+[root]:
            try:
                fd=os.open(p,os.O_RDONLY); os.fsync(fd); os.close(fd)
            except Exception: pass

    def _build_setd_package(self,stage:dict[str,Any],package_id:str,staging:Path)->dict[str,Any]:
        components=staging/"components"; components.mkdir(parents=True,exist_ok=True)
        header={k:v for k,v in stage.items() if k not in {"knowledge","content_sha256"}}
        atomic_write_json(staging/"header.json",header)
        descriptors=[]
        for key,value in (stage.get("knowledge") or {}).items(): descriptors.append(self._write_json_component(components,key,value))
        manifest={"schema":self.SETD_MANIFEST_SCHEMA,"package_id":package_id,"state":"COMMITTED","episode_id":stage.get("episode_id"),"experience_count":stage.get("experience_count"),
                  "created_at":stage.get("created_at"),"header_file":"header.json","header_sha256":sha256_file(staging/"header.json"),"logical_content_sha256":stage.get("content_sha256"),
                  "components":descriptors,"layout":"multi-file-sharded","raw_training_trajectory_in_setd":False,"loadable":True}
        manifest["manifest_content_sha256"]=sha256_obj({k:v for k,v in manifest.items() if k!="manifest_content_sha256"})
        atomic_write_json(staging/"manifest.json",manifest); self._fsync_tree(staging); return manifest

    def _load_package_stage(self,package_dir:Path)->dict[str,Any]:
        mf=json.loads((package_dir/"manifest.json").read_text(encoding="utf-8"))
        if mf.get("schema")!=self.SETD_MANIFEST_SCHEMA or mf.get("state")!="COMMITTED": raise ValueError("setd_manifest_schema_or_state")
        chk=sha256_obj({k:v for k,v in mf.items() if k!="manifest_content_sha256"})
        if chk!=mf.get("manifest_content_sha256"): raise ValueError("setd_manifest_content_hash")
        hp=package_dir/mf.get("header_file","header.json")
        if not hp.exists() or sha256_file(hp)!=mf.get("header_sha256"): raise ValueError("setd_header_hash")
        stage=json.loads(hp.read_text(encoding="utf-8")); knowledge={}
        for desc in mf.get("components") or []: knowledge[desc.get("key")]=self._read_component(package_dir,desc)
        stage["knowledge"]=knowledge; stage["content_sha256"]=mf.get("logical_content_sha256")
        if stage["content_sha256"]!=sha256_obj({k:v for k,v in stage.items() if k!="content_sha256"}): raise ValueError("setd_logical_content_hash")
        return stage

    def _verify_package_dir(self,p:Path)->bool:
        try: self._load_package_stage(p); return True
        except Exception: return False

    def finalize(self,knowledge:dict[str,Any])->dict[str,Any]:
        if self.episode_id is None: raise RuntimeError("no offline episode active")
        if self.sequence < int(self.config.get("knowledge.min_experience_records",1)): raise RuntimeError("offline episode incomplete: no critical experience records")
        if self.critical_loss: raise RuntimeError("critical experience loss; episode incomplete")
        if self.backend=="FILE":
            ss=self.verify_ss()
            if not ss.get("ok"): raise RuntimeError("SS-KStore verification failed before SETD commit: "+";".join(ss.get("reasons") or []))
        stage={"schema":"SETD-KStore-v2","episode_id":self.episode_id,"experience_count":self.sequence,"knowledge":knowledge,"created_at":time.time(),
               "policy_version":self.config.get("governance.policy_version"),"model_version":self.config.get("governance.model_set_id"),"last_record_hash":self.last_hash,
               "source_ss_manifest_sha256":sha256_file(self._ss_manifest_path()) if self.backend=="FILE" and self._ss_manifest_path().exists() else None}
        stage["content_sha256"]=sha256_obj({k:v for k,v in stage.items() if k!="content_sha256"})
        package_id=f"SETD_{self.episode_id}_{int(stage['created_at']*1e6)}"
        if self.backend=="FILE":
            staging=self.root/"setd"/"staging"/(package_id+".stage"); final_dir=self.root/"setd"/"packages"/package_id
            if staging.exists(): shutil.rmtree(staging)
            staging.mkdir(parents=True,exist_ok=False); manifest=self._build_setd_package(stage,package_id,staging)
            if not self._verify_package_dir(staging): self._quarantine(staging,"setd_staging_verify"); raise RuntimeError("SETD staging verification failed")
            os.replace(staging,final_dir)
            try:
                fd=os.open(final_dir.parent,os.O_RDONLY); os.fsync(fd); os.close(fd)
            except Exception: pass
            pointer={"schema":self.POINTER_SCHEMA,"package_id":package_id,"manifest_file":str((final_dir/"manifest.json").relative_to(self.root)),"manifest_sha256":sha256_file(final_dir/"manifest.json"),"logical_content_sha256":stage["content_sha256"]}
            atomic_write_json(self.root/"SETD_KSTORE.json",pointer)  # small pointer only; never the full knowledge object
            root_manifest={"schema":self.ROOT_MANIFEST_SCHEMA,"backend":"FILE","setd_pointer":"SETD_KSTORE.json","setd_pointer_sha256":sha256_file(self.root/"SETD_KSTORE.json"),
                           "package_id":package_id,"package_manifest":pointer["manifest_file"],"package_manifest_sha256":pointer["manifest_sha256"],"episode_id":self.episode_id,
                           "experience_count":self.sequence,"last_sequence":self.sequence,"last_record_hash":self.last_hash,"ss_manifest":"ss/SS_MANIFEST.json","ss_manifest_sha256":sha256_file(self._ss_manifest_path()),
                           "committed":True,"committed_at":time.time(),"layout":"segmented-ss+multi-file-setd-v2","knowledge_quality":dict((knowledge or {}).get("knowledge_quality") or {})}
            atomic_write_json(self.root/"MANIFEST.json",root_manifest); manifest=root_manifest
        else:
            # DB backend retains transactional logical snapshot; file sharding applies to FILE backend only.
            payload=json.dumps(stage,sort_keys=True,ensure_ascii=False)
            manifest={"schema":self.ROOT_MANIFEST_SCHEMA,"backend":"EMBEDDED_DB","episode_id":self.episode_id,"experience_count":self.sequence,"last_sequence":self.sequence,
                      "last_record_hash":self.last_hash,"setd_content_sha256":stage["content_sha256"],"committed":True,"committed_at":time.time(),"knowledge_quality":dict((knowledge or {}).get("knowledge_quality") or {})}
            con=sqlite3.connect(self.root/"kstore.sqlite3")
            try:
                con.execute("BEGIN IMMEDIATE"); con.execute("insert or replace into manifests values(?,?,?)",("SETD:"+self.episode_id,payload,time.time())); con.execute("insert or replace into manifests values(?,?,?)",("CURRENT",json.dumps(manifest,sort_keys=True),time.time())); con.commit()
            except Exception: con.rollback(); raise
            finally: con.close()
            atomic_write_json(self.root/"MANIFEST.json",manifest)
        atomic_write_json(self.root/"OFFLINE_EPISODE_COMPLETED.json",{"episode_id":self.episode_id,"manifest_sha256":sha256_file(self.root/"MANIFEST.json"),"knowledge_quality":dict((knowledge or {}).get("knowledge_quality") or {})})
        active=self.root/"ACTIVE_EPISODE.json"
        if active.exists(): active.unlink()
        self.episode_id=None; return manifest

    def verify_ss(self)->dict[str,Any]:
        if self.backend!="FILE": return {"ok":True,"backend":"EMBEDDED_DB","reasons":[]}
        p=self._ss_manifest_path(); reasons=[]
        if not p.exists(): return {"ok":False,"reasons":["ss_manifest_missing"]}
        try: m=json.loads(p.read_text(encoding="utf-8"))
        except Exception: return {"ok":False,"reasons":["ss_manifest_parse"]}
        if m.get("schema")!=self.SS_MANIFEST_SCHEMA: reasons.append("ss_manifest_schema")
        prev_record=None; expected_seq=1; previous_segment_sha=None; total=0
        for seg in m.get("segments") or []:
            sp=self.root/seg.get("file","")
            if not sp.exists(): reasons.append("segment_missing:"+str(seg.get("file"))); continue
            if sha256_file(sp)!=seg.get("sha256"): reasons.append("segment_hash:"+str(seg.get("file"))); continue
            if seg.get("prev_segment_sha256")!=previous_segment_sha: reasons.append("segment_chain:"+str(seg.get("file")))
            previous_segment_sha=seg.get("sha256")
            count=0
            for line in sp.read_text(encoding="utf-8",errors="strict").splitlines():
                try:
                    d=json.loads(line); chk=sha256_obj({k:v for k,v in d.items() if k not in {"record_hash","state_commit"}})
                    if d.get("state_commit")!="COMMITTED" or chk!=d.get("record_hash") or d.get("h_prev")!=prev_record or int(d.get("seq"))!=expected_seq: raise ValueError("record_chain")
                    prev_record=d.get("record_hash"); expected_seq+=1; count+=1; total+=1
                except Exception: reasons.append("record_invalid:"+str(seg.get("file"))); break
            if count!=int(seg.get("records",count)): reasons.append("segment_record_count:"+str(seg.get("file")))
        if total!=int(m.get("last_sequence",total)): reasons.append("ss_last_sequence")
        if total and prev_record!=m.get("last_hash"): reasons.append("ss_last_hash")
        return {"ok":not reasons,"reasons":reasons,"manifest":m,"records":total,"segments":len(m.get("segments") or [])}

    def verify(self)->dict[str,Any]:
        mf=self.root/"MANIFEST.json"; reasons=[]; m={}
        if not mf.exists(): reasons.append("manifest_missing")
        if not reasons:
            try: m=json.loads(mf.read_text(encoding="utf-8"))
            except Exception: reasons.append("manifest_parse")
        if not reasons and not m.get("committed"): reasons.append("manifest_not_committed")
        if not reasons and self.backend=="FILE":
            ptr=self.root/m.get("setd_pointer","SETD_KSTORE.json")
            if not ptr.exists(): reasons.append("setd_pointer_missing")
            elif sha256_file(ptr)!=m.get("setd_pointer_sha256"): reasons.append("setd_pointer_hash")
            pm=self.root/m.get("package_manifest","")
            if not pm.exists(): reasons.append("package_manifest_missing")
            elif sha256_file(pm)!=m.get("package_manifest_sha256"): reasons.append("package_manifest_hash")
            else:
                package_dir=pm.parent
                if not self._verify_package_dir(package_dir): reasons.append("setd_package_invalid")
            ss=self.verify_ss()
            if not ss.get("ok"): reasons.extend(["ss:"+x for x in ss.get("reasons") or []])
        return {"ok":not reasons,"reasons":reasons,"manifest":m if not reasons else None}

    def load_setd(self)->dict[str,Any]:
        v=self.verify()
        if not v["ok"]:
            rec=self.recover()
            if not rec.get("ok"): return {"ok":False,"reason":";".join(v["reasons"]),"recovery":rec}
            v=self.verify()
            if not v.get("ok"): return {"ok":False,"reason":"post_recovery_verify_failed","recovery":self.recovery_status}
        m=json.loads((self.root/"MANIFEST.json").read_text(encoding="utf-8"))
        if self.backend=="FILE":
            stage=self._load_package_stage((self.root/m["package_manifest"]).parent)
        else:
            con=sqlite3.connect(self.root/"kstore.sqlite3")
            try:
                row=con.execute("select payload from manifests where name=?",("SETD:"+str(m.get("episode_id")),)).fetchone()
            finally: con.close()
            if not row: return {"ok":False,"reason":"db_setd_missing","recovery":self.recovery_status}
            stage=json.loads(row[0])
        return {"ok":True,"manifest":m,"knowledge":stage,"reason":None,"recovery":self.recovery_status}

    def recover(self)->dict[str,Any]:
        self._quarantine_staging()
        if self.backend=="EMBEDDED_DB":
            v=self.verify(); self.recovery_status={"performed":True,"ok":bool(v.get("ok")),"action":"db_transactional_state" if v.get("ok") else "no_valid_committed_snapshot"}; return self.recovery_status
        valid=[]; packages=self.root/"setd"/"packages"
        for p in sorted([x for x in packages.iterdir() if x.is_dir()],key=lambda x:x.stat().st_mtime,reverse=True) if packages.exists() else []:
            if self._verify_package_dir(p): valid.append(p)
            else: self._quarantine(p,"invalid_setd_package")
        if not valid:
            self.recovery_status={"performed":True,"ok":False,"action":"no_valid_committed_snapshot"}; return self.recovery_status
        p=valid[0]; pm=p/"manifest.json"; md=json.loads(pm.read_text(encoding="utf-8")); stage=self._load_package_stage(p)
        pointer={"schema":self.POINTER_SCHEMA,"package_id":md.get("package_id"),"manifest_file":str(pm.relative_to(self.root)),"manifest_sha256":sha256_file(pm),"logical_content_sha256":md.get("logical_content_sha256")}
        atomic_write_json(self.root/"SETD_KSTORE.json",pointer)
        ss_sha=sha256_file(self._ss_manifest_path()) if self._ss_manifest_path().exists() else None
        mf={"schema":self.ROOT_MANIFEST_SCHEMA,"backend":"FILE","setd_pointer":"SETD_KSTORE.json","setd_pointer_sha256":sha256_file(self.root/"SETD_KSTORE.json"),"package_id":md.get("package_id"),
            "package_manifest":str(pm.relative_to(self.root)),"package_manifest_sha256":sha256_file(pm),"episode_id":stage.get("episode_id"),"experience_count":stage.get("experience_count"),
            "last_sequence":stage.get("experience_count"),"last_record_hash":stage.get("last_record_hash"),"ss_manifest":"ss/SS_MANIFEST.json","ss_manifest_sha256":ss_sha,"committed":True,"recovered_at":time.time(),"layout":"segmented-ss+multi-file-setd-v2","knowledge_quality":dict(((stage.get("knowledge") or {}).get("knowledge_quality") or {}))}
        atomic_write_json(self.root/"MANIFEST.json",mf); self.recovery_status={"performed":True,"ok":True,"action":"load_latest_committed_verified","package_dir":str(p)}; return self.recovery_status

    def _quarantine_staging(self):
        # root *.stage files from v1 plus v2 staging directories are never visible knowledge.
        for p in self.root.glob("*.stage"): self._quarantine(p,"incomplete_tail")
        sd=self.root/"setd"/"staging"
        for p in list(sd.iterdir()) if sd.exists() else []: self._quarantine(p,"incomplete_setd_staging")

    def _quarantine(self,p:Path,reason:str):
        try:
            q=self.root/"quarantine"; q.mkdir(exist_ok=True); dst=q/(p.name+f".{reason}.{int(time.time()*1e6)}"); os.replace(p,dst)
        except Exception as e: self.faults.append(f"quarantine:{e!r}")

    def _recover_file_tail(self):
        if self.backend!="FILE": return
        segdir=self.root/"ss"/"segments"
        segs=sorted(segdir.glob("segment_*.jsonl")) if segdir.exists() else []
        if not segs: return
        prev=None; expected=1; last_good_hash=None; last_good_seq=0; repaired=False
        for pos,p in enumerate(segs):
            good=[]; bad=False
            for line in p.read_text(encoding="utf-8",errors="replace").splitlines():
                try:
                    d=json.loads(line); chk=sha256_obj({k:v for k,v in d.items() if k not in {"record_hash","state_commit"}})
                    if d.get("state_commit")!="COMMITTED" or chk!=d.get("record_hash") or d.get("h_prev")!=prev or int(d.get("seq"))!=expected: raise ValueError("record_chain")
                    good.append(line); prev=d.get("record_hash"); last_good_hash=prev; last_good_seq=expected; expected+=1
                except Exception: bad=True; break
            if bad:
                repaired=True; self._quarantine(p,"incomplete_tail_segment")
                if good: atomic_write_text(p,"\n".join(good)+"\n")
                for later in segs[pos+1:]: self._quarantine(later,"after_incomplete_tail")
                break
        if repaired:
            self.sequence=last_good_seq; self.last_hash=last_good_hash; self.recovery_status={"performed":True,"ok":True,"action":"quarantine_incomplete_segment_tail","last_sequence":last_good_seq}
            # rebuild manifest from surviving valid segments
            self._rebuild_ss_manifest();
            if self.episode_id is not None: self._write_active()

    def _rebuild_ss_manifest(self):
        segdir=self.root/"ss"/"segments"; segs=[]; prev_seg=None; total=0; last_hash=None; first=1
        for idx,p in enumerate(sorted(segdir.glob("segment_*.jsonl")) if segdir.exists() else []):
            lines=p.read_text(encoding="utf-8").splitlines(); count=len(lines)
            if count:
                first_seq=json.loads(lines[0]).get("seq"); last=json.loads(lines[-1]); last_seq=last.get("seq"); last_hash=last.get("record_hash")
            else: first_seq=first; last_seq=first-1
            sh=sha256_file(p); segs.append({"index":idx,"file":str(p.relative_to(self.root)),"first_seq":first_seq,"last_seq":last_seq,"records":count,"bytes":p.stat().st_size,"sha256":sh,"prev_segment_sha256":prev_seg}); prev_seg=sh; total+=count; first=last_seq+1
        m={"schema":self.SS_MANIFEST_SCHEMA,"episode_id":self.episode_id,"last_sequence":total,"last_hash":last_hash,"segments":segs,"checkpoint":None,"segment_records_max":max(1,int(self.config.get("knowledge.file_segment_records",256))),"segment_bytes_max":max(1024,int(self.config.get("knowledge.file_segment_max_bytes",1048576))),"committed":True}
        atomic_write_json(self._ss_manifest_path(),m)

    def compact(self)->dict[str,Any]:
        # Compact is intentionally outside the hard real-time path. It never rewrites SS history into one file.
        keep=max(1,int(self.config.get("knowledge.setd_keep_versions",3))); removed=[]
        if self.backend=="FILE":
            packages=self.root/"setd"/"packages"; valid=sorted([p for p in packages.iterdir() if p.is_dir() and self._verify_package_dir(p)],key=lambda x:x.stat().st_mtime,reverse=True) if packages.exists() else []
            for p in valid[keep:]:
                self._quarantine(p,"retention_compact"); removed.append(p.name)
        s={"schema":"KStore-compact-v2","backend":self.backend,"sequence":self.sequence,"buffer_records":len(self.buf),"removed_old_setd_packages":removed,"single_file_compaction":False,"created_at":time.time()}; s["sha256"]=sha256_obj({k:v for k,v in s.items() if k!="sha256"}); atomic_write_json(self.root/"COMPACT_SUMMARY.json",s); return s

    def status(self):
        ss=self._load_ss_manifest() if self.backend=="FILE" else {}
        completed_marker=self.root/"OFFLINE_EPISODE_COMPLETED.json"
        return {"backend":self.backend,"root":str(self.root),"root_source":self.root_source,"environment_variable":"TDDT_KSTORE_ROOT","layout":"segmented-ss+multi-file-setd-v2" if self.backend=="FILE" else "transactional-db",
                "episode_id":self.episode_id,"sequence":self.sequence,"buffer_records":len(self.buf),"buffer_capacity":self.buf.maxlen,"queue_records":len(self.queue),"queue_capacity":self.persist_queue_max,
                "offline_episode_completed":completed_marker.exists(),"offline_episode_startable":bool(self.episode_id is None and not completed_marker.exists()),
                "critical_loss":self.critical_loss,"health":not self.critical_loss,"faults":list(self.faults),"last_record_hash":self.last_hash,"recovery":self.recovery_status,
                "interface":["Open","Append","Checkpoint","AtomicCommit","Load","Verify","Recover","Compact"],"full_dataset_in_ram":False,"monolithic_ss_stream":False,
                "ss_segments":len(ss.get("segments") or []),"ss_segment_records_max":max(1,int(self.config.get("knowledge.file_segment_records",256))),
                "ss_segment_bytes_max":max(1024,int(self.config.get("knowledge.file_segment_max_bytes",1048576))),"ss_record_bytes_max":max(1024,int(self.config.get("knowledge.file_record_max_bytes",262144))),
                "setd_multi_file":self.backend=="FILE","setd_component_items_max":max(1,int(self.config.get("knowledge.setd_component_items",128))),
                "setd_component_bytes_max":max(4096,int(self.config.get("knowledge.setd_component_max_bytes",1048576))),"online_persistent_writes":False}
