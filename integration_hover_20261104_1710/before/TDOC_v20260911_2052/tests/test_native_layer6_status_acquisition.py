"""Integrity work precedes fresh telemetry; local witnesses never weaken guards."""
from copy import deepcopy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from test_native_complete_readiness import complete_status
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter


def guarded_adapter(tmp_path,monkeypatch,*,delay=0.):
    import layer6.binding as binding
    import layer6.timebase as timebase
    import tddt_tdoc.native_binding as native
    forcing=tmp_path/'forcing';forcing.write_bytes(b'accepted forcing')
    model=tmp_path/'model';model.write_bytes(b'accepted model')
    paths=(forcing,model)
    hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    expected={'schema':'TDOC-native-environment-binding-v2','binding_id':'binding',
        'binding_hash':'binding-hash','context_sha256':'context','model_id':'model',
        'handoff_hash':'handoff','forcing_file_sha256':'forcing','effective_climate_epoch':'epoch',
        'plant_epoch_s':0.,'input_files':[{'path':str(p)} for p in paths]}
    publication=tmp_path/'binding.json';publication.write_text(json.dumps(expected))
    calls=[]
    def full_guard(path):
        calls.append('full_guard')
        if delay:time.sleep(delay)
        assert cfg.data=={'test':'immutable'}
        assert json.loads(path.read_text())==expected
        if any(hashlib.sha256(p.read_bytes()).hexdigest()!=hashes[p] for p in paths):
            raise ValueError('bound input/model changed')
        return SimpleNamespace(binding=deepcopy(expected))
    # Isolate scheduling from the separate semantic-binding test suite. The
    # expensive guard still reads/hashes real files, and the production adapter
    # performs its actual publication/stat/digest/freshness checks below.
    monkeypatch.setattr(binding,'context_guard',full_guard)
    monkeypatch.setattr(native,'read_binding',lambda p:json.loads(p.read_text()))
    def clock(b,env):
        if env.get('clock_mapping')!={'binding_id':b['binding_id']}:
            raise ValueError('clock binding mismatch')
    def readback(env):
        if env.get('readback_verified') is not True:raise ValueError('readback unverified')
    monkeypatch.setattr(timebase,'validate_clock_mapping',clock)
    monkeypatch.setattr(binding,'validate_wind_application',readback)
    monkeypatch.setattr(binding,'forcing_time',lambda b,p:('dataset-time',p))
    cfg=SimpleNamespace(data={'test':'immutable'},get=lambda k,d=None:d)
    layer5=SimpleNamespace(native_actuator_feedback=lambda s:{'ok':True,'feedback':{'source':'FCU'}})
    adapter=Layer6OnlineAdapter(cfg,layer5)
    adapter._environment_binding=expected;adapter._environment_binding_path=publication
    adapter._session_open=True
    adapter._option=lambda k,d=None:True if k=='require_bound_environment' else d
    def gui_status(**kwargs):
        calls.append('GUI_snapshot')
        status=complete_status(1.)
        status['environment_contract']={**binding.consumption_identity(expected),'ok':True,
            'updated_monotonic':time.monotonic(),'plant_time_s':1.,
            'simulation_time_dataset':'dataset-time','clock_mapping':{'binding_id':'binding'},
            'readback_verified':True}
        return status
    adapter._client=SimpleNamespace(gui_status=gui_status)
    return adapter,calls,paths,publication


def test_slow_full_guard_precedes_snapshot_and_is_not_repeated_during_ingress(tmp_path,monkeypatch):
    adapter,calls,_,_=guarded_adapter(tmp_path,monkeypatch,delay=.06)
    states=[];original=adapter._state
    adapter._state=lambda s:states.append(s) or original(s)
    status=adapter._status(include_imu_samples=True)
    assert calls==['full_guard','GUI_snapshot']
    assert adapter.validated_environment_status(status)
    assert adapter._ingress_from_status(status)['ok']
    assert calls==['full_guard','GUI_snapshot'] and len(states)==1
    adapter._status()
    assert calls==['full_guard','GUI_snapshot','full_guard','GUI_snapshot']


def test_direct_or_copied_status_cannot_install_process_local_witness(tmp_path,monkeypatch):
    adapter,calls,_,_=guarded_adapter(tmp_path,monkeypatch)
    status=adapter._client.gui_status()
    assert adapter._ingress_from_status(status)['ok']
    assert calls.count('full_guard')==1
    fresh=adapter._status()
    copied=deepcopy(fresh);copied['environment_validated']=True
    assert adapter.validated_environment_status(copied)
    assert calls.count('full_guard')==3  # direct + acquisition + strict copy check


@pytest.mark.parametrize('fault',['input','publication_deleted','publication_changed','configuration'])
def test_changed_integrity_inputs_invalidate_same_acquisition_witness(tmp_path,monkeypatch,fault):
    adapter,calls,paths,publication=guarded_adapter(tmp_path,monkeypatch)
    status=adapter._status()
    if fault=='input':paths[0].write_bytes(b'changed forcing')
    elif fault=='publication_deleted':publication.unlink()
    elif fault=='publication_changed':publication.write_text('{"different":"identity"}')
    else:adapter.config.data['test']='changed'
    assert adapter.validated_environment_status(status) is False
    assert calls.count('full_guard')==1
    with pytest.raises((ValueError,FileNotFoundError,AssertionError)):
        adapter._status()


@pytest.mark.parametrize('fault',['clock','readback','identity','telemetry_stale'])
def test_modified_snapshot_or_stale_observations_never_reuse_local_witness(tmp_path,monkeypatch,fault):
    adapter,calls,_,_=guarded_adapter(tmp_path,monkeypatch)
    status=adapter._status()
    if fault=='clock':status['environment_contract']['clock_mapping']={'binding_id':'other'}
    elif fault=='readback':status['environment_contract']['readback_verified']=False
    elif fault=='identity':status['environment_contract']['handoff_hash']='other'
    else:
        for meta in status['telemetry_metadata'].values():
            meta['received_monotonic']-=.3
            meta['freshness']['evaluated_monotonic']-=.3
    assert adapter.validated_environment_status(status) is False
    assert calls.count('full_guard')==2  # changed object falls back to strict check


def test_next_acquisition_does_not_memoize_unchanged_stat_or_model_bytes(tmp_path,monkeypatch):
    adapter,calls,paths,_=guarded_adapter(tmp_path,monkeypatch)
    adapter._status()
    paths[1].write_bytes(b'changed model')
    with pytest.raises(ValueError,match='input/model changed'):adapter._status()
    assert calls.count('full_guard')==2 and calls.count('GUI_snapshot')==1
