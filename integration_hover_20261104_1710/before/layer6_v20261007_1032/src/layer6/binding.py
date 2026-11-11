"""Read the TDOC-owned preparation context; never create or rebase a binding."""
from __future__ import annotations
from datetime import datetime,timedelta
import importlib
import json
import math
import os
from pathlib import Path
import sys
import time
from collections import OrderedDict
from copy import deepcopy

REVISION='native-binding-consumers-r4.02-v1'
WIND_READBACK_TOLERANCE=.002
_CONTEXTS=OrderedDict()


class BindingGuard:
    """Keep a semantically approved publication; rehash live inputs on every use.

    Only immutable semantic validation is reused. Model contents, input bytes,
    file presence and publication identity are never accepted from a cache.
    """
    def __init__(self,path):
        self.path=Path(path).absolute();self._resolved=self.path.resolve(strict=True)
        binding,self.config=load_context(self.path)
        self._approved=json.dumps(binding,sort_keys=True,allow_nan=False)
        self._configuration=self._configuration_snapshot()

    @property
    def binding(self):return json.loads(self._approved)

    def _configuration_snapshot(self):
        keys=('TDDT_LAYER5_KSTORE_ROOT','TDDT_MAP_ROOT','TDDT_SARG_ROOT','TDDT_CRP_ROOT',
              'TDDT_REGION_FILE','TDDT_CLIMATE_FORCING_FILE')
        addresses={}
        for key in keys:
            value=self.config.path_value(key)
            addresses[key]=str(value.resolve()) if value is not None else None
        return (str(self.config.package_root.resolve()),str(self.config.runtime_root.resolve()),
                deepcopy(self.config.data),addresses)

    def _read_inputs(self):
        from tddt_tdoc.native_binding import read_binding,verify_bound_input_files
        expected=self.binding
        if self._configuration_snapshot()!=self._configuration:
            raise ValueError('binding configuration or data address changed')
        if self.path.resolve(strict=True)!=self._resolved:raise ValueError('binding publication link target changed')
        if read_binding(self.path)!=expected:raise ValueError('binding publication replaced during consumption')
        stats=verify_bound_input_files(expected)
        if read_binding(self.path)!=expected:raise ValueError('binding publication changed during input verification')
        return expected,stats

    def query_inputs(self):
        """Query-only input guard. Full model validation is required before writes."""
        expected,_=self._read_inputs()
        return expected

    def check(self):
        from tddt_tdoc.native_binding import read_binding,_stat
        from tddt_tdoc.model_compatibility import capture_model_dependencies
        expected,stats=self._read_inputs()
        if capture_model_dependencies(self.config)!=expected['model_dependencies']:
            raise ValueError('binding model no longer current')
        if any(_stat(p)!=s for p,s in stats.items()):raise ValueError('binding inputs changed during model verification')
        if read_binding(self.path)!=expected:raise ValueError('binding publication changed during verification')
        return expected


def context_guard(path):
    """Bounded cache of approved semantics, with complete live hash revalidation."""
    from tddt_tdoc.native_binding import read_binding
    path=Path(path).absolute();path.resolve(strict=True);current=read_binding(path)
    root=os.environ.get('TDDT_ORCHESTRATOR_ROOT')
    if not root:raise ValueError('TDDT_ORCHESTRATOR_ROOT required')
    key=(str(Path(root).resolve(strict=True)),str(path))
    guard=_CONTEXTS.get(key)
    if guard is not None and current!=guard.binding:
        if current['binding_id']==guard.binding['binding_id']:
            raise ValueError('immutable binding ID reused for changed content')
        guard=None
    if guard is None:
        guard=BindingGuard(path);_CONTEXTS[key]=guard
    guard.check();_CONTEXTS.move_to_end(key)
    while len(_CONTEXTS)>16:_CONTEXTS.popitem(last=False)
    return guard


