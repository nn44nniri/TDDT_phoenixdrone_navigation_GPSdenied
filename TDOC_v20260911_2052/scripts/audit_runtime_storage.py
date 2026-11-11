#!/usr/bin/env python3
from __future__ import annotations
import argparse, gzip, json, os, struct
from pathlib import Path


def _count_lines_gz(path: Path) -> int:
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return -1


def audit(root: Path) -> dict:
    runtime = root / 'runtime'
    files = [p for p in runtime.rglob('*') if p.is_file()]
    raw_knowledge = []
    gzip_files = []
    chunk_files = []
    by_layer = {}
    for p in files:
        rel = p.relative_to(root).as_posix()
        if p.suffix == '.gz':
            gzip_files.append(p)
        if p.name.endswith('.jsonl.gz') and '/chunks/' in f'/{rel}':
            chunk_files.append(p)
        # Persistent knowledge/runtime histories must not remain as plain JSON/JSONL.
        # Small authoritative snapshots/registries are explicitly excluded.
        if p.suffix in ('.json', '.jsonl'):
            parts = set(p.parts)
            if ('kstore' in parts or 'knowledge' in parts or 'records' in parts or 'invocations' in parts or 'candidates' in p.name or 'provenance' in p.name):
                raw_knowledge.append(rel)
        for n in range(2,8):
            marker = f'layer{n}'
            if marker in p.parts:
                x = by_layer.setdefault(str(n), {'files':0,'gzip_files':0,'chunk_files':0,'bytes':0,'records_in_chunks':0})
                x['files'] += 1; x['bytes'] += p.stat().st_size
                if p.suffix == '.gz': x['gzip_files'] += 1
                if p.name.endswith('.jsonl.gz') and 'chunks' in p.parts:
                    x['chunk_files'] += 1
                    c=_count_lines_gz(p)
                    if c>=0: x['records_in_chunks'] += c
    # Estimate compression ratio efficiently from the gzip ISIZE trailer.
    # ISIZE is exact modulo 2^32; TDOC runtime files are bounded chunks and far below 4 GiB.
    compressed = 0; uncompressed = 0; isize_failures=[]
    for p in gzip_files:
        compressed += p.stat().st_size
        try:
            with p.open('rb') as f:
                f.seek(-4, os.SEEK_END)
                uncompressed += struct.unpack('<I', f.read(4))[0]
        except Exception as exc:
            isize_failures.append({'file':str(p),'error':repr(exc)})
    layer5 = by_layer.get('5',{})
    l5_record_chunks=list((runtime/'layer5'/'records'/'chunks').glob('chunk_*.jsonl.gz'))
    l5_knowledge_chunks=list((runtime/'layer5'/'knowledge'/'chunks').glob('chunk_*.jsonl.gz'))
    checks = {
        'runtime_exists': runtime.is_dir(),
        'no_plain_persistent_knowledge_json_jsonl': not raw_knowledge,
        'gzip_storage_present': bool(gzip_files),
        'chunked_storage_present': bool(chunk_files),
        'layer5_namespace_present': (runtime/'layer5').is_dir(),
        'layer5_records_present': bool(l5_record_chunks),
        'layer5_knowledge_present': bool(l5_knowledge_chunks),
    }
    return {
        'schema':'TDOC-runtime-storage-audit-v1',
        'runtime_root':str(runtime),
        'checks':checks,
        'ok':all(checks.values()),
        'plain_persistent_knowledge_files':raw_knowledge,
        'gzip_file_count':len(gzip_files),
        'chunk_file_count':len(chunk_files),
        'persistent_compressed_bytes':compressed,
        'estimated_uncompressed_bytes':uncompressed,
        'compression_ratio_compressed_over_raw':(compressed/uncompressed if uncompressed else None),
        'space_saving_ratio':(1.0-compressed/uncompressed if uncompressed else None),
        'uncompressed_size_method':'gzip_ISIZE_exact_for_bounded_files',
        'gzip_isize_failures':isize_failures,
        'layer5_record_chunks':len(l5_record_chunks),
        'layer5_knowledge_chunks':len(l5_knowledge_chunks),
        'layers':by_layer,
    }


def main():
    ap=argparse.ArgumentParser(description='Audit TDOC gzip-at-rest and chunked runtime storage.')
    ap.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--output',default=None)
    args=ap.parse_args()
    root=Path(args.root).resolve(); out=audit(root)
    dest=Path(args.output) if args.output else root/'reports/validation/STORAGE_COMPRESSION_AUDIT.json'
    dest.parent.mkdir(parents=True,exist_ok=True); dest.write_text(json.dumps(out,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(out,indent=2,ensure_ascii=False))
    return 0 if out['ok'] else 2

if __name__=='__main__':
    raise SystemExit(main())
