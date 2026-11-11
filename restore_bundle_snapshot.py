#!/usr/bin/env python3
"""Verify the complete exported bundle and optionally restore nested Git metadata."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile


def file_sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--restore-git-metadata', action='store_true')
    ap.add_argument('--repair-checkout', action='store_true', help='Restore normalized text from exact committed Git blobs')
    args = ap.parse_args()
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / 'bundle_metadata/SNAPSHOT_MANIFEST.json').read_text())
    rows = manifest['files']
    metadata = {r['path']: r for r in rows if '.git' in Path(r['path']).parts}
    for a in manifest['nested_git_archives']:
        path = root / a['path']
        if file_sha(path) != a['sha256']:
            raise ValueError('Metadata archive hash mismatch: ' + a['path'])
        with tarfile.open(path, 'r:gz') as archive:
            members = archive.getmembers()
            if len(members) != a['members']:
                raise ValueError('Archive member count mismatch')
            for member in members:
                rel = Path(member.name)
                if rel.is_absolute() or '..' in rel.parts or member.name not in metadata:
                    raise ValueError('Unexpected archive member')
                row = metadata[member.name]
                if row['type'] != 'file' or not member.isfile():
                    raise ValueError('Unexpected Git metadata type')
                raw = archive.extractfile(member).read()
                if hashlib.sha256(raw).hexdigest() != row['sha256']:
                    raise ValueError('Archived metadata differs from source')
                if args.restore_git_metadata:
                    target = root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.parent.resolve().is_relative_to(root):
                        raise ValueError('External metadata destination')
                    target.write_bytes(raw)
                    target.chmod(row['mode'])
    bad = []
    repaired = []
    direct = 0
    for row in rows:
        if row['path'] in metadata and not args.restore_git_metadata:
            continue
        p = root / row['path']
        direct += 1
        if row['type'] == 'symlink':
            valid = p.is_symlink() and os.readlink(p) == row['target']
        else:
            valid = p.is_file() and not p.is_symlink() and p.stat().st_size == row['bytes'] and file_sha(p) == row['sha256']
            if not valid and args.repair_checkout and row['path'] not in metadata:
                # show returns the exact Git blob, independent of EOL attributes.
                raw = subprocess.check_output(['git', 'show', 'HEAD:' + row['path']], cwd=root)
                if hashlib.sha256(raw).hexdigest() == row['sha256']:
                    p.write_bytes(raw)
                    p.chmod(row['mode'])
                    valid = True
                    repaired.append(row['path'])
        if not valid:
            bad.append(row['path'])
        if direct % 25000 == 0:
            print('verified paths', direct, flush=True)
    report = {'status': 'PASS' if not bad else 'FAIL', 'verified_direct_paths': direct,
              'archived_git_paths': len(metadata), 'git_metadata_restored': args.restore_git_metadata,
              'source_file_count': len(rows), 'failures': bad, 'repaired_checkout_paths': repaired,
              'excluded_markdown_count': manifest['inventory']['excluded_markdown_count'],
              'README_count': manifest['inventory']['README_count']}
    (root / 'TRANSFER_VERIFICATION.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