def wind_parameter_plan(outputs):
    """MicroMet u=east/v=north, meteorological direction FROM true north.

    MicroMet already applies its altitude model. Disable ArduPilot's second
    height law and extra synthetic turbulence. Keep its documented minimum LPF.
    """
    u=outputs['wind_u_3dw'];v=outputs['wind_v_3dw']
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in (u,v)):
        raise ValueError('finite MicroMet horizontal wind required')
    if any(isinstance(outputs.get(k),bool) or outputs.get(k)!=1. for k in ('formula_valid','wind_3dw_valid')):
        raise ValueError('invalid MicroMet wind formula')
    speed=math.hypot(u,v)
    return {'SIM_WIND_T':1.,'SIM_WIND_TURB':0.,'SIM_WIND_TC':.1,'SIM_WIND_DIR_Z':0.,
        'SIM_WIND_DIR':math.degrees(math.atan2(-u,-v))%360. if speed else 0.,'SIM_WIND_SPD':speed}


def validate_wind_application(state,*,now=None):
    """Validate parameter evidence scope; this never grants flight authority."""
    now=time.monotonic() if now is None else now
    def finite(x):return not isinstance(x,bool) and isinstance(x,(int,float)) and math.isfinite(x)
    age=now-state['updated_monotonic'];query_age=now-state['sampled_monotonic']
    if not all(finite(x) for x in (age,query_age)) or not 0<=age<=1. or not 0<=query_age<=1.:
        raise ValueError('native forcing evidence stale')
    app=state['application']
    if (app['schema']!='Layer6-native-wind-readback-v1' or app['scope']!='horizontal_wind_parameters_only'
        or app['verified'] is not True or state['readback_verified'] is not True
        or app['binding_hash']!=state['binding_hash'] or app['plant_time_s']!=state['plant_time_s']
        or app['simulation_time_dataset']!=state['simulation_time_dataset']
        or state.get('application_pending') is not False or state.get('application_error')
        or state.get('actuation_authority') is not False or state.get('navigation_truth_injection') is not False):
        raise ValueError('native forcing evidence incomplete or incompatible')
    if (app.get('instantaneous_atmosphere_verified') is not False or app.get('atomic_parameter_transaction') is not False
        or any(isinstance(app.get(k),bool) or not isinstance(app.get(k),int) or not 1<=app[k]<=255
               for k in ('target_system','target_component'))):
        raise ValueError('native forcing scope/source invalid')
    for name in ('temperature','humidity','pressure','density','precipitation','vertical_wind'):
        unsupported=state['unsupported_fields'][name]
        if (unsupported.get('value') is not None or unsupported.get('verified') is not False
            or unsupported.get('applied') is not False or not unsupported.get('reason')):
            raise ValueError('unsupported forcing cannot claim application: '+name)
    if state['plant_time_source'] not in ('MAVLink/ATTITUDE.time_boot_ms','MAVLink/GLOBAL_POSITION_INT.time_boot_ms','MAVLink/SYSTEM_TIME.time_boot_ms'):
        raise ValueError('native forcing requires observed FCU clock')
    if state['plant_time_s']<5.5:raise ValueError('native wind startup delay not elapsed')
    if (state.get('environment_position_source')!='MAVLink/GLOBAL_POSITION_INT'
        or not finite(state.get('environment_position_time_s'))
        or not finite(state.get('environment_position_received_monotonic'))
        or not 0<=state['plant_time_s']-state['environment_position_time_s']<=1.
        or not 0<=now-state['environment_position_received_monotonic']<=1.):
        raise ValueError('fresh native environment position required')
    for name,wanted in wind_parameter_plan(state['model_outputs']).items():
        row=app['parameters'][name]
        if (row.get('verified') is not True or row.get('message_type')!='PARAM_VALUE'
            or row.get('source_system')!=app['target_system'] or row.get('source_component')!=app['target_component']
            or any(not finite(row.get(k)) for k in ('requested','readback','read_requested_monotonic','read_received_monotonic'))
            or abs(row['requested']-wanted)>1e-9 or abs(row['readback']-wanted)>WIND_READBACK_TOLERANCE
            or not state['sampled_monotonic']<=row['read_requested_monotonic']<=row['read_received_monotonic']<=state['updated_monotonic']):
            raise ValueError('native forcing parameter unverified: '+name)
    return True


