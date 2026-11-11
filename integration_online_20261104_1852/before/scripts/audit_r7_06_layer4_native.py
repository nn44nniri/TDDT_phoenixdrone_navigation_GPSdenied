#!/usr/bin/env python3
"""R7.06 actual TDOC -> Layer4 -> Native provider/certificate acceptance."""
import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import socket
import time

from layer4.native_provider_contract import DOMAIN,digest
from layer4.native_reference_provider import make_reference_request
from layer4.native_prediction_validation import sha,validate_native_pair,validation_policy
from layer6.independent_validation import VisibleNativeRunner,IndependentObservationRunner
from layer6.native_replay import reserve_ports
from layer6.native_session import processes,write
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_environment import prepare_binding
from tddt_tdoc.native_certificate import NativeCertificateAuthority,read_bundle
from tddt_tdoc.layer4_native_reference import Layer4NativeReferenceService
from tddt_tdoc.adapters.layer4 import Layer4Adapter
from tddt_tdoc.model_compatibility import capture_model_dependencies


def panel_continuity(runner,reference):
    ui=json.loads((runner.output/'GUI_OBSERVATION.json').read_text())
    mapping=json.loads((runner.output/'map/observation.json').read_text())
    low=reference['plant_start_bounds_s'][0]
    assert ui['session_id']==mapping['session_id']==runner.session_id
    assert ui['source_fcu_boot_s']>=low+.3 and mapping['source_fcu_boot_s']>=low+.3
    write(runner.output/'PANEL_CONTINUITY.json',{'status':'PASS','both_updated_during_rollout':True,
          'reference_start_s':low,'GUI_source_s':ui['source_fcu_boot_s'],'map_source_s':mapping['source_fcu_boot_s']})


def rejected(result):
    assert not result['verification']['verified'] and not result['verified_reference_candidates']
    assert result['verified_candidates']==[] and not result['flight_authorized'] and result['prediction'] is None


