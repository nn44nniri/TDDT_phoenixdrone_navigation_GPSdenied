#!/usr/bin/env python3
from __future__ import annotations
import argparse, shutil
from pathlib import Path
from tddt_tdoc.storage import GzipChunkStore, iter_jsonl_auto


def main():
    ap=argparse.ArgumentParser(description='Rebuild runtime/layerN/knowledge compressed views from the authoritative TDOC knowledge ledger.')
    ap.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    args=ap.parse_args(); root=Path(args.root).resolve(); runtime=root/'runtime'; ledger=runtime/'knowledge'/'ledger'
    for n in range(2,8): shutil.rmtree(runtime/f'layer{n}'/'knowledge',ignore_errors=True)
    stores={str(n):GzipChunkStore(runtime/f'layer{n}'/'knowledge',max_records=64,max_uncompressed_bytes=1_048_576) for n in range(2,8)}
    count={str(n):0 for n in range(2,8)}
    for mode_dir in sorted(p for p in ledger.iterdir() if p.is_dir()) if ledger.is_dir() else []:
        for chunk in sorted((mode_dir/'chunks').glob('chunk_*.jsonl.gz')):
            for r in iter_jsonl_auto(chunk):
                levels=r.get('knowledge_levels') or {}; hashes=r.get('hashes') or {}
                for layer,present in levels.items():
                    if present is not True or layer not in stores: continue
                    stores[layer].append({'schema':'TDOC-layer-knowledge-view-v1','layer':int(layer),'transaction_id':r.get('transaction_id'),'recorded_at':r.get('recorded_at'),'learning_mode':r.get('learning_mode'),'target_layer':r.get('target_layer'),'knowledge_target_reached':r.get('knowledge_target_reached'),'layer_hash':hashes.get(f'layer{layer}'),'source_ledger_schema':r.get('schema'),'compressed_at_rest':True})
                    count[layer]+=1
    print(' '.join(f'L{k}={v}' for k,v in count.items()))
    return 0
if __name__=='__main__': raise SystemExit(main())
