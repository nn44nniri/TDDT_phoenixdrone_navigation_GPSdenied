from __future__ import annotations
from pathlib import Path
from typing import Any
from .utils import sha256_obj, utc_now
from .storage import GzipChunkStore

class KnowledgeLedger:
    """TDOC-owned per-cycle compressed knowledge index.

    It does not replace Layer-3 SS/SETD KStores or Layer-5/7 KStore content. It records
    immutable references/hashes so every offline cycle is traceable through Layer 5 and
    every online cycle through Layer 7. Persistent records are chunked JSONL gzip files;
    the latest snapshot and manifest are gzip-compressed too.
    """
    def __init__(self, root: Path, *, chunk_records: int = 64, chunk_bytes: int = 1_048_576):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.chunk_records=int(chunk_records); self.chunk_bytes=int(chunk_bytes)
        self._stores:dict[str,GzipChunkStore]={}
        self._layer_stores:dict[str,GzipChunkStore]={}

    def _store(self, mode:str)->GzipChunkStore:
        key=mode.lower()
        if key not in self._stores:
            self._stores[key]=GzipChunkStore(self.root/key,max_records=self.chunk_records,max_uncompressed_bytes=self.chunk_bytes)
        return self._stores[key]

    def _layer_store(self, layer:str)->GzipChunkStore:
        key=str(layer)
        if key not in self._layer_stores:
            if self.root.name == 'ledger' and self.root.parent.name == 'knowledge':
                runtime_root=self.root.parent.parent
            elif self.root.name == 'knowledge':
                runtime_root=self.root.parent
            else:
                runtime_root=self.root.parent
            self._layer_stores[key]=GzipChunkStore(runtime_root/f"layer{key}"/"knowledge",max_records=self.chunk_records,max_uncompressed_bytes=self.chunk_bytes)
        return self._layer_stores[key]

    @staticmethod
    def _present(v: Any) -> bool:
        return v is not None and v != {} and v != []

    def record(self, cycle: dict[str,Any]) -> dict[str,Any]:
        mode=str((cycle.get('provenance') or {}).get('learning_mode') or 'OFFLINE_SE').upper()
        ds=cycle.get('downstream_layers') or cycle.get('future_layers') or {}
        l3=cycle.get('layer3_result') or {}; l4=cycle.get('layer4_result') or {}
        d5=ds.get('5') or {}; d6=ds.get('6') or {}; d7=ds.get('7') or {}
        l5out=d5.get('output') or {}; l6out=d6.get('output') or {}; l7out=d7.get('output') or {}
        levels={
            '2': self._present(cycle.get('layer2_packets')),
            '3': self._present(l3),
            '4': self._present(l4),
            '5': self._present(l5out.get('result')),
            '6': self._present(l6out.get('execution_evidence')) if mode=='ONLINE_JSBSIM' else None,
            '7': self._present(l7out.get('result')) if mode=='ONLINE_JSBSIM' else None,
        }
        target='7' if mode=='ONLINE_JSBSIM' else '5'
        needed=['2','3','4','5'] + (['6','7'] if mode=='ONLINE_JSBSIM' else [])
        record={
            'schema':'TDOC-knowledge-ledger-v2-gzip-chunked',
            'transaction_id':cycle.get('transaction_id'),
            'recorded_at':utc_now(),
            'learning_mode':mode,
            'target_layer':target,
            'knowledge_levels':levels,
            'knowledge_target_reached':all(levels.get(k) is True for k in needed),
            'operational_status':cycle.get('status'),
            'knowledge_completion_kind':('observation_only' if mode=='ONLINE_JSBSIM' and d6.get('status')=='OBSERVATION_ONLY' else ('operational' if mode=='ONLINE_JSBSIM' and d6.get('status')=='EXECUTED' else 'offline_decision')),
            'actuation_executed':(d6.get('status')=='EXECUTED') if mode=='ONLINE_JSBSIM' else None,
            'layer3_fast_index':(((l3.get('snapshot') or {}).get('provenance') or {}).get('fast_index')),
            'layer3_has_meso':self._present(l3.get('meso_event')),
            'layer3_has_slow':self._present(l3.get('slow_event')),
            'layer4_candidate_count':len(l4.get('candidates') or []),
            'layer4_verified_count':len(l4.get('verified_candidates') or []),
            'layer5_status':(ds.get('5') or {}).get('status'),
            'layer6_status':(ds.get('6') or {}).get('status') if mode=='ONLINE_JSBSIM' else 'NOT_IN_OFFLINE_PATH',
            'layer7_status':(ds.get('7') or {}).get('status') if mode=='ONLINE_JSBSIM' else 'NOT_IN_OFFLINE_PATH',
            'hashes':{
                'layer2':sha256_obj(cycle.get('layer2_packets') or {}),
                'layer3':sha256_obj(l3), 'layer4':sha256_obj(l4),
                'layer5':sha256_obj(ds.get('5') or {}),
                'layer6':sha256_obj(ds.get('6') or {}) if mode=='ONLINE_JSBSIM' else None,
                'layer7':sha256_obj(ds.get('7') or {}) if mode=='ONLINE_JSBSIM' else None,
            },
            'storage':{'format':'jsonl.gz','chunked':True,'compressed_at_rest':True},
        }
        loc=self._store(mode).append(record)
        record['storage'].update(loc)
        # Per-layer compressed knowledge views make the knowledge produced/reached at each
        # numbered layer directly discoverable under runtime/layerN/knowledge.
        for layer, present in levels.items():
            if present is True:
                self._layer_store(layer).append({
                    'schema':'TDOC-layer-knowledge-view-v1',
                    'layer':int(layer),
                    'transaction_id':record['transaction_id'],
                    'recorded_at':record['recorded_at'],
                    'learning_mode':mode,
                    'target_layer':target,
                    'knowledge_target_reached':record['knowledge_target_reached'],
                    'layer_hash':record['hashes'].get(f'layer{layer}'),
                    'source_ledger_schema':record['schema'],
                    'compressed_at_rest':True,
                })
        return record

    def iter_records(self, mode:str):
        yield from self._store(mode).iter_records()

    def latest(self, mode:str):
        return self._store(mode).latest()
