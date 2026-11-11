"""Explicit time domains, accepted raw civil clocks and real MicroMet queries."""
from copy import deepcopy
import csv
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import pytest

from test_r4_02_binding_consumers import bound,explicit_readonly_reference,TDOC
from test_r4_01_unified_binding import binding as anchored_binding,seal
from layer6.timebase import NativeClock,validate_clock_mapping,monotonic_domain,_hash
from layer6.binding import forcing_time
from phoenix_env_adapter.bound import BoundEnvironment
from tddt_tdoc import native_binding as nb
from tddt_tdoc.native_time import project_native_time,validate_native_time,stamp_climate_time
from test_online_climate_handoff_alignment_v2052 import mod as workflow


def packet(ms,channel='ATTITUDE',system=1,component=1):
    return SimpleNamespace(time_boot_ms=ms,time_unix_usec=9999999999999999,
        get_type=lambda:channel,get_srcSystem=lambda:system,get_srcComponent=lambda:component)


def event_state(bound):
    path,b=bound;clock=NativeClock(b,1,1)
    observed=clock.observe(packet(10000))
    state=BoundEnvironment(path).sample(10.,source='MAVLink/ATTITUDE.time_boot_ms')
    state.update(sampled_monotonic=state['updated_monotonic'],runtime_clock_mapping_verified=True,
        clock_mapping=clock.mapping(observed))
    return b,clock,state


def test_real_handoff_and_all_raw_forcing_times_are_preserved(bound):
    _,b=bound
    hm=json.loads(gzip.decompress((TDOC/'runtime/knowledge/handoff/offline_latest.json.gz').read_bytes()))
    accepted=hm['reuse_contract']['climate_time_alignment'];raw=Path(b['forcing_file']).read_bytes()
    rows=list(csv.DictReader(raw.decode().splitlines()))
    reference=[{'point':r['point'],'time':r['time']} for r in rows]
    assert b['effective_climate_epoch']==accepted['effective_climate_epoch']=='2026-10-28T16:00:00'
    assert b['forcing']['time_reference']['effective_epoch_exact']==accepted['effective_climate_epoch']
    assert b['forcing']['time_reference']['source_times_sha256']==nb.digest(reference)
    assert len(reference)==120 and all(datetime.fromisoformat(r['time']).tzinfo is None for r in reference)
    assert b['forcing']['metadata']['timezone_request_selectors']==['auto']
    assert b['time_contract']['timezone_status']=='unspecified'
    assert not b['time_contract']['utc_mapping_verified'] and not b['time_contract']['UTC_conversion_performed']
    assert hashlib.sha256(raw).hexdigest()==accepted['forcing_file_sha256']


@pytest.mark.parametrize('zone',['UTC','Pacific/Honolulu','Asia/Tehran'])
def test_host_timezone_never_changes_query_or_monotonic_domain(bound,monkeypatch,zone):
    before=monotonic_domain();previous=os.environ.get('TZ')
    try:
        monkeypatch.setenv('TZ',zone);time.tzset()
        result=BoundEnvironment(bound[0]).sample(900.)
        assert result['simulation_time_dataset']=='2026-10-28T16:15:00'
        assert result['environment_query']['time']=='2026-10-28T16:15:00'
        assert result['timezone_status']=='unspecified' and not result['UTC_mapping_verified']
        assert monotonic_domain()==before
    finally:
        if previous is None:os.environ.pop('TZ',None)
        else:os.environ['TZ']=previous
        time.tzset()


