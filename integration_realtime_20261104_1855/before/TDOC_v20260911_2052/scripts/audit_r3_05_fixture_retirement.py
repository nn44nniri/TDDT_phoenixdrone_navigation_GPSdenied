#!/usr/bin/env python3
"""R3.05 real-data binding and raw FMU proof, without native execution or ARM."""
import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_ground_policy import require_native_contacts,POLICY_SHA256
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.ground_contact_audit import analyze
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer3 import Layer3Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,data):path.write_text(json.dumps(data,indent=2)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True)
    args=p.parse_args();tdoc=Path(__file__).resolve().parents[1]
    out=args.output_dir.resolve();out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    cfg=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml');policy=require_native_contacts(cfg)
    native=cfg.path_value('TDDT_LAYER6_ROOT',required=True)
    active=native/'runtime/tdoc_environment_binding.json'
    active_before=sha(active) if active.is_file() else None
    evidence=tdoc/'reports/online/R3_04_20261029_ground_v4'
    assert sha(evidence/'R3_04_CLOSEOUT_FA_20261029.md')==policy['basis']['report_sha256']
    assert sha(evidence/'ground_suite_verified/GROUND_SUITE_RESULT.json')==policy['basis']['suite_summary_sha256']
    cases={}
    for name,duration,start in [('native_reset',30,20),('refined_dt',30,20),('template_yaw53',30,20),('negative_no_stiffness',2,1.5)]:
        cases[name]=analyze(evidence/'ground_suite_verified'/name/'ground.csv',duration_s=duration,settled_from_s=start)
    assert all(cases[n]['status']=='PASS' for n in ('native_reset','refined_dt','template_yaw53'))
    assert cases['negative_no_stiffness']['status']=='FAIL' and 'CG_COLLAPSE' in cases['negative_no_stiffness']['violations']
    write(out/'R3_04_REANALYSIS.json',cases)
    binding=prepare_binding(cfg,output_path=out/'ISOLATED_REAL_DATA_BINDING.json')
    assert 'launch_fixture' not in binding and binding['ground_contact_policy']['revision']==policy['revision']
    assert not binding['actuation_authority']
    legacy=TDOCConfig(cfg.path,cfg.package_root,deepcopy(cfg.data))
    legacy.data['layers']['layer6']['online']['ideal_launch_fixture']=True
    try:prepare_binding(legacy,output_path=out/'FORBIDDEN_LEGACY_BINDING.json')
    except ValueError as exc:legacy_error=str(exc)
    else:raise AssertionError('legacy fixture was admitted')
    assert 'retired by R3.05' in legacy_error and not (out/'FORBIDDEN_LEGACY_BINDING.json').exists()
    adapter=Layer2Adapter(cfg);session=adapter.open_isolated_session()
    pitch=math.radians(89.)/2
    state=dict(pn=0.,pe=0.,pd=-.141025821708132,vn=0.,ve=0.,vd=0.,qw=math.cos(pitch),qx=0.,qy=math.sin(pitch),qz=0.,
               p_rate=0.,q_rate=0.,r_rate=0.,omega_left=0.,omega_right=0.,soc=1.)
    try:
        after,record=Layer3Adapter._wing27_physical_step(session['wing'],state,
            dict(left_motor_cmd=0.,right_motor_cmd=0.,left_elevon_rad=0.,right_elevon_rad=0.),
            dt=.1,wind_ned=[0.,0.,0.],air_density=1.2,ground_supported=True)
        assert after['pd']>state['pd'] and after['vd']>.9
        assert record['outputs']['next_pd']==after['pd'] and 'launch_fixture' not in record
        write(out/'WINGMPET_UNCLAMPED.json',{'initial_state':state,'final_state':after,'record':record,
            'acceptance_scope':'raw-output preservation only; this is not a valid ground-contact prediction',
            'native_provider_required':'R7','flight_ready':False})
    finally:adapter.close_isolated_session(session);adapter.close()
    preflight=Layer6OnlineAdapter(cfg,SimpleNamespace()).preflight()
    write(out/'LAYER6_PREFLIGHT.json',preflight)
    assert 'ground_contact_policy_rejected' not in preflight['failure_reasons']
    assert (sha(active) if active.is_file() else None)==active_before
    result={'step':'R3.05','status':'PASS','policy_sha256':POLICY_SHA256,
        'fixture_retired':True,'R3_04_measured_support_reanalyzed':True,
        'real_data_binding_prepared_without_fixture':True,'binding_path':str(out/'ISOLATED_REAL_DATA_BINDING.json'),
        'legacy_option_rejection':legacy_error,'raw_WingMPET_outputs_preserved':True,
        'preflight_ready':preflight['ok'],'remaining_package_api_blockers':preflight['failure_reasons'],
        'active_binding_changed':False,'native_stack_executed':False,'ARM_sent':False,
        'actuation_authority':False,'flight_ready':False,'full_model_compatibility_pending':'R3.06','native_prediction_pending':'R7'}
    write(out/'FIXTURE_RETIREMENT_RESULT.json',result);print(json.dumps(result))


if __name__=='__main__':main()
