"""Correlate observed FCU boot time with dataset time and host monotonic receipt.

No wall-clock epoch, assumed real-time rate, extrapolation or first-packet rebase.
The declared binding anchor remains immutable across runtime attachments.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

SCHEMA='Layer6-native-clock-mapping-r4.04-v1'
SCOPE='observed-source-to-dataset-and-monotonic-receipt-only'
CHANNELS=('ATTITUDE','GLOBAL_POSITION_INT','SYSTEM_TIME')


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def finite(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)


def monotonic_domain():
    """Identify the Linux host boot, without publishing machine identifiers."""
    info=time.get_clock_info('monotonic')
    if not info.monotonic or info.adjustable:raise ValueError('non-adjustable monotonic clock required')
    machine=Path('/etc/machine-id').read_text().strip()
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not machine or not boot:raise ValueError('host monotonic clock identity unavailable')
    return _hash({'host':machine,'boot':boot,'clock':info.implementation})


def _boot(value):
    if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<2**32:
        raise ValueError('source time_boot_ms must be uint32; no fabricated or wrapped clock')
    return value/1000.


class NativeClock:
    def __init__(self,binding,system,component):
        if any(isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=255 for v in (system,component)):
            raise ValueError('observed FCU clock source required')
        self.binding=binding;self.system=system;self.component=component
        self.session=uuid.uuid4().hex;self.domain=monotonic_domain()
        self.by_channel={};self.latest=None;self.last_received=None;self.sequence=0

    def observe(self,message,*,received=None):
        if (message.get_srcSystem(),message.get_srcComponent())!=(self.system,self.component):return None
        channel=message.get_type()
        if channel not in CHANNELS:return None
        raw=getattr(message,'time_boot_ms',None);plant=_boot(raw)
        received=time.monotonic() if received is None else received
        if not finite(received) or received<0 or (self.last_received is not None and received<self.last_received):
            raise ValueError('monotonic observation clock reversed or invalid')
        self.last_received=received
        prior=self.by_channel.get(channel)
        if prior is not None and raw<prior:raise ValueError('native source clock reversed/restarted/wrapped; no rebase')
        if prior==raw:return None  # Duplicate packets cannot refresh position or readiness.
        from .binding import forcing_time
        timestamp,elapsed=forcing_time(self.binding,plant)
        self.by_channel[channel]=raw;self.sequence+=1
        event={'message_type':channel,'source_system':self.system,'source_component':self.component,
            'raw_time_boot_ms':raw,'plant_time_s':plant,'mission_elapsed_s':elapsed,
            'forcing_timestamp':timestamp,'received_monotonic_s':received,'sequence':self.sequence}
        newest=self.latest is None or plant>self.latest['plant_time_s']
        previous=self.latest
        if newest:self.latest=event
        return {'event':event,'previous':previous,'newest':newest}

    def mapping(self,observation):
        if not observation['newest']:raise ValueError('late channel sample cannot advance the forcing query')
        from .binding import consumption_identity
        event=observation['event'];previous=observation['previous'];rate=None
        if previous is not None:
            dm=event['received_monotonic_s']-previous['received_monotonic_s']
            dp=event['plant_time_s']-previous['plant_time_s']
            if dm>0:rate={'delta_monotonic_s':dm,'delta_plant_s':dp,'plant_seconds_per_monotonic_second':dp/dm}
        result={'schema':SCHEMA,'scope':SCOPE,**consumption_identity(self.binding),
            'session_id':self.session,'monotonic_domain_id':self.domain,'observation':event,
            'previous_observation':previous,'measured_rate_diagnostic':rate,
            'mission_elapsed_equation':'plant_time_s - declared_binding_plant_epoch_s',
            'dataset_equation':'accepted_effective_climate_epoch + mission_elapsed_s',
            'monotonic_role':'local receipt/freshness/cadence only; no epoch or rate inference',
            'declared_anchor_observed':False,'first_packet_rebase':False,'UTC_conversion_performed':False,
            'clock_rate_assumed':False,'synchronized_absolute_clocks':False}
        result['mapping_hash']=_hash(result)
        return result


def validate_clock_mapping(binding,state,*,now=None):
    from .binding import consumption_identity,forcing_time
    mapping=state['clock_mapping'];event=mapping['observation']
    now=time.monotonic() if now is None else now
    if (mapping.get('schema')!=SCHEMA or mapping.get('scope')!=SCOPE
        or mapping.get('mapping_hash')!=_hash({k:v for k,v in mapping.items() if k!='mapping_hash'})
        or any(mapping.get(k)!=v for k,v in consumption_identity(binding).items())
        or mapping.get('monotonic_domain_id')!=monotonic_domain()
        or uuid.UUID(mapping['session_id']).hex!=mapping['session_id'] or uuid.UUID(mapping['session_id']).version!=4
        or state.get('runtime_clock_mapping_verified') is not True):
        raise ValueError('native clock mapping identity/domain/evidence mismatch')
    if any(mapping.get(k) is not False for k in ('declared_anchor_observed','first_packet_rebase',
            'UTC_conversion_performed','clock_rate_assumed','synchronized_absolute_clocks')):
        raise ValueError('unproven clock conversion/rebase/synchronization')
    if (mapping.get('mission_elapsed_equation')!='plant_time_s - declared_binding_plant_epoch_s'
        or mapping.get('dataset_equation')!='accepted_effective_climate_epoch + mission_elapsed_s'
        or mapping.get('monotonic_role')!='local receipt/freshness/cadence only; no epoch or rate inference'):
        raise ValueError('native clock mapping equations unsupported')
    plant=_boot(event['raw_time_boot_ms']);timestamp,elapsed=forcing_time(binding,plant)
    received=event['received_monotonic_s'];sampled=state['sampled_monotonic']
    if (event['message_type'] not in CHANNELS or not all(finite(x) for x in (now,received,sampled))
        or not 0<=now-received<=1. or not received<=sampled<=state['updated_monotonic']<=now
        or event['plant_time_s']!=plant or event['mission_elapsed_s']!=elapsed or event['forcing_timestamp']!=timestamp
        or state['plant_time_s']!=plant or state['mission_elapsed_s']!=elapsed or state['simulation_time_dataset']!=timestamp
        or state['environment_query']['time']!=timestamp
        or state.get('runtime_clock_synchronization_verified') is not False
        or state['plant_time_source']!='MAVLink/'+event['message_type']+'.time_boot_ms'
        or state['clock_domain']!=binding['time_contract']['clock_domain']
        or state['timezone_status']!=binding['time_contract']['timezone_status']
        or state['UTC_mapping_verified'] is not binding['time_contract']['utc_mapping_verified']):
        raise ValueError('native clock mapping projection/freshness mismatch')
    if 'application' in state:
        app=state['application']
        if (event['source_system'],event['source_component'])!=(app['target_system'],app['target_component']):
            raise ValueError('native clock and application FCU sources differ')
    previous=mapping['previous_observation'];rate=mapping['measured_rate_diagnostic']
    if previous is None:
        if rate is not None:raise ValueError('unobserved clock rate cannot be reported')
    else:
        dp=plant-previous['plant_time_s'];dm=received-previous['received_monotonic_s']
        if dp<=0 or dm<0:raise ValueError('clock observation order invalid')
        expected=None if dm==0 else {'delta_plant_s':dp,'delta_monotonic_s':dm,'plant_seconds_per_monotonic_second':dp/dm}
        if rate!=expected:raise ValueError('clock rate diagnostic inconsistent with observations')
    return True
