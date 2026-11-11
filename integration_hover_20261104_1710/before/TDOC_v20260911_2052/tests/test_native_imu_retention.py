"""Continuous read-only raw retention across real observed decision-time gaps."""
from collections import deque
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from layer6.status import project_status, SCHEMA
from layer6.telemetry_source import native_source_contract
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter, _NativeIMUHistoryReader
from tddt_tdoc.online_imu import prepare_native_imu_history, native_imu_history_samples
from test_r5_01_telemetry_receivers import wire_message
from test_r5_05_command_correlation import driver


def current_status(link):
    stamp=link.telemetry_receiver.latest['SCALED_IMU']['fields']['time_boot_ms']
    link.master.queue=[wire_message('HEARTBEAT',changes={'autopilot':3,'type':1,'custom_mode':18}),
        wire_message('ATTITUDE',changes={'time_boot_ms':stamp}),
        wire_message('ATTITUDE_QUATERNION',changes={'time_boot_ms':stamp}),
        wire_message('GLOBAL_POSITION_INT',changes={'time_boot_ms':stamp}),
        wire_message('VFR_HUD'),wire_message('SERVO_OUTPUT_RAW',changes={'time_usec':stamp*1000})]
    link.drain();p=project_status(link,include_imu_samples=True)
    return {**p,'schema':SCHEMA,'schema_version':1,'ok':True,'mavlink_connected':p['connected'],
        'monotonic_domain_id':p['domain'],'telemetry_metadata':p['metadata'],'telemetry_age_s':p['ages'],
        'target_system':1,'target_component':1,'status_quality':{'native_session_id':'b'*32}}


class HistoryOnlyClient:
    def __init__(self,link):self.link=link;self.closed=False;self.calls=[];self.mutate=None
    def telemetry_history(self,**query):
        self.calls.append(dict(query));query.pop('timeout')
        page=self.link.telemetry_receiver.history.page(**query)
        if self.mutate:self.mutate(page)
        return page
    def close(self):self.closed=True
    def submit(self,*a,**k):pytest.fail('read-only collector submitted a command')
    def action(self,*a,**k):pytest.fail('read-only collector sent an action')


def setup_reader(*,capacity=8192):
    link=driver(native_source_contract());history=link.telemetry_receiver.history
    history.capacity=96;history.records=deque(history.records,maxlen=96)
    status=current_status(link);client=HistoryOnlyClient(link)
    reader=_NativeIMUHistoryReader(client,status,capacity=capacity);reader.poll_once()
    return link,client,reader


def test_one_transport_timeout_recovers_only_from_actual_original_cursor():
    link,client,reader=setup_reader()
    advance(link,5)
    original=client.telemetry_history;calls=[]
    def query(**kwargs):
        calls.append(kwargs['after_seq'])
        if len(calls)==1:return None
        result=original(**kwargs)
        reader.stopping.set()
        return result
    client.telemetry_history=query
    before=reader.cursor
    reader._run()
    assert reader.error is None and client.closed
    assert calls==[before,before] and reader.cursor>before
    assert reader.last_source==link.telemetry_receiver.latest['SCALED_IMU']['fields']['time_boot_ms']


def test_repeated_transport_timeouts_are_bounded_and_do_not_advance_cursor():
    _,client,reader=setup_reader();before=reader.cursor
    client.telemetry_history=lambda **_:None
    reader._run()
    assert '3 consecutive timeouts' in reader.error and client.closed
    assert reader.cursor==before


def advance(link,count):
    previous=link.telemetry_receiver.latest['SCALED_IMU']['fields']['time_boot_ms']
    link.master.queue=[wire_message('SCALED_IMU',changes={'time_boot_ms':previous+20*(index+1)}) for index in range(count)]
    link.drain()


def prepare(reader,pages,after):
    return prepare_native_imu_history(pages,expected_session_id=reader.session,
        expected_monotonic_domain_id=reader.domain,target_system=1,target_component=1,after_seq=after)


