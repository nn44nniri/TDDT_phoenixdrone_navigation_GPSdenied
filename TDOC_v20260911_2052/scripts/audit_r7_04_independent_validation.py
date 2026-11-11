#!/usr/bin/env python3
"""Predeclared-tolerance comparison against a fresh independent Native rollout."""
import argparse
from copy import deepcopy
import ctypes
import ctypes.util
import json
from pathlib import Path
import re
import socket
import subprocess

from layer4.native_provider_contract import DOMAIN, digest
from layer4.native_reference_provider import NativeReferenceProvider, make_reference_request
from layer4.native_prediction_validation import sha, validate_native_pair, validation_policy
from layer6.independent_validation import IndependentObservationRunner, VisibleNativeRunner
from layer6.native_replay import reserve_ports
from layer6.native_session import processes, write
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.model_compatibility import capture_model_dependencies
from tddt_tdoc.native_environment import prepare_binding


def capture_panels(runner):
    tree=subprocess.run(['xwininfo','-root','-tree'],capture_output=True,text=True,timeout=5,check=True).stdout
    root=subprocess.run(['xwininfo','-root'],capture_output=True,text=True,timeout=5,check=True).stdout
    width=int(re.search(r'Width:\s+(\d+)',root)[1]);height=int(re.search(r'Height:\s+(\d+)',root)[1])
    windows=[]
    for prefix in ('Phoenix validation GUI ','Phoenix reference map '):
        title=prefix+runner.session_id[:8]
        found=[line.strip() for line in tree.splitlines() if title in line]
        if len(found)!=1: raise RuntimeError('owned native panel not uniquely visible: '+title)
        window=int(found[0].split()[0],16)
        info=subprocess.run(['xwininfo','-id',hex(window)],capture_output=True,text=True,timeout=5,check=True).stdout
        if 'Map State: IsViewable' not in info: raise RuntimeError('native panel is not mapped: '+title)
        windows.append({'title':title,'window_id':window,'viewable':True})
    lib=ctypes.CDLL(ctypes.util.find_library('X11'))
    lib.XOpenDisplay.argtypes=[ctypes.c_char_p];lib.XOpenDisplay.restype=ctypes.c_void_p
    lib.XMoveResizeWindow.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_int,ctypes.c_uint,ctypes.c_uint]
    lib.XFlush.argtypes=[ctypes.c_void_p];lib.XCloseDisplay.argtypes=[ctypes.c_void_p]
    display=lib.XOpenDisplay(None)
    if not display: raise RuntimeError('native X display unavailable')
    try:
        left=max(1000,int(width*.58));right=max(500,width-left-20)
        for i,row in enumerate(windows):
            lib.XMoveResizeWindow(display,row['window_id'],0 if i==0 else left+10,0,
                                 left-10 if i==0 else right,min(height-80,850))
        lib.XFlush(display)
    finally: lib.XCloseDisplay(display)
    for i,row in enumerate(windows):
        target=runner.output/('PHOENIX_GUI.png' if i==0 else 'NATIVE_MAP.png')
        subprocess.run(['import','-window',hex(row['window_id']),str(target)],check=True,timeout=10)
        row['screenshot_sha256']=sha(target)
    gui=json.loads((runner.output/'GUI_OBSERVATION.json').read_text())
    mapping=json.loads((runner.output/'map/observation.json').read_text())
    assert gui['session_id']==mapping['session_id']==runner.session_id and gui['connected'] and gui['visible']
    assert mapping['position_matches_map'] and mapping['map_alive']
    assert abs(gui['latitude_deg']-mapping['latitude_deg'])<1e-5 and abs(gui['longitude_deg']-mapping['longitude_deg'])<1e-5
    write(runner.output/'VISIBLE_PANELS.json',{'session_id':runner.session_id,'windows':windows,
          'screen_pixels':[width,height],'GUI':gui,'map':mapping,'both_mapped':True})


