"""Preserve the PASS model/prior/forcing identity without touching control state."""
from pathlib import Path
import hashlib
import json


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def tree_hashes(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): file_hash(p)
            for p in sorted(root.rglob('*')) if p.is_file()}


def snapshot(cfg):
    from tddt_tdoc.model_compatibility import capture_model_dependencies
    from tddt_tdoc.climate_forcing import resolve_climate_forcing
    from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
    forcing = resolve_climate_forcing(cfg.path_value('TDDT_CLIMATE_ROOT'), require_exists=True)
    handoff = cfg.runtime_root / 'knowledge/handoff/offline_latest.json.gz'
    gate = OfflineKnowledgeHandoff(cfg.runtime_root).validate_online_start()
    return {
        'model_dependencies': capture_model_dependencies(cfg),
        'config_sha256': file_hash(cfg.path),
        'handoff_sha256': file_hash(handoff) if handoff.is_file() else None,
        'handoff_gate': {k: gate.get(k) for k in ('ok', 'status', 'reason')},
        'forcing_sha256': file_hash(forcing), 'forcing_file': str(forcing),
        'kstore_files': tree_hashes(cfg.path_value('TDDT_LAYER5_KSTORE_ROOT')),
        'offline_reports': tree_hashes(cfg.package_root / 'reports/offline'),
    }


def compare(before, after):
    checks = {name: before[name] == after[name] for name in (
        'model_dependencies', 'config_sha256', 'handoff_sha256',
        'forcing_sha256', 'kstore_files', 'offline_reports')}
    return {'status': 'PASS' if all(checks.values()) else 'FAIL',
            'checks': checks, 'model_id': after['model_dependencies']['model_id'],
            'scope': 'control sources, binaries, prior, forcing and Offline reports unchanged'}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
