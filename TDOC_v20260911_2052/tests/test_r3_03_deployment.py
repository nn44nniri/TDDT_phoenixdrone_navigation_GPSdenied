"""Native deployment contracts and installer persistence; no command authority."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from tddt_tdoc.phoenix_geometry import aligned_root,validate_native_geometry

ROOT=Path(__file__).resolve().parents[2]
NATIVE=ROOT/'layer6_v20261007_1032'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def config(native):return SimpleNamespace(path_value=lambda *a,**kw:native)


def helper():
    path=NATIVE/'scripts/apply_r3_03_model_alignment.py'
    spec=importlib.util.spec_from_file_location('r303_deployer',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def installed(tmp_path):
    target=tmp_path/'FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3';target.mkdir(parents=True)
    for name in ('phoenix_v3.xml','reset.xml'):shutil.copyfile(aligned_root()/name,target/name)
    bridge=tmp_path/'FlightStack/ardupilot/libraries/SITL/SIM_JSBSim.cpp';bridge.parent.mkdir(parents=True)
    shutil.copyfile(NATIVE/'FlightStack/ardupilot/libraries/SITL/SIM_JSBSim.cpp',bridge)
    return tmp_path,target


def test_installed_native_pair_and_data_use_pinned_four_foot_contract():
    fs=NATIVE/'FlightStack'
    assert (fs/'data/phoenix_drone_v3.yaml').read_bytes()==(ROOT/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml').read_bytes()
    assert sha(fs/'models/phoenix_v3/aircraft/phoenix_v3/phoenix_v3.xml')==sha(aligned_root()/'phoenix_v3.xml')
    data=validate_native_geometry(config(NATIVE))
    assert not data['flight_ready'] and not data['reset_origin_binding_verified']


def test_dynamic_reset_origin_changes_only_geometry_acceptance(installed):
    native,target=installed
    path=target/'reset.xml'
    path.write_text(path.read_text().replace('40.010000','40.020000').replace('53.000000','0.000000'))
    result=validate_native_geometry(config(native))
    assert result['installed_reset_sha256']==sha(path)
    assert not result['reset_origin_binding_verified'] and not result['actuation_authority']


@pytest.mark.parametrize('old,new',[
    ('0.141025821708132','0.08'),('unit="M"','unit="FT"'),
    ('altitudeAGL','altitude'),('40.010000','nan'),('40.010000','140.010000'),
    ('</initialize>','<gamma>0</gamma></initialize>'),
    ('89.0','90.0'),('<ubody unit="FT/SEC"> 0.0','<ubody unit="FT/SEC"> 1.0')])
def test_bad_runtime_reset_is_rejected(installed,old,new):
    native,target=installed;p=target/'reset.xml';p.write_text(p.read_text().replace(old,new))
    with pytest.raises(ValueError):validate_native_geometry(config(native))


def test_archive_extraction_then_real_preinstall_retains_four_foot_model(tmp_path):
    import tarfile
    module=helper();assets,alignment,profile,contract,patch=module.sources()
    fixture=tmp_path/'fixture';model=fixture/'models/phoenix_v3'
    shutil.copytree(NATIVE/'FlightStack/models/phoenix_v3',model)
    # Reproduce exact pre-R3.03 validator/patch installer and Reset generator.
    subprocess.run(['patch','--batch','--reverse','-p1','--input',str(patch)],cwd=model,check=True,capture_output=True)
    packaged=model/'patched_source/SIM_JSBSim.cpp'
    raw=packaged.read_bytes().replace(rb'<altitudeAGL unit=\"M\"> 0.141025821708132 </altitudeAGL>',rb'<altitude unit=\"M\"> 0.08 </altitude>')
    assert hashlib.sha256(raw).hexdigest()==alignment['native_bridge_before_sha256']
    packaged.write_bytes(raw)
    bridge=fixture/'ardupilot/libraries/SITL/SIM_JSBSim.cpp';bridge.parent.mkdir(parents=True);bridge.write_bytes(raw)
    archive=tmp_path/'legacy_sources.tar.gz'
    with tarfile.open(archive,'w:gz') as stream:stream.add(fixture,arcname='.')
    fs=tmp_path/'installed';fs.mkdir()
    subprocess.run(['tar','-xzf',str(archive),'-C',str(fs)],check=True)
    result=module.apply(fs)
    assert result['four_foot_source_staged'] and not result['flight_ready']
    model=fs/'models/phoenix_v3'
    (fs/'ardupilot/waf').symlink_to(NATIVE/'FlightStack/ardupilot/waf')
    subprocess.run(['bash',str(model/'scripts/preinstall.sh'),'--ardupilot-root',str(fs/'ardupilot'),'--no-clean'],check=True,capture_output=True)
    assert sha(fs/'ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml')==sha(assets/'phoenix_v3.xml')
    assert sha(fs/'ardupilot/Tools/autotest/aircraft/phoenix_v3/reset.xml')==sha(assets/'reset.xml')
    assert (fs/'data/phoenix_drone_v3.yaml').read_bytes()==profile.read_bytes()
    before=sha(fs/'ardupilot/libraries/SITL/SIM_JSBSim.cpp')
    module.apply(fs)
    assert sha(fs/'ardupilot/libraries/SITL/SIM_JSBSim.cpp')==before
    (fs/'ardupilot/libraries/SITL/SIM_JSBSim.cpp').write_text('unrecognized source')
    with pytest.raises(ValueError,match='refusing overwrite'):module.apply(fs)


def test_installer_shares_the_aligned_model_installation_path():
    p=NATIVE/'scripts/install_flightstack_rev1.sh';text=p.read_text()
    subprocess.run(['bash','-n',str(p)],check=True)
    assert text.count('install_aligned_model')==3
    assert text.index('tar -xzf "$MODEL_ARCHIVE"')<text.rindex('install_aligned_model')
    assert 'export PYTHON="$AP_VENV/bin/python"' in text
