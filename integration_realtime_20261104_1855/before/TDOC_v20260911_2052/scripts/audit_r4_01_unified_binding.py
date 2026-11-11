#!/usr/bin/env python3
"""R4.01 real-data preparation audit; no native launch or active-state writes."""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.native_binding import read_binding,validate_binding,verify_binding_inputs
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.model_compatibility import certificate_compatibility
from tddt_tdoc.storage import read_json_auto


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',required=True,type=Path)
    args=p.parse_args();tdoc=Path(__file__).resolve().parents[1];root=tdoc.parent
    out=args.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    cfg=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml')
    spec=importlib.util.spec_from_file_location('protection',tdoc/'scripts/freeze_r1_02_state.py')
    protection=importlib.util.module_from_spec(spec);spec.loader.exec_module(protection)
    baseline=root/'recovery_baselines/R3_05_20261029_fixture_retirement/PROTECTED_MANIFEST.json'
    before=protection.verify_manifest(baseline);assert before['status']=='PASS';write(out/'PROTECTION_BEFORE.json',before)
    active=cfg.path_value('TDDT_LAYER6_ROOT',required=True)/'runtime/tdoc_environment_binding.json'
    active_before=sha(active) if active.is_file() else None
    binding=prepare_binding(cfg,output_path=out/'BINDING.json')
    assert read_binding(out/'BINDING.json')==binding
    verified=verify_binding_inputs(binding,cfg);assert verified['ok'];write(out/'INPUT_IDENTITY_CHECK.json',verified)
    # The actual user preparation CLI must emit the same semantic context with
    # its own publication ID. It is pointed at an isolated output explicitly.
    argv=[sys.executable,'-B',str(tdoc/'scripts/prepare_native_layer6_session.py'),
        '--config',str(cfg.path),'--output',str(out/'CLI_BINDING.json')]
    completed=subprocess.run(argv,cwd=tdoc,text=True,capture_output=True,timeout=60)
    (out/'PREPARATION_CLI.log').write_text(completed.stdout+completed.stderr)
    assert completed.returncode==0,completed.stderr
    cli=read_binding(out/'CLI_BINDING.json')
    assert cli['context_sha256']==binding['context_sha256'] and cli['binding_id']!=binding['binding_id']
    cli_result=json.loads(completed.stdout)
    assert not cli_result['flight_started'] and not cli_result['knowledge_modified']
    corrupt=deepcopy(binding);corrupt['forcing_file_sha256']='0'*64
    try:validate_binding(corrupt)
    except ValueError as exc:negative=str(exc)
    else:raise AssertionError('corrupt binding admitted')
    h=OfflineKnowledgeHandoff.__new__(OfflineKnowledgeHandoff)
    h.latest_path=Path(binding['knowledge']['handoff_file']['path']);gate=h.validate_online_start();assert gate['ok']
    hm=gate['manifest'];cycle=read_json_auto(cfg.runtime_root/'cycles'/(hm['source_transaction_id']+'.json.gz'))
    old=cycle['layer_status']['4']['evidence']['offline_model_control']['certificate']
    old_gate=certificate_compatibility(old,binding['model_dependencies']);assert not old_gate['ok']
    write(out/'HISTORICAL_MODEL_CERTIFICATE_GATE.json',old_gate)
    previous=read_json_auto(tdoc/'reports/online/R3_06_20261029_model_compatibility_v4/real_model_audit_final/CURRENT_MODEL_DEPENDENCIES.json')
    old_files=previous['artifacts'];new_files=binding['model_dependencies']['artifacts']
    delta={'added':sorted(set(new_files)-set(old_files)),'removed':sorted(set(old_files)-set(new_files)),
        'changed':sorted(k for k in set(old_files)&set(new_files) if old_files[k]!=new_files[k])}
    write(out/'MODEL_IDENTITY_TRANSITION.json',{'previous_model_id':previous['model_id'],'current_model_id':binding['model_id'],
        'dependency_difference':delta,'historical_certificate_upgraded':False})
    after=protection.verify_manifest(baseline);assert after['status']=='PASS';write(out/'PROTECTION_AFTER.json',after)
    assert (sha(active) if active.is_file() else None)==active_before
    result={'step':'R4.01','status':'PASS','binding_id':binding['binding_id'],'binding_hash':binding['binding_hash'],
        'context_sha256':binding['context_sha256'],'schema':binding['schema'],'revision':binding['revision'],
        'model_id':binding['model_id'],'model_dependencies':len(new_files),'identity_groups':list(binding['identity_hashes']),
        'source_files_verified':len(binding['input_files']),'setd_package_files_verified':len(binding['knowledge']['verified_package_files']),
        'handoff_hash':binding['handoff_hash'],'handoff_file_sha256':binding['knowledge']['handoff_file']['file_sha256'],
        'forcing_file_sha256':binding['forcing_file_sha256'],'plant_epoch_s':binding['plant_epoch_s'],
        'effective_climate_epoch':binding['effective_climate_epoch'],'clock_domain':binding['time_contract']['clock_domain'],
        'UTC_mapping_verified':binding['time_contract']['utc_mapping_verified'],
        'mission_origin':binding['mission_origin'],'actual_preparation_CLI_passed':True,'corruption_rejection':negative,
        'active_binding_changed':False,'protected_baseline_unchanged':True,'historical_model_certificate_reused':False,
        'native_stack_started':False,'ARM_sent':False,'forcing_refreshed':False,'offline_training_started':False,
        'execution_environment_verified':False,'runtime_clock_synchronization_verified':False,'flight_ready':False,
        'next_microstep':'R4.02'}
    write(out/'UNIFIED_BINDING_RESULT.json',result);print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
