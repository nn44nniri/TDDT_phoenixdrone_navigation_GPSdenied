#!/usr/bin/env python3
"""R2.05 acceptance matrix; active prior read-only, faults/reset/build isolated."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.knowledge_policy import KnowledgePolicyCompiler
from tddt_tdoc.knowledge_startup import optional_policy_admission


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def suite(tdoc):
    sys.path.insert(0,str(tdoc/'tests'))
    return importlib.import_module('test_r2_05_prior_acceptance')


def matrix(tdoc,output):
    import pytest
    tests=suite(tdoc);rows=[]
    for case in tests.CASES:
        with tempfile.TemporaryDirectory(prefix='tddt_R205_case_',dir='/tmp') as tmp, pytest.MonkeyPatch.context() as patch:
            row=tests.evaluate_case(case,Path(tmp),patch)
        assert row['outcome']==row['expected'],row
        rows.append(row)
    result={'step':'R2.05','status':'PASS','cases':rows,'passed':len(rows),
            'native_execution':False,'synthetic_faults_in_active_inputs':False}
    write(output/'ACCEPTANCE_MATRIX.json',result)
    return tests


def actual_prior(tdoc,output,tests):
    h=object.__new__(OfflineKnowledgeHandoff);h.latest_path=tdoc/'runtime/knowledge/handoff/offline_latest.json.gz'
    gate=h.validate_online_start({});assert gate['ok'];m=gate['manifest']
    forcing=Path(m['reuse_contract']['climate_time_alignment']['forcing_file'])
    hashes={str(p):sha(p) for p in (h.latest_path,forcing)}
    assert sha(forcing)==m['reuse_contract']['climate_time_alignment']['forcing_file_sha256']
    compiler=object.__new__(KnowledgePolicyCompiler)
    compiler.path=tdoc/'runtime/knowledge/policy/online_policy.json.gz'
    compiler.handoff_path=h.latest_path;compiler.timeline_path=tdoc/'reports/offline/mission/MISSION_TIMELINE.csv'
    loaded=compiler.load(handoff_hash=m['handoff_hash']);admission=optional_policy_admission(loaded)
    assert admission['ok'] and not admission['policy_available'] and not compiler.path.exists()
    climate_root=Path(os.environ['TDDT_CLIMATE_ROOT'])
    window=tests.workflow.forcing_window(climate_root)
    alignment=tests.workflow._load_online_climate_alignment(tdoc/'runtime',h.latest_path,window,climate_root)
    assert alignment['ok']
    req={'climate':{},'context':{}}
    time=tests.workflow._apply_online_climate_time(req,window,0.,alignment)
    assert req['context']['online_climate_effective_epoch']==alignment['effective_climate_epoch']
    assert hashes=={p:sha(p) for p in hashes}
    result={'status':'PASS','handoff_hash':m['handoff_hash'],'setd_package_id':m['setd']['package_id'],
        'setd_integrity_valid':gate['setd_integrity_valid'],'control_knowledge_valid':m['control_knowledge_valid'],
        'joint_control_proof_cycles':m['knowledge_quality'].get('joint_control_proof_cycles'),
        'legacy_valid_setd':m['valid_setd'],'policy_available':False,'alignment':alignment,
        'bound_query_time':time.isoformat(),'clock_scope':'recorded forcing identity/epoch; not wall-clock flight freshness certification',
        'input_hashes':hashes,'active_inputs_unchanged':True,'actuation_authority':False,'flight_ready':False}
    write(output/'ACTUAL_PRIOR_STATE.json',result)


def persistence(tdoc,output,build_python):
    env={k:v for k,v in os.environ.items() if not k.startswith('TDDT_') and k!='PYTHONPATH'}
    env.update(PYTHONDONTWRITEBYTECODE='1',TMPDIR='/tmp')
    def run(argv,cwd,name):
        r=subprocess.run(argv,cwd=cwd,env=env,text=True,capture_output=True,timeout=60)
        (output/name).write_text(r.stdout+r.stderr)
        if r.returncode:raise RuntimeError(f'{name}: exit {r.returncode}')
        return r
    with tempfile.TemporaryDirectory(prefix='tddt_R205_build_',dir='/tmp') as tmp:
        scratch=Path(tmp).resolve();copies=[]
        for source in (tdoc,tdoc.parent/'layer5_v20260911_1510'):
            target=scratch/source.name
            shutil.copytree(source/'src',target/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info'))
            shutil.copyfile(source/'pyproject.toml',target/'pyproject.toml');copies.append(target)
        package=copies[0];(package/'scripts').mkdir();(package/'tests').mkdir()
        shutil.copyfile(tdoc/'scripts/run_training_workflows.py',package/'scripts/run_training_workflows.py')
        for name in ('test_r2_03_optional_startup.py','test_r2_04_prediction_trigger.py','test_r2_05_prior_acceptance.py'):
            shutil.copyfile(tdoc/'tests'/name,package/'tests'/name)
        sources=['src/tddt_tdoc/knowledge_handoff.py','scripts/run_training_workflows.py']
        expected={p:sha(tdoc/p) for p in sources}
        for name in ('runtime','reports','logs'):
            (package/name).mkdir();(package/name/'GENERATED_SENTINEL').write_text('isolated data only')
        code="""
