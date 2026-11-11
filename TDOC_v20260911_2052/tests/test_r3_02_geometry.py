"""Geometry/datum contracts and deployment rejection, without native actuation."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import importlib.util
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import yaml
from twins_fmi.ground_geometry import GroundGeometry,structural_to_frd,body_to_ned
from tddt_tdoc.phoenix_frames import euler_quaternion
from tddt_tdoc.phoenix_geometry import aligned_root,geometry_profile,initialization_geometry,validate_native_geometry

ROOT=Path(__file__).resolve().parents[2]


def config(native=None,override=None):
    return SimpleNamespace(path_value=lambda key,required=False:
        ROOT/'twins_layer2_v20260820_1233' if key=='TDDT_TWINS_ROOT' else native or ROOT/'layer6_v20261007_1032',
        get=lambda key,default=None:override if key=='tdoc.online_guidance.ground_cg_height_m' else default)


def test_nonzero_cg_structural_conversion_matches_jsbsim_contract():
    assert structural_to_frd([1.,2.,3.],[.1,.2,.3])==pytest.approx([-.9,1.8,-2.7])


@pytest.mark.parametrize('pitch,height,count',[(89.,.1410258217081318,2),(90.,.14,4)])
def test_four_foot_contact_plane_and_cg_height(pitch,height,count):
    q=euler_quaternion(0.,math.radians(pitch),.9)
    data=initialization_geometry(config(),q)
    assert data['cg_height_agl_m']==pytest.approx(height,abs=1e-12)
    assert sum(abs(v)<1e-12 for v in data['foot_clearances_m'].values())==count
    assert min(data['foot_clearances_m'].values())>=-1e-12
    assert not data['ground_load_bearing_verified'] and not data['actuation_authority']


def test_old_reset_penetrates_and_old_cg_default_floats_above_plane():
    _,geometry=geometry_profile(config());q=euler_quaternion(0.,math.radians(89.),0.)
    assert min(geometry.clearances(.08,q).values())==pytest.approx(-.0610258217081318)
    assert min(geometry.clearances(.24,q).values())==pytest.approx(.09897417829186819)


def test_aligned_reset_uses_explicit_agl_and_original_reference_stays_unchanged():
    reset=ET.parse(aligned_root()/'reset.xml').getroot()
    assert reset.find('altitude') is None
    assert float(reset.find('altitudeAGL').text)==pytest.approx(.1410258217081318)
    assert float(reset.find('theta').text)==89.
    from tddt_tdoc.phoenix_reference import inspect_reference
    assert inspect_reference()['reset']['altitude']['value']==.08


def test_canonical_geometry_is_shared_by_layer2_and_layer5_profiles():
    a=yaml.safe_load((ROOT/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml').read_text())
    b=yaml.safe_load((ROOT/'layer5_v20260911_1510/resources/phoenix_drone_v3.yaml').read_text())
    assert a['ground_geometry']==b['ground_geometry']
    assert 'jsbsim_ground_contact_v071' not in a and 'jsbsim_ground_contact_v071' not in b
    assert a['inertia']['inertial_pose_xyz_m']==b['inertia']['inertial_pose_xyz_m']==[0.,0.,0.]


@pytest.mark.parametrize('q',[(0.,0.,0.,0.),(float('nan'),0.,0.,0.)])
def test_invalid_live_quaternion_is_rejected_without_fallback(q):
    with pytest.raises(ValueError):initialization_geometry(config(),q)


def test_old_cg_override_is_rejected_instead_of_overriding_geometry():
    with pytest.raises(ValueError,match='contradicts'):
        initialization_geometry(config(override=.24),euler_quaternion(0.,math.radians(89.),0.))


@pytest.mark.parametrize('fault',['missing','frame','body','cg','schema','structural_frame','nan_height'])
def test_inconsistent_layer2_geometry_rejects(fault):
    profile,_=geometry_profile(config());profile=deepcopy(profile)
    if fault=='missing':profile.pop('ground_geometry')
    elif fault=='frame':profile['ground_geometry']['body_frame']='STRUCTURAL'
    elif fault=='body':profile['ground_geometry']['contacts'][0]['body_frd_m'][0]=.14
    elif fault=='cg':profile['ground_geometry']['cg_structural_m'][0]=.1
    elif fault=='schema':profile['ground_geometry']['schema']='unknown'
    elif fault=='structural_frame':profile['ground_geometry']['structural_frame']='BODY_FRD_CG'
    elif fault=='nan_height':profile['ground_geometry']['cg_height_agl_at_reset_m']=float('nan')
    with pytest.raises(ValueError):GroundGeometry(profile)


def test_old_native_deployment_is_rejected_before_binding(tmp_path):
    target=tmp_path/'FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3'
    target.mkdir(parents=True)
    (target/'phoenix_v3.xml').write_text('<contact name="BELLY_GUARD"/>')
    with pytest.raises(ValueError,match='R3.03 required'):
        validate_native_geometry(config(tmp_path))


def test_native_reset_patch_applies_only_to_isolated_source_and_binds_geometry(tmp_path):
    manifest=json.loads((aligned_root()/'ALIGNMENT_MANIFEST.json').read_text())
    native=tmp_path/'native';ap=native/'FlightStack/ardupilot'
    bridge=ap/'libraries/SITL/SIM_JSBSim.cpp';bridge.parent.mkdir(parents=True)
    source=ROOT/'layer6_v20261007_1032/FlightStack/models/phoenix_v3/patched_source/SIM_JSBSim.cpp'
    before=source.read_bytes();bridge.write_bytes(before)
    if hashlib.sha256(before).hexdigest()==manifest['native_bridge_after_sha256']:
        subprocess.run(['patch','--batch','--reverse','-p1','--input',str(aligned_root()/'native_reset_alignment.patch')],cwd=ap,check=True,capture_output=True)
    old_bridge=bridge.read_bytes()
    result=subprocess.run(['patch','-p1','--input',str(aligned_root()/'native_reset_alignment.patch')],cwd=ap,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert hashlib.sha256(bridge.read_bytes()).hexdigest()==manifest['native_bridge_after_sha256']
    target=ap/'Tools/autotest/aircraft/phoenix_v3';target.mkdir(parents=True)
    for name in ('phoenix_v3.xml','reset.xml'):shutil.copyfile(aligned_root()/name,target/name)
    admitted=validate_native_geometry(config(native))
    assert admitted['revision']==manifest['revision'] and not admitted['flight_ready']
    assert source.read_bytes()==before
    bridge.write_bytes(old_bridge)
    with pytest.raises(ValueError,match='generated Reset geometry mismatch'):
        validate_native_geometry(config(native))


def test_environment_factory_uses_same_cg_agl_and_home_terrain_datum():
    path=ROOT/'layer6_v20261007_1032/FlightStack/phoenix_environment_adapter_v2/phoenix_env_adapter/vehicle.py'
    spec=importlib.util.spec_from_file_location('r302_vehicle',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    vehicle=module.PhoenixVehicle(ROOT/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml')
    before=deepcopy(vehicle.data);result=vehicle.ground_vtol_copy(1800.)
    sim=result['simulation_assumptions']
    assert sim['initial_agl_m']==pytest.approx(.1410258217081318)
    assert sim['initial_altitude_msl_m']==pytest.approx(1800.141025821708)
    assert sim['home_alt_msl_m_default']==1800. and vehicle.data==before
    vehicle.data.pop('ground_geometry')
    with pytest.raises(ValueError):vehicle.ground_vtol_copy(1800.)


def test_both_fmus_embed_current_four_foot_profile():
    import zipfile
    profile=ROOT/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml'
    for name in ('MESA_NAV','WingMPET'):
        with zipfile.ZipFile(ROOT/'twins_layer2_v20260820_1233/fmus'/f'{name}.fmu') as archive:
            raw=archive.read('resources/phoenix_drone_v3.yaml')
        assert raw==profile.read_bytes()
        assert len(GroundGeometry(yaml.safe_load(raw)).contacts)==4


def test_environment_preparation_separates_home_terrain_from_cg(tmp_path,monkeypatch):
    import importlib
    envroot=ROOT/'layer6_v20261007_1032/FlightStack/phoenix_environment_adapter_v2'
    monkeypatch.syspath_prepend(str(envroot))
    prepare=importlib.import_module('phoenix_env_adapter.prepare')
    assert Path(prepare.__file__).resolve().is_relative_to(envroot)
    monkeypatch.setattr(prepare.DatasetLayout,'from_tar',lambda *a:SimpleNamespace(validate=lambda:{'dem_path':'fixture'},climate_dir='fixture'))
    point=SimpleNamespace(lat=40.01,lon=-105.3)
    monkeypatch.setattr(prepare,'Region',lambda *a:SimpleNamespace(start=point,destination=point,name='fixture',bbox=[]))
    monkeypatch.setattr(prepare,'TerrainProvider',lambda *a:SimpleNamespace(elevation_msl=lambda *p:1800.,close=lambda:None))
    weather=SimpleNamespace(temperature_c=20.,relative_humidity_pct=40.,wind_speed_m_s=0.,wind_direction_from_deg=0.,wind_north_m_s=0.,wind_east_m_s=0.)
    monkeypatch.setattr(prepare,'ClimateProvider',lambda *a:SimpleNamespace(sample=lambda *p:weather))
    result=prepare.prepare('fixture','fixture',ROOT/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml',
                           '2026-10-29T00:00:00Z',tmp_path)
    assert result['home']['terrain_msl_m']==1800.
    assert result['home']['initial_CG_msl_m']==pytest.approx(1800.141025821708)
    assert result['home']['initial_agl_m']==pytest.approx(.1410258217081318)
    assert ',1800.00,' in (tmp_path/'locations.txt.fragment').read_text()


def test_installer_reapplies_environment_alignment_after_archive_extraction(tmp_path):
    # Reconstruct the exact two original source files by reversing the pinned
    # patch; the external full installer archive is not part of this bundle.
    import sys
    import tarfile
    native=ROOT/'layer6_v20261007_1032'
    helper=native/'scripts/apply_r3_02_environment_alignment.py'
    installer=native/'scripts/install_flightstack_rev1.sh'
    subprocess.run(['bash','-n',str(installer)],check=True)
    text=installer.read_text()
    assert text.index('tar -xzf "$ENV_ARCHIVE"')<text.index('python3 "$ALIGNMENT_HELPER" --environment-root')<text.index('cd "$FLIGHTSTACK_DIR/phoenix_environment_adapter_v2"')
    assert 'VEHICLE_FILE="$(python3 "$ALIGNMENT_HELPER" --print-profile)"' in text
    original=tmp_path/'original';original.mkdir()
    for name in ('vehicle.py','prepare.py'):
        dest=original/'phoenix_env_adapter'/name;dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(native/'FlightStack/phoenix_environment_adapter_v2/phoenix_env_adapter'/name,dest)
    subprocess.run(['patch','--batch','--reverse','-p1','--input',str(aligned_root()/'environment_geometry_alignment.patch')],cwd=original,check=True,capture_output=True)
    archive=tmp_path/'original_sources.tar.gz'
    with tarfile.open(archive,'w:gz') as stream:stream.add(original,arcname='adapter')
    target=tmp_path/'installed';target.mkdir()
    subprocess.run(['tar','-xzf',str(archive),'--strip-components=1','-C',str(target)],check=True)
    for _ in range(2):
        result=subprocess.run([sys.executable,'-B',str(helper),'--environment-root',str(target)],check=True,capture_output=True,text=True)
        assert json.loads(result.stdout)['environment_source_aligned']
    profile=subprocess.run([sys.executable,'-B',str(helper),'--print-profile'],check=True,capture_output=True,text=True)
    assert Path(profile.stdout.strip())==ROOT/'twins_layer2_v20260820_1233/config/phoenix_drone_v3.yaml'
    (target/'phoenix_env_adapter/vehicle.py').write_text('unrecognized source')
    rejected=subprocess.run([sys.executable,'-B',str(helper),'--environment-root',str(target)],capture_output=True,text=True)
    assert rejected.returncode!=0 and 'refusing to overwrite' in rejected.stderr