def test_continuous_reader_preserves_32_seconds_after_gui_ring_has_evicted_start():
    link,client,reader=setup_reader()
    first=reader.anchor
    for _ in range(40):
        advance(link,40);reader.poll_once()
    last=link.telemetry_receiver.latest['SCALED_IMU']['history_sequence']
    assert link.telemetry_receiver.history.records[0]['history_sequence']>first
    pages=reader.window(first,last)
    samples=native_imu_history_samples(prepare(reader,pages,first))
    assert len(samples)==1600 and samples[-1]['plant_time_s']-reader.anchor_plant_time_s==32.
    assert samples[0]['source_time_delta_ms']==20
    assert all(page['retention_origin']=='TDOC-process-local-primary-ring' for page in pages)
    assert all(page['original_gui_page_sha256'] for page in pages)
    assert all(sample['wire_hex'] and sample['wire_sha256'] for sample in samples)
    assert len(project_status(link,include_imu_samples=True)['imu_samples'])<=64
    assert all(call['channels']==['SCALED_IMU'] for call in client.calls)


@pytest.mark.parametrize('fault',['session','domain','gap','wire','predecessor','source_reverse','cursor'])
def test_corrupt_or_missing_primary_history_never_becomes_retained_evidence(fault):
    link,client,reader=setup_reader();advance(link,2)
    def mutate(page):
        if fault=='session':page['session_id']='c'*32
        elif fault=='gap':page['retention_gap']=True;page['lost_unfiltered_samples']=1
        elif fault=='cursor':page['next_after_seq']=page['after_seq'];page['has_more']=True
        elif fault=='domain':page['samples'][0]['monotonic_domain_id']='foreign'
        elif fault=='wire':page['samples'][0]['wire_sha256']='0'*64
        elif fault=='predecessor':page['samples'][0]['previous_channel_sequence']=99999
        elif fault=='source_reverse':page['samples'][0]['source_time']['value']=0
    client.mutate=mutate
    with pytest.raises(ValueError):reader.poll_once()


def test_local_retention_overflow_fails_instead_of_repairing_the_interval():
    link,client,reader=setup_reader(capacity=4);advance(link,10);reader.poll_once()
    with pytest.raises(ValueError,match='evicted'):
        reader.window(reader.anchor,reader.last_primary)


def test_reader_uses_separate_readonly_client_and_stops_its_thread():
    link,client,reader=setup_reader();reader.start();reader.stop()
    assert client.closed and not reader.thread.is_alive()
    assert len(link.master.port.wires)==0


def test_history_decode_finishes_before_final_current_gui_acquisition(monkeypatch):
    import tddt_tdoc.online_imu as module
    link,client,reader=setup_reader()
    for _ in range(2):advance(link,40);reader.poll_once()
    calls=[];original=module.prepare_native_imu_history
    def prepared(*args,**kwargs):calls.append('decode_history');return original(*args,**kwargs)
    monkeypatch.setattr(module,'prepare_native_imu_history',prepared)
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);adapter._native_history_reader=reader
    def status(**kwargs):
        calls.append('fresh_final_status' if kwargs.get('include_imu_samples') else 'cutoff_status')
        if kwargs.get('include_imu_samples'):advance(link,1);reader.poll_once()
        return current_status(link)
    adapter._status=status;adapter._native_health=lambda status:None
    adapter._ingress_from_status=lambda status:{'ok':True,'gui_state':status,'imu_samples':status['imu_samples']}
    ingress=adapter.online_runtime_ingress(history_after_seq=reader.anchor,history_session_id=reader.session)
    assert ingress['ok'] and calls==['cutoff_status','decode_history','fresh_final_status']
    retained=native_imu_history_samples(ingress['native_imu_history'])
    assert len(retained)==80
    assert retained[-1]['plant_time_s']<ingress['gui_state']['plant_time_s']


