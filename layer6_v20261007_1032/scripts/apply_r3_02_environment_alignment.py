#!/usr/bin/env python3
"""Preserve R3.02 environment source through FlightStack archive extraction."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

MANIFEST_SHA256 = '402c4e0a9366065996ad06dbbcda4e9101213677c6539dc190172d257983a386'
PROFILE_SHA256 = '68f3f4e611e370aac01607345a3d972e4f6627ce9b75f8c82052bdcff4fbbd89'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources():
    bundle = Path(__file__).resolve().parents[2]
    assets = bundle/'TDOC_v20260911_2052/src/tddt_tdoc/resources/phoenix_fourfoot/r3_02_v1'
    path = assets/'ALIGNMENT_MANIFEST.json'
    if sha(path) != MANIFEST_SHA256:
        raise ValueError('R3.02 alignment manifest identity mismatch')
    profile = bundle/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml'
    if sha(profile) != PROFILE_SHA256:
        raise ValueError('R3.02 canonical environment profile identity mismatch')
    return assets, json.loads(path.read_text()), profile


def apply(environment_root):
    assets, manifest, profile = sources()
    patch = assets/'environment_geometry_alignment.patch'
    if sha(patch) != manifest['files'][patch.name]['sha256']:
        raise ValueError('R3.02 environment patch identity mismatch')
    root = environment_root.resolve(strict=True)
    expected = manifest['environment_adapter_sources']
    current = {name: sha(root/name) for name in expected}
    before = {name: row['before_sha256'] for name, row in expected.items()}
    after = {name: row['after_sha256'] for name, row in expected.items()}
    if current == before:
        argv = ['patch', '--batch', '--forward', '-p1', '--input', str(patch)]
        subprocess.run(argv+['--dry-run'], cwd=root, check=True, capture_output=True)
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    elif current != after:
        raise ValueError('unrecognized environment source; refusing to overwrite')
    if {name: sha(root/name) for name in expected} != after:
        raise ValueError('R3.02 installed environment source identity mismatch')
    return {'step': 'R3.02', 'status': 'PASS', 'environment_source_aligned': True,
            'profile': str(profile), 'profile_sha256': PROFILE_SHA256,
            'native_model_installed': False, 'flight_ready': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--print-profile', action='store_true')
    group.add_argument('--environment-root', type=Path)
    args = parser.parse_args()
    if args.print_profile:
        print(sources()[2])
    else:
        print(json.dumps(apply(args.environment_root)))


if __name__ == '__main__':
    main()
