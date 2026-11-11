#!/usr/bin/env python3
"""R2.04 contract probe using read-only real prior and isolated current-state doubles."""
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

from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from tddt_tdoc.orchestrator import TDOCOrchestrator
from tddt_tdoc.storage import read_json_auto

SOURCE_PATHS=['src/tddt_tdoc/orchestrator.py','src/tddt_tdoc/knowledge_startup.py',
              'src/tddt_tdoc/invocation.py','src/tddt_tdoc/rtmo/artifact_gate.py']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


def probe(tdoc,output,before_orchestrator):
    h=object.__new__(OfflineKnowledgeHandoff)
    h.latest_path=tdoc/'runtime/knowledge/handoff/offline_latest.json.gz'
    gate=h.validate_online_start({});assert gate['ok']
    manifest=gate['manifest']
    forcing=Path(manifest['reuse_contract']['climate_time_alignment']['forcing_file'])
    assert sha(forcing)==manifest['reuse_contract']['climate_time_alignment']['forcing_file_sha256']
    package=Path(manifest['kstore_fingerprint']['root'])/manifest['setd']['package_manifest']
    if not package.exists():package=Path(str(package)+'.gz')
    raw=gzip.decompress(package.read_bytes()) if package.suffix=='.gz' else package.read_bytes()
    assert hashlib.sha256(raw).hexdigest()==manifest['setd']['package_manifest_sha256']
    assert json.loads(raw)['package_id']==manifest['setd']['package_id']
    policy=tdoc/'runtime/knowledge/policy/online_policy.json.gz';assert not policy.exists()
    inputs=[h.latest_path,forcing,package,tdoc/'reports/offline/mission/MISSION_TIMELINE.csv']
    hashes={str(p):sha(p) for p in inputs}
    harness=module(tdoc/'tests/test_r2_04_prediction_trigger.py','r204_test_harness')
    old=module(before_orchestrator,'tddt_tdoc.r204_before_orchestrator')
    with tempfile.TemporaryDirectory(prefix='tddt_R204_probe_',dir='/tmp') as tmp:
        fixture=harness.make_context(Path(tmp))
        fixture.orch.offline_handoff=h
        compiler=object.__new__(KnowledgePolicyCompiler)
        compiler.config=fixture.orch.config;compiler.path=policy;compiler.handoff_path=h.latest_path
        compiler.timeline_path=inputs[-1];compiler.compile=harness.forbidden
        fixture.orch.knowledge_policy=compiler
        # Read-only SETD identity projection. No mutable KStore loader runs.
        fixture.orch.adapters[5].load_setd=lambda:{'ok':True,'manifest':{
            'package_id':manifest['setd']['package_id'],'committed':True,
            'knowledge_quality':manifest.get('knowledge_quality') or {}}}
        request=harness.request()
        before=old.TDOCOrchestrator.run_cycle(fixture.orch,request).to_dict()
        assert not before['layer_status']['4']['ready'] and not fixture.adapters[4].calls
        after=TDOCOrchestrator.run_cycle(fixture.orch,request).to_dict()
        assert after['layer_status']['4']['ready'] and len(fixture.adapters[4].calls)==1
        harness.assert_no_release(fixture,after)
        assert not after['layer_status']['4']['evidence']['prior_admission']['policy_available']
        write(output/'BEFORE_TRIGGER.json',before)
        write(output/'AFTER_TRIGGER.json',after)
        write(output/'L4_INPUT.json',fixture.adapters[4].calls[0])
    assert hashes=={p:sha(p) for p in hashes} and not policy.exists()
    result={'status':'PASS','handoff_hash':manifest['handoff_hash'],
        'setd_package_id':manifest['setd']['package_id'],'control_knowledge_valid':manifest['control_knowledge_valid'],
        'before_l4_ready':False,'after_l4_ready':True,'policy_available':False,
        'final':False,'actuation_executed':False,'native_execution':False,
        'scope':'production run_cycle/_invoke/gates; L2/L3/L4/L5 contract doubles; current state explicitly synthetic',
        'real_prior_read_only':True,'setd_check_scope':'compressed manifest bytes and identity; no workspace restore',
        'input_hashes':hashes,'policy_created':False,'flight_ready':False}
    write(output/'ACTUAL_PRIOR_TRIGGER_RESULT.json',result)
    return result


