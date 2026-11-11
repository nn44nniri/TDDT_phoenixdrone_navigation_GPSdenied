from __future__ import annotations
import json
from pathlib import Path
from tddt_tdoc.storage import GzipChunkStore, CompressedAtRestDirectory, read_json_auto


def test_repeated_workspace_close_preserves_committed_metadata_and_history(tmp_path):
    import hashlib
    import pytest
    from tddt_tdoc.storage import CompressedPersistentWorkspace
    root=tmp_path/'persistent';w=CompressedPersistentWorkspace(root)
    (w.working_root/'knowledge.json').write_text('{"prior":"must survive"}')
    (w.working_root/'history.jsonl').write_text('{"cycle":1}\n{"cycle":2}\n')
    def hashes():return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob('*') if p.is_file()}
    w.close();before=hashes();w.close();w.close()
    assert hashes()==before
    with pytest.raises(RuntimeError,match='closed'):w.sync()
    restored=CompressedPersistentWorkspace(root)
    assert json.loads((restored.working_root/'knowledge.json').read_text())=={'prior':'must survive'}
    assert len((restored.working_root/'history.jsonl').read_text().splitlines())==2
    restored.close();restored.close()
    assert hashes()==before


def test_failed_terminal_flush_cannot_erase_last_commit_on_second_close(tmp_path,monkeypatch):
    import hashlib
    import pytest
    from tddt_tdoc.storage import CompressedPersistentWorkspace
    root=tmp_path/'persistent';first=CompressedPersistentWorkspace(root)
    (first.working_root/'knowledge.json').write_text('{"prior":1}')
    first.close();before=(root/'knowledge.json.gz').read_bytes()
    second=CompressedPersistentWorkspace(root)
    monkeypatch.setattr(second,'_sync_unlocked',lambda **kw:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError,match='disk full'):second.close()
    second.close()
    assert (root/'knowledge.json.gz').read_bytes()==before


def test_gzip_chunk_store_rotates_and_roundtrips(tmp_path):
    store=GzipChunkStore(tmp_path/'records',max_records=2,max_uncompressed_bytes=10_000)
    for i in range(5): store.append({'i':i,'value':'x'*20})
    chunks=sorted((tmp_path/'records/chunks').glob('chunk_*.jsonl.gz'))
    assert len(chunks)==3
    assert [r['i'] for r in store.iter_records()]==list(range(5))
    assert read_json_auto(tmp_path/'records/latest.json.gz')['i']==4
    assert not list((tmp_path/'records/chunks').glob('*.jsonl'))


def test_compressed_at_rest_directory_exact_roundtrip(tmp_path):
    root=tmp_path/'kstore'; (root/'ss').mkdir(parents=True)
    p=root/'ss/segment_000000.jsonl'; raw='{"a":1}\n{"a":2}\n'; p.write_text(raw,encoding='utf-8')
    q=root/'manifest.json'; q.write_text(json.dumps({'file':'ss/segment_000000.jsonl'}),encoding='utf-8')
    facade=CompressedAtRestDirectory(root); rep=facade.compress()
    assert rep['files_compressed']==2 and not p.exists() and (Path(str(p)+'.gz')).is_file()
    facade.materialize(); assert p.read_text(encoding='utf-8')==raw and q.is_file()
    facade.compress(); assert not p.exists() and not q.exists()


def test_knowledge_ledger_creates_per_layer_views(tmp_path):
    from tddt_tdoc.knowledge import KnowledgeLedger
    from tddt_tdoc.storage import iter_jsonl_auto
    k=KnowledgeLedger(tmp_path/'runtime'/'knowledge',chunk_records=2,chunk_bytes=10000)
    cycle={
        'transaction_id':'tx1','status':'DEGRADED',
        'provenance':{'learning_mode':'OFFLINE_SE'},
        'layer2_packets':{'A':{'state':{'x':1}}},
        'layer3_result':{'snapshot':{'provenance':{'fast_index':1}}},
        'layer4_result':{'candidates':[{'candidate_id':'c1'}],'verified_candidates':[]},
        'downstream_layers':{'5':{'status':'OK','output':{'result':{'operational_mode':'HOLD'}}}},
    }
    r=k.record(cycle)
    assert r['knowledge_target_reached'] is True
    p=next((tmp_path/'runtime/layer5/knowledge/chunks').glob('chunk_*.jsonl.gz'))
    rows=list(iter_jsonl_auto(p))
    assert rows[0]['layer']==5 and rows[0]['transaction_id']=='tx1'


def test_compressed_persistent_workspace_jsonl_chunk_roundtrip(tmp_path):
    from tddt_tdoc.storage import CompressedPersistentWorkspace
    root=tmp_path/'native'
    w=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=128)
    raw=''.join(json.dumps({'i':i,'payload':'x'*80},separators=(',',':'))+'\n' for i in range(2000))
    p=w.working_root/'events.jsonl'; p.write_text(raw,encoding='utf-8')
    rep=w.sync(); assert rep['persistent_compressed'] and rep['jsonl_chunked']
    w.close()
    chunks=sorted((root/'events.jsonl.chunks/chunks').glob('chunk_*.jsonl.gz'))
    assert len(chunks)>=2 and not (root/'events.jsonl.gz').exists() and not (root/'events.jsonl').exists()
    w2=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=128)
    assert (w2.working_root/'events.jsonl').read_text(encoding='utf-8')==raw
    with (w2.working_root/'events.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps({'i':2000})+'\n')
    w2.sync(); w2.close()
    w3=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=128)
    text=(w3.working_root/'events.jsonl').read_text(encoding='utf-8')
    assert text.startswith(raw) and '"i": 2000' in text
    w3.close()


