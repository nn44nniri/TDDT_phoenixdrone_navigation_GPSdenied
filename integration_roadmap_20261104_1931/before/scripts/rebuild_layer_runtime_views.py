#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, shutil
from pathlib import Path
from tddt_tdoc.storage import GzipChunkStore, iter_jsonl_auto


def _timeline(root: Path):
    p=root/'reports/offline/mission/MISSION_TIMELINE.csv'
    out={}
    if p.is_file():
        with p.open(newline='',encoding='utf-8') as f:
            for r in csv.DictReader(f):
                tx=r.get('transaction_id')
                if tx: out[tx]=r
    return out


def main():
    ap=argparse.ArgumentParser(description='Rebuild compact per-layer runtime chunk views from the authoritative compressed knowledge ledger without re-reading large cycle payloads.')
    ap.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
    args=ap.parse_args(); root=Path(args.root).resolve(); runtime=root/'runtime'; ledger=runtime/'knowledge'/'ledger'
    for n in range(2,8): shutil.rmtree(runtime/f'layer{n}'/'records',ignore_errors=True)
    stores={n:GzipChunkStore(runtime/f'layer{n}'/'records',max_records=64,max_uncompressed_bytes=2_097_152) for n in range(2,8)}
    tmap=_timeline(root); count=0; per={n:0 for n in range(2,8)}
    for mode_dir in sorted(p for p in ledger.iterdir() if p.is_dir()) if ledger.is_dir() else []:
        for chunk in sorted((mode_dir/'chunks').glob('chunk_*.jsonl.gz')):
            for k in iter_jsonl_auto(chunk):
                tx=k.get('transaction_id'); row=tmap.get(tx,{})
                try: ref=float(row.get('reference_time_s')) if row.get('reference_time_s') not in (None,'') else None
                except Exception: ref=None
                for n in range(2,8):
                    present=(k.get('knowledge_levels') or {}).get(str(n))
                    summary={'payload_hash':(k.get('hashes') or {}).get(f'layer{n}'),'knowledge_present':present}
                    if n==3:
                        summary.update({'fast_index':k.get('layer3_fast_index'),'has_meso':k.get('layer3_has_meso'),'has_slow':k.get('layer3_has_slow')})
                    elif n==4:
                        summary.update({'candidate_count':k.get('layer4_candidate_count'),'verified_count':k.get('layer4_verified_count')})
                    elif n==5:
                        summary.update({'status':k.get('layer5_status'),'decision_mode':row.get('l5_mode'),'final_gate':row.get('l5_final_gate')})
                    elif n==6:
                        summary.update({'status':k.get('layer6_status'),'actuation_executed':k.get('actuation_executed')})
                    elif n==7:
                        summary.update({'status':k.get('layer7_status'),'completion_kind':k.get('knowledge_completion_kind')})
                    rec={'schema':'TDOC-layer-runtime-record-v3-ledger-indexed','layer':n,'transaction_id':tx,'reference_time_s':ref,'learning_mode':k.get('learning_mode'),'cycle_ref':f'runtime/cycles/{tx}.json.gz','summary':summary}
                    stores[n].append(rec); per[n]+=1
                count+=1
    print(f'ledger_records={count}; '+' '.join(f'L{n}={per[n]}' for n in range(2,8)))
    return 0
if __name__=='__main__': raise SystemExit(main())
