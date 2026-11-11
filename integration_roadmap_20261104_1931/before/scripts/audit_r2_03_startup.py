#!/usr/bin/env python3
"""Read-only R2.03 probe; reset, packaging and binding writes stay isolated."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.orchestrator import TDOCOrchestrator
from tddt_tdoc.storage import read_json_auto

SOURCES = ['src/tddt_tdoc/'+n+'.py' for n in
           ('knowledge_startup','knowledge_assessment','native_environment','orchestrator')]
SOURCES += ['scripts/run_training_workflows.py']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def probe(tdoc, output, before_native):
    cfg=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml')
    h=object.__new__(OfflineKnowledgeHandoff)
    h.latest_path=tdoc/'runtime/knowledge/handoff/offline_latest.json.gz'
    gate=h.validate_online_start({})
    assert gate['ok']
    manifest=gate['manifest']
    forcing=Path(manifest['reuse_contract']['climate_time_alignment']['forcing_file'])
    raw_policy=tdoc/'runtime/knowledge/policy/online_policy.json.gz'
    active_binding=cfg.path_value('TDDT_LAYER6_ROOT',required=True)/'runtime/tdoc_environment_binding.json'
    assert not raw_policy.exists() and not active_binding.exists()
    inputs=[h.latest_path,forcing,tdoc/'reports/offline/mission/MISSION_TIMELINE.csv']
    hashes={str(p):sha(p) for p in inputs}
    compiler=object.__new__(KnowledgePolicyCompiler)
    compiler.config=cfg;compiler.path=raw_policy;compiler.handoff_path=h.latest_path
    compiler.timeline_path=inputs[-1]
    def forbidden(*a,**kw):
        raise AssertionError('Online startup must not compile a policy')
    compiler.compile=forbidden
    fake=SimpleNamespace(offline_handoff=h,knowledge_policy=compiler)
    startup=TDOCOrchestrator.ensure_online_knowledge_policy(fake)
    assert startup['ok'] and not startup['policy_available'] and not startup['compiled']
    package=Path(manifest['kstore_fingerprint']['root'])/manifest['setd']['package_manifest']
    if not package.exists(): package=Path(str(package)+'.gz')
    loaded=read_json_auto(package)
    assert loaded['package_id']==manifest['setd']['package_id'] and manifest['finalization']['committed']
    package_bytes=gzip.decompress(package.read_bytes()) if package.suffix=='.gz' else package.read_bytes()
    assert hashlib.sha256(package_bytes).hexdigest()==manifest['setd']['package_manifest_sha256']
    hashes[str(package)]=sha(package)
    with tempfile.TemporaryDirectory(prefix='tddt_R203_binding_',dir='/tmp') as tmp:
        target=Path(tmp)/'native'
        # Isolate only the target and the independently broken R3 launch-fixture
        # branch. Real mission reference, region, DEM and forcing are still read.
        wrapped=SimpleNamespace(package_root=tdoc,runtime_root=tdoc/'runtime',
            get=lambda key,default=None:False if key=='layers.layer6.online.ideal_launch_fixture' else cfg.get(key,default),
            path_value=lambda key,required=False:target if key=='TDDT_LAYER6_ROOT' else cfg.path_value(key,required=required))
        spec=importlib.util.spec_from_file_location('tddt_tdoc.r203_before_native',before_native)
        old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        try:
            old.prepare_binding(wrapped)
        except ValueError as exc:
            old_failure=str(exc)
        else:
            raise AssertionError('Expected old policy/reference identity blocker')
        assert 'policy/reference identity' in old_failure and not target.exists()
        binding=prepare_binding(wrapped)
        assert binding['handoff_hash']==manifest['handoff_hash'] and not binding['actuation_authority']
        assert binding['forcing_file_sha256']==sha(forcing)
        assert (target/'runtime/tdoc_environment_binding.json').is_file()
        write(output/'ISOLATED_REAL_DATA_BINDING.json',binding)
    assert hashes=={p:sha(p) for p in hashes}
    assert not raw_policy.exists() and not active_binding.exists()
    result={'status':'PASS','startup':startup,'handoff_integrity_ok':True,
        'control_knowledge_valid':manifest['control_knowledge_valid'],
        'offline_control_coverage_complete':manifest['offline_control_coverage_complete'],
        'setd_package_id':loaded['package_id'],'setd_committed':manifest['finalization']['committed'],
        'setd_check_scope':'read-only manifest identity; no mutable SETD loader or workspace reconstruction',
        'before_binding_error':old_failure,'after_isolated_binding_ok':True,
        'probe_override':'temporary Layer6 target; ideal_launch_fixture=False in memory only',
        'preparation_scope':'environment binding only; R3 fixture and native execution not accepted',
        'input_hashes':hashes,'active_binding_created':False,'policy_created':False,
        'flight_ready':False,'actuation_authority':False}
    write(output/'ACTUAL_STARTUP_STATES.json',result)
    return result


def persistence(tdoc,output,build_python):
    env={k:v for k,v in os.environ.items() if not k.startswith('TDDT_') and k!='PYTHONPATH'}
    env['PYTHONDONTWRITEBYTECODE']='1'
    def run(argv,cwd,name):
        r=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
        (output/name).write_text(r.stdout+r.stderr)
        if r.returncode: raise RuntimeError(f'{name} failed: {r.returncode}')
        return r
    with tempfile.TemporaryDirectory(prefix='tddt_R203_build_',dir='/tmp') as tmp:
        scratch=Path(tmp).resolve();package=scratch/'TDOC';package.mkdir()
        shutil.copytree(tdoc/'src',package/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
        (package/'scripts').mkdir();(package/'tests').mkdir()
        shutil.copyfile(tdoc/'scripts/run_training_workflows.py',package/'scripts/run_training_workflows.py')
        shutil.copyfile(tdoc/'tests/test_r2_03_optional_startup.py',package/'tests/test_r2_03_optional_startup.py')
        shutil.copyfile(tdoc/'pyproject.toml',package/'pyproject.toml')
        expected={p:sha(tdoc/p) for p in SOURCES}
        assert expected=={p:sha(package/p) for p in SOURCES}
        for folder in ('runtime','reports','logs'):
            (package/folder).mkdir();(package/folder/'GENERATED_SENTINEL').write_text('isolated only')
        code="""