def load_context(path):
    raw=os.environ.get('TDDT_ORCHESTRATOR_ROOT')
    if not raw:raise ValueError('TDDT_ORCHESTRATOR_ROOT required; source configs/environment_fixed.sh')
    root=Path(raw).resolve(strict=True);src=root/'src'
    if str(src) not in sys.path:sys.path.insert(0,str(src))
    for name in ('tddt_tdoc','tddt_tdoc.config','tddt_tdoc.native_binding'):
        module=importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(root):
            raise ValueError('binding consumer TDOC import outside authoritative root: '+name)
    from tddt_tdoc.config import TDOCConfig
    from tddt_tdoc.native_binding import read_binding,verify_binding_inputs
    config=TDOCConfig.from_yaml(root/'configs/tdoc.yaml')
    binding=read_binding(path);verify_binding_inputs(binding,config)
    return binding,config


def forcing_time(binding,plant_time_s):
    """Declared plant->dataset projection; never wall time or implicit UTC."""
    if isinstance(plant_time_s,bool) or not isinstance(plant_time_s,(float,int)) or not math.isfinite(plant_time_s):
        raise ValueError('finite observed plant time required')
    elapsed=float(plant_time_s)-float(binding['plant_epoch_s'])
    if elapsed<0:raise ValueError('plant time precedes declared binding anchor')
    epoch=datetime.fromisoformat(binding['effective_climate_epoch'].replace('Z','+00:00'))
    requested=epoch+timedelta(seconds=elapsed)
    forcing=binding['forcing']
    start=datetime.fromisoformat(forcing['common_start']);end=datetime.fromisoformat(forcing['common_end'])
    if not start<=requested<=end:raise ValueError('bound forcing window exhausted; no clamp/rebase/repeat')
    return requested.isoformat(),elapsed


def consumption_identity(binding):
    return {k:binding[k] for k in ('binding_id','binding_hash','context_sha256','model_id','handoff_hash',
        'forcing_file_sha256','effective_climate_epoch','plant_epoch_s')}


def environment_status(binding_path,state_path):
    """Expose verified horizontal wind parameters, with explicit unsupported fields."""
    try:
        binding=context_guard(binding_path).binding
        state=json.loads(Path(state_path).read_text(encoding='utf-8'))
        if any(state.get(k)!=v for k,v in consumption_identity(binding).items()):
            raise ValueError('environment state binding identity mismatch')
        try:
            if state.get('ok') is not True:raise ValueError('environment producer is not ready')
            from .timebase import validate_clock_mapping
            validate_clock_mapping(binding,state)
            validate_wind_application(state)
            if forcing_time(binding,state['plant_time_s'])[0]!=state['simulation_time_dataset']:
                raise ValueError('forcing projection differs from binding')
        except Exception as exc:
            return {**state,'ok':False,'binding_consumed':True,'reason':'native_forcing_not_verified',
                    'error':str(exc),'actuation_authority':False}
        return {**state,'ok':True,'reason':'horizontal_wind_parameters_readback_verified',
                'binding_consumed':True,'actuation_authority':False}
    except Exception as exc:
        return {'ok':False,'binding_consumed':False,'reason':'bound_environment_unavailable',
                'error':str(exc),'actuation_authority':False}


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--binding',required=True)
    args=p.parse_args();binding,_=load_context(args.binding)
    print(json.dumps({'ok':True,**consumption_identity(binding),'scope':'INPUT_IDENTITY_ONLY','actuation_authority':False}))


if __name__=='__main__':main()