def test_explicit_offset_is_preserved_without_implicit_utc_normalization(tmp_path):
    p=tmp_path/'forcing.csv';p.write_text('point,time\np,2026-10-28T12:00:00-04:00\np,2026-10-28T13:00:00-04:00\n')
    (tmp_path/'forcing.csv.meta.json').write_text(json.dumps({'points':[{'url':'https://example.invalid/?timezone=auto'}]}))
    contract={'forcing_file':str(p),'forcing_file_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
        'effective_climate_epoch':'2026-10-28T12:00:00-04:00',
        'forcing_start':'2026-10-28T12:00:00-04:00','forcing_end':'2026-10-28T13:00:00-04:00'}
    forcing,epoch=nb._forcing(contract,nb._Inputs())
    assert epoch.isoformat()=='2026-10-28T12:00:00-04:00'
    assert forcing['common_start']=='2026-10-28T12:00:00-04:00'
    assert forcing['time_reference']['effective_epoch_exact']==contract['effective_climate_epoch']
    assert forcing['timezone_status']=='offset_explicit' and forcing['utc_mapping_verified']


def test_nonzero_declared_anchor_maps_elapsed_and_never_first_message_rebases(anchored_binding):
    b=anchored_binding;clock=NativeClock(b,1,1)
    result=clock.observe(packet(18250),received=100.)
    assert result['event']['plant_time_s']==18.25 and result['event']['mission_elapsed_s']==1.
    assert result['event']['forcing_timestamp']=='2026-10-28T16:00:01'
    mapped=clock.mapping(result)
    assert mapped['plant_epoch_s']==17.25 and not mapped['declared_anchor_observed'] and not mapped['first_packet_rebase']


def test_runtime_reattachment_has_new_session_and_same_declared_origin(bound):
    b=bound[1];first=NativeClock(b,1,1);second=NativeClock(b,1,1)
    a=first.mapping(first.observe(packet(7000),received=100.))
    z=second.mapping(second.observe(packet(9000),received=200.))
    assert a['session_id']!=z['session_id'] and a['binding_hash']==z['binding_hash']
    assert a['observation']['mission_elapsed_s']==7. and z['observation']['mission_elapsed_s']==9.
    assert z['effective_climate_epoch']==b['effective_climate_epoch']


def test_monotonic_is_observation_clock_not_assumed_simulation_rate(bound):
    clock=NativeClock(bound[1],1,1);clock.observe(packet(7000),received=100.)
    observed=clock.observe(packet(9000),received=100.5);m=clock.mapping(observed)
    assert m['measured_rate_diagnostic']['plant_seconds_per_monotonic_second']==4.
    assert not m['clock_rate_assumed'] and m['observation']['forcing_timestamp']=='2026-10-28T16:00:09'


def test_late_channel_preserves_source_time_and_duplicate_cannot_refresh_clock(bound):
    clock=NativeClock(bound[1],1,1);clock.observe(packet(10000),received=100.)
    late=clock.observe(packet(9900,'GLOBAL_POSITION_INT'),received=100.1)
    assert not late['newest'] and late['event']['plant_time_s']==9.9
    assert late['event']['forcing_timestamp']=='2026-10-28T16:00:09.900000'
    with pytest.raises(ValueError,match='late channel'):clock.mapping(late)
    assert clock.observe(packet(9900,'GLOBAL_POSITION_INT'),received=100.2) is None
    assert clock.latest['plant_time_s']==10.


@pytest.mark.parametrize('value',[None,True,1.5,-1,2**32,float('nan')])
def test_invalid_or_wrapped_source_clock_rejected(bound,value):
    with pytest.raises(ValueError,match='uint32'):NativeClock(bound[1],1,1).observe(packet(value))


def test_restart_wrong_source_and_monotonic_reversal_do_not_rebase(bound):
    c=NativeClock(bound[1],1,1);c.observe(packet(10000),received=100.)
    assert c.observe(packet(11000,system=2),received=101.) is None
    with pytest.raises(ValueError,match='restarted'):c.observe(packet(500),received=101.)
    with pytest.raises(ValueError,match='monotonic'):c.observe(packet(11000),received=99.)


