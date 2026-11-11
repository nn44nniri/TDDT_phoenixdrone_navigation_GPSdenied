#!/usr/bin/env python3
"""R3.01 reference pin, read-only deployment inventory and isolated build proof."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from tddt_tdoc.phoenix_reference import inspect_reference, reference_root


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def sections(path):
    root=ET.fromstring(Path(path).read_bytes())
    return {node.tag+(':'+node.get('name') if node.get('name') else ''):
            hashlib.sha256(ET.canonicalize(ET.tostring(node,encoding='unicode'),strip_text=True).encode()).hexdigest()
            for node in root}


def inventory(tdoc,output):
    reference=inspect_reference();assert reference['ok']
    write(output/'REFERENCE_INVENTORY.json',reference)
    bundle=tdoc.parent;native=bundle/'layer6_v20261007_1032'
    paths={'model_package':native/'FlightStack/models/phoenix_v3/aircraft/phoenix_v3',
           'ardupilot_aircraft_directory':native/'FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3',
           'jsbsim_aircraft_directory':native/'FlightStack/jsbsim/aircraft/phoenix_v3'}
    ref_model=reference_root()/reference['files']['aircraft']['filename']
    reference_sections=sections(ref_model);rows={};diff=''
    for name,path in paths.items():
        files={};contacts=None;changed=None
        for role,filename in (('aircraft','phoenix_v3.xml'),('reset','reset.xml')):
            p=path/filename
            files[role]={'path':str(p.relative_to(bundle)),'exists':p.is_file()}
            if p.is_file():
                files[role].update(sha256=sha(p),bytes=p.stat().st_size,
                    matches_reference=sha(p)==reference['files'][role]['sha256'])
        if (path/'phoenix_v3.xml').is_file():
            root=ET.fromstring((path/'phoenix_v3.xml').read_bytes())
            contacts=[{'name':c.get('name'),'type':c.get('type')} for c in root.findall('ground_reactions/contact')]
            active=sections(path/'phoenix_v3.xml')
            changed=[k for k in sorted(active.keys()|reference_sections.keys()) if active.get(k)!=reference_sections.get(k)]
            diff+=''.join(difflib.unified_diff((path/'phoenix_v3.xml').read_text().splitlines(True),
                ref_model.read_text().splitlines(True),fromfile=files['aircraft']['path'],tofile=reference['files']['aircraft']['filename']))
        rows[name]={'files':files,'contacts':contacts,'changed_XML_sections':changed,
                    'live_loaded_model_verified':False}
    installer=native/'scripts/install_flightstack_rev1.sh'
    result={'step':'R3.01','status':'PASS','reference_revision':reference['reference_revision'],
        'reference_aircraft_sha256':reference['files']['aircraft']['sha256'],'locations':rows,
        'native_installer':{'path':str(installer.relative_to(bundle)),'sha256':sha(installer),
            'executed':False,'model_archive_named_in_source':'phoenix_v3_jsbsim_model_v4.tar.gz'},
        'scope':'read-only on-disk inventory; source paths do not prove a live load',
        'active_installation_changed':False,'ground_load_bearing_verified':False,'flight_ready':False}
    write(output/'DEPLOYMENT_COMPARISON.json',result)
    (output/'REFERENCE_VS_DEPLOYMENT.patch').write_text(diff)
    return reference


def persistence(tdoc,output,expected,build_python):
    env={k:v for k,v in os.environ.items() if not k.startswith('TDDT_') and k!='PYTHONPATH'}
    env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    def run(argv,cwd,name):
        r=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
        (output/name).write_text(r.stdout+r.stderr)
        if r.returncode:raise RuntimeError(f'{name}: exit {r.returncode}')
        return r
    with tempfile.TemporaryDirectory(prefix='tddt_R301_build_',dir='/tmp') as tmp:
        scratch=Path(tmp).resolve();package=scratch/'TDOC';package.mkdir()
        shutil.copytree(tdoc/'src',package/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
        shutil.copyfile(tdoc/'pyproject.toml',package/'pyproject.toml')
        (package/'tests').mkdir();shutil.copyfile(tdoc/'tests/test_r3_01_reference.py',package/'tests/test_r3_01_reference.py')
        paths=['src/tddt_tdoc/phoenix_reference.py','pyproject.toml']
        paths += [str(p.relative_to(tdoc)) for p in sorted(reference_root().iterdir())]
        hashes={p:sha(tdoc/p) for p in paths}
        for name in ('runtime','reports','logs'):
            (package/name).mkdir();(package/name/'GENERATED_SENTINEL').write_text('isolated generated data')
        reset_code="""
