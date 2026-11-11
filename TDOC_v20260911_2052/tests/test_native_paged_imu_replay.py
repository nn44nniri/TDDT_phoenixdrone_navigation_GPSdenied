"""Original MAVLink history bridges slow decisions without repairing sample clocks."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from layer6.telemetry import TelemetryReceiver
from layer6.telemetry_source import native_source_contract
from layer6.telemetry_normalization import normalize
from test_r5_01_telemetry_receivers import wire_message
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.online_imu import (CHANNELS,prepare_native_imu_history,native_imu_history_samples,
    extend_native_imu_history,attach_native_imu_history,canonical_imu,measured_intervals)


def source_case(count=170,cutoff=120):
    contract=native_source_contract();receiver=TelemetryReceiver(1,1,source_contract=contract)
    records=[]
    for i in range(count):
        records.append(receiver.ingest(wire_message('SCALED_IMU',changes={
            'time_boot_ms':1000+i*20,'xacc':1000,'yacc':i%10,'zacc':0,'xgyro':i%7})))
        receiver.ingest(wire_message('SYS_STATUS'))  # Global cursor is not a primary sample index.
    after=records[0]['history_sequence'];through=records[cutoff-1]['history_sequence']
    pages=[];cursor=after
    while True:
        page=receiver.history.page(after_seq=cursor,through_seq=through,limit=16,
            session_id=receiver.history.session_id,channels=['SCALED_IMU'])
        pages.append(page)
        if not page['has_more']:break
        cursor=page['next_after_seq']
    def sample(record):
        n=normalize(record,contract)
        return {**{k:n['fields'][k] for k in CHANNELS.values()},'source':'MAVLink/SCALED_IMU',
            'frame':'BODY_FRD','plant_time_s':n['source_time_s'],'time_boot_ms':record['fields']['time_boot_ms'],
            'history_sequence':record['history_sequence'],'session_id':record['session_id'],
            'monotonic_domain_id':record['monotonic_domain_id'],'wire_sha256':record['wire_sha256'],
            'timestamp_monotonic':record['timestamp_monotonic'],'source_time_delta_ms':record['source_time_delta'],
            'source_time_relation':record['source_time_relation']}
    tail=[sample(r) for r in records[-64:]];current=tail[-1]
    ingress={'ok':True,'source':'Layer6/GUI/MAVLink','released':True,'imu_samples':tail,
        'gui_state':{'session_id':receiver.history.session_id,'monotonic_domain_id':receiver.contracts.domain,
            'target_system':1,'target_component':1,'imu':current},
        'state':{**{k:current[k] for k in CHANNELS.values()},'imu_time_s':current['plant_time_s'],
            'imu_frame':'BODY_FRD','imu_source':'MAVLink/SCALED_IMU','runtime_ready':1.}}
    kwargs={'expected_session_id':receiver.history.session_id,'expected_monotonic_domain_id':receiver.contracts.domain,
            'target_system':1,'target_component':1,'after_seq':after}
    return pages,kwargs,ingress,records


def retained_window(pages,after,through):
    """Local ring pages retaining the exact original raw records/contracts."""
    from tddt_tdoc.utils import sha256_obj
    pairs=[(r,c,sha256_obj(page)) for page in pages
        for r,c in zip(page['samples'],page['sample_contracts'])
        if after<r['history_sequence']<=through]
    result=[];cursor=after
    for start in range(0,len(pairs),16):
        selected=pairs[start:start+16];more=start+16<len(pairs)
        next_cursor=selected[-1][0]['history_sequence'] if more else through
        result.append({**deepcopy(pages[0]),'after_seq':cursor,'through_seq':through,
            'next_after_seq':next_cursor,'has_more':more,
            'samples':[deepcopy(r) for r,_,_ in selected],
            'sample_contracts':[deepcopy(c) for _,c,_ in selected],
            'retention_origin':'TDOC-process-local-primary-ring',
            'original_gui_page_sha256':list(dict.fromkeys(h for _,_,h in selected))})
        cursor=next_cursor
    return result


def test_incremental_catchup_after_initial_decode_outlives_fresh_64_sample_tail(monkeypatch):
    # Initial decoding began at 2.98 s; the next GUI acquisition is at 7.98 s.
    # Its 64-sample tail cannot bridge that actual five-second source interval.
    pages,args,ingress,records=source_case(350,350)
    cutoff=records[99]['history_sequence']
    initial=prepare_native_imu_history(retained_window(pages,args['after_seq'],cutoff),**args)
    with pytest.raises(ValueError,match='exceeds operation-5 grant'):
        attach_native_imu_history(ingress,initial,after_seq=args['after_seq'],last_time=1.,max_dt=.15)
    import layer6.telemetry_normalization as normalization
    original=normalization.normalize;decoded=[]
    def track(record,contract):
        decoded.append(record['history_sequence'])
        return original(record,contract)
    monkeypatch.setattr(normalization,'normalize',track)
    caught_up=extend_native_imu_history(initial,retained_window(pages,cutoff,records[-1]['history_sequence']))
    assert decoded==[r['history_sequence'] for r in records[100:]]
    assert caught_up.samples[0] is initial.samples[0]  # Prefix was not decoded/copied again.
    combined=attach_native_imu_history(ingress,caught_up,after_seq=args['after_seq'],last_time=1.,max_dt=.15)
    steps=measured_intervals(combined,1.,canonical_imu(ingress),.15)
    assert len(steps)==349 and all(dt==pytest.approx(.02) for dt,_ in steps)
    assert [s['time_boot_ms'] for s in combined['imu_samples']]==[r['fields']['time_boot_ms'] for r in records[1:]]
    assert sum(dt for dt,_ in steps)==pytest.approx(6.98)
    assert extend_native_imu_history(caught_up,[]) is caught_up
    json.dumps(combined)


@pytest.mark.parametrize('fault',['session','missing_primary','boundary_delta','duplicate','mutated_prefix','provenance','sample_bound','page_bound'])
def test_incremental_catchup_rejects_unavailable_or_changed_actual_history(fault):
    from dataclasses import replace
    from tddt_tdoc.utils import sha256_obj
    pages,args,_,records=source_case(170,170)
    cutoff=records[99]['history_sequence']
    initial=prepare_native_imu_history(retained_window(pages,args['after_seq'],cutoff),**args)
    suffix=retained_window(pages,cutoff,records[-1]['history_sequence'])
    if fault=='session':suffix[0]['session_id']='0'*32
    elif fault=='missing_primary':
        suffix[0]['samples'].pop(0);suffix[0]['sample_contracts'].pop(0)
    elif fault=='boundary_delta':suffix[0]['samples'][0]['source_time_delta']+=1
    elif fault=='duplicate':suffix[0]['samples'][0]=deepcopy(pages[0]['samples'][0])
    elif fault=='mutated_prefix':initial.samples[0]['gyro_x_rad_s']+=1
    elif fault=='provenance':
        for page in suffix:page.pop('retention_origin')
    elif fault=='sample_bound':
        samples=initial.samples*83
        initial=replace(initial,samples=samples,samples_sha256=sha256_obj(samples))
    else:initial=replace(initial,page_sha256=('a'*64,)*1024)
    with pytest.raises(ValueError):extend_native_imu_history(initial,suffix)


@pytest.mark.parametrize('count,cutoff',[(170,120),(1600,1550)])
def test_actual_retained_samples_bridge_more_than_the_gui_64_sample_window(count,cutoff):
    pages,args,ingress,records=source_case(count,cutoff)
    packet=canonical_imu(ingress)
    with pytest.raises(ValueError,match='exceeds operation-5 grant'):
        measured_intervals(ingress,1.,packet,.15)
    prepared=prepare_native_imu_history(pages,**args)
    combined=attach_native_imu_history(ingress,prepared,after_seq=args['after_seq'],last_time=1.,max_dt=.15)
    steps=measured_intervals(combined,1.,packet,.15)
    assert len(steps)==count-1 and max(dt for dt,_ in steps)<=.15
    assert sum(dt for dt,_ in steps)==pytest.approx((count-1)*.02)
    assert all(dt==pytest.approx(.02) for dt,_ in steps)
    assert [s['time_boot_ms'] for s in combined['imu_samples']]==[r['fields']['time_boot_ms'] for r in records[1:]]
    assert len(combined['imu_samples'])>64
    assert combined['native_imu_history_provenance']['interpolation'] is False
    assert combined['native_imu_history_provenance']['clock_rebased'] is False
    assert 'native_imu_history' not in combined and 'imu_history_pages' not in combined
    json.dumps(combined)  # Opaque process-local receipt never enters JSON context.


@pytest.mark.parametrize('fault',['session','retention','wire','payload','frame','missing_primary','incomplete_pages','cursor'])
def test_unverifiable_historical_source_never_becomes_inertial_steps(fault):
    pages,args,_,_=source_case();pages=deepcopy(pages)
    if fault=='session':pages[0]['session_id']='0'*32
    elif fault=='retention':pages[0]['retention_gap']=True
    elif fault=='wire':pages[0]['samples'][0]['wire_hex']='00'
    elif fault=='payload':pages[0]['samples'][0]['fields']['xacc']+=1
    elif fault=='frame':pages[0]['sample_contracts'][0]['normalized']['frame']='BODY_FLU'
    elif fault=='missing_primary':
        pages[0]['samples'].pop(1);pages[0]['sample_contracts'].pop(1)
    elif fault=='incomplete_pages':pages=pages[:-1]
    else:pages[1]['after_seq']+=1
    with pytest.raises(ValueError):prepare_native_imu_history(pages,**args)


@pytest.mark.parametrize('fault',['session','overlap','tail_gap','fake_json','receipt_mutation'])
def test_final_fresh_tail_cannot_relabel_or_repair_history(fault):
    pages,args,ingress,_=source_case(cutoff=80 if fault=='tail_gap' else 120)
    history=prepare_native_imu_history(pages,**args)
    if fault=='session':ingress['gui_state']['session_id']='0'*32
    elif fault=='overlap':ingress['imu_samples'][0]['wire_sha256']='0'*64
    elif fault=='fake_json':history={'samples':list(history.samples),'validated':True}
    elif fault=='receipt_mutation':history.samples[0]['accel_x_m_s2']+=1
    with pytest.raises(ValueError):
        attach_native_imu_history(ingress,history,after_seq=args['after_seq'],last_time=1.,max_dt=.15)


def test_local_ring_wrapper_keeps_original_page_lineage_without_impersonating_gui():
    pages,args,ingress,_=source_case()
    for page in pages:
        page['retention_origin']='TDOC-process-local-primary-ring'
        page['original_gui_page_sha256']=['a'*64]
    history=prepare_native_imu_history(pages,**args)
    combined=attach_native_imu_history(ingress,history,after_seq=args['after_seq'],last_time=1.,max_dt=.15)
    assert combined['native_imu_history_provenance']['retention_origin']=='TDOC-process-local-primary-ring'
    assert combined['native_imu_history_provenance']['original_gui_page_sha256']==['a'*64]
    exported=native_imu_history_samples(history);exported[0]['accel_x_m_s2']+=1
    assert exported[0]['accel_x_m_s2']!=history.samples[0]['accel_x_m_s2']


def test_layer2_uses_registered_history_callback_and_does_not_advance_cursor_on_download():
    pages,args,ingress,_=source_case()
    history=prepare_native_imu_history(pages,**args)
    adapter=Layer2Adapter.__new__(Layer2Adapter)
    adapter.config=SimpleNamespace(get=lambda key,default=None:default)
    adapter._online_mesa_initialized_episode='online';adapter._online_mesa_last_time=1.
    adapter._online_mesa_history_cursor={'sequence':args['after_seq'],'session_id':args['expected_session_id'],'plant_time_s':1.}
    original=deepcopy(adapter._online_mesa_history_cursor);calls=[]
    def provider(**kwargs):
        calls.append(kwargs)
        return {**ingress,'native_imu_history':history,'imu_history_pages':pages}
    adapter.native_history_observation_source=provider
    result=adapter._fresh_native_observation(lambda:pytest.fail('wrong provider'),{}, {})
    assert calls==[{'history_after_seq':args['after_seq'],'history_session_id':args['expected_session_id']}]
    assert adapter._online_mesa_history_cursor==original
    assert 'native_imu_history' not in result and len(result['imu_samples'])==169


def test_unregistered_json_history_flags_cannot_select_the_paged_provider():
    _,_,ingress,_=source_case()
    adapter=Layer2Adapter.__new__(Layer2Adapter)
    adapter._online_mesa_initialized_episode='online'
    with pytest.raises(ValueError,match='without registered measured cursor'):
        adapter._fresh_native_observation(lambda:{**ingress,'native_imu_history':{'validated':True}},
            {'native_history_provider':True},{})


def test_bootstrap_history_is_plain_clock_evidence_without_preinitialization_mesa_integration():
    pages,args,ingress,_=source_case()
    history=prepare_native_imu_history(pages,**args)
    adapter=Layer2Adapter.__new__(Layer2Adapter)
    adapter.config=SimpleNamespace(get=lambda key,default=None:default)
    anchor={'history_sequence':args['after_seq'],'plant_time_s':1.,
        'session_id':args['expected_session_id'],'monotonic_domain_id':args['expected_monotonic_domain_id']}
    adapter.native_history_observation_source=lambda:{**ingress,'native_imu_history':history,
        'imu_history_pages':pages,'native_imu_history_anchor':anchor}
    result=adapter._fresh_native_observation(lambda:pytest.fail('unregistered bootstrap source'),{}, {})
    assert len(result['imu_samples'])==169 and 'native_imu_history' not in result
    assert result['native_imu_history_provenance']['bootstrap_clock_history_only'] is True
    assert result['native_imu_history_provenance']['historical_MESA_integration_before_initialization'] is False
    assert getattr(adapter,'_online_mesa_last_time',None) is None
    assert getattr(adapter,'_online_mesa_history_cursor',None) is None
    json.dumps(result)


@pytest.mark.parametrize('fault',['unavailable','missing_anchor','wrong_anchor_session','fake_receipt','stale_final_observation'])
def test_bootstrap_missing_or_stale_actual_evidence_fails_closed(fault):
    pages,args,ingress,_=source_case()
    history=prepare_native_imu_history(pages,**args)
    anchor={'history_sequence':args['after_seq'],'plant_time_s':1.,
        'session_id':args['expected_session_id'],'monotonic_domain_id':args['expected_monotonic_domain_id']}
    value={**ingress,'native_imu_history':history,'native_imu_history_anchor':anchor}
    if fault=='unavailable':value={'ok':False,'error':'retention missing'}
    elif fault=='missing_anchor':value.pop('native_imu_history_anchor')
    elif fault=='wrong_anchor_session':anchor['session_id']='0'*32
    elif fault=='fake_receipt':value['native_imu_history']={'validated':True}
    else:
        from test_native_complete_readiness import complete_status
        gui=complete_status(ingress['state']['imu_time_s'])
        gui.update(session_id=args['expected_session_id'],imu=ingress['gui_state']['imu'])
        gui['telemetry_metadata']['SCALED_IMU']['wire_sha256']=gui['imu']['wire_sha256']
        for meta in gui['telemetry_metadata'].values():
            meta['received_monotonic']-=.3;meta['freshness']['evaluated_monotonic']-=.3
        value['gui_state']=gui
    adapter=Layer2Adapter.__new__(Layer2Adapter)
    adapter.config=SimpleNamespace(get=lambda key,default=None:default)
    adapter.native_history_observation_source=lambda:value
    with pytest.raises(ValueError):
        result=adapter._fresh_native_observation(lambda:None,{}, {})
        canonical_imu(result)  # Actual invocation checks freshness before MESA operation 5.
    assert getattr(adapter,'_online_mesa_last_time',None) is None