def test_real_query_and_observed_clock_capsule_validate_without_absolute_sync(bound):
    b,_,state=event_state(bound)
    assert validate_clock_mapping(b,state)
    assert state['runtime_clock_mapping_verified'] and not state['runtime_clock_synchronization_verified']
    assert not state['UTC_mapping_verified'] and not state['clock_mapping']['synchronized_absolute_clocks']


@pytest.mark.parametrize('change',[
    lambda m:m.update(monotonic_domain_id='another-host-boot'),
    lambda m:m.update(first_packet_rebase=True),
    lambda m:m.update(UTC_conversion_performed=True),
    lambda m:m.update(clock_rate_assumed=True),
    lambda m:m.update(effective_climate_epoch='2026-10-28T16:00:00Z'),
    lambda m:m['observation'].update(forcing_timestamp='2026-10-28T16:00:10Z'),
    lambda m:m['observation'].update(received_monotonic_s=0.),
])
def test_resealed_wrong_domain_implicit_utc_rebase_and_stale_mapping_reject(bound,change):
    b,_,state=event_state(bound);m=state['clock_mapping'];change(m)
    m['mapping_hash']=_hash({k:v for k,v in m.items() if k!='mapping_hash'})
    with pytest.raises(ValueError):validate_clock_mapping(b,state)


def test_preparation_recipe_cannot_be_rehashed_into_hidden_rebase(bound):
    bad=deepcopy(bound[1]);bad['time_contract']['forcing_time_equation']='forcing_time=host_utc_now'
    with pytest.raises(ValueError,match='time mapping'):nb.validate_binding(seal(bad))


def test_r402_binding_is_explicitly_retired_instead_of_silently_upgraded(bound):
    bad=deepcopy(bound[1]);bad['revision']='native-unified-binding-r4.02-v1'
    with pytest.raises(ValueError,match='schema/revision'):nb.validate_binding(bad)


def native_request(bound,plant=10.):
    path,b=bound
    contract={'ok':True,'effective_climate_epoch':b['effective_climate_epoch']}
    req={'climate':{},'context':{}}
    start=datetime.fromisoformat(b['forcing']['common_start']);end=datetime.fromisoformat(b['forcing']['common_end'])
    workflow._apply_online_climate_time(req,(start,end),999.,contract,native_binding_path=path,plant_time_s=plant)
    return req


def test_native_workflow_ignores_scheduler_elapsed_and_preserves_clock_domain(bound):
    req=native_request(bound)
    assert req['climate']['time']['value']=='2026-10-28T16:00:10'
    assert req['climate']['time']['time_reference']=='ACCEPTED_DATASET_NAIVE'
    p=req['context']['native_climate_time_projection']
    assert validate_native_time(p) and p['mission_elapsed_s']==10.
    assert p['effective_climate_epoch']==bound[1]['effective_climate_epoch']


def test_native_workflow_never_falls_back_to_scheduler_or_zero(bound):
    with pytest.raises(ValueError,match='observed native plant'):native_request(bound,None)


def test_online_timezone_mismatch_does_not_strip_or_inherit_offset():
    aware=datetime.fromisoformat('2026-10-28T16:00:00+00:00');naive=datetime.fromisoformat('2026-10-28T16:00:00')
    with pytest.raises(RuntimeError,match='clock domain'):workflow._online_datetime_to_forcing_clock(aware,naive)
    with pytest.raises(RuntimeError,match='clock domain'):workflow._online_datetime_to_forcing_clock(naive,aware)


def layer3_boundary():
    from layer3.config import Layer3Config
    from layer3.tdoc_adapter import TDOCAdapter
    from layer3.contracts import SignalContractRegistry
    root=Path(os.environ['TDDT_LAYER3_ROOT'])
    config=Layer3Config.from_yaml(root/'configs/configuration_boulder.yaml')
    return config,SignalContractRegistry(config,TDOCAdapter(config))


