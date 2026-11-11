from __future__ import annotations
from collections import deque
from pathlib import Path
import json, os, time
from .utils import atomic_write_json, sha256_json


class BoundedKStore:
    """Bounded SS-KStore in RAM and persistent storage.

    The persistent FILE backend is segmented and retains at most
    ``max_segments`` files.  Oldest segments are removed after rotation, so
    history is bounded on Edge storage as required by Formalism_layer7_v2.
    """
    def __init__(self, max_records: int = 256, root: str | Path | None = None,
                 segment_records: int = 64, max_segments: int = 8):
        self.records = deque(maxlen=max_records)
        self.max_records = max_records; self.segment_records = max(1, segment_records)
        self.max_segments = max(1, max_segments)
        self.root = Path(root) if root else None
        self.segment_index = 0; self.segment_count = 0
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True); self._load_manifest(); self._prune_segments()

    @classmethod
    def from_environment(cls, max_records=256, segment_records=64, max_segments=8):
        root = os.getenv('TDDT_KSTORE_ROOT')
        return cls(max_records, Path(root) / 'layer7' / 'SS-KStore' if root else None,
                   segment_records, max_segments)

    def _load_manifest(self):
        p = self.root / 'manifest.json'
        if p.exists():
            try:
                d = json.loads(p.read_text())
                self.segment_index = int(d.get('segment_index', 0)); self.segment_count = int(d.get('segment_count', 0))
            except Exception:
                pass

    def _segments(self):
        return sorted(self.root.glob('segment_*.jsonl')) if self.root else []

    def _prune_segments(self):
        if not self.root: return
        segs = self._segments()
        # Keep current + newest older segments within hard budget.
        while len(segs) > self.max_segments:
            segs[0].unlink(missing_ok=True); segs.pop(0)

    def append(self, record: dict):
        rec = dict(record); rec.setdefault('stored_at', time.time()); rec.setdefault('record_hash', sha256_json(rec))
        self.records.append(rec)
        if self.root:
            seg = self.root / f'segment_{self.segment_index:05d}.jsonl'
            with seg.open('a', encoding='utf-8') as f: f.write(json.dumps(rec, sort_keys=True, default=str) + '\n')
            self.segment_count += 1
            if self.segment_count >= self.segment_records:
                self.segment_index += 1; self.segment_count = 0; self._prune_segments()
            atomic_write_json(self.root / 'manifest.json', {
                'segment_index': self.segment_index, 'segment_count': self.segment_count,
                'max_records_runtime': self.max_records, 'segment_records': self.segment_records,
                'max_segments': self.max_segments, 'retained_segments': len(self._segments()),
            })
        return rec

    def snapshot(self): return list(self.records)


class SETDKStore:
    """Compact/versioned Slow evidence packages with bounded retention."""
    def __init__(self, root: str | Path | None = None, max_packages: int = 32):
        self.root = Path(root) if root else None; self.max_packages = max(1, max_packages)
        if self.root: self.root.mkdir(parents=True, exist_ok=True); self._prune()

    @classmethod
    def from_environment(cls, max_packages=32):
        root = os.getenv('TDDT_KSTORE_ROOT')
        return cls(Path(root) / 'layer7' / 'SETD-KStore' if root else None, max_packages)

    def _prune(self):
        if not self.root: return
        files = sorted([p for p in self.root.glob('*.json') if not p.name.startswith('.')], key=lambda p: p.stat().st_mtime)
        while len(files) > self.max_packages:
            files[0].unlink(missing_ok=True); files.pop(0)

    def aggregate(self, records: list[dict], version: str, context: dict | None = None) -> dict:
        numeric = {}
        for rec in records:
            for k, v in rec.get('residuals', {}).items():
                if isinstance(v, (int, float)): numeric.setdefault(k, []).append(float(v))
        summary = {k: {'mean': sum(v)/len(v), 'max': max(v), 'min': min(v)} for k, v in numeric.items() if v}
        package = {'version': version, 'count': len(records), 'summary': summary, 'context': context or {}}
        package['hash'] = sha256_json(package)
        if self.root:
            staging = self.root / f'.{version}.staging.json'; final = self.root / f'{version}.json'
            atomic_write_json(staging, package); verify = json.loads(staging.read_text())
            if verify.get('hash') != package['hash']: raise RuntimeError('SETD staging verification failed')
            staging.replace(final); self._prune()
        return package
