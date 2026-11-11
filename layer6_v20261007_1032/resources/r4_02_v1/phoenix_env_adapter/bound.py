"""Consume TDOC forcing and verify the supported native wind parameters."""
from __future__ import annotations
import importlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace


def unsupported_fields():
    return {name:{'value':None,'applied':False,'verified':False,
        'reason':'not implemented by pinned ArduPilot SIM_JSBSim bridge'}
        for name in ('temperature','humidity','pressure','density','precipitation','vertical_wind')}


def _layer6():
    raw=os.environ.get('TDDT_LAYER6_ROOT')
    if not raw:raise ValueError('TDDT_LAYER6_ROOT required for bound Environment Adapter')
    root=Path(raw).resolve(strict=True)
    if str(root/'src') not in sys.path:sys.path.insert(0,str(root/'src'))
    module=importlib.import_module('layer6.binding')
    if not Path(module.__file__).resolve().is_relative_to(root):raise ValueError('Layer6 binding import outside authoritative root')
    return module


class BoundEnvironment:
    def __init__(self,binding_path):
        self.api=_layer6();self.guard=self.api.context_guard(binding_path)
        self.binding,self.config=self.guard.binding,self.guard.config
        self.binding_path=Path(binding_path).resolve()
        root=self.config.path_value('TDDT_CLIMATE_ROOT',required=True)
        if str(root/'python') not in sys.path:sys.path.insert(0,str(root/'python'))
        self.estimate=importlib.import_module('estimate_climate').estimate
        for name in ('estimate_climate','terrain_truth','climate_io','fmu_client'):
            if not Path(sys.modules[name].__file__).resolve().is_relative_to(root):
                raise ValueError('MicroMet import outside authoritative root: '+name)
        self.fmu=root/'fmu/MicroMetTwin.fmu'

    def publish(self,path,state):
        target=Path(path).resolve()
        protected={Path(r['path']) for r in self.binding['input_files']}|{self.binding_path,self.fmu}
        if target in protected:raise ValueError('environment output cannot overwrite a bound input')
        allowed=(self.config.path_value('TDDT_LAYER6_ROOT')/'runtime',self.config.package_root/'reports',Path(tempfile.gettempdir()))
        if not any(target.is_relative_to(p.resolve()) for p in allowed):raise ValueError('environment output outside runtime/reports/temp')
        write_state(target,state)

    def sample(self,plant_time_s,*,latitude=None,longitude=None,altitude_msl_m=None,source='explicit-isolated-query'):
        self.check_context(query_only=True)
        b=self.binding;origin=b['mission_origin']
        timestamp,elapsed=self.api.forcing_time(b,plant_time_s)
        args=SimpleNamespace(input=b['forcing_file'],fmu=str(self.fmu),time=timestamp,
            latitude=origin['latitude_deg'] if latitude is None else float(latitude),
            longitude=origin['longitude_deg'] if longitude is None else float(longitude),
            elevation_sea_level=origin['altitude_msl_m'] if altitude_msl_m is None else float(altitude_msl_m),
            elevation_ground_level=None,dem=b['origin']['terrain_file']['path'],dem_dir=None,terrain_policy='require-dem',
            curvature_radius_m=1000.,ground_elevation_m=None,slope_deg=0.,aspect_deg=0.,curvature=0.,barnes_filter_km=20.,
            with_metadata=False,tddt_layer2=False)
        if not all(math.isfinite(v) for v in (args.latitude,args.longitude,args.elevation_sea_level)):
            raise ValueError('non-finite environment sampling position')
        # Check immutable inputs before each sample; no legacy cache or fallback.
        import hashlib
        if hashlib.sha256(Path(args.input).read_bytes()).hexdigest()!=b['forcing_file_sha256']:
            raise ValueError('accepted forcing changed after binding consumption')
        estimate=self.estimate(args)
        self.check_context()  # Query-time mutation cannot reach PARAM_SET.
        if estimate.get('ok') is False:raise ValueError('bound MicroMet query rejected: '+str(estimate.get('error')))
        output=estimate['fmu_outputs']
        if float(output.get('formula_valid',0))<.5 or float(output.get('wind_3dw_valid',0))<.5:
            raise ValueError('bound MicroMet formula/wind validity failed')
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in output.values()):
            raise ValueError('bound MicroMet outputs contain invalid values')
        return {'schema':'Layer6-bound-environment-consumption-v1',**self.api.consumption_identity(b),
            'binding_consumed':True,'ok':False,'reason':'native_forcing_readback_not_requested',
            'simulation_time_dataset':timestamp,'clock_domain':b['time_contract']['clock_domain'],
            'timezone_status':b['time_contract']['timezone_status'],'UTC_mapping_verified':b['time_contract']['utc_mapping_verified'],
            'plant_time_s':float(plant_time_s),'plant_time_source':source,'mission_elapsed_s':elapsed,
            'updated_monotonic':time.monotonic(),'environment_query':estimate['request'],
            'model_outputs':output,'model_output_semantics':None,'model_output_semantics_status':'unavailable; no fabricated metadata',
            'forcing_path':args.input,'terrain_path':args.dem,'legacy_dataset_used':False,
            'applied_fields':{},'readback_verified':False,'navigation_truth_injection':False,'unsupported_fields':unsupported_fields(),
            'runtime_clock_synchronization_verified':False,'runtime_clock_mapping_verified':False,'actuation_authority':False}

    def check_context(self,*,query_only=False):
        current=self.guard.query_inputs() if query_only else self.guard.check()
        if current!=self.binding:raise ValueError('consumed in-memory binding modified')