def test_bootstrap_callback_requests_interval_from_actual_startup_anchor():
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    adapter._native_history_reader=SimpleNamespace(anchor=41,anchor_plant_time_s=1.2,session='a'*32,domain='domain')
    calls=[];adapter.online_runtime_ingress=lambda **kwargs:calls.append(kwargs) or {'ok':True}
    result=adapter.online_history_ingress()
    assert calls==[{'history_after_seq':41,'history_session_id':'a'*32}]
    assert result['history_anchor_plant_time_s']==1.2 and result['history_anchor_sequence']==41


def test_slow_initial_decode_catches_up_suffix_before_returning_fresh_tail(monkeypatch):
    import tddt_tdoc.online_imu as module
    link,client,reader=setup_reader()
    for _ in range(2):advance(link,40);reader.poll_once()
    decoded=[];original=module.prepare_native_imu_history
    def prepare_only_interval(pages,**kwargs):
        decoded.append(sum(len(page['samples']) for page in pages))
        return original(pages,**kwargs)
    monkeypatch.setattr(module,'prepare_native_imu_history',prepare_only_interval)
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);adapter._native_history_reader=reader
    final_queries=[]
    def status(**kwargs):
        if kwargs.get('include_imu_samples'):
            final_queries.append(True)
            # Simulate >1.28 s of actual source progress while the initial
            # decode was busy: 150 genuine 20-ms observations, continuously
            # retained while GUI history/tail overwrite their early samples.
            if len(final_queries)==1:
                for _ in range(5):advance(link,30);reader.poll_once()
            else:advance(link,1);reader.poll_once()
        return current_status(link)
    health_checks=[]
    adapter._status=status;adapter._native_health=lambda value:health_checks.append(value['session_id'])
    adapter._ingress_from_status=lambda value:{'ok':True,'gui_state':value,'imu_samples':value['imu_samples']}
    result=adapter.online_runtime_ingress(history_after_seq=reader.anchor,history_session_id=reader.session)
    assert result['ok'] and len(final_queries)==2
    assert health_checks==[reader.session]
    assert decoded==[80,150]  # The original prefix is never decoded twice.
    samples=native_imu_history_samples(result['native_imu_history'])
    assert len(samples)==230
    assert samples[-1]['plant_time_s']<result['gui_state']['plant_time_s']
    assert adapter._history_tail_covers(result['native_imu_history'],result['gui_state'])


def test_catchup_is_bounded_when_each_decode_outlasts_gui_tail():
    link,client,reader=setup_reader();advance(link,20);reader.poll_once()
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);adapter._native_history_reader=reader
    attempts=[]
    def status(**kwargs):
        if kwargs.get('include_imu_samples'):
            attempts.append(True)
            for _ in range(4):advance(link,25);reader.poll_once()
        return current_status(link)
    adapter._status=status;adapter._native_health=lambda value:None
    adapter._ingress_from_status=lambda value:pytest.fail('noncontiguous history was accepted')
    result=adapter.online_runtime_ingress(history_after_seq=reader.anchor,history_session_id=reader.session)
    assert not result['ok'] and len(attempts)==8
    assert 'bounded final-observation' in result['error']


def test_status_error_reports_strict_environment_rejection():
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);adapter._session_open=True
    adapter._option=lambda name,default=None:0. if name=='telemetry_resample_timeout_s' else default
    adapter._environment_rejection='native clock mapping projection/freshness mismatch'
    status={'telemetry_age_s':{'SCALED_IMU':.001},'environment_contract':{'ok':True,'reason':'verified'}}
    adapter._acquire_environment_status=lambda **kwargs:(status,False)
    with pytest.raises(RuntimeError,match='validation_rejection=native clock mapping'):
        adapter._status()


def test_full_component_health_precedes_final_fresh_observation():
    link,client,reader=setup_reader();calls=[]
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    def status(**kwargs):
        final=kwargs.get('include_imu_samples',False)
        calls.append('final_status' if final else 'probe_status')
        if final:advance(link,1)
        return current_status(link)
    adapter._status=status
    adapter._native_health=lambda value:calls.append('full_process_and_panel_health')
    adapter._ingress_from_status=lambda value:calls.append('ingress_current_validation') or {'ok':True,'gui_state':value}
    result=adapter.online_runtime_ingress()
    assert result['ok']
    assert calls==['probe_status','full_process_and_panel_health','final_status','ingress_current_validation']
    diagnostic=result['native_acquisition_timing']
    assert diagnostic['source_receipt_age_s']>=0 and diagnostic['source_receipt_retimed'] is False


