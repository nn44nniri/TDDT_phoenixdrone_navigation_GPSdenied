from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
import threading
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


def _json_clean(obj: Any) -> Any:
    """Convert NaN/Inf and numpy-like scalar values to strict JSON-safe values."""
    import math
    if obj is None or isinstance(obj,(str,bool,int)):
        return obj
    if isinstance(obj,float):
        return obj if math.isfinite(obj) else None
    # numpy/pandas scalar compatibility without making them hard dependencies.
    if hasattr(obj,'item') and not isinstance(obj,(bytes,bytearray)):
        try:
            return _json_clean(obj.item())
        except Exception:
            pass
    if isinstance(obj,dict):
        return {str(k):_json_clean(v) for k,v in obj.items()}
    if isinstance(obj,(list,tuple,set)):
        return [_json_clean(v) for v in obj]
    try:
        v=float(obj)
        return v if math.isfinite(v) else None
    except Exception:
        return str(obj)


def write_json_gz(path: str | Path, obj: Any, *, indent: int | None = None) -> Path:
    """Atomically write UTF-8 JSON compressed with gzip."""
    p = Path(path)
    if p.suffix != ".gz":
        p = Path(str(p) + ".gz")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + p.name + ".", suffix=".tmp", dir=str(p.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(_json_clean(obj), f, ensure_ascii=False, sort_keys=(indent is None), indent=indent, allow_nan=False)
            f.write("\n")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    return p


def read_json_auto(path: str | Path) -> Any:
    """Read JSON from .gz or plain legacy JSON without changing callers."""
    p = Path(path)
    candidates = [p]
    if p.suffix == ".gz":
        candidates.append(p.with_suffix(""))
    else:
        candidates.append(Path(str(p) + ".gz"))
    for q in candidates:
        if not q.is_file():
            continue
        if q.suffix == ".gz":
            with gzip.open(q, "rt", encoding="utf-8") as f:
                return json.load(f)
        with q.open("r", encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError(str(p))


def iter_jsonl_auto(path: str | Path) -> Iterable[dict[str, Any]]:
    """Yield JSONL records from gzip or plain legacy stream."""
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


class GzipChunkStore:
    """Bounded append-only JSONL chunks compressed at rest.

    The manifest and latest snapshot are also gzip-compressed.  Appending to a gzip
    stream creates a standards-compliant concatenated gzip member, which Python's
    gzip reader transparently reconstructs.
    """
    def __init__(self, root: str | Path, *, max_records: int = 64, max_uncompressed_bytes: int = 1_048_576):
        self.root = Path(root)
        self.chunks = self.root / "chunks"
        self.manifest_path = self.root / "manifest.json.gz"
        self.latest_path = self.root / "latest.json.gz"
        self.max_records = max(1, int(max_records))
        self.max_uncompressed_bytes = max(1024, int(max_uncompressed_bytes))
        self.chunks.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".append.lock"
        self._thread_lock = threading.RLock()

    def _manifest(self) -> dict[str, Any]:
        if self.manifest_path.is_file():
            try:
                return read_json_auto(self.manifest_path)
            except Exception:
                pass
        return {"schema": "TDOC-gzip-chunk-manifest-v1", "chunks": [], "records": 0,
                "max_records": self.max_records, "max_uncompressed_bytes": self.max_uncompressed_bytes}

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        # Serialize writers within the process and, on POSIX, across processes.
        # This protects chunk/manifest/latest updates when two TDOC workers accidentally
        # target the same runtime root.
        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as lockf:
                try:
                    import fcntl
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                except Exception:
                    fcntl = None
                line = json.dumps(_json_clean(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
                b = len(line.encode("utf-8"))
                m = self._manifest()
                entries = m.setdefault("chunks", [])
                if entries:
                    cur = entries[-1]
                    if int(cur.get("records", 0)) >= self.max_records or int(cur.get("uncompressed_bytes", 0)) + b > self.max_uncompressed_bytes:
                        cur = None
                else:
                    cur = None
                if cur is None:
                    idx = int(entries[-1]["index"] + 1) if entries else 0
                    cur = {"index": idx, "file": f"chunks/chunk_{idx:06d}.jsonl.gz", "records": 0, "uncompressed_bytes": 0}
                    entries.append(cur)
                p = self.root / cur["file"]
                p.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(p, "at", encoding="utf-8", compresslevel=6) as f:
                    f.write(line)
                cur["records"] = int(cur.get("records", 0)) + 1
                cur["uncompressed_bytes"] = int(cur.get("uncompressed_bytes", 0)) + b
                cur["compressed_bytes"] = p.stat().st_size
                m["records"] = int(m.get("records", 0)) + 1
                m["max_records"] = self.max_records
                m["max_uncompressed_bytes"] = self.max_uncompressed_bytes
                write_json_gz(self.manifest_path, m, indent=2)
                write_json_gz(self.latest_path, record, indent=2)
                # Closing lockf releases the POSIX advisory lock on return.
                return {"chunk": str(p), "chunk_index": cur["index"], "record_index": cur["records"] - 1,
                        "records_total": m["records"], "compressed": True}

    def iter_records(self) -> Iterable[dict[str, Any]]:
        for p in sorted(self.chunks.glob("chunk_*.jsonl.gz")):
            yield from iter_jsonl_auto(p)
        # Backward-compatible legacy chunks if a deployment is upgraded in-place.
        for p in sorted(self.chunks.glob("chunk_*.jsonl")):
            yield from iter_jsonl_auto(p)

    def latest(self) -> dict[str, Any] | None:
        try:
            return read_json_auto(self.latest_path)
        except FileNotFoundError:
            return None


class CompressedAtRestDirectory:
    """Compatibility facade for external FILE KStores that only understand plain JSON/JSONL.

    TDOC materializes exact original bytes immediately before invoking the external
    package and gzip-compresses them again immediately after the call.  Therefore
    external package hashes/manifests remain valid while persistent storage stays
    gzip-compressed at rest.
    """
    def __init__(self, root: str | Path, *, extensions=(".json", ".jsonl")):
        self.root = Path(root)
        self.extensions = set(extensions)
        self.root.mkdir(parents=True, exist_ok=True)

    def materialize(self) -> int:
        n = 0
        for gz in sorted(self.root.rglob("*.gz")):
            target = gz.with_suffix("")
            # Only restore files TDOC itself compressed from recognized KStore types.
            if target.suffix not in self.extensions or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name("." + target.name + ".tmp")
            with gzip.open(gz, "rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)
            n += 1
        return n

    def compress(self) -> dict[str, Any]:
        files = 0
        before = 0
        after = 0
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or p.suffix not in self.extensions or p.name.startswith("."):
                continue
            raw_size = p.stat().st_size
            gz = Path(str(p) + ".gz")
            tmp = gz.with_name("." + gz.name + ".tmp")
            with p.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, gz)
            p.unlink()
            files += 1
            before += raw_size
            after += gz.stat().st_size
        return {"files_compressed": files, "raw_bytes": before, "gzip_bytes": after,
                "saved_bytes": max(0, before - after), "compressed_at_rest": True}

    @contextmanager
    def materialized(self):
        self.materialize()
        try:
            yield self.root
        finally:
            self.compress()


class LayerRuntimeArchive:
    """Per-layer compressed chunk archive for cycle-visible runtime evidence.

    To avoid duplicating the large full cycle snapshot, layer archives persist a compact
    summary + SHA-256 + cycle reference. Layer 5/6/7 payloads are small and operationally
    important, so they are retained inline. The authoritative full payload remains in the
    gzip cycle snapshot under runtime/cycles/.
    """
    def __init__(self, runtime_root: str | Path, *, max_records: int = 64, max_uncompressed_bytes: int = 2_097_152):
        self.runtime_root = Path(runtime_root)
        self.stores = {
            layer: GzipChunkStore(self.runtime_root / f"layer{layer}" / "records",
                                  max_records=max_records,
                                  max_uncompressed_bytes=max_uncompressed_bytes)
            for layer in range(2, 8)
        }

    @staticmethod
    def _compact(layer: int, payload: Any) -> dict[str, Any]:
        from .utils import sha256_obj
        if not isinstance(payload, dict):
            return {"payload_hash": sha256_obj(payload), "payload_type": type(payload).__name__}
        base = {"payload_hash": sha256_obj(payload)}
        if layer == 2:
            base.update({"twin_count": len(payload), "twins": sorted(payload.keys())})
        elif layer == 3:
            snap = payload.get("snapshot") or {}; prov = snap.get("provenance") or {}
            base.update({"fast_index": prov.get("fast_index"), "has_meso": bool(payload.get("meso_event")),
                         "has_slow": bool(payload.get("slow_event")), "snapshot_version": snap.get("version"),
                         "coc": payload.get("coc") or payload.get("consistency")})
        elif layer == 4:
            candidates = payload.get("candidates") or []; verified = payload.get("verified_candidates") or []
            best = candidates[0] if candidates else {}
            base.update({"candidate_count": len(candidates), "verified_count": len(verified),
                         "best_candidate": {k: best.get(k) for k in ("candidate_id","scenario_id","verified","feasible","pareto_rank","scalar_cost","objective_vector") if k in best}})
        elif layer in (5,6,7):
            base["payload"] = payload
        return base

    def record_cycle(self, cycle: dict[str, Any]) -> dict[str, Any]:
        tx = cycle.get("transaction_id")
        ref = cycle.get("reference_time_s")
        mode = ((cycle.get("provenance") or {}).get("learning_mode"))
        downstream = cycle.get("downstream_layers") or cycle.get("future_layers") or {}
        payloads = {
            2: cycle.get("layer2_packets") or {},
            3: cycle.get("layer3_result") or {},
            4: cycle.get("layer4_result") or {},
            5: downstream.get("5") or {},
            6: downstream.get("6") or {},
            7: downstream.get("7") or {},
        }
        out: dict[str, Any] = {}
        cycle_ref=f"runtime/cycles/{tx}.json.gz"
        for layer, payload in payloads.items():
            rec = {"schema": "TDOC-layer-runtime-record-v2-compact", "layer": layer,
                   "transaction_id": tx, "reference_time_s": ref, "learning_mode": mode,
                   "cycle_ref": cycle_ref, "summary": self._compact(layer,payload)}
            out[str(layer)] = self.stores[layer].append(rec)
        return out


class CompressedPersistentWorkspace:
    """Transient plaintext workspace backed by gzip-only persistent storage.

    JSON files are stored as ``*.json.gz``. Append-heavy JSONL files are persisted
    as bounded gzip chunks under ``<name>.jsonl.chunks/chunks/`` plus a compressed
    manifest. External packages still see the exact original plaintext file in a
    temporary workspace; TDOC reconstructs it by byte-concatenating the chunks.
    """
    def __init__(self, persistent_root: str | Path, *, prefix: str = "tdoc-kstore-", extensions=(".json", ".jsonl"), jsonl_chunk_bytes: int = 1_048_576,
                 working_parent: str | Path | None = None):
        self.persistent_root=Path(persistent_root); self.persistent_root.mkdir(parents=True,exist_ok=True)
        self.extensions=set(extensions); self.jsonl_chunk_bytes=max(64*1024,int(jsonl_chunk_bytes))
        # Append-heavy native registries can be much larger than a host's tmpfs.
        # Keep their plaintext reconstruction transient, but allow TDOC to place it
        # on the same capacity-managed filesystem as its runtime.  Persistence is
        # still gzip-only and close() still removes the complete working directory.
        parent=None if working_parent is None else Path(working_parent)
        if parent is not None:
            parent.mkdir(parents=True,exist_ok=True)
        self.working_root=Path(tempfile.mkdtemp(prefix=prefix,dir=str(parent) if parent is not None else None))
        self._state:dict[str,tuple[int,int]]={}
        self._thread_lock=threading.RLock()
        self.recovery_actions:list[dict[str,Any]]=[]
        try:
            with self._persistent_lock():
                self._restore()
        except Exception:
            # Construction failure must not strand a multi-gigabyte plaintext
            # reconstruction.  The persistent compressed root remains authoritative.
            shutil.rmtree(self.working_root,ignore_errors=True)
            raise

    @contextmanager
    def _persistent_lock(self):
        """Serialize restore/sync across TDOC main and refresh workers."""
        lock_path=self.persistent_root/'.workspace.lock'
        lock_path.parent.mkdir(parents=True,exist_ok=True)
        with self._thread_lock, lock_path.open('a+b') as lockf:
            try:
                import fcntl
                fcntl.flock(lockf.fileno(),fcntl.LOCK_EX)
            except Exception:
                fcntl=None
            yield

    def _chunk_root(self, rel: str | Path) -> Path:
        relp=Path(rel)
        return self.persistent_root/relp.parent/(relp.name+'.chunks')

    def _chunk_manifest(self, rel: str | Path) -> dict[str,Any] | None:
        p=self._chunk_root(rel)/'manifest.json.gz'
        if not p.is_file(): return None
        try: return read_json_auto(p)
        except Exception: return None

    def _restore_jsonl_chunks(self):
        for d in sorted(p for p in self.persistent_root.rglob('*.jsonl.chunks') if p.is_dir()):
            rel=d.relative_to(self.persistent_root)
            target_rel=rel.parent/rel.name[:-len('.chunks')]
            target=self.working_root/target_rel; target.parent.mkdir(parents=True,exist_ok=True)
            manifest=self._chunk_manifest(target_rel)
            declared=list((manifest or {}).get('chunks') or [])
            if declared:
                chunk_rows=[(rec,d/'chunks'/str(rec.get('file'))) for rec in declared]
                declared_paths={p.resolve() for _,p in chunk_rows}
                # A crash may leave a completed-name gzip member before the
                # manifest commit.  The append manifest is authoritative: such a
                # member was never committed and must not be replayed on restart.
                for orphan in sorted((d/'chunks').glob('chunk_*.jsonl.gz')):
                    if orphan.resolve() in declared_paths:
                        continue
                    quarantine=(self.persistent_root/'quarantine'/target_rel.parent/
                                (target_rel.name+'.chunks')/(orphan.name+'.uncommitted'))
                    quarantine.parent.mkdir(parents=True,exist_ok=True)
                    os.replace(orphan,quarantine)
                    self.recovery_actions.append({
                        'schema':'TDOC-external-jsonl-tail-recovery-v1',
                        'reason':'uncommitted_trailing_chunk',
                        'quarantined_file':str(orphan.relative_to(self.persistent_root)),
                        'quarantine_path':str(quarantine),
                        'valid_chunk_count':len(declared),
                    })
            else:
                chunk_rows=[({'file':p.name,'raw_bytes':None},p) for p in sorted((d/'chunks').glob('chunk_*.jsonl.gz'))]
            with target.open('wb') as dst:
                for index,(record,c) in enumerate(chunk_rows):
                    offset=dst.tell()
                    try:
                        with gzip.open(c,'rb') as src: shutil.copyfileobj(src,dst)
                    except (OSError,EOFError,gzip.BadGzipFile,zlib.error) as exc:
                        # Only an unreadable final append can be recovered without
                        # hiding a hole in append-only history.  Interior corruption
                        # remains fail-closed.  The bad member is quarantined and the
                        # manifest is atomically rolled back to its last valid prefix.
                        if manifest is None or index != len(chunk_rows)-1:
                            raise
                        dst.seek(offset); dst.truncate()
                        valid=declared[:index]
                        manifest['chunks']=valid
                        manifest['chunk_count']=len(valid)
                        manifest['raw_bytes']=sum(int(x.get('raw_bytes') or 0) for x in valid)
                        manifest['recovery']={
                            'schema':'TDOC-external-jsonl-tail-recovery-v1',
                            'reason':'trailing_gzip_member_corrupt_or_incomplete',
                            'quarantined_file':str(c.relative_to(self.persistent_root)),
                            'exception':repr(exc),
                            'valid_chunk_count':len(valid),
                        }
                        write_json_gz(d/'manifest.json.gz',manifest,indent=2)
                        quarantine=(self.persistent_root/'quarantine'/target_rel.parent/
                                    (target_rel.name+'.chunks')/(c.name+'.corrupt'))
                        quarantine.parent.mkdir(parents=True,exist_ok=True)
                        if c.exists(): os.replace(c,quarantine)
                        self.recovery_actions.append({**manifest['recovery'],
                                                      'quarantine_path':str(quarantine)})

    def _restore(self):
        # Chunked JSONL is authoritative where present.
        self._restore_jsonl_chunks()
        chunked_targets={str(p.relative_to(self.working_root)) for p in self.working_root.rglob('*.jsonl')}
        # Restore legacy/small single gzip representations and accepted plaintext once.
        for p in sorted(self.persistent_root.rglob('*')):
            if not p.is_file(): continue
            if any(part.endswith('.jsonl.chunks') for part in p.parts): continue
            if p.suffix=='.gz':
                target_rel=p.relative_to(self.persistent_root).with_suffix('')
                if target_rel.suffix not in self.extensions or str(target_rel) in chunked_targets: continue
                target=self.working_root/target_rel; target.parent.mkdir(parents=True,exist_ok=True)
                with gzip.open(p,'rb') as src, target.open('wb') as dst: shutil.copyfileobj(src,dst)
            elif p.suffix in self.extensions:
                target=self.working_root/p.relative_to(self.persistent_root); target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,target)
        for p in self.working_root.rglob('*'):
            if p.is_file() and p.suffix in self.extensions:
                st=p.stat(); self._state[str(p.relative_to(self.working_root))]=(st.st_mtime_ns,st.st_size)
        # Persistent plaintext is never authoritative after restore.
        for p in list(self.persistent_root.rglob('*')):
            if p.is_file() and p.suffix in self.extensions: p.unlink()
        # Migrate legacy single JSONL gzip to chunked representation; JSON remains single gzip.
        # Restored bytes are unchanged. Reuse committed chunks instead of destructively
        # rebuilding all historical logs at every preflight/constructor call. Legacy
        # files with no chunk manifest are still migrated by sync(force=False).
        self._sync_unlocked(force=False)

    def _write_json_single(self,p:Path,rel:str)->tuple[int,int]:
        gz=Path(str(self.persistent_root/rel)+'.gz'); gz.parent.mkdir(parents=True,exist_ok=True)
        tmp=gz.with_name('.'+gz.name+'.tmp')
        with p.open('rb') as src, gzip.open(tmp,'wb',compresslevel=6) as dst: shutil.copyfileobj(src,dst)
        os.replace(tmp,gz)
        return p.stat().st_size,gz.stat().st_size

    def _rebuild_jsonl_chunks(self,p:Path,rel:str)->tuple[int,int]:
        d=self._chunk_root(rel); shutil.rmtree(d,ignore_errors=True); (d/'chunks').mkdir(parents=True,exist_ok=True)
        chunks=[]; buf=bytearray(); raw_total=0; gz_total=0; idx=0
        def flush():
            nonlocal buf,idx,gz_total
            if not buf: return
            cp=d/'chunks'/f'chunk_{idx:06d}.jsonl.gz'
            with gzip.open(cp,'wb',compresslevel=6) as f: f.write(buf)
            chunks.append({'index':idx,'file':cp.name,'raw_bytes':len(buf),'gzip_bytes':cp.stat().st_size})
            gz_total+=cp.stat().st_size; idx+=1; buf=bytearray()
        with p.open('rb') as f:
            for line in f:
                if buf and len(buf)+len(line)>self.jsonl_chunk_bytes: flush()
                buf.extend(line); raw_total+=len(line)
                if len(buf)>=self.jsonl_chunk_bytes: flush()
        flush()
        manifest={'schema':'TDOC-external-jsonl-chunks-v1','source_rel':rel,'raw_bytes':raw_total,'chunk_bytes_target':self.jsonl_chunk_bytes,'chunks':chunks,'chunk_count':len(chunks),'compressed_at_rest':True}
        mp=write_json_gz(d/'manifest.json.gz',manifest,indent=2); gz_total+=mp.stat().st_size
        Path(str(self.persistent_root/rel)+'.gz').unlink(missing_ok=True)
        return raw_total,gz_total

    def _append_jsonl_chunks(self,p:Path,rel:str,manifest:dict[str,Any])->tuple[int,int]:
        old=int(manifest.get('raw_bytes') or 0); size=p.stat().st_size
        if size<old: return self._rebuild_jsonl_chunks(p,rel)
        if size==old: return 0,0
        d=self._chunk_root(rel); chunks=manifest.setdefault('chunks',[]); (d/'chunks').mkdir(parents=True,exist_ok=True)
        raw_added=0; gz_written=0
        def append_chunk(data:bytearray):
            nonlocal gz_written
            idx=len(chunks); cp=d/'chunks'/f'chunk_{idx:06d}.jsonl.gz'
            tmp=cp.with_name('.'+cp.name+'.tmp')
            try:
                with gzip.open(tmp,'wb',compresslevel=6) as z: z.write(data)
                os.replace(tmp,cp)
            finally:
                tmp.unlink(missing_ok=True)
            chunks.append({'index':idx,'file':cp.name,'raw_bytes':len(data),'gzip_bytes':cp.stat().st_size})
            gz_written+=cp.stat().st_size
        with p.open('rb') as f:
            f.seek(old)
            pending=bytearray()
            for line in f:
                pending.extend(line); raw_added+=len(line)
                if len(pending)>=self.jsonl_chunk_bytes:
                    append_chunk(pending); pending=bytearray()
            if pending:
                # Each sync appends one bounded member/chunk. This avoids rewriting historical data.
                append_chunk(pending)
        manifest['raw_bytes']=size; manifest['chunk_count']=len(chunks); manifest['compressed_at_rest']=True
        mp=write_json_gz(d/'manifest.json.gz',manifest,indent=2); gz_written+=mp.stat().st_size
        Path(str(self.persistent_root/rel)+'.gz').unlink(missing_ok=True)
        return raw_added,gz_written

    def _sync_unlocked(self, *, force: bool = False) -> dict[str, Any]:
        changed=0; raw_bytes=0; gzip_bytes=0; current=set()
        for p in sorted(self.working_root.rglob('*')):
            if not p.is_file() or p.suffix not in self.extensions or p.name.startswith('.'): continue
            rel=str(p.relative_to(self.working_root)); current.add(rel); st=p.stat(); sig=(st.st_mtime_ns,st.st_size)
            if p.suffix=='.jsonl':
                manifest=self._chunk_manifest(rel)
                if not force and self._state.get(rel)==sig and manifest is not None: continue
                if force or manifest is None or int(manifest.get('raw_bytes') or 0)>st.st_size:
                    r,g=self._rebuild_jsonl_chunks(p,rel)
                else:
                    r,g=self._append_jsonl_chunks(p,rel,manifest)
                if r or force: changed+=1; raw_bytes+=r; gzip_bytes+=g
            else:
                gz=Path(str(self.persistent_root/rel)+'.gz')
                if not force and self._state.get(rel)==sig and gz.is_file(): continue
                r,g=self._write_json_single(p,rel); changed+=1; raw_bytes+=r; gzip_bytes+=g
            self._state[rel]=sig
        # Propagate deletions to both persistent forms.
        for rel in list(self._state):
            if rel not in current:
                Path(str(self.persistent_root/rel)+'.gz').unlink(missing_ok=True)
                shutil.rmtree(self._chunk_root(rel),ignore_errors=True)
                self._state.pop(rel,None)
        return {'changed_files':changed,'raw_bytes_processed':raw_bytes,'gzip_bytes_written':gzip_bytes,'persistent_compressed':True,'jsonl_chunked':True,'working_root':str(self.working_root),'persistent_root':str(self.persistent_root)}

    def sync(self, *, force: bool = False) -> dict[str, Any]:
        with self._persistent_lock():
            return self._sync_unlocked(force=force)

    def close(self):
        # A failed terminal flush (most notably ENOSPC on the transient tmpfs)
        # must not strand the potentially large plaintext reconstruction.  The
        # compressed persistent copy remains authoritative; preserve the sync
        # exception for the caller while making transient cleanup unconditional.
        try:
            self.sync()
        finally:
            shutil.rmtree(self.working_root,ignore_errors=True)
