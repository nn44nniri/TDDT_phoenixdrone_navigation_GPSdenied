#!/usr/bin/env python3
"""R2.01: read-only audit; optional reset/build test operates solely in /tmp.

Never finalize/load a mutable KStore workspace, train, rebuild FMUs or reset the
active bundle. The old signed manifest and timeline are read without rewriting.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tddt_tdoc.knowledge_assessment import assess_offline_knowledge
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rebuild_check(tdoc, output, handoff, timeline, expected, build_python):
    env={k:v for k,v in os.environ.items() if not k.startswith('TDDT_') and k!='PYTHONPATH'}
    env['PYTHONDONTWRITEBYTECODE']='1'
    commands=[]

    def run(argv, cwd, log):
        commands.append(argv)
        result=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
        (output/log).write_text(result.stdout+result.stderr)
        if result.returncode:
            raise RuntimeError(f'isolated persistence check failed ({log}): {result.returncode}')
        return result

    with tempfile.TemporaryDirectory(prefix='tddt_R201_') as tmp:
        scratch=Path(tmp).resolve()
        package=scratch/'TDOC'
        package.mkdir()
        shutil.copytree(tdoc/'src',package/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
        (package/'scripts').mkdir()
        shutil.copyfile(tdoc/'scripts/run_training_workflows.py',package/'scripts/run_training_workflows.py')
        shutil.copyfile(tdoc/'pyproject.toml',package/'pyproject.toml')
        for name in ('runtime','reports','logs'):
            (package/name).mkdir()
            (package/name/'GENERATED_SENTINEL').write_text('isolated generated data')
        source_paths=['src/tddt_tdoc/knowledge_assessment.py','src/tddt_tdoc/knowledge_handoff.py','scripts/run_training_workflows.py']
        before={p:sha(package/p) for p in source_paths}
        # Environment-owned aliases are intentionally absent in this subprocess.
        # The actual reset function receives a minimal, contained runtime config.
        reset_code="""
import json,os,sys
from pathlib import Path
root=Path.cwd().resolve()
assert root.parent.name.startswith('tddt_R201_')
assert not any(k.startswith('TDDT_') for k in os.environ)
sys.path.insert(0,str(root/'src'))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.reset import reset_tdoc
cfg=TDOCConfig(root/'configs/tdoc.yaml',root,{'runtime':{'root':'runtime'}})
report=reset_tdoc(cfg,include_external=False)
assert report['ok'] and not report['dry_run']
assert all(not (root/name/'GENERATED_SENTINEL').exists() for name in ('runtime','reports','logs'))
print(json.dumps(report))
"""
        reset=run([sys.executable,'-B','-c',reset_code],package,'ISOLATED_RESET.log')
        (output/'ISOLATED_RESET_RESULT.json').write_text(reset.stdout)
        assert before=={p:sha(package/p) for p in source_paths}
        wheels=output/'rebuilt_wheel'
        wheels.mkdir()
        # Use Debian's locally installed PEP 517 backend; no network/install of
        # build dependencies and no changes to the working virtual environment.
        build_code="import json; from importlib.metadata import version; from setuptools import build_meta; print(json.dumps({'setuptools':version('setuptools'),'wheel':version('wheel')})); print(build_meta.build_wheel("+repr(str(wheels))+"))"
        run([build_python,'-B','-c',build_code],package,'WHEEL_BUILD.log')
        wheel,=wheels.glob('*.whl')
        target=scratch/'installed'
        run([sys.executable,'-B','-m','pip','--no-cache-dir','install','--no-index','--no-deps','--no-compile','--target',str(target),str(wheel)],scratch,'WHEEL_INSTALL.log')
        inputs=scratch/'inputs'
        inputs.mkdir()
        shutil.copyfile(handoff,inputs/'handoff.json.gz')
        shutil.copyfile(timeline,inputs/'timeline.csv')
        expected_file=inputs/'expected.json'
        expected_file.write_text(json.dumps(expected))
        check_code="""