@pytest.mark.parametrize('changed',['session_id','monotonic_domain_id','native_session_id','target_system','target_component'])
def test_identity_change_after_health_is_rejected_before_ingress(changed):
    link,client,reader=setup_reader();adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    def status(**kwargs):
        value=current_status(link)
        if kwargs.get('include_imu_samples'):
            if changed=='native_session_id':value['status_quality'][changed]='c'*32
            else:value[changed]=2 if changed.startswith('target_') else 'c'*32
        return value
    adapter._status=status;adapter._native_health=lambda value:None
    adapter._ingress_from_status=lambda value:pytest.fail('changed identity reached navigation ingress')
    result=adapter.online_runtime_ingress()
    assert not result['ok'] and 'identity changed after component-health' in result['error']


def test_missing_component_stops_before_final_snapshot_or_ingress():
    link,client,reader=setup_reader();adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);calls=[]
    adapter._status=lambda **kwargs:calls.append(kwargs) or current_status(link)
    def failed_health(value):raise RuntimeError('native components unavailable: console_not_alive')
    adapter._native_health=failed_health
    adapter._ingress_from_status=lambda value:pytest.fail('degraded Native session reached navigation')
    result=adapter.online_runtime_ingress()
    assert not result['ok'] and 'console_not_alive' in result['error']
    assert calls==[{}]


def test_ack_clock_continuity_uses_retained_raw_samples_before_gui_tail():
    link,client,reader=setup_reader()
    for _ in range(5):advance(link,40);reader.poll_once()
    status=current_status(link)
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);adapter._native_history_reader=reader
    payload={'network_snapshot':{'reference_time_s':reader.anchor_plant_time_s}}
    samples=adapter._retained_ack_clock_samples(payload,status)
    assert len(samples)==201 and samples[0]['plant_time_s']==reader.anchor_plant_time_s
    assert samples[-1]['plant_time_s']==status['plant_time_s']
    assert samples[0]['timestamp_monotonic']<samples[-1]['timestamp_monotonic']
    status['status_quality']['native_session_id']='c'*32
    with pytest.raises(ValueError,match='session mismatch'):adapter._retained_ack_clock_samples(payload,status)


def test_clock_continuity_failure_does_not_erase_confirmed_execution_ack():
    from test_native_layer6_migration import adapter,payload
    instance=adapter(verified=True)
    instance.config.overrides['layers.layer6.online.execution_profile']='closed_loop_sitl'
    instance._native_health=lambda status:None
    def unavailable(*args):raise ValueError('retained interval evicted')
    instance._retained_ack_clock_samples=unavailable
    result=instance.invoke(payload())
    assert result['ok'] and result['status']=='EXECUTED' and result['actuation_executed']
    assert result['execution_evidence']['plant_clock_samples']==[]
    assert 'evicted' in result['execution_evidence']['plant_clock_sample_error']


def test_history_api_signature_probe_accepts_real_client_without_instantiation():
    from layer6.client import Layer6Client
    from tddt_tdoc.layer6_diagnostics import inspect_client_api
    result=inspect_client_api(Layer6Client,['telemetry_history'])
    assert result['api_ok'] and 'telemetry_history' in result['checked_api']


def test_history_api_signature_probe_rejects_incompatible_legacy_client():
    from layer6.client import Layer6Client
    from tddt_tdoc.layer6_diagnostics import inspect_client_api
    class BadHistoryClient(Layer6Client):
        def telemetry_history(self,after_seq=0):pytest.fail('inspection called history')
    result=inspect_client_api(BadHistoryClient,['telemetry_history'])
    assert not result['api_ok']
    assert any(item['method']=='telemetry_history' for item in result['signature_mismatches'])