def test_compressed_persistent_workspace_close_cleans_tmp_after_sync_failure(tmp_path, monkeypatch):
    from tddt_tdoc.storage import CompressedPersistentWorkspace
    root=tmp_path/'native'
    w=CompressedPersistentWorkspace(root,prefix='tdoc-close-failure-')
    working_root=w.working_root
    (working_root/'events.jsonl').write_text('{"cycle":1}\n',encoding='utf-8')

    def fail_sync(*, force=False):
        raise OSError(28,'No space left on device')

    monkeypatch.setattr(w,'sync',fail_sync)
    try:
        w.close()
    except OSError as exc:
        assert exc.errno==28
    else:
        raise AssertionError('close must preserve the terminal sync failure')
    assert not working_root.exists()


def test_compressed_workspace_uses_capacity_managed_transient_parent(tmp_path):
    from tddt_tdoc.storage import CompressedPersistentWorkspace
    persistent=tmp_path/'runtime'/'layer4'/'native'
    transient=tmp_path/'runtime'/'.transient'
    w=CompressedPersistentWorkspace(
        persistent,prefix='tdoc-layer4-',working_parent=transient)
    working_root=w.working_root
    try:
        assert working_root.parent == transient
        (working_root/'candidates.jsonl').write_text('{"cycle":1}\n',encoding='utf-8')
    finally:
        w.close()
    assert not working_root.exists()
    assert (persistent/'candidates.jsonl.chunks'/'manifest.json.gz').is_file()


def test_workspace_recovers_only_corrupt_trailing_chunk_and_quarantines_it(tmp_path):
    from tddt_tdoc.storage import CompressedPersistentWorkspace, read_json_auto
    root=tmp_path/'native'; transient=tmp_path/'.transient'
    w=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=65536,working_parent=transient)
    raw=''.join(json.dumps({'i':i,'payload':'x'*180})+'\n' for i in range(1200))
    (w.working_root/'events.jsonl').write_text(raw,encoding='utf-8')
    w.sync(); w.close()
    manifest_path=root/'events.jsonl.chunks/manifest.json.gz'
    before=read_json_auto(manifest_path); assert len(before['chunks'])>=2
    last=root/'events.jsonl.chunks/chunks'/before['chunks'][-1]['file']
    damaged=last.read_bytes(); last.write_bytes(damaged[:-8])

    restored=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=65536,working_parent=transient)
    try:
        assert len(restored.recovery_actions)==1
        assert 'trailing_gzip_member_corrupt_or_incomplete' in restored.recovery_actions[0]['reason']
        after=read_json_auto(manifest_path)
        assert len(after['chunks'])==len(before['chunks'])-1
        assert (restored.working_root/'events.jsonl').stat().st_size==after['raw_bytes']
        assert Path(restored.recovery_actions[0]['quarantine_path']).is_file()
    finally:
        restored.close()


def test_workspace_interior_corruption_fails_closed_without_transient_leak(tmp_path):
    from tddt_tdoc.storage import CompressedPersistentWorkspace, read_json_auto
    root=tmp_path/'native'; transient=tmp_path/'.transient'
    w=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=65536,working_parent=transient)
    raw=''.join(json.dumps({'i':i,'payload':'y'*180})+'\n' for i in range(1200))
    (w.working_root/'events.jsonl').write_text(raw,encoding='utf-8')
    w.sync(); w.close()
    manifest=read_json_auto(root/'events.jsonl.chunks/manifest.json.gz'); assert len(manifest['chunks'])>=2
    first=root/'events.jsonl.chunks/chunks'/manifest['chunks'][0]['file']
    first.write_bytes(first.read_bytes()[:-8])
    try:
        CompressedPersistentWorkspace(root,jsonl_chunk_bytes=65536,working_parent=transient)
    except (OSError,EOFError):
        pass
    else:
        raise AssertionError('interior append-only corruption must fail closed')
    assert not list(transient.glob('tdoc-kstore-*'))


def test_workspace_ignores_and_quarantines_uncommitted_tail(tmp_path):
    from tddt_tdoc.storage import CompressedPersistentWorkspace, read_json_auto
    root=tmp_path/'native'; transient=tmp_path/'.transient'
    w=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=65536,working_parent=transient)
    raw=''.join(json.dumps({'i':i,'payload':'z'*180})+'\n' for i in range(800))
    (w.working_root/'events.jsonl').write_text(raw,encoding='utf-8'); w.sync(); w.close()
    manifest=read_json_auto(root/'events.jsonl.chunks/manifest.json.gz')
    orphan=root/'events.jsonl.chunks/chunks'/f"chunk_{len(manifest['chunks']):06d}.jsonl.gz"
    orphan.write_bytes(b'not-a-committed-gzip-member')
    restored=CompressedPersistentWorkspace(root,jsonl_chunk_bytes=65536,working_parent=transient)
    try:
        assert (restored.working_root/'events.jsonl').read_text(encoding='utf-8')==raw
        assert restored.recovery_actions[0]['reason']=='uncommitted_trailing_chunk'
        assert Path(restored.recovery_actions[0]['quarantine_path']).is_file()
    finally:
        restored.close()
