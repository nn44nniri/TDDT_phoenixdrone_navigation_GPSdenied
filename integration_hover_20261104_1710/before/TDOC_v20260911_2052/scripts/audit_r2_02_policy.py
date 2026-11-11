#!/usr/bin/env python3
"""R2.02 read-only evidence and optional isolated reset/two-package wheel check."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from layer5.phoenix import PhoenixSpecialization


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def probe(tdoc, output):
    compiler=object.__new__(KnowledgePolicyCompiler)  # avoid config.runtime_root's mkdir
    compiler.config=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml')
    compiler.path=tdoc/'runtime/knowledge/policy/online_policy.json.gz'
    compiler.handoff_path=tdoc/'runtime/knowledge/handoff/offline_latest.json.gz'
    compiler.timeline_path=tdoc/'reports/offline/mission/MISSION_TIMELINE.csv'
    loaded=compiler.load()
    phase=compiler.phase_policy('hover')
    compile_probe=copy.copy(compiler)
    compile_probe.path=output/'uncreated_policy/online_policy.json.gz'
    compiled=compile_probe.compile()
    assert not compile_probe.path.exists()
    specialization=object.__new__(PhoenixSpecialization)
    command,diagnostic=specialization._compiled_policy_action(
        {'compiled_online_policy':phase},[],[],'hover',None)
    assert command is None
    assert loaded['status']==phase['status']==compiled['status']==diagnostic['status']=='UNAVAILABLE'
    return {'artifact_load':loaded,'phase_guide':phase,'zero_joint_compile':compiled,
            'layer5_prior_command':command,'layer5_diagnostic':diagnostic,
            'compile_output_created':False,'actuation_authority':False}


def persistence(tdoc,output,build_python):
    env={k:v for k,v in os.environ.items() if not k.startswith('TDDT_') and k!='PYTHONPATH'}
    env['PYTHONDONTWRITEBYTECODE']='1'

    def run(argv,cwd,name):
        result=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
        (output/name).write_text(result.stdout+result.stderr)
        if result.returncode:
            raise RuntimeError(f'isolated persistence check failed: {name}: {result.returncode}')
        return result

    with tempfile.TemporaryDirectory(prefix='tddt_R202_') as tmp:
        scratch=Path(tmp).resolve()
        roots=[tdoc,tdoc.parent/'layer5_v20260911_1510']
        copies=[]
        before={}
        for source in roots:
            target=scratch/source.name
            shutil.copytree(source/'src',target/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
            shutil.copyfile(source/'pyproject.toml',target/'pyproject.toml')
            copies.append(target)
        expected_sources={'TDOC_v20260911_2052/src/tddt_tdoc/knowledge_policy.py':sha(tdoc/'src/tddt_tdoc/knowledge_policy.py'),
                          'layer5_v20260911_1510/src/layer5/phoenix.py':sha(roots[1]/'src/layer5/phoenix.py')}
        for relative,digest in expected_sources.items():
            assert sha(scratch/relative)==digest
            before[relative]=digest
        package=copies[0]
        for folder in ('runtime','reports','logs'):
            (package/folder).mkdir()
            (package/folder/'GENERATED_SENTINEL').write_text('isolated data only')
        code="""