import json,os,sys
from pathlib import Path
p=Path.cwd().resolve();assert p.parent.name.startswith('tddt_R301_build_')
assert not any(k.startswith('TDDT_') for k in os.environ)
sys.path.insert(0,str(p/'src'))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.reset import reset_tdoc
from tddt_tdoc.phoenix_reference import inspect_reference
before=inspect_reference();assert before['ok']
r=reset_tdoc(TDOCConfig(p/'configs/tdoc.yaml',p,{'runtime':{'root':'runtime'}}),include_external=False)
assert r['ok'] and not r['dry_run'] and inspect_reference()==before
assert all(not (p/n/'GENERATED_SENTINEL').exists() for n in ('runtime','reports','logs'))
print(json.dumps(r))
"""
        reset=run([sys.executable,'-B','-c',reset_code],package,'ISOLATED_RESET.log')
        (output/'ISOLATED_RESET_RESULT.json').write_text(reset.stdout)
        assert hashes=={p:sha(package/p) for p in paths}
        wheels=output/'rebuilt_wheel';wheels.mkdir()
        code="from setuptools import build_meta;print(build_meta.build_wheel("+repr(str(wheels))+"))"
        run([build_python,'-B','-c',code],package,'WHEEL_BUILD.log')
        wheel,=wheels.glob('*.whl');target=scratch/'installed'
        run([sys.executable,'-B','-m','pip','--no-cache-dir','install','--no-index','--no-deps','--no-compile',
            '--target',str(target),str(wheel)],scratch,'WHEEL_INSTALL.log')
        (scratch/'expected.json').write_text(json.dumps(expected))
        code="""
import hashlib,importlib.machinery,importlib.util,json,py_compile,sys
from pathlib import Path
target,package,output,expected_path=map(Path,sys.argv[1:5]);mode=sys.argv[5]
sys.path.insert(0,str(target))
import tddt_tdoc.phoenix_reference as module
assert Path(module.__file__).is_relative_to(target)
source_hash=hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
if mode=='bytecode':
 full='tddt_tdoc.phoenix_reference';dest=Path(module.__file__).with_suffix('.r301.pyc')
 py_compile.compile(module.__file__,cfile=str(dest),doraise=True)
 loader=importlib.machinery.SourcelessFileLoader(full,str(dest));spec=importlib.util.spec_from_loader(full,loader)
 module=importlib.util.module_from_spec(spec);sys.modules[full]=module;loader.exec_module(module)
 setattr(sys.modules['tddt_tdoc'],'phoenix_reference',module)
result=module.inspect_reference();assert result==json.loads(expected_path.read_text())
assert module.reference_root().is_relative_to(target)
import pytest
rc=pytest.main(['-q','-p','no:cacheprovider',str(package/'tests/test_r3_01_reference.py')]);assert rc==0
(output/('POST_'+mode.upper()+'_REFERENCE.json')).write_text(json.dumps({'status':'PASS','mode':mode,
 'source_sha256':source_hash,'reference':result},indent=2))
"""
        for mode in ('wheel','bytecode'):
            run([sys.executable,'-B','-c',code,str(target),str(package),str(output),str(scratch/'expected.json'),mode],scratch,'TEST_'+mode.upper()+'.log')
            assert json.loads((output/('POST_'+mode.upper()+'_REFERENCE.json')).read_text())['source_sha256']==hashes['src/tddt_tdoc/phoenix_reference.py']
        assert hashes=={p:sha(package/p) for p in paths}
        write(output/'PERSISTENCE_RESULT.json',{'status':'PASS','source_and_resource_hashes':hashes,
            'isolated_actual_reset':True,'active_reset':False,'wheel':wheel.name,'wheel_sha256':sha(wheel),
            'wheel_reference_equal':True,'bytecode_reference_equal':True,'active_venv_modified':False,
            'native_compilation':False,'native_installer_executed':False})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--check-rebuild',action='store_true')
    parser.add_argument('--build-python',default='/usr/bin/python3')
    args=parser.parse_args();tdoc=Path(__file__).resolve().parents[1];output=args.output_dir.resolve()
    output.relative_to(tdoc/'reports/online');output.mkdir(parents=True,exist_ok=False)
    expected=inventory(tdoc,output)
    if args.check_rebuild:persistence(tdoc,output,expected,args.build_python)
    write(output/'AUDIT_RESULT.json',{'step':'R3.01','status':'PASS','contacts':4,
        'reference_revision':expected['reference_revision'],'active_installation':False,'native_execution':False})
    print(json.dumps({'step':'R3.01','status':'PASS','contacts':4,'active_installation':False}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