def write_state(path,state):
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+target.name+'.',suffix='.tmp',dir=target.parent)
    tmp=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(state,f,ensure_ascii=False,sort_keys=True,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,target)
    finally:tmp.unlink(missing_ok=True)


def prepare_bound(binding_path,output):
    provider=BoundEnvironment(binding_path)
    result=provider.sample(provider.binding['plant_epoch_s'],source='declared-preparation-anchor; not observed execution')
    provider.publish(Path(output)/'binding_environment_preparation.json',result)
    return result


def run_bound_runtime(binding_path,state_file,*,mavlink,update_hz=1.,dry_run=False,plant_time_s=None,max_updates=None,param_retries=3,param_timeout=1.):
    provider=BoundEnvironment(binding_path)
    if not math.isfinite(update_hz) or update_hz<=0:raise ValueError('positive Environment Adapter cadence required')
    if dry_run:
        if plant_time_s is None:raise ValueError('dry-run requires explicit --plant-time-s; no fabricated clock')
        state=provider.sample(plant_time_s);provider.publish(state_file,state);print(json.dumps(state));return state
    if plant_time_s is not None:raise ValueError('live bound runtime takes plant time only from MAVLink')
    if max_updates is not None and max_updates<1:raise ValueError('max-updates must be positive')
    # Clear a prior success even if dependency import or connection creation fails.
    provider.publish(state_file,{**provider.api.consumption_identity(provider.binding),
        'binding_consumed':True,'ok':False,'readback_verified':False,'application_pending':True,
        'reason':'native_forcing_connecting','updated_monotonic':time.monotonic(),
        'actuation_authority':False,'navigation_truth_injection':False})
    master=None;state=None
    try:
        from pymavlink import mavutil
        from .native_forcing import apply_wind
        from layer6.timebase import NativeClock,validate_clock_mapping
        master=mavutil.mavlink_connection(mavlink)
        heartbeat=master.wait_heartbeat(timeout=20)
        if heartbeat is None:raise RuntimeError('native environment heartbeat unavailable')
        target=(heartbeat.get_srcSystem(),heartbeat.get_srcComponent())
        master.target_system,master.target_component=target
        clock=NativeClock(provider.binding,*target)
        position={};position_time=None;last_send=None;count=0;last_clock_received=time.monotonic();newest_observation=None
        position_received=None;position_wait_started=time.monotonic()
        while True:
            if time.monotonic()-last_clock_received>2.:
                raise RuntimeError('native source clock stalled; no wall-time substitution')
            msg=master.recv_match(type=['ATTITUDE','GLOBAL_POSITION_INT','SYSTEM_TIME'],blocking=True,timeout=2.)
            if msg is None:raise RuntimeError('native plant clock unavailable; no wall-time substitution')
            observation=clock.observe(msg,received=time.monotonic())
            if observation is None:continue
            plant=observation['event']['plant_time_s']
            channel=msg.get_type()
            if channel=='GLOBAL_POSITION_INT':
                position=dict(latitude=msg.lat/1e7,longitude=msg.lon/1e7,altitude_msl_m=msg.alt/1000.)
                if not (-90<=position['latitude']<=90 and -180<=position['longitude']<=180):
                    raise ValueError('native environment position out of range')
                position_time=plant;position_received=observation['event']['received_monotonic_s']
            if observation['newest']:
                newest_observation=observation
                last_clock_received=observation['event']['received_monotonic_s']
            # ATTITUDE can precede GLOBAL_POSITION_INT with the same boot epoch.
            # The latter supplies fresh position without becoming a newer clock.
            # Keep the actual newest event and its original receipt/source; do
            # not drop the position update or redate the cached observation.
            if newest_observation is None:continue
            observation=newest_observation;plant=observation['event']['plant_time_s']
            now=time.monotonic()
            if not position or now-position_received>1. or not 0<=plant-position_time<=1.:
                if now-position_wait_started>2.:raise RuntimeError('fresh native environment position unavailable; no origin fallback')
                continue
            position_wait_started=now
            if last_send is not None and now-last_send<1./update_hz:continue
            # Reserve the rest of the one-second freshness budget for real readback.
            if now-position_received>.25 or plant-position_time>.25:continue
            state=provider.sample(plant,source='MAVLink/'+observation['event']['message_type']+'.time_boot_ms',**position)
            state.update(sampled_monotonic=state['updated_monotonic'],application_pending=True)
            state.update(clock_mapping=clock.mapping(observation),runtime_clock_mapping_verified=True)
            state.update(environment_position_source='MAVLink/GLOBAL_POSITION_INT',
                environment_position_time_s=position_time,environment_position_received_monotonic=position_received)
            # Invalidate the previous success before any non-atomic parameter changes.
            provider.publish(state_file,state)
            state['application']=apply_wind(master,state,retries=param_retries,timeout=param_timeout)
            provider.check_context()  # Changed inputs during readback invalidate this update.
            state.update(application_pending=False,readback_verified=state['application'].get('verified') is True,updated_monotonic=time.monotonic(),
                applied_fields=state['application']['parameters'],reason='horizontal_wind_parameters_readback_verified')
            # A slow readback or startup transient never becomes readiness.
            try:
                validate_clock_mapping(provider.binding,state)
                provider.api.validate_wind_application(state);state['ok']=True
            except (ValueError,KeyError,TypeError) as exc:state.update(ok=False,reason=str(exc))
            provider.publish(state_file,state);print(json.dumps(state),flush=True);count+=1;last_send=now
            if max_updates is not None and count>=max_updates:return state
    except Exception as exc:
        # Even a heartbeat/query failure must replace a previous ready state.
        failed={**provider.api.consumption_identity(provider.binding),'binding_consumed':True,
            'ok':False,'readback_verified':False,'application_pending':False,'application_error':str(exc),
            'reason':'native_forcing_application_failed','updated_monotonic':time.monotonic(),
            'actuation_authority':False,'navigation_truth_injection':False}
        provider.publish(state_file,failed)
        raise
    finally:
        if master is not None:master.close()