def faults(adapter,service,request):
    rows=[]
    for change in ('candidate','commit','missing_service','disabled','parameter','CRC','binding'):
        r=deepcopy(request);path=None;before=None
        if change=='candidate':r['context']['candidate_id']+='-other'
        if change=='commit':r['timing']['commit_segment_s']=-.1
        if change=='parameter':path=service.case/'prediction/PARAMETERS.json'
        if change=='CRC':path=service.case/'observation/telemetry.jsonl'
        if change=='binding':path=service.authority.guard.path
        if path is not None:before=path.read_bytes();st=path.stat()
        cfg=adapter.engine.config.data.setdefault('verification',{})
        previous=deepcopy(cfg.get('native_reference'))
        try:
            if change=='disabled':cfg['native_reference']={'enabled':False}
            if change in ('parameter','binding'):
                obj=json.loads(before)
                if change=='parameter':obj['SERVO1_TRIM']+=1
                else:obj['binding_id']+='-other'
                path.write_text(json.dumps(obj))
            if change=='CRC':
                trace=[json.loads(x) for x in before.decode().splitlines()]
                row=next(x for x in trace if x['message']['mavpackettype']=='LOCAL_POSITION_NED')
                frame=bytearray.fromhex(row['raw_MAVLink_hex']);frame[-1]^=1;row['raw_MAVLink_hex']=frame.hex()
                path.write_text(''.join(json.dumps(x)+'\n' for x in trace))
            out=adapter.verify_native_reference(r,None if change=='missing_service' else service)
            rejected(out)
            assert 'expired' not in out['verification']['reason'],out
            rows.append({'case':change,'status':'PASS','verification':out['verification']})
        finally:
            if previous is None:cfg.pop('native_reference',None)
            else:cfg['native_reference']=previous
            if path is not None:
                path.write_bytes(before);os.chmod(path,st.st_mode);os.utime(path,ns=(st.st_atime_ns,st.st_mtime_ns))
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--output-dir',required=True)
    a=p.parse_args();cfg=TDOCConfig.from_yaml(a.config);out=Path(a.output_dir).resolve();out.mkdir(parents=True,exist_ok=False)
    model=capture_model_dependencies(cfg);write(out/'MODEL_DEPENDENCIES.json',model)
    prepare_binding(cfg,output_path=out/'binding.json')
    write(out/'TOLERANCE_POLICY_BEFORE_RUN.json',validation_policy())
    authority=NativeCertificateAuthority(cfg,out/'binding.json');write(out/'NEGATIVE_HISTORY.json',authority._history())
    l6=cfg.path_value('TDDT_LAYER6_ROOT',required=True);fs=l6/'FlightStack';ap=fs/'ardupilot'
    params=cfg.package_root/'reports/online/R6_04_20261031_physical_reacceptance_v7/native_final/NATIVE_SESSION/mav.parm'
    hashes={'arduplane':sha(ap/'build/sitl/bin/arduplane'),'jsbsim':sha(fs/'jsbsim/build/src/JSBSim'),
            'aircraft':sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'),
            'reset':sha(ap/'Tools/autotest/aircraft/phoenix_v3/reset.xml'),
            'defaults':sha(ap/'Tools/autotest/default_params/phoenix_v3.parm'),
            'gui_config':sha(l6/'gui/config/f310_phoenix.yaml'),'parameters':sha(params)}
    registry=l6/'runtime/flightstack_state.json';registry_before=registry.read_bytes()
    mission=json.loads(registry_before)['session_id'];mission_before=processes(mission)
    write(out/'MISSION_BEFORE.json',{'registry_sha256':sha(registry),'processes':mission_before})
    helper=cfg.package_root/'scripts/audit_r7_04_independent_validation.py'
    spec=importlib.util.spec_from_file_location('r704_visible_panel_capture',helper)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    write(out/'PANEL_HELPER.json',{'path':str(helper),'sha256':sha(helper)})
    private=deepcopy(cfg);private.data.setdefault('runtime',{})['root']=str(out/'adapter_runtime')
    adapter=Layer4Adapter(private);adapter._ensure()
    results=[]
    try:
        for index,mode in enumerate(('FBWA','QHOVER','QSTABILIZE')):
            case=out/mode;case.mkdir();instance=58+index*2
            plan=[{'domain':DOMAIN,'u_cmd':[x,y,0.,z],'mode':mode,'discrete':[]}
                  for x,y,z in ((.2,-.1,.1),(-.15,.1,-.1),(.1,.05,.05))]
            def new_runner(cls,kind,number):
                for port in (21000+number*10,22000+number*10):
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:probe.bind(('127.0.0.1',port))
                return cls(layer6_root=l6,output=case/kind,binding_file=out/'binding.json',
                           parameter_file=params,backend_hashes=hashes,mission_session_id=mission,instance=number)
            with reserve_ports(instance):
                runner=new_runner(VisibleNativeRunner,'prediction',instance)
                try:
                    runner.start(show_map=True);module.capture_panels(runner)
                    reference=runner.prepare_reference(mode)
                    request=make_reference_request(reference,candidate_id='R706-'+mode,pilot_plan=plan,admission_seconds=90.)
                    def observe(r):
                        with reserve_ports(instance+1):
                            other=new_runner(IndependentObservationRunner,'observation',instance+1)
                            try:
                                other.start(show_map=True);module.capture_panels(other)
                                ref=other.prepare_reference(mode)
                                observation=other.observe_plan(r['pilot_plan'],r['timing']['prediction_dt_s'],
                                    r['timing']['commit_segment_s'],r['context']['candidate_hash'])
                                panel_continuity(other,ref)
                                return observation
                            finally:other.stop()
                    service=Layer4NativeReferenceService(runner,observe,authority,case)
                    result=adapter.verify_native_reference(request,service)
                    write(case/'LAYER4_RESULT.json',result)
                    panel_continuity(runner,reference)
                finally:runner.stop()
            b=read_bundle(case)
            validation=validate_native_pair(b['request'],b['prediction'],b['evidence'],b['observation'],
                prediction_trace=b['prediction_trace'],observation_trace=b['observation_trace'],policy=validation_policy())
            write(case/'VALIDATION.json',validation)
            if result['verification']['verified']:
                assert validation['status']=='CONSISTENT_IN_EXPERIMENTAL_SCOPE'
                negative=faults(adapter,service,request);write(case/'LAYER4_NEGATIVE_CHECKS.json',negative)
                restored=adapter.verify_native_reference(request,service)
                assert restored['verification']['verified'],restored
                write(case/'LAYER4_AFTER_RESTORE.json',restored)
                if mode=='QSTABILIZE':
                    while time.monotonic()<service.certificate['validity']['expires_monotonic_s']+.01:time.sleep(.05)
                    expired=adapter.verify_native_reference(request,service);rejected(expired)
                    assert 'expired' in expired['verification']['reason']
                    write(case/'LAYER4_REAL_EXPIRY_REJECTION.json',expired)
            else:
                assert validation['status']=='REJECTED' and validation['reason']=='finite residual budget exceeded',result
                rejected(result)
            assert service.prediction_calls==service.observation_calls==1
            assert result['verified_candidates']==[] and not result['flight_authorized']
            write(case/'CALL_CHAIN.json',{'entrypoint':'TDOC.Layer4Adapter -> Layer4Engine.run -> FullFidelityVerifier -> Layer4NativeReferenceService -> NativeReferenceProvider',
                'provider_predict_calls':service.prediction_calls,'independent_observation_calls':service.observation_calls,
                'candidate_id':request['context']['candidate_id'],'request_sha256':digest(request),
                'legacy_FMU_or_qLPV_model_used_as_native_prediction':False,'mission_actuated':False})
            row={'mode':mode,'verified_reference':result['verification']['verified'],
                 'maximum_PWM_residual_us':max(r['metrics']['PWM_max_abs_us'] for r in validation['residuals']),
                 'operational_verified':False,'flight_authorized':False}
            results.append(row);print(json.dumps(row),flush=True)
    finally:adapter.close()
    assert any(r['verified_reference'] for r in results)
    assert registry.read_bytes()==registry_before and processes(mission)==mission_before
    assert capture_model_dependencies(cfg)==model
    write(out/'MISSION_AFTER.json',{'registry_sha256':sha(registry),'processes':processes(mission),'unchanged':True})
    write(out/'NATIVE_LAYER4_ACCEPTANCE.json',{'step':'R7.06','status':'PASS','model_id':model['model_id'],'cases':results,
          'scope':'NATIVE_EXACT_REFERENCE_REVIEW','actual_Layer4_Engine_and_TDOC_adapter_used':True,
          'operational_Layer5_FINAL_qualified':False,'flight_authorized':False})


if __name__=='__main__':main()
