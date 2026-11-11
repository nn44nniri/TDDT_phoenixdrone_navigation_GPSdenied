#!/usr/bin/env python3
"""R3.02 geometry, isolated native FMU rebuild, reset/wheel/bytecode proof."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def audit(tdoc,out,build_python,publish):
    bundle=tdoc.parent;l2=bundle/'twins_layer2_v20260820_1233';env={k:v for k,v in os.environ.items() if not k.startswith('TDDT_') and k!='PYTHONPATH'}
    env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    def run(argv,cwd,name):
        r=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
        (out/name).write_text(r.stdout+r.stderr)
        if r.returncode:raise RuntimeError(f'{name}: {r.returncode}')
        return r
    with tempfile.TemporaryDirectory(prefix='tddt_R302_',dir='/tmp') as tmp:
        scratch=Path(tmp);package=scratch/tdoc.name;package.mkdir()
        shutil.copytree(tdoc/'src',package/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
        shutil.copyfile(tdoc/'pyproject.toml',package/'pyproject.toml');(package/'tests').mkdir()
        shutil.copyfile(tdoc/'tests/test_r3_02_geometry.py',package/'tests/test_r3_02_geometry.py')
        twins=scratch/l2.name
        shutil.copytree(l2,twins,ignore=shutil.ignore_patterns('__pycache__','*.pyc','build','fmus','.pytest_cache'))
        for rel in ['layer5_v20260911_1510/resources/phoenix_drone_v3.yaml',
            'layer6_v20261007_1032/scripts/install_flightstack_rev1.sh',
            'layer6_v20261007_1032/scripts/apply_r3_02_environment_alignment.py',
            'layer6_v20261007_1032/FlightStack/ardupilot/libraries/SITL/SIM_JSBSim.cpp',
            'layer6_v20261007_1032/FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml',
            'layer6_v20261007_1032/FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3/reset.xml',
            'layer6_v20261007_1032/FlightStack/phoenix_environment_adapter_v2/phoenix_env_adapter/vehicle.py']:
            target=scratch/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(bundle/rel,target)
        envrel='layer6_v20261007_1032/FlightStack/phoenix_environment_adapter_v2/phoenix_env_adapter'
        shutil.copytree(bundle/envrel,scratch/envrel,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        expected={str(p.relative_to(package)):sha(p) for p in (package/'src/tddt_tdoc/resources/phoenix_fourfoot/r3_02_v1').iterdir()}
        expected.update({p:sha(package/p) for p in ['src/tddt_tdoc/phoenix_geometry.py','src/tddt_tdoc/online_mission.py','src/tddt_tdoc/native_environment.py','pyproject.toml']})
        for folder in ('runtime','reports','logs'):
            (package/folder).mkdir();(package/folder/'GENERATED_SENTINEL').write_text('isolated data')
        code="""
import json,os,sys
from pathlib import Path
p=Path.cwd();assert p.parent.name.startswith('tddt_R302_')
assert not any(k.startswith('TDDT_') for k in os.environ)
sys.path.insert(0,str(p/'src'))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.reset import reset_tdoc
r=reset_tdoc(TDOCConfig(p/'configs/tdoc.yaml',p,{'runtime':{'root':'runtime'}}),include_external=False)
assert r['ok'] and not r['dry_run']
assert all(not (p/n/'GENERATED_SENTINEL').exists() for n in ('runtime','reports','logs'))
print(json.dumps(r))
"""
        result=run([sys.executable,'-B','-c',code],package,'ISOLATED_RESET.log')
        (out/'ISOLATED_RESET_RESULT.json').write_text(result.stdout)
        assert expected=={p:sha(package/p) for p in expected}
        run(['bash',str(twins/'scripts/build_all.sh')],twins,'L2_NATIVE_BUILD.log')
        new_fmus=out/'rebuilt_fmus';new_fmus.mkdir()
        for name in ('MESA_NAV','WingMPET'):
            shutil.copyfile(twins/'fmus'/f'{name}.fmu',new_fmus/f'{name}.fmu')
            with zipfile.ZipFile(new_fmus/f'{name}.fmu') as z:
                assert z.read('resources/phoenix_drone_v3.yaml')==(l2/'config/phoenix_drone_v3.yaml').read_bytes()
        # Real native free-flight calculation, on private FMI instances. Geometry
        # metadata must not silently alter the existing airborne equation law.
        from twins_fmi import FormulaFMU,DroneConfig
        values=[]
        for path in (tdoc/'reports/online/R3_02_20261029_geometry_v4/before'/l2.name/'fmus/WingMPET.fmu',new_fmus/'WingMPET.fmu'):
            f=FormulaFMU(path,config=DroneConfig(l2/'config/phoenix_drone_v3.yaml'))
            try:
                c=math.sqrt(.5)
                values.append(f.evaluate_operation(27,dt=.01,pn=0,pe=0,pd=-1800.2,vn=0,ve=0,vd=0,
                    qw=c,qx=0,qy=c,qz=0,p_rate=0,q_rate=0,r_rate=0,omega_left=650,omega_right=650,soc=1,
                    left_motor_cmd=.82,right_motor_cmd=.82,left_elevon_rad=0,right_elevon_rad=0,
                    wind_n=0,wind_e=0,wind_d=0,air_density=1.225))
            finally:f.close()
        error=max(abs(values[0][k]-values[1][k]) for k in values[0]);assert error<1e-10
        write(out/'FMU_REBUILD_RESULT.json',{'status':'PASS','free_flight_max_abs_difference':error,
            'fmus':{p.name:{'sha256':sha(p),'bytes':p.stat().st_size} for p in new_fmus.iterdir()},
            'four_foot_profile_embedded':True,'ground_contact_force_solver_added':False})
        wheels=out/'rebuilt_wheel';wheels.mkdir()
        code="from setuptools import build_meta;print(build_meta.build_wheel("+repr(str(wheels))+"))"
        run([build_python,'-B','-c',code],package,'WHEEL_BUILD.log')
        wheel,=wheels.glob('*.whl');target=scratch/'installed'
        run([sys.executable,'-B','-m','pip','--no-cache-dir','install','--no-index','--no-deps','--no-compile',
            '--target',str(target),str(wheel)],scratch,'WHEEL_INSTALL.log')
        code="""