def persistence(tdoc,output,build_python):
    env={k:v for k,v in os.environ.items() if not k.startswith('TDDT_') and k!='PYTHONPATH'}
    env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    def run(argv,cwd,name):
        r=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
        (output/name).write_text(r.stdout+r.stderr)
        if r.returncode:raise RuntimeError(f'{name}: exit {r.returncode}')
        return r
    with tempfile.TemporaryDirectory(prefix='tddt_R204_build_',dir='/tmp') as tmp:
        scratch=Path(tmp).resolve();package=scratch/'TDOC';package.mkdir()
        shutil.copytree(tdoc/'src',package/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
        (package/'tests').mkdir()
        shutil.copyfile(tdoc/'tests/test_r2_04_prediction_trigger.py',package/'tests/test_r2_04_prediction_trigger.py')
        shutil.copyfile(tdoc/'pyproject.toml',package/'pyproject.toml')
        expected={p:sha(tdoc/p) for p in SOURCE_PATHS}
        for name in ('runtime','reports','logs'):
            (package/name).mkdir();(package/name/'GENERATED_SENTINEL').write_text('isolated generated data')
        reset_code="""
import json,os,sys
from pathlib import Path
p=Path.cwd().resolve();assert p.parent.name.startswith('tddt_R204_build_')
assert not any(k.startswith('TDDT_') for k in os.environ)
sys.path.insert(0,str(p/'src'))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.reset import reset_tdoc
r=reset_tdoc(TDOCConfig(p/'configs/tdoc.yaml',p,{'runtime':{'root':'runtime'}}),include_external=False)
assert r['ok'] and not r['dry_run']
assert all(not (p/n/'GENERATED_SENTINEL').exists() for n in ('runtime','reports','logs'))
print(json.dumps(r))
"""
        reset=run([sys.executable,'-B','-c',reset_code],package,'ISOLATED_RESET.log')
        (output/'ISOLATED_RESET_RESULT.json').write_text(reset.stdout)
        assert expected=={p:sha(package/p) for p in SOURCE_PATHS}
        wheels=output/'rebuilt_wheel';wheels.mkdir()
        build_code="from setuptools import build_meta;print(build_meta.build_wheel("+repr(str(wheels))+"))"
        run([build_python,'-B','-c',build_code],package,'WHEEL_BUILD.log')
        wheel,=wheels.glob('*.whl');target=scratch/'installed'
        run([sys.executable,'-B','-m','pip','--no-cache-dir','install','--no-index','--no-deps','--no-compile',
            '--target',str(target),str(wheel)],scratch,'WHEEL_INSTALL.log')
        check="""
import hashlib,importlib,importlib.machinery,importlib.util,json,py_compile,sys
from pathlib import Path
target,package,output=map(Path,sys.argv[1:4]);mode=sys.argv[4]
sys.path.insert(0,str(target));hashes={}
for name in ('knowledge_startup','invocation','rtmo.artifact_gate','orchestrator'):
 full='tddt_tdoc.'+name;m=importlib.import_module(full);source=Path(m.__file__)
 assert source.is_relative_to(target)
 hashes['src/tddt_tdoc/'+name.replace('.','/')+'.py']=hashlib.sha256(source.read_bytes()).hexdigest()
 if mode=='bytecode':
  dest=target/(name.replace('.','_')+'.pyc');py_compile.compile(str(source),cfile=str(dest),doraise=True)
  loader=importlib.machinery.SourcelessFileLoader(full,str(dest));spec=importlib.util.spec_from_loader(full,loader)
  m=importlib.util.module_from_spec(spec);sys.modules[full]=m;loader.exec_module(m)
  parent,_,attr=full.rpartition('.');setattr(sys.modules[parent],attr,m)
import pytest
rc=pytest.main(['-q','-p','no:cacheprovider',str(package/'tests/test_r2_04_prediction_trigger.py')])
assert rc==0
(output/('POST_'+mode.upper()+'_STATES.json')).write_text(json.dumps({'status':'PASS','mode':mode,'module_hashes':hashes},indent=2))
"""
        for mode in ('wheel','bytecode'):
            run([sys.executable,'-B','-c',check,str(target),str(package),str(output),mode],scratch,'TEST_'+mode.upper()+'.log')
            assert json.loads((output/('POST_'+mode.upper()+'_STATES.json')).read_text())['module_hashes']==expected
        assert expected=={p:sha(package/p) for p in SOURCE_PATHS}
        result={'status':'PASS','isolated_actual_reset':True,'active_reset':False,'source_hashes_preserved':expected,
            'wheel':wheel.name,'wheel_sha256':sha(wheel),'installed_wheel_tests':'PASS',
            'regenerated_bytecode_tests':'PASS','active_venv_modified':False,'native_compilation':False}
        write(output/'PERSISTENCE_RESULT.json',result)
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',required=True,type=Path)
    p.add_argument('--before-orchestrator',required=True,type=Path)
    p.add_argument('--check-rebuild',action='store_true')
    p.add_argument('--build-python',default='/usr/bin/python3')
    a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];output=a.output_dir.resolve()
    output.relative_to(tdoc/'reports/online');output.mkdir(exist_ok=False,parents=True)
    result=probe(tdoc,output,a.before_orchestrator.resolve())
    if a.check_rebuild:persistence(tdoc,output,a.build_python)
    write(output/'AUDIT_RESULT.json',{'step':'R2.04','status':'PASS','real_prior_unchanged':True,
        'native_execution':False,'active_reset':False,'scope':result['scope']})
    print(json.dumps({'step':'R2.04','status':'PASS','prior_unchanged':True,'native_execution':False}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
