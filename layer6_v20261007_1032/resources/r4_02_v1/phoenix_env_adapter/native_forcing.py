"""Apply only the wind controls implemented by the pinned ArduPilot JSBSim bridge.

PARAM_VALUE is a parameter readback, not evidence of an instantaneous atmospheric
state. MAVLink PARAM protocol has no transaction ID; a drained queue, an explicit
read request and source/value checks provide its available correlation boundary.
"""
from __future__ import annotations
import math
import time


def _name(message):
    value=message.param_id
    if isinstance(value,bytes):value=value.decode('ascii',errors='strict')
    return value.rstrip('\x00')


def apply_wind(master,state,*,retries=3,timeout=1.):
    from .bound import _layer6
    api=_layer6();plan=api.wind_parameter_plan(state['model_outputs'])
    if isinstance(retries,bool) or not isinstance(retries,int) or retries<1:
        raise ValueError('positive integer parameter retries required')
    if not math.isfinite(timeout) or timeout<=0:raise ValueError('positive parameter timeout required')
    target=(master.target_system,master.target_component)
    if any(isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=255 for v in target):
        raise ValueError('heartbeat-derived FCU system/component required')
    result={'schema':'Layer6-native-wind-readback-v1','scope':'horizontal_wind_parameters_only',
        'target_system':target[0],'target_component':target[1],
        'binding_hash':state['binding_hash'],'plant_time_s':state['plant_time_s'],
        'simulation_time_dataset':state['simulation_time_dataset'],
        'parameters':{},'verified':False,'instantaneous_atmosphere_verified':False,
        'atomic_parameter_transaction':False,'startup_wind_delay_s':5.,'minimum_smoothing_tc_s':.1}
    def drain():
        for _ in range(2048):
            if master.recv_match(type='PARAM_VALUE',blocking=False) is None:return
        raise RuntimeError('parameter receive queue cannot be drained')
    def collect(pending,end):
        received={}
        while pending and time.monotonic()<end:
            msg=master.recv_match(type='PARAM_VALUE',blocking=True,timeout=max(0.,end-time.monotonic()))
            if msg is None:break
            if (msg.get_srcSystem(),msg.get_srcComponent())!=target:continue
            name=_name(msg)
            if name not in pending:continue
            actual=float(msg.param_value)
            if not math.isfinite(actual) or abs(actual-plan[name])>api.WIND_READBACK_TOLERANCE:continue
            received[name]=(actual,time.monotonic());pending.remove(name)
        return received
    # Batch transport round trips, not parameter semantics: consume every SET
    # echo, drain, then issue independent GETs. No SET echo is a verified GET.
    for attempt in range(1,retries+1):
        drain()
        for name,wanted in plan.items():master.mav.param_set_send(*target,name.encode('ascii'),float(wanted),9)
        echoes=collect(set(plan),time.monotonic()+timeout)
        if set(echoes)!=set(plan):continue
        drain();requested={}
        for name in plan:
            requested[name]=time.monotonic();master.mav.param_request_read_send(*target,name.encode('ascii'),-1)
        replies=collect(set(plan),time.monotonic()+timeout)
        if set(replies)!=set(plan):continue
        result['parameters']={name:{'requested':wanted,'readback':replies[name][0],'verified':True,'message_type':'PARAM_VALUE',
            'source_system':target[0],'source_component':target[1],'attempt':attempt,
            'read_requested_monotonic':requested[name],'read_received_monotonic':replies[name][1]} for name,wanted in plan.items()}
        result['verified']=True
        return result
    raise RuntimeError('native parameter readback unavailable or mismatched: '+','.join(plan))