def negative_checks(request,prediction,evidence,observation,pred_path,obs_path,policy):
    results=[]
    for change in ('same_session','candidate','mode','parameter','origin','trace_hash','policy','unavailable',
                   'first_PWM','source_time','qualification','horizon','missing_witness'):
        r,p,e,o=map(deepcopy,(request,prediction,evidence,observation));pol=deepcopy(policy)
        if change=='same_session': o['branch_session_id']=p['isolation']['branch_session_id']
        if change=='candidate': o['candidate_hash']='0'*64
        if change=='mode': o['mode']='QLOITER'
        if change=='parameter':
            o['reference']['backend']['parameters']['SERVO1_TRIM']+=1
            o['reference']['backend']['parameter_snapshot_sha256']=digest(o['reference']['backend']['parameters'])
        if change=='origin': o['reference']['coordinate_origin']['message']['latitude']+=1
        if change=='trace_hash': o['trace_sha256']='0'*64
        if change=='policy': pol['limits']['PWM_max_abs_us']=10000.
        if change=='unavailable': p['status']='UNAVAILABLE';p['samples']=[];e['prediction_sha256']=digest(p)
        if change=='first_PWM': e['first_move']['channels']['post_mixer_pwm']['value'][0]+=150
        if change=='source_time': e['first_move']['channels']['plant_state']['source']['source_time_s']+=.01
        if change=='qualification': e['qualified']=True
        if change=='horizon': e['forecast_horizon_s']+=.1
        if change=='missing_witness': e['source_time_witnesses']=[]
        result=validate_native_pair(r,p,e,o,prediction_trace=pred_path,observation_trace=obs_path,policy=pol)
        assert result['status']=='REJECTED',(change,result)
        results.append({'case':change,'status':'PASS','rejection_reason':result['reason']})
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default='configs/tdoc.yaml');p.add_argument('--output-dir',required=True)
    p.add_argument('--case-limit',type=int,default=3);p.add_argument('--repeats',type=int,default=2)
    p.add_argument('--mode',choices=('FBWA','QHOVER','QSTABILIZE'),help='run one explicitly selected diagnostic mode')
    a=p.parse_args()
    if not 1<=a.case_limit<=3 or not 1<=a.repeats<=3:p.error('case limit/repeats outside 1..3')
    cfg=TDOCConfig.from_yaml(a.config);root=cfg.package_root.parent;l6=cfg.path_value('TDDT_LAYER6_ROOT',required=True)
    out=Path(a.output_dir).resolve();out.mkdir(parents=True,exist_ok=False)
    policy=validation_policy();write(out/'TOLERANCE_POLICY_BEFORE_RUN.json',policy)
    policy_before=sha(out/'TOLERANCE_POLICY_BEFORE_RUN.json')
    model=capture_model_dependencies(cfg);write(out/'MODEL_DEPENDENCIES.json',model)
    prior=json.loads((cfg.package_root/'reports/online/R7_03_20261031_native_prediction_v8/MODEL_DEPENDENCIES.json').read_text())
    for name in ('arduplane_binary','jsbsim_binary','native_aircraft','native_bridge','native_body_rate_decoder',
                 'layer6/native_forecast.py','layer4/native_reference_provider.py'):
        assert model['artifacts'][name]==prior['artifacts'][name],name
    binding=prepare_binding(cfg,output_path=out/'binding.json')
    registry=l6/'runtime/flightstack_state.json';registry_before=registry.read_bytes()
    mission=json.loads(registry_before)['session_id'];mission_before=processes(mission)
    write(out/'MISSION_BEFORE.json',{'registry_sha256':sha(registry),'processes':mission_before})
    ap=l6/'FlightStack/ardupilot';fs=l6/'FlightStack'
    params=cfg.package_root/'reports/online/R6_04_20261031_physical_reacceptance_v7/native_final/NATIVE_SESSION/mav.parm'
    hashes={'arduplane':sha(ap/'build/sitl/bin/arduplane'),'jsbsim':sha(fs/'jsbsim/build/src/JSBSim'),
            'aircraft':sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'),
            'reset':sha(ap/'Tools/autotest/aircraft/phoenix_v3/reset.xml'),
            'defaults':sha(ap/'Tools/autotest/default_params/phoenix_v3.parm'),
            'gui_config':sha(l6/'gui/config/f310_phoenix.yaml'),'parameters':sha(params)}
    results=[]
    modes=(a.mode,) if a.mode else ('FBWA','QHOVER','QSTABILIZE')[:a.case_limit]
    for mode in modes:
        for repeat in range(a.repeats):
            case=out/(mode+'_%02d'%repeat);case.mkdir()
            plan=[{'domain':DOMAIN,'u_cmd':[x,y,0.,z],'mode':mode,'discrete':[]}
                  for x,y,z in ((.2,-.1,.1),(-.15,.1,-.1),(.1,.05,.05))]
            for kind,cls in (('prediction',VisibleNativeRunner),('observation',IndependentObservationRunner)):
                # Separate endpoints per branch avoid rapid reuse/TIME_WAIT.
                # An occupied endpoint is still rejected; no foreign owner is
                # killed and no SO_REUSEADDR bypass is used.
                instance=52+2*(3*('FBWA','QHOVER','QSTABILIZE').index(mode)+repeat)+(kind=='observation')
                with reserve_ports(instance):
                    for port in (21000+10*instance,22000+10*instance):
                        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:s.bind(('127.0.0.1',port))
                    runner=cls(layer6_root=l6,output=case/kind,binding_file=out/'binding.json',
                               parameter_file=params,backend_hashes=hashes,mission_session_id=mission,instance=instance)
                    try:
                        runner.start(show_map=True)
                        capture_panels(runner)
                        reference=runner.prepare_reference(mode)
                        if kind=='prediction':
                            request=make_reference_request(reference,candidate_id='R704-'+mode+'-%d'%repeat,pilot_plan=plan)
                            provider=NativeReferenceProvider(runner)
                            prediction=provider.predict(request);evidence=provider.last_evidence
                            write(case/'REQUEST.json',request);write(case/'PREDICTION.json',prediction);write(case/'FORECAST_EVIDENCE.json',evidence)
                            assert prediction['status']=='PREDICTED',prediction['reason']
                        else:
                            observation=runner.observe_plan(plan,.2,.1,request['context']['candidate_hash'])
                            write(case/'OBSERVATION.json',observation)
                    except Exception as exc:
                        write(out/'ERROR.json',{'mode':mode,'repeat':repeat,'branch':kind,'reason':repr(exc)})
                        raise
                    finally:runner.stop()
                assert not processes(runner.session_id)
                ui=json.loads((runner.output/'GUI_OBSERVATION.json').read_text())
                mapping=json.loads((runner.output/'map/observation.json').read_text())
                start=reference['plant_start_bounds_s'][0]
                assert ui['source_fcu_boot_s']>=start+.3 and mapping['source_fcu_boot_s']>=start+.3
                assert ui['session_id']==mapping['session_id']==runner.session_id
                write(runner.output/'PANEL_CONTINUITY.json',{'status':'PASS','reference_start_s':start,
                      'GUI_source_s':ui['source_fcu_boot_s'],'map_source_s':mapping['source_fcu_boot_s'],
                      'both_updated_during_rollout':True})
            pred_path=case/'prediction/telemetry.jsonl';obs_path=case/'observation/telemetry.jsonl'
            validation=validate_native_pair(request,prediction,evidence,observation,
                                           prediction_trace=pred_path,observation_trace=obs_path,policy=policy)
            write(case/'VALIDATION.json',validation)
            # A real residual outside the preregistered budget is a valid
            # negative experiment, not permission to enlarge the budget or
            # silently retry until a matching trace is obtained.
            assert (validation['status']=='CONSISTENT_IN_EXPERIMENTAL_SCOPE' or
                    (validation['status']=='REJECTED' and
                     validation['reason']=='finite residual budget exceeded')),validation
            write(case/'NEGATIVE_CHECKS.json',negative_checks(request,prediction,evidence,observation,pred_path,obs_path,policy))
            summary={'mode':mode,'repeat':repeat,'status':'PASS','validation_status':validation['status'],
                     'max_residuals':{k:max(r['metrics'][k] for r in validation['residuals']) for k in policy['limits']}}
            results.append(summary);print(json.dumps(summary),flush=True)
    assert registry.read_bytes()==registry_before and processes(mission)==mission_before
    assert capture_model_dependencies(cfg)==model and sha(out/'TOLERANCE_POLICY_BEFORE_RUN.json')==policy_before
    write(out/'MISSION_AFTER.json',{'registry_sha256':sha(registry),'processes':processes(mission),
          'unchanged':True,'active_mission_present':bool(mission_before)})
    assert any(r['validation_status']=='CONSISTENT_IN_EXPERIMENTAL_SCOPE' for r in results)
    write(out/'NATIVE_VALIDATION_ACCEPTANCE.json',{'step':'R7.04','roadmap':'v9 linked document','status':'PASS',
          'model_id':model['model_id'],'cases':results,'policy_sha256':digest(policy),'policy_unchanged':True,
          'GUI_and_map_same_branch':True,'independent_execution_and_raw_codec_validation':True,
          'unbounded_internal_state_retained':True,'all_predictions_within_budget':all(
              r['validation_status']=='CONSISTENT_IN_EXPERIMENTAL_SCOPE' for r in results),
          'provider_certificate_issued':False,'flight_authorized':False})


if __name__=='__main__':main()