def test_real_layer3_boundary_and_fmu_use_bound_dataset_time_without_utc(bound):
    from layer3.runtime import Layer3FMURuntime,RuntimeRequest
    req=native_request(bound);b=bound[1];h=b['mission_origin']
    req['climate'].update(input=b['forcing_file'],dem=b['origin']['terrain_file']['path'])
    for name,value,unit,frame in [('latitude',h['latitude_deg'],'deg','WGS84'),('longitude',h['longitude_deg'],'deg','WGS84'),
        ('elevation_sea_level',h['altitude_msl_m'],'m','MSL')]:
        req['climate'][name]={'value':value,'unit':unit,'frame':frame,'semantic':name}
    config,contracts=layer3_boundary();runtime=Layer3FMURuntime.__new__(Layer3FMURuntime)
    runtime.config=config;runtime.contracts=contracts
    request=RuntimeRequest(reference_time_s=10.,regime_id=None,climate=req['climate'],context=req['context'])
    args=runtime._climate_args(request)
    assert args.time=='2026-10-28T16:00:10' and args.input==b['forcing_file']
    result=BoundEnvironment(bound[0]).estimate(args)
    assert result['request']['time']==args.time and result['fmu_outputs']['formula_valid']==1.


def test_layer3_alignment_preserves_raw_dataset_timestamp_and_uses_explicit_plant_projection(bound):
    from layer3.alignment import TimeSynchronizer
    config,contracts=layer3_boundary();req=native_request(bound)
    packet=stamp_climate_time({'twin_id':'MicroMetTwin','timestamp':'2026-10-28T16:00:10'},req['context'])
    aligned=TimeSynchronizer(config,contracts.tdoc).align(packet,10.,context=req['context'])
    assert aligned.source_time_s==10. and aligned.age_s==0. and not aligned.warning
    assert packet['timestamp']=='2026-10-28T16:00:10' and packet['timestamp_reference']=='ACCEPTED_DATASET_NAIVE'
    wrong=deepcopy(packet);wrong['timestamp']+='Z'
    with pytest.raises(ValueError,match='time/binding mismatch'):TimeSynchronizer(config,contracts.tdoc).align(wrong,10.,context=req['context'])


def test_layer3_rejects_naive_time_mislabeled_utc_or_unbound_dataset_clock(bound):
    _,contracts=layer3_boundary();req=native_request(bound)
    bad=deepcopy(req['climate']);bad['time']['time_reference']='UTC'
    with pytest.raises(ValueError,match='domain/binding mismatch'):
        contracts.normalize_climate_request(bad,context=req['context'])
    with pytest.raises(ValueError,match='implicit UTC forbidden'):
        contracts.normalize_climate_request(req['climate'],context={})


def test_mesa_coupled_query_cannot_replace_bound_time_with_configured_epoch(bound):
    from layer3.runtime import Layer3FMURuntime,RuntimeRequest
    req=native_request(bound);home=bound[1]['mission_origin'];config,contracts=layer3_boundary()
    origin={}
    for name,value,unit,frame in [('latitude',home['latitude_deg'],'deg','WGS84'),('longitude',home['longitude_deg'],'deg','WGS84'),
        ('altitude_msl_m',home['altitude_msl_m'],'m','MSL')]:
        origin[name]={'value':value,'unit':unit,'frame':frame,'semantic':name}
    req['context']['geodetic_origin']=origin
    request=RuntimeRequest(reference_time_s=10.,regime_id=None,climate=req['climate'],context=req['context'])
    runtime=Layer3FMURuntime.__new__(Layer3FMURuntime);runtime.config=config;runtime.contracts=contracts;runtime.tdoc=contracts.tdoc
    query,provenance=runtime._mesa_query(request,{'state':{'position_n':0.,'position_e':0.,'position_d':0.},'timestamp':9999.},1.)
    assert query['time']=='2026-10-28T16:00:10' and provenance['climate_contract_time_binding']=='native_accepted_dataset_clock'
