#!/usr/bin/env python3
"""Install the exact native PQR transport; no model, knowledge or binary writes."""
import argparse
import hashlib
import json
from pathlib import Path

MANIFEST_SHA256='4945da1b7ba7b117cc226951692473f5e5d06c7437b39c13cbe5c4d5681da456'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sources():
    root=Path(__file__).resolve().parents[1]/'resources/r6_04_body_rates_v1'
    manifest=root/'OVERLAY_MANIFEST.json'
    if sha(manifest)!=MANIFEST_SHA256:
        raise ValueError('body-rate overlay manifest identity mismatch')
    data=json.loads(manifest.read_text())
    for rel,pins in data['files'].items():
        for phase in ('before','after'):
            expected=pins[phase+'_sha256']
            if expected is not None and sha(root/phase/Path(rel).name)!=expected:
                raise ValueError('body-rate overlay resource mismatch: '+rel)
    return root,data


def apply(ardupilot_root,restore_r303_base=False):
    root,data=sources();ap=Path(ardupilot_root).resolve(strict=True)
    bridge=ap/'libraries/SITL/SIM_JSBSim.cpp'
    legacy=sha(bridge) in data['legacy_bridge_sha256']
    if restore_r303_base and legacy:
        # R3.03 owns validation/staging of these exact stock/v2 references.
        return {'status':'PASS','legacy_source_delegated_to_R303':True,'flight_authorized':False}
    for rel,pins in data['files'].items():
        if sha(ap/rel) not in {pins['before_sha256'],pins['after_sha256']}:
            raise ValueError('unknown native source; refusing overwrite: '+rel)
    phase='before' if restore_r303_base else 'after'
    for rel,pins in data['files'].items():
        expected=pins[phase+'_sha256'];target=ap/rel
        if expected is None or sha(target)==expected:
            continue # The known decoder can remain unused during the base stage.
        source=root/phase/Path(rel).name;staged=target.with_name(target.name+'.r604.tmp')
        staged.write_bytes(source.read_bytes());staged.chmod(target.stat().st_mode & 0o777 if target.exists() else 0o644)
        staged.replace(target)
    return {'status':'PASS','revision':data['revision'],'phase':phase,'source_only':True,
            'native_compile_required':True,'flight_authorized':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--ardupilot-root',required=True,type=Path)
    p.add_argument('--restore-r303-base',action='store_true');args=p.parse_args()
    print(json.dumps(apply(args.ardupilot_root,args.restore_r303_base)))


if __name__=='__main__':main()
