"""Read-only salvage of committed SS evidence; never issue a replacement handoff."""
import gzip,hashlib,json
from pathlib import Path
from layer5.utils import sha256_obj

audit=Path(__file__).resolve().parent;e=audit/'evidence'
before=json.loads((e/'attempt1_prior_before_online.json').read_text())
store=audit/'attempt1_full_prior/runtime/layer5/kstore'
files=sorted(store.glob('ss/segments/segment_*.jsonl.chunks/chunks/chunk_*.jsonl.gz'))
out=e/'recovered_committed_SS.jsonl.gz';previous=None;count=0;last=None
with out.open('wb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as dst:
    for path in files:
        for line in gzip.decompress(path.read_bytes()).splitlines():
            if not line.strip():continue
            row=json.loads(line);count+=1
            assert row['seq']==count and row['state_commit']=='COMMITTED' and row['h_prev']==previous
            assert row['id_E']==before['handoff']['setd']['episode_id']
            assert sha256_obj(row['payload'])==row['h_payload']
            assert sha256_obj({k:v for k,v in row.items() if k not in ('record_hash','state_commit')})==row['record_hash']
            previous=row['record_hash'];last=row;dst.write(line+b'\n')
assert count==before['handoff']['setd']['experience_count']
assert previous==before['handoff']['setd']['last_record_hash']
remaining={str(p.relative_to(store)):hashlib.sha256(p.read_bytes()).hexdigest() for p in store.rglob('*') if p.is_file()}
missing={k:v for k,v in before['kstore_files'].items() if k not in remaining}
changed={k:{'before':v,'after':remaining[k]} for k,v in before['kstore_files'].items() if k in remaining and v!=remaining[k]}
result={'status':'COMMITTED_SS_CHAIN_FULLY_SALVAGED','record_count':count,'expected_record_count':count,
    'episode_id':last['id_E'],'last_record_hash':previous,'matches_original_handoff_last_hash':True,
    'SS_payload_and_chain_hashes_verified':True,'source_chunk_files':len(files),
    'export_sha256':hashlib.sha256(out.read_bytes()).hexdigest(),
    'original_SETD_metadata_byte_exact_restored':False,'missing_metadata':missing,
    'changed_surviving_files':changed,'original_archive_modified':False,
    'flight_authority':False,'online_prior_admitted':False,
    'note':'This is recovered original evidence, not a replacement certificate/SETD/handoff.'}
(e/'SS_RECOVERY_RESULT.json').write_text(json.dumps(result,indent=2))
(e/'recovered_last_SS_Theta5.json').write_text(json.dumps({'scope':'RECOVERED_SS_SNAPSHOT_NOT_ADMITTED_PRIOR',
    'source_record_hash':previous,'Theta5':last['payload'].get('Theta5')},indent=2))
print(result['status'],count,'records; missing metadata',len(missing),'changed survivors',len(changed))