import csv,hashlib,importlib.machinery,importlib.util,json,py_compile,sys
from pathlib import Path
target,inputs,output=map(Path,sys.argv[1:])
sys.path.insert(0,str(target))
import tddt_tdoc.knowledge_assessment as assessment
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
assert Path(assessment.__file__).is_relative_to(target)
resolver=object.__new__(OfflineKnowledgeHandoff)
resolver.latest_path=inputs/'handoff.json.gz'
resolved=resolver.resolve()
assert resolved['ok']
rows=list(csv.DictReader((inputs/'timeline.csv').open()))
result=assessment.assess_offline_knowledge(resolved['manifest'],handoff_hash_verified=True,handoff_gate_ok=True,timeline_rows=rows)
assert result==json.loads((inputs/'expected.json').read_text())
compiled=target.parent/'assessment.pyc'
py_compile.compile(assessment.__file__,cfile=str(compiled),doraise=True)
loader=importlib.machinery.SourcelessFileLoader('r201_compiled',str(compiled))
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec);loader.exec_module(module)
assert module.assess_offline_knowledge(resolved['manifest'],handoff_hash_verified=True,handoff_gate_ok=True,timeline_rows=rows)==result
(output/'REGENERATED_ASSESSMENT.json').write_text(json.dumps(result,indent=2))
print(json.dumps({'status':'PASS','wheel_import_verified':True,'compiled_bytecode_executed':True,'regenerated_projection_matches':True,'module_sha256':hashlib.sha256(Path(assessment.__file__).read_bytes()).hexdigest()}))
"""
        checked=run([sys.executable,'-B','-c',check_code,str(target),str(inputs),str(output)],scratch,'WHEEL_REGENERATION.log')
        result=json.loads(checked.stdout)
        assert result['module_sha256']==sha(tdoc/'src/tddt_tdoc/knowledge_assessment.py')
        assert before=={p:sha(package/p) for p in source_paths}
        result.update(isolated_reset_executed=True,active_reset_executed=False,
                      source_hashes_preserved=before,wheel=str(wheel),wheel_sha256=sha(wheel),
                      check_scope='Python source, isolated TDOC reset, PEP517 wheel rebuild/install and bytecode execution; native FMUs unchanged')
        (output/'PERSISTENCE_RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--check-rebuild',action='store_true')
    parser.add_argument('--build-python',default='/usr/bin/python3')
    args=parser.parse_args()
    tdoc=Path(__file__).resolve().parents[1]
    output=args.output_dir.resolve()
    output.relative_to(tdoc/'reports/online')
    output.mkdir(parents=True,exist_ok=False)
    handoff=tdoc/'runtime/knowledge/handoff/offline_latest.json.gz'
    timeline=tdoc/'reports/offline/mission/MISSION_TIMELINE.csv'
    before={str(p):sha(p) for p in (handoff,timeline)}
    resolver=object.__new__(OfflineKnowledgeHandoff)  # bypass mkdir-only constructor for active read
    resolver.latest_path=handoff
    resolved=resolver.resolve()
    if not resolved.get('ok'):
        raise RuntimeError('existing handoff rejected: '+str(resolved.get('reason')))
    rows=list(csv.DictReader(timeline.open(newline='')))
    assessment=assess_offline_knowledge(resolved['manifest'],handoff_hash_verified=True,handoff_gate_ok=True,timeline_rows=rows)
    (output/'KNOWLEDGE_ASSESSMENT.json').write_text(json.dumps(assessment,indent=2,ensure_ascii=False)+'\n')
    assert before=={str(p):sha(p) for p in (handoff,timeline)}
    result={'step':'R2.01','status':'PASS','inputs':before,'input_files_unchanged':True,
            'joint_control_proof_cycles':assessment['quality']['joint_control_proof_cycles'],
            'timeline_rows':len(rows),'missing_control_phases':assessment['coverage']['missing_control_phases'],
            'consistency_issues':assessment['consistency_issues'],'actuation_authority':False,
            'package_bytes_verified_here':False}
    if args.check_rebuild:
        result['persistence']=rebuild_check(tdoc,output,handoff,timeline,assessment,args.build_python)
    (output/'AUDIT_RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='persistence'}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