import json,os,sys
from pathlib import Path
p=Path.cwd().resolve();assert p.parent.name.startswith('tddt_R203_build_')
assert not any(k.startswith('TDDT_') for k in os.environ)
sys.path.insert(0,str(p/'src'))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.reset import reset_tdoc
r=reset_tdoc(TDOCConfig(p/'configs/tdoc.yaml',p,{'runtime':{'root':'runtime'}}),include_external=False)
assert r['ok'] and not r['dry_run']
assert all(not (p/n/'GENERATED_SENTINEL').exists() for n in ('runtime','reports','logs'))
print(json.dumps(r))
"""
        reset=run([sys.executable,'-B','-c',code],package,'ISOLATED_RESET.log')
        (output/'ISOLATED_RESET_RESULT.json').write_text(reset.stdout)
        assert expected=={p:sha(package/p) for p in SOURCES}
        wheels=output/'rebuilt_wheel';wheels.mkdir()
        code="from setuptools import build_meta; print(build_meta.build_wheel("+repr(str(wheels))+"))"
        run([build_python,'-B','-c',code],package,'WHEEL_BUILD.log')
        wheel,=wheels.glob('*.whl');installed=scratch/'installed'
        run([sys.executable,'-B','-m','pip','--no-cache-dir','install','--no-index','--no-deps','--no-compile',
             '--target',str(installed),str(wheel)],scratch,'WHEEL_INSTALL.log')
        # Execute the same acceptance suite first from the installed wheel, then
        # from regenerated bytecode. No source-tree path is exported to children.
        code="""
import hashlib,importlib,importlib.machinery,importlib.util,json,py_compile,sys
from pathlib import Path
target,package,output=map(Path,sys.argv[1:4]);mode=sys.argv[4]
sys.path.insert(0,str(target))
hashes={}
for name in ('knowledge_startup','knowledge_assessment','native_environment','orchestrator'):
 full='tddt_tdoc.'+name;m=importlib.import_module(full);p=Path(m.__file__)
 assert p.is_relative_to(target)
 hashes['src/tddt_tdoc/'+name+'.py']=hashlib.sha256(p.read_bytes()).hexdigest()
 if mode=='bytecode':
  dest=target/(name+'.pyc');py_compile.compile(str(p),cfile=str(dest),doraise=True)
  loader=importlib.machinery.SourcelessFileLoader(full,str(dest))
  spec=importlib.util.spec_from_loader(full,loader);m=importlib.util.module_from_spec(spec)
  sys.modules[full]=m;loader.exec_module(m)
  setattr(sys.modules['tddt_tdoc'],name,m)
import pytest
rc=pytest.main(['-q','-p','no:cacheprovider',str(package/'tests/test_r2_03_optional_startup.py')])
assert rc==0
(output/('POST_'+mode.upper()+'_STATES.json')).write_text(json.dumps({'status':'PASS','mode':mode,'module_hashes':hashes},indent=2))
"""
        for mode in ('wheel','bytecode'):
            run([sys.executable,'-B','-c',code,str(installed),str(package),str(output),mode],scratch,'TEST_'+mode.upper()+'.log')
            data=json.loads((output/('POST_'+mode.upper()+'_STATES.json')).read_text())
            assert data['module_hashes']=={p:expected[p] for p in SOURCES if p.startswith('src/')}
        assert expected=={p:sha(package/p) for p in SOURCES}
        result={'status':'PASS','isolated_actual_reset':True,'active_reset':False,
                'source_hashes_preserved':expected,'wheel':wheel.name,'wheel_sha256':sha(wheel),
                'installed_wheel_tests':'PASS','regenerated_bytecode_tests':'PASS',
                'active_venv_modified':False,'native_compilation':False,'native_execution':False}
        write(output/'PERSISTENCE_RESULT.json',result)
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--before-native',type=Path,required=True)
    p.add_argument('--check-rebuild',action='store_true')
    p.add_argument('--build-python',default='/usr/bin/python3')
    a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];output=a.output_dir.resolve()
    output.relative_to(tdoc/'reports/online');output.mkdir(exist_ok=False,parents=True)
    result=probe(tdoc,output,a.before_native.resolve())
    if a.check_rebuild: persistence(tdoc,output,a.build_python)
    write(output/'AUDIT_RESULT.json',{'step':'R2.03','status':'PASS','read_only_active_inputs':True,
         'startup_ok':result['startup']['ok'],'native_execution':False,'active_reset':False})
    print(json.dumps({'step':'R2.03','status':'PASS','startup_ok':True,'policy_available':False}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