import json,os,sys
from pathlib import Path
p=Path.cwd().resolve();assert p.parent.name.startswith('tddt_R202_')
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
        assert before=={p:sha(scratch/p) for p in before}
        wheel_dir=output/'rebuilt_wheels'
        wheel_dir.mkdir()
        for index,source in enumerate(copies):
            code="import json; from importlib.metadata import version; from setuptools import build_meta; print(json.dumps({'setuptools':version('setuptools'),'wheel':version('wheel')})); print(build_meta.build_wheel("+repr(str(wheel_dir))+"))"
            run([build_python,'-B','-c',code],source,f'BUILD_{index}.log')
        wheels=list(wheel_dir.glob('*.whl'))
        assert len(wheels)==2
        installed=scratch/'installed'
        run([sys.executable,'-B','-m','pip','--no-cache-dir','install','--no-index','--no-deps','--no-compile','--target',str(installed),*[str(p) for p in wheels]],scratch,'INSTALL.log')
        inputs=scratch/'inputs'
        inputs.mkdir()
        shutil.copyfile(tdoc/'runtime/knowledge/handoff/offline_latest.json.gz',inputs/'handoff.json.gz')
        shutil.copyfile(tdoc/'reports/offline/mission/MISSION_TIMELINE.csv',inputs/'timeline.csv')
        check="""
import hashlib,importlib.machinery,importlib.util,json,py_compile,sys
from pathlib import Path
from types import SimpleNamespace
target,inputs,output=map(Path,sys.argv[1:]);sys.path.insert(0,str(target))
import tddt_tdoc.knowledge_policy as policy
import layer5.phoenix as phoenix
for module in (policy,phoenix):assert Path(module.__file__).is_relative_to(target)
def exercise(kpc,cls):
 c=object.__new__(kpc);c.config=SimpleNamespace(path_value=lambda key:None)
 c.path=inputs/'policy_never_created.json.gz';c.handoff_path=inputs/'handoff.json.gz';c.timeline_path=inputs/'timeline.csv'
 load=c.load();phase=c.phase_policy('hover');compiled=c.compile()
 u,d=object.__new__(cls)._compiled_policy_action({'compiled_online_policy':phase},[],[],'hover',None)
 assert u is None and all(x['status']=='UNAVAILABLE' for x in (load,phase,compiled,d))
 assert not c.path.exists()
 return {'load_status':load['status'],'compile_reason':compiled['reason'],'phase_status':phase['status'],'prior_command':u,'layer5_status':d['status']}
first=exercise(policy.KnowledgePolicyCompiler,phoenix.PhoenixSpecialization)
compiled=[]
for module,name in ((policy,'tddt_tdoc.r202_compiled_policy'),(phoenix,'layer5.r202_compiled_phoenix')):
 p=inputs/(name.split('.')[-1]+'.pyc');py_compile.compile(module.__file__,cfile=str(p),doraise=True)
 loader=importlib.machinery.SourcelessFileLoader(name,str(p));spec=importlib.util.spec_from_loader(name,loader)
 m=importlib.util.module_from_spec(spec);loader.exec_module(m);compiled.append(m)
assert exercise(compiled[0].KnowledgePolicyCompiler,compiled[1].PhoenixSpecialization)==first
result={'status':'PASS','wheel_import_verified':True,'compiled_bytecode_executed':True,'states':first,
 'module_hashes':{'TDOC_v20260911_2052/src/tddt_tdoc/knowledge_policy.py':hashlib.sha256(Path(policy.__file__).read_bytes()).hexdigest(),
 'layer5_v20260911_1510/src/layer5/phoenix.py':hashlib.sha256(Path(phoenix.__file__).read_bytes()).hexdigest()}}
(output/'POST_BUILD_STATES.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
"""
        tested=run([sys.executable,'-B','-c',check,str(installed),str(inputs),str(output)],scratch,'REGENERATE.log')
        result=json.loads(tested.stdout)
        assert result['module_hashes']==expected_sources
        assert before=={p:sha(scratch/p) for p in before}
        result.update(active_reset_executed=False,isolated_reset_executed=True,
                      wheels=[{'name':p.name,'sha256':sha(p)} for p in wheels],
                      active_venv_modified=False)
        (output/'PERSISTENCE_RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--check-rebuild',action='store_true')
    parser.add_argument('--build-python',default='/usr/bin/python3')
    args=parser.parse_args()
    tdoc=Path(__file__).resolve().parents[1]
    output=args.output_dir.resolve()
    output.relative_to(tdoc/'reports/online')
    output.mkdir(parents=True,exist_ok=False)
    files=[tdoc/'runtime/knowledge/handoff/offline_latest.json.gz',tdoc/'reports/offline/mission/MISSION_TIMELINE.csv']
    before={str(p):sha(p) for p in files}
    result=probe(tdoc,output)
    assert before=={str(p):sha(p) for p in files}
    (output/'ACTUAL_POLICY_STATES.json').write_text(json.dumps(result,indent=2)+'\n')
    if args.check_rebuild:
        persistence(tdoc,output,args.build_python)
    (output/'AUDIT_RESULT.json').write_text(json.dumps({'step':'R2.02','status':'PASS','input_hashes':before,
        'input_files_unchanged':True,'native_execution':False,'active_reset':False},indent=2)+'\n')
    print(json.dumps({'step':'R2.02','status':'PASS','load':result['artifact_load']['status'],
                      'compile':result['zero_joint_compile']['status'],'prior_command':result['layer5_prior_command']}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
