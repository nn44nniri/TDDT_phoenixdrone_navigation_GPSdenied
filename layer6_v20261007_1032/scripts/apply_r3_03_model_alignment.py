#!/usr/bin/env python3
"""Install the pinned four-foot source contract; never grant flight readiness."""
import argparse
import difflib
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import subprocess

MANIFEST_SHA256 = '2d20179aafe16552b05868940c688dfac460c0972db6b9c2a33f826fbf4a66b0'
OLD_MODELS = {'144483b69a37f99c7616409768fd334a224b54b7a9f300c411515e258fa4b48f',
              'a2812ee4c5d346b58c6c80fa76229854e0e7af4da177ad8ac0c8c653cd848341'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, data):
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name+'.r303.tmp')
    staged.write_bytes(data)
    if path.exists():
        staged.chmod(path.stat().st_mode & 0o777)
    staged.replace(path)


def sources():
    spec = importlib.util.spec_from_file_location('r302_alignment', Path(__file__).with_name('apply_r3_02_environment_alignment.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assets, alignment, profile = module.sources()
    deployed = assets.parent/'r3_03_v1'
    manifest = deployed/'DEPLOYMENT_MANIFEST.json'
    if sha(manifest) != MANIFEST_SHA256:
        raise ValueError('R3.03 deployment manifest identity mismatch')
    contract = json.loads(manifest.read_text())
    patch = deployed/'model_tools_alignment.patch'
    if sha(patch) != contract['patch_sha256']:
        raise ValueError('R3.03 model tools patch identity mismatch')
    for name in ('phoenix_v3.xml', 'reset.xml'):
        if sha(assets/name) != alignment['files'][name]['sha256']:
            raise ValueError('R3.02 aircraft/Reset identity mismatch')
    return assets, alignment, profile, contract, patch


def bridge_bytes(path, alignment):
    digest = sha(path)
    if digest == alignment['native_bridge_after_sha256']:
        return path.read_bytes()
    if digest != alignment['native_bridge_before_sha256']:
        raise ValueError('unrecognized SIM_JSBSim source; refusing overwrite')
    data = path.read_bytes().replace(
        rb'<altitude unit=\"M\"> 0.08 </altitude>',
        rb'<altitudeAGL unit=\"M\"> 0.141025821708132 </altitudeAGL>')
    if hashlib.sha256(data).hexdigest() != alignment['native_bridge_after_sha256']:
        raise ValueError('generated Reset patch output identity mismatch')
    return data


def apply(fs):
    assets, alignment, profile, contract, patch = sources()
    fs = fs.resolve(strict=True)
    model = fs/'models/phoenix_v3'
    aircraft = model/'aircraft/phoenix_v3/phoenix_v3.xml'
    from apply_native_realtime_model import expected_hash
    if sha(aircraft) not in OLD_MODELS | {alignment['files']['phoenix_v3.xml']['sha256'],expected_hash()}:
        raise ValueError('unrecognized packaged aircraft; refusing overwrite')
    expected = contract['tools']
    current = {name: sha(model/name) for name in expected}
    before = {name: value['before_sha256'] for name, value in expected.items()}
    after = {name: value['after_sha256'] for name, value in expected.items()}
    if current not in (before, after):
        raise ValueError('unrecognized model tools; refusing overwrite')
    bridge = fs/'ardupilot/libraries/SITL/SIM_JSBSim.cpp'
    packaged = model/'patched_source/SIM_JSBSim.cpp'
    packaged_data = bridge_bytes(packaged, alignment)
    # Full installer may begin with a stock/v2 checkout. Its existing preinstall
    # installs the pinned packaged source before building; never replace unknown code.
    stock = [sha(model/'reference_sources'/name) for name in ('SIM_JSBSim.cpp.stock', 'SIM_JSBSim.cpp.v2')]
    native_data = None if sha(bridge) in stock else bridge_bytes(bridge, alignment)
    if current == before:
        argv = ['patch', '--batch', '--forward', '-p1', '--input', str(patch)]
        subprocess.run(argv+['--dry-run'], cwd=model, check=True, capture_output=True)
        subprocess.run(argv, cwd=model, check=True, capture_output=True)
    assert {name: sha(model/name) for name in expected} == after
    write(packaged, packaged_data)
    if native_data is not None:
        write(bridge, native_data)
    for source, target in [('SIM_JSBSim.cpp.stock', 'SIM_JSBSim_phoenix_v3.patch'),
                           ('SIM_JSBSim.cpp.v2', 'SIM_JSBSim_phoenix_v3_v2_to_v3.patch')]:
        old = (model/'reference_sources'/source).read_text().splitlines(keepends=True)
        new = packaged_data.decode().splitlines(keepends=True)
        data = ''.join(difflib.unified_diff(old, new, fromfile='a/libraries/SITL/SIM_JSBSim.cpp',
                                          tofile='b/libraries/SITL/SIM_JSBSim.cpp')).encode()
        write(model/'patches'/target, data)
    for name in ('phoenix_v3.xml', 'reset.xml'):
        write(model/'aircraft/phoenix_v3'/name, (assets/name).read_bytes())
    readme = (
        '# Phoenix four-foot native model, R3.03\n\n'
        'The pinned aircraft contains TAIL_FOOT_L1/L2/R1/R2. Structural axes are\n'
        'aft/right/up; body axes are FRD about CG. Initial pitch is 89 degrees,\n'
        'zero body speed, and explicit altitudeAGL is 0.141025821708132 m.\n'
        'Native SITL regenerates latitude, longitude and yaw from its home input.\n'
        'The template and generated Reset have separate recorded hashes.\n\n'
        'This installation does not certify ground load-bearing, forcing binding,\n'
        'controller prediction or flight readiness. R3.04 tests ground support.\n'
        'Original R3.01 references remain immutable in the TDOC resources.\n'
    ).encode()
    write(model/'aircraft/phoenix_v3/README.md',readme)
    write(fs/'ardupilot/Tools/autotest/aircraft/phoenix_v3/README.md',readme)
    write(fs/'data/phoenix_drone_v3.yaml', profile.read_bytes())
    return {'step': 'R3.03', 'status': 'PASS', 'four_foot_source_staged': True,
            'data_profile_sha256': sha(profile), 'native_compile_required': True,
            'flight_ready': False, 'actuation_authority': False}


def refresh_ground_profile(fs):
    env = fs/'phoenix_environment_adapter_v2'
    path = env/'runtime/runtime_environment.json'
    if not path.is_file():
        return {'derived_profile': 'absent; prepare required before use'}
    # Preserve the existing example's climate/time/terrain. This only rebuilds
    # geometry-dependent output; accepted forcing binding remains an R4 task.
    code = """
import json,sys
from pathlib import Path
from phoenix_env_adapter.vehicle import PhoenixVehicle
fs=Path(sys.argv[1]);env=fs/'phoenix_environment_adapter_v2';p=env/'runtime/runtime_environment.json'
data=json.loads(p.read_text());home=data['home'];height=float(home['terrain_msl_m'])
v=PhoenixVehicle(fs/'data/phoenix_drone_v3.yaml');derived=v.ground_vtol_copy(height)
v.write_ground_vtol(env/'runtime/phoenix_drone_v3_ground_vtol.yaml',height)
home.update(initial_agl_m=derived['simulation_assumptions']['initial_agl_m'],
            initial_CG_msl_m=derived['simulation_assumptions']['initial_altitude_msl_m'],position_origin='CG')
data['ground_geometry_revision']='phoenix-fourfoot-r3.02-v1'
p.write_text(json.dumps(data,indent=2)+'\\n')
print(json.dumps({'derived_profile':'rebuilt','forcing_binding_verified':False}))
"""
    result = subprocess.run([str(env/'.venv/bin/python'), '-B', '-c', code, str(fs)],
                            cwd=env, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def relocate_venvs(fs):
    """Repair the two copied venvs without downloading or changing packages."""
    result=[]
    for relative in ('ardupilot/venv-ardupilot-debian13', 'phoenix_environment_adapter_v2/.venv'):
        venv=fs/relative
        if not (venv/'lib').exists() and (venv/'lib64').is_dir():
            (venv/'lib').symlink_to('lib64',target_is_directory=True)
        activate=venv/'bin/activate'
        text=activate.read_text()
        # Python's activate script embeds the creation-time absolute prefix.
        text=re.sub(r'(?m)^(\s*export VIRTUAL_ENV=).*$',lambda m:m[1]+str(venv),text)
        text=re.sub(r'(?m)^(\s*VIRTUAL_ENV=\$\(cygpath ).*\)$',lambda m:m[1]+str(venv)+')',text)
        write(activate,text.encode())
        check=subprocess.run([str(venv/'bin/python'),'-B','-c','import yaml; print(yaml.__file__)'],
                             check=True,capture_output=True,text=True)
        result.append({'venv':str(venv),'yaml_origin':check.stdout.strip()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flightstack-dir', type=Path, required=True)
    parser.add_argument('--refresh-ground-profile', action='store_true')
    parser.add_argument('--repair-venvs-only', action='store_true')
    args = parser.parse_args()
    if args.repair_venvs_only:
        print(json.dumps({'venvs':relocate_venvs(args.flightstack_dir.resolve())}))
        return
    if args.refresh_ground_profile:
        # This post-build operation must not restage the pre-R6 bridge/model.
        # The installer has already validated, overlaid and compiled the stack.
        print(json.dumps(refresh_ground_profile(args.flightstack_dir.resolve())))
        return
    result = apply(args.flightstack_dir)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