import hashlib,importlib,importlib.machinery,importlib.util,json,py_compile,sys
from pathlib import Path
target,package,twins,output=map(Path,sys.argv[1:5]);mode=sys.argv[5]
sys.path.insert(0,str(target));sys.path.insert(0,str(twins/'python'))
hashes={}
for name in ('phoenix_geometry','online_mission','native_environment'):
 full='tddt_tdoc.'+name;m=importlib.import_module(full);p=Path(m.__file__);assert p.is_relative_to(target)
 hashes['src/tddt_tdoc/'+name+'.py']=hashlib.sha256(p.read_bytes()).hexdigest()
 if mode=='bytecode':
  dest=p.with_suffix('.r302.pyc');py_compile.compile(str(p),cfile=str(dest),doraise=True)
  loader=importlib.machinery.SourcelessFileLoader(full,str(dest));spec=importlib.util.spec_from_loader(full,loader)
  m=importlib.util.module_from_spec(spec);sys.modules[full]=m;loader.exec_module(m)
  setattr(sys.modules['tddt_tdoc'],name,m)
import pytest
rc=pytest.main(['-q','-p','no:cacheprovider',str(package/'tests/test_r3_02_geometry.py')]);assert rc==0
(output/('POST_'+mode.upper()+'_GEOMETRY.json')).write_text(json.dumps({'status':'PASS','mode':mode,'source_hashes':hashes},indent=2))
"""
        for mode in ('wheel','bytecode'):
            run([sys.executable,'-B','-c',code,str(target),str(package),str(twins),str(out),mode],scratch,'TEST_'+mode.upper()+'.log')
            hashes=json.loads((out/('POST_'+mode.upper()+'_GEOMETRY.json')).read_text())['source_hashes']
            assert hashes=={k:expected[k] for k in hashes}
        assert expected=={p:sha(package/p) for p in expected}
        if publish:
            for name in ('MESA_NAV','WingMPET'):
                destination=l2/'fmus'/f'{name}.fmu'
                backup=tdoc/'reports/online/R3_02_20261029_geometry_v4/before'/l2.name/'fmus'/destination.name
                assert sha(destination) in {sha(backup),sha(new_fmus/destination.name)},'active FMU changed since validated backup/build'
                staged=destination.with_suffix('.r302.tmp');shutil.copyfile(new_fmus/destination.name,staged);os.replace(staged,destination)
        write(out/'PERSISTENCE_RESULT.json',{'status':'PASS','isolated_actual_reset':True,'active_reset':False,
            'L2_native_compile':True,'L2_FMUs_published':publish,'source_hashes':expected,
            'wheel':wheel.name,'wheel_sha256':sha(wheel),'wheel_tests':'PASS','bytecode_tests':'PASS',
            'native_Layer6_compile':False,'native_bridge_modified':False,'native_execution':False})


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--publish-fmus',action='store_true');p.add_argument('--build-python',default='/usr/bin/python3')
    a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];out=a.output_dir.resolve()
    out.relative_to(tdoc/'reports/online');out.mkdir(parents=True,exist_ok=False)
    audit(tdoc,out,a.build_python,a.publish_fmus)
    print(json.dumps({'step':'R3.02','status':'PASS','FMUs_published':a.publish_fmus,'native_execution':False}))


if __name__=='__main__':main()