import json,os,sys
from pathlib import Path
p=Path.cwd().resolve();assert p.parent.name.startswith('tddt_R205_build_')
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
        assert expected=={p:sha(package/p) for p in sources}
        wheels=output/'rebuilt_wheels';wheels.mkdir()
        for index,source in enumerate(copies):
            code="from setuptools import build_meta; print(build_meta.build_wheel("+repr(str(wheels))+"))"
            run([build_python,'-B','-c',code],source,f'WHEEL_BUILD_{index}.log')
        wheel_paths=sorted(wheels.glob('*.whl'));assert len(wheel_paths)==2
        installed=scratch/'installed'
        run([sys.executable,'-B','-m','pip','--no-cache-dir','install','--no-index','--no-deps','--no-compile',
            '--target',str(installed),*[str(p) for p in wheel_paths]],scratch,'WHEEL_INSTALL.log')
        code="""
import hashlib,importlib,importlib.machinery,importlib.util,json,py_compile,sys
from pathlib import Path
target,package,output=map(Path,sys.argv[1:4]);mode=sys.argv[4]
sys.path.insert(0,str(target));sys.path.insert(0,str(package/'tests'))
import tddt_tdoc.knowledge_handoff as handoff
import layer5.phoenix as phoenix
assert Path(handoff.__file__).is_relative_to(target) and Path(phoenix.__file__).is_relative_to(target)
hashes={'src/tddt_tdoc/knowledge_handoff.py':hashlib.sha256(Path(handoff.__file__).read_bytes()).hexdigest(),
        'scripts/run_training_workflows.py':hashlib.sha256((package/'scripts/run_training_workflows.py').read_bytes()).hexdigest()}
if mode=='bytecode':
 full='tddt_tdoc.knowledge_handoff';dest=target/'handoff.pyc'
 py_compile.compile(handoff.__file__,cfile=str(dest),doraise=True)
 loader=importlib.machinery.SourcelessFileLoader(full,str(dest));spec=importlib.util.spec_from_loader(full,loader)
 compiled=importlib.util.module_from_spec(spec);sys.modules[full]=compiled;loader.exec_module(compiled)
 setattr(sys.modules['tddt_tdoc'],'knowledge_handoff',compiled)
tests=importlib.import_module('test_r2_05_prior_acceptance')
if mode=='bytecode':
 dest=package/'scripts/run_training_workflows.pyc'
 py_compile.compile(str(package/'scripts/run_training_workflows.py'),cfile=str(dest),doraise=True)
 spec=importlib.util.spec_from_file_location('r205_compiled_workflow',dest)
 workflow=importlib.util.module_from_spec(spec);spec.loader.exec_module(workflow)
 tests.workflow=workflow
 assert Path(tests.workflow.__file__).suffix=='.pyc'
import pytest
rc=pytest.main(['-q','-p','no:cacheprovider',str(package/'tests/test_r2_05_prior_acceptance.py')]);assert rc==0
(output/('POST_'+mode.upper()+'_STATES.json')).write_text(json.dumps({'status':'PASS','mode':mode,'source_hashes':hashes,
 'Layer5_dependency_sha256':hashlib.sha256(Path(phoenix.__file__).read_bytes()).hexdigest()},indent=2))
"""
        for mode in ('wheel','bytecode'):
            run([sys.executable,'-B','-c',code,str(installed),str(package),str(output),mode],scratch,'TEST_'+mode.upper()+'.log')
            data=json.loads((output/('POST_'+mode.upper()+'_STATES.json')).read_text())
            assert data['source_hashes']==expected
            assert data['Layer5_dependency_sha256']==sha(tdoc.parent/'layer5_v20260911_1510/src/layer5/phoenix.py')
        assert expected=={p:sha(package/p) for p in sources}
        result={'status':'PASS','isolated_actual_reset':True,'active_reset':False,'source_hashes_preserved':expected,
            'wheels':[{'name':p.name,'sha256':sha(p)} for p in wheel_paths],
            'installed_wheel_matrix':'PASS','regenerated_bytecode_matrix':'PASS',
            'workflow_script_bytecode_executed':True,'active_venv_modified':False,'native_compilation':False}
        write(output/'PERSISTENCE_RESULT.json',result)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--check-rebuild',action='store_true');p.add_argument('--build-python',default='/usr/bin/python3')
    a=p.parse_args();tdoc=Path(__file__).resolve().parents[1];output=a.output_dir.resolve()
    output.relative_to(tdoc/'reports/online');output.mkdir(exist_ok=False,parents=True)
    tests=matrix(tdoc,output);actual_prior(tdoc,output,tests)
    if a.check_rebuild:persistence(tdoc,output,a.build_python)
    write(output/'AUDIT_RESULT.json',{'step':'R2.05','status':'PASS','matrix_cases':len(tests.CASES),
        'native_execution':False,'active_reset':False})
    print(json.dumps({'step':'R2.05','status':'PASS','matrix_cases':len(tests.CASES)}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
