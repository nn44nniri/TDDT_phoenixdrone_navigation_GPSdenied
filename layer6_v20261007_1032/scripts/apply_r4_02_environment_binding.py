#!/usr/bin/env python3
"""Install the canonical R4.02 Environment Adapter overlay after extraction."""
import argparse
import hashlib
import json
from pathlib import Path

MANIFEST_SHA256='431d7b1306cbb9ddc8ef5aee2b55b7d7ff50035a67b3b27d7be1ad8c6c596b9e'


def apply(root):
    assets=Path(__file__).resolve().parents[1]/'resources/r4_02_v1'
    raw=(assets/'OVERLAY_MANIFEST.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=MANIFEST_SHA256:raise ValueError('R4.02 overlay manifest hash mismatch')
    manifest=json.loads(raw)
    root=Path(root).resolve(strict=True)
    prepared=[]
    for name,row in manifest['files'].items():
        source=assets/name;data=source.read_bytes();target=root/name
        if hashlib.sha256(data).hexdigest()!=row['after_sha256']:raise ValueError('R4.02 overlay source hash mismatch')
        actual=hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        if actual not in [row.get('before_sha256'),row['after_sha256']]+row.get('upgrade_from_sha256',[]):raise ValueError('unrecognized Environment Adapter source: '+name)
        prepared.append((target,data))
    for target,data in prepared:
        if target.is_file() and target.read_bytes()==data:continue
        target.parent.mkdir(parents=True,exist_ok=True);tmp=target.with_suffix('.r402.tmp');tmp.write_bytes(data);tmp.replace(target)
    return {'step':'R4.02','status':'PASS','files':list(manifest['files']),'environment_started':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--environment-root',required=True,type=Path)
    print(json.dumps(apply(p.parse_args().environment_root)))
