#!/usr/bin/env python3
"""Read-only R7.01 alignment against the accepted R6.05 native backend.

This audits a historical deployment and contract semantics, not prediction or
freshness of the current mission. Never opens sockets, launches FCU or arms.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path

import yaml
from layer4.native_provider_contract import contract, digest, REQUIRED_PARAMETERS, MODES, DISCRETE
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.model_compatibility import capture_model_dependencies


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='configs/tdoc.yaml')
    parser.add_argument('--output-dir',required=True)
    args=parser.parse_args()
    cfg=TDOCConfig.from_yaml(args.config);root=cfg.package_root.parent
    out=Path(args.output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    write=lambda n,v:(out/n).write_text(json.dumps(v,indent=2,ensure_ascii=False)+'\n')
    old=cfg.package_root/'reports/online/R6_05_20261031_native_lifecycle_v7'
    accepted=json.loads((old/'CLOSEOUT_RESULT.json').read_text());assert accepted['status']=='PASS'
    before=json.loads((old/'MODEL_DEPENDENCIES.json').read_text())
    current=capture_model_dependencies(cfg)
    stable=('arduplane_binary','jsbsim_binary','native_aircraft','packaged_aircraft','ardupilot_default_parameters',
            'native_gui_configuration','pilot_configuration','native_gui/phoenix_f310_gui.py',
            'layer6/contracts.py','layer6/executor.py','layer6/virtual_pilot.py','layer6/mavlink_link.py',
            'layer6/segments.py','native_launcher','layer6/native_session.py')
    assert all(current['artifacts'][k]==before['artifacts'][k] for k in stable)
    native=cfg.path_value('TDDT_LAYER6_ROOT',required=True);fs=native/'FlightStack'
    source_session=old/'native_verified'/('SESSION_'+accepted['native_acceptance']['first_session'])
    parameters={}
    for line in (source_session/'mav.parm').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            k,v=line.split();parameters[k]=float(v)
    assert all(k in parameters for k in REQUIRED_PARAMETERS)
    gui=yaml.safe_load((native/'gui/config/f310_phoenix.yaml').read_text())
    gains={k:gui['controller'][k] for k in ('max_pitch','max_roll','max_yaw')}
    executor=ast.parse((native/'src/layer6/executor.py').read_text())
    constants={n.targets[0].id:ast.literal_eval(n.value) for n in executor.body
               if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)
               and n.targets[0].id in ('MODE_TO_ACTION','DISCRETE_TO_ACTION')}
    assert set(constants['MODE_TO_ACTION'])==set(MODES)
    assert set(constants['DISCRETE_TO_ACTION'])==set(DISCRETE)
    plane=fs/'ardupilot/ArduPlane/GCS_MAVLink_Plane.cpp';text=plane.read_text()
    required=("manual_override(plane.channel_roll, packet.y, 1000, 2000, tnow)",
              "manual_override(plane.channel_pitch, packet.x, 1000, 2000, tnow, true)",
              "manual_override(plane.channel_throttle, packet.z, 0, 1000, tnow)",
              "manual_override(plane.channel_rudder, packet.r, 1000, 2000, tnow)")
    assert all(x in text for x in required)
    paths=[native/'src/layer6/contracts.py',native/'src/layer6/executor.py',
           native/'src/layer6/virtual_pilot.py',native/'src/layer6/mavlink_link.py',
           native/'src/layer6/segments.py',native/'src/gui/phoenix_f310_gui.py',plane]
    sources={str(p.relative_to(root)):sha(p) for p in paths}
    meta=json.loads((source_session/'session.json').read_text())
    state=json.loads((old/'native_verified/TRANSPORT_PREPARATION.json').read_text())['current_gui_state']
    backend={'arduplane_sha256':current['artifacts']['arduplane_binary']['sha256'],
             'jsbsim_sha256':current['artifacts']['jsbsim_binary']['sha256'],
             'aircraft_sha256':meta['aircraft_sha256'],
             'defaults_sha256':current['artifacts']['ardupilot_default_parameters']['sha256'],
             'gui_config_sha256':sha(native/'gui/config/f310_phoenix.yaml'),
             'execution_path_sha256':digest(sources),'parameter_snapshot_sha256':digest(parameters),
             'parameters':parameters,'gui_gains':gains,'initial_mode':state['mode'],'armed':state['armed']}
    write('REFERENCE_BACKEND.json',{'scope':'HISTORICAL_R605_BACKEND_REFERENCE_NOT_PROVIDER_REQUEST',
          'backend':backend,'parameter_source':str((source_session/'mav.parm').relative_to(root)),
          'parameter_source_sha256':sha(source_session/'mav.parm'),'historical_native_session_id':meta['session_id'],
          'historical_telemetry_session_id':state['session_id'],'historical_model_id':meta['model_id'],
          'fresh_online_state_claimed':False,'restorable_controller_checkpoint_available':False,
          'unobservable_state':['controller','mixer','spool','actuator_dynamics','rc_override','motor_speed'],
          'navigation_state_from_truth_created':False,'actual_L3_snapshot_or_L4_candidate_created':False})
    write('EXECUTION_SOURCE_HASHES.json',sources)
    write('PROVIDER_CONTRACT.json',contract());write('MODEL_DEPENDENCIES.json',current)
    result={'step':'R7.01','status':'PASS','scope':'contract and backend alignment only',
            'source_reference':'accepted R6.05 native_verified historical evidence',
            'stable_R605_execution_artifacts':list(stable),'all_stable_artifacts_match':True,
            'current_model_id':current['model_id'],'R605_model_id':before['model_id'],
            'contract_sha256':digest(contract()),'parameter_count':len(parameters),
            'gui_gains':gains,'FCU_manual_pitch_reversed':True,'mode_and_discrete_maps_match':True,
            'contract_exported_in_Layer4_source':True,'provider_implemented':False,
            'replay_executed':False,'active_mission_advanced':False,'flight_authorized':False,
            'physical_HOLD_qualified':False,'open_finding':'R604-PHYSICAL-01'}
    write('BACKEND_CONTRACT_AUDIT.json',result);print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
