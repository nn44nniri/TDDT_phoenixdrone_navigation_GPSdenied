"""Acceptance/rejection contracts. MAVLink unit doubles are not flight evidence."""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid
import pytest

from test_r4_02_binding_consumers import bound,explicit_readonly_reference,TDOC,NATIVE
from test_r4_01_unified_binding import seal
from test_r4_03_native_forcing import Link
from tddt_tdoc import native_binding as nb
from tddt_tdoc.native_time import project_native_time
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from layer6.binding import load_context,context_guard,environment_status
from layer6.timebase import NativeClock,_hash
from phoenix_env_adapter.bound import BoundEnvironment,run_bound_runtime
from phoenix_env_adapter.native_forcing import apply_wind


def own_binding(bound,tmp_path):
    b=deepcopy(bound[1]);b['binding_id']=uuid.uuid4().hex;seal(b)
    path=tmp_path/'binding.json';path.write_text(json.dumps(b));return path,b


def rejected_binding(case,b):
    b=deepcopy(b);b['binding_id']=uuid.uuid4().hex
    if case=='missing':return None
    if case=='malformed':return '{not-json'
    if case=='bad_schema':b['schema']='legacy'
    elif case=='old_revision':b['revision']='native-unified-binding-r4.02-v1'
    elif case=='bad_policy':b['policy_sha256']='0'*64
    elif case=='bad_hash':b['binding_hash']='0'*64;return json.dumps(b)
    elif case=='wrong_forcing':b['forcing']['sha256']=b['forcing_file_sha256']='0'*64
    elif case=='wrong_prior':b['knowledge']['handoff_hash']=b['handoff_hash']='0'*64
    elif case=='changed_origin':
        for item in (b['mission_origin'],b['origin']['initialization']):item['latitude_deg']+=.001
    elif case in ('hidden_epoch_rebase','implicit_utc'):
        epoch=b['effective_climate_epoch']
        if case=='hidden_epoch_rebase':epoch=(datetime.fromisoformat(epoch)+timedelta(seconds=60)).isoformat()
        else:
            epoch+='Z'
            for item in (b['forcing'],b['time_contract']):
                item.update(clock_domain='UTC_OFFSET_AWARE',timezone_status='offset_explicit',utc_mapping_verified=True)
            for k in ('common_start','common_end'):b['forcing'][k]+='Z'
            for row in b['forcing']['point_coverage'].values():
                for k in ('start','end'):row[k]+='Z'
        b['effective_climate_epoch']=b['time_contract']['effective_climate_epoch']=b['forcing']['time_reference']['effective_epoch_exact']=epoch
        b['time_contract']['normalized_effective_epoch']=datetime.fromisoformat(epoch.replace('Z','+00:00')).isoformat()
    return json.dumps(seal(b))


BINDING_CASES=['missing','malformed','bad_schema','old_revision','bad_policy','bad_hash',
    'wrong_forcing','wrong_prior','changed_origin','hidden_epoch_rebase','implicit_utc']


@pytest.mark.parametrize('case',BINDING_CASES)
@pytest.mark.parametrize('consumer',['context','guard','provider','projection','launcher','runtime'])
def test_bad_binding_rejects_before_transport_or_execution(bound,tmp_path,monkeypatch,case,consumer):
    path=tmp_path/'invalid.json';raw=rejected_binding(case,bound[1])
    if raw is not None:path.write_text(raw)
    # No MAVLink factory, GUI, pilot or parameter operation may be reached.
    def forbidden(*a,**kw):pytest.fail('invalid binding reached transport')
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=forbidden)))
    with pytest.raises((ValueError,FileNotFoundError)):
        if consumer=='context':load_context(path)
        elif consumer=='guard':context_guard(path)
        elif consumer=='provider':BoundEnvironment(path)
        elif consumer=='projection':project_native_time(path,10.)
        elif consumer=='launcher':
            import run_flightstack
            monkeypatch.setattr(run_flightstack,'_resolve',forbidden)
            run_flightstack._commands(NATIVE/'FlightStack',path)
        else:run_bound_runtime(path,tmp_path/'state.json',mavlink='forbidden',max_updates=1)


def complete_unit_state(path,b):
    provider=BoundEnvironment(path)
    clock=NativeClock(b,1,1)
    msg=SimpleNamespace(time_boot_ms=10000,get_type=lambda:'ATTITUDE',get_srcSystem=lambda:1,get_srcComponent=lambda:1)
    observed=clock.observe(msg);state=provider.sample(10.,source='MAVLink/ATTITUDE.time_boot_ms')
    state.update(sampled_monotonic=state['updated_monotonic'],clock_mapping=clock.mapping(observed),runtime_clock_mapping_verified=True,
        environment_position_source='MAVLink/GLOBAL_POSITION_INT',environment_position_time_s=10.,
        environment_position_received_monotonic=observed['event']['received_monotonic_s'],application_pending=False)
    state['application']=apply_wind(Link(),state,timeout=.001)
    state.update(ok=True,readback_verified=True,updated_monotonic=time.monotonic(),applied_fields=state['application']['parameters'])
    return state


def adapter(path,b):
    from tddt_tdoc.config import TDOCConfig
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter);a.config=TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml')
    a._option=lambda k,d=None:True if k=='require_bound_environment' else d
    a._environment_binding=b;a._environment_binding_path=path
    return a


@pytest.mark.parametrize('consumer',['GUI','TDOC'])
def test_positive_fresh_state_with_unspecified_timezone_is_accepted(bound,tmp_path,consumer):
    path,b=own_binding(bound,tmp_path);state=complete_unit_state(path,b)
    if consumer=='GUI':
        dest=tmp_path/'state.json';dest.write_text(json.dumps(state));result=environment_status(path,dest)
        assert result['ok'] and result['timezone_status']=='unspecified' and not result['UTC_mapping_verified']
    else:assert adapter(path,b)._environment_ready({'environment_contract':state})
    assert not state['actuation_authority']


STATE_CASES=['missing','malformed','stale','stale_receipt_only','future','wrong_binding','wrong_hash',
    'foreign_boot','implicit_utc','query_implicit_utc','timezone_relabel','absolute_sync_claim','producer_failure']


def change_state(case,state):
    if case=='producer_failure':state['ok']=False
    elif case=='stale':
        for k in ('updated_monotonic','sampled_monotonic','environment_position_received_monotonic'):state[k]-=2.
        state['clock_mapping']['observation']['received_monotonic_s']-=2.
        for row in state['application']['parameters'].values():
            for k in ('read_requested_monotonic','read_received_monotonic'):row[k]-=2.
    elif case=='stale_receipt_only':state['clock_mapping']['observation']['received_monotonic_s']-=2.
    elif case=='future':state['updated_monotonic']+=2.
    elif case=='wrong_binding':state['binding_id']=uuid.uuid4().hex
    elif case=='wrong_hash':state['binding_hash']='0'*64
    elif case=='foreign_boot':state['clock_mapping']['monotonic_domain_id']='foreign-boot'
    elif case=='implicit_utc':state['simulation_time_dataset']+='Z'
    elif case=='query_implicit_utc':state['environment_query']['time']+='Z'
    elif case=='timezone_relabel':state.update(timezone_status='offset_explicit',UTC_mapping_verified=True)
    elif case=='absolute_sync_claim':state['runtime_clock_synchronization_verified']=True
    m=state['clock_mapping'];m['mapping_hash']=_hash({k:v for k,v in m.items() if k!='mapping_hash'})
    return state


@pytest.mark.parametrize('case',STATE_CASES)
@pytest.mark.parametrize('consumer',['GUI','TDOC'])
def test_unavailable_stale_incompatible_or_relabelled_state_cannot_be_ready(bound,tmp_path,case,consumer):
    path,b=own_binding(bound,tmp_path);state=change_state(case,complete_unit_state(path,b))
    dest=tmp_path/'state.json'
    if case=='malformed':dest.write_text('{bad-json')
    elif case!='missing':dest.write_text(json.dumps(state))
    if consumer=='GUI':assert not environment_status(path,dest)['ok']
    else:
        env={} if case in ('missing','malformed') else state
        assert not adapter(path,b)._environment_ready({'environment_contract':env})


def test_cached_approval_rehashes_inputs_and_current_model_every_time(bound,tmp_path,monkeypatch):
    from tddt_tdoc import model_compatibility as mc
    path,b=own_binding(bound,tmp_path);g=context_guard(path)
    original=Path.read_bytes;target=Path(b['forcing_file'])
    monkeypatch.setattr(Path,'read_bytes',lambda p:original(p)+b'changed' if p.resolve()==target.resolve() else original(p))
    with pytest.raises(ValueError,match='input hash mismatch'):g.check()
    monkeypatch.setattr(Path,'read_bytes',original)
    assert g.check()==b
    capture=mc.capture_model_dependencies
    def changed(c):
        result=deepcopy(capture(c));result['model_id']='stale-model';return result
    monkeypatch.setattr(mc,'capture_model_dependencies',changed)
    with pytest.raises(ValueError,match='no longer current'):g.check()


def test_cached_publication_deletion_and_same_id_mutation_reject(bound,tmp_path):
    path,b=own_binding(bound,tmp_path);g=context_guard(path);raw=path.read_bytes();path.unlink()
    with pytest.raises(FileNotFoundError):g.check()
    path.write_bytes(raw);changed=deepcopy(b);changed['prepared_at_utc']='2020-01-01T00:00:00+00:00';seal(changed)
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='immutable binding ID'):context_guard(path)
    with pytest.raises(ValueError,match='publication replaced'):g.check()


def test_missing_publication_rejects_even_if_GUI_payload_is_still_fresh(bound,tmp_path):
    path,b=own_binding(bound,tmp_path);state=complete_unit_state(path,b);path.unlink()
    assert not adapter(path,b)._environment_ready({'environment_contract':state})


def test_change_during_fmu_query_stops_before_any_parameter_write(bound,tmp_path,monkeypatch):
    from phoenix_env_adapter import bound as runtime,native_forcing
    path,b=own_binding(bound,tmp_path);original=runtime.BoundEnvironment.sample
    def modified_sample(self,*a,**kw):
        estimate=self.estimate
        def changed(args):
            result=estimate(args);path.unlink();return result
        self.estimate=changed
        return original(self,*a,**kw)
    monkeypatch.setattr(runtime.BoundEnvironment,'sample',modified_sample)
    msg=SimpleNamespace(time_boot_ms=10000,lat=400100000,lon=-1053000000,alt=1798657,
        get_type=lambda:'GLOBAL_POSITION_INT',get_srcSystem=lambda:1,get_srcComponent=lambda:1)
    closed=[];link=SimpleNamespace(wait_heartbeat=lambda **kw:msg,recv_match=lambda **kw:msg,close=lambda:closed.append(True))
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=lambda endpoint:link)))
    monkeypatch.setattr(native_forcing,'apply_wind',lambda *a,**kw:pytest.fail('changed binding reached PARAM_SET'))
    dest=tmp_path/'state.json'
    with pytest.raises(FileNotFoundError):run_bound_runtime(path,dest,mavlink='unit-only',max_updates=1)
    assert closed and not json.loads(dest.read_text())['ok']


@pytest.mark.parametrize('case',['hidden_epoch_rebase','implicit_utc','changed_origin'])
def test_rehashed_structural_validity_never_replaces_accepted_semantics(bound,tmp_path,case):
    path=tmp_path/'resealed.json';path.write_text(rejected_binding(case,bound[1]))
    assert nb.read_binding(path)  # The missing gate was semantic/current-input validation.
    with pytest.raises(ValueError,match='semantic context'):load_context(path)


def test_well_sealed_different_source_identity_is_stale_against_actual_current_code(bound,tmp_path):
    # A structurally valid binding with another deployed source identity must
    # reject independently of binding content-hash validation. Actual files and
    # the accepted current prior remain untouched; only this local copy changes.
    path,b=own_binding(bound,tmp_path)
    model=b['model_dependencies']
    artifact=model['artifacts']['environment_adapter/bound.py']
    previous=artifact['sha256']
    artifact['sha256']='0'*64 if previous!='0'*64 else '1'*64
    model['model_id']=nb.digest({k:v for k,v in model.items() if k!='model_id'})
    b['model_id']=model['model_id']
    seal(b);path.write_text(json.dumps(b))
    assert nb.read_binding(path)==b
    assert nb.verify_bound_input_files(b)  # Original bound data bytes still match.
    with pytest.raises(ValueError,match='model no longer current'):load_context(path)
    assert bound[1]['model_dependencies']['artifacts']['environment_adapter/bound.py']['sha256']==previous


def test_deleted_or_retargeted_publication_link_cannot_keep_cached_context(bound,tmp_path):
    path,b=own_binding(bound,tmp_path);link=tmp_path/'published.json';link.symlink_to(path)
    g=context_guard(link);link.unlink()
    with pytest.raises(FileNotFoundError):g.check()
    duplicate=tmp_path/'other.json';duplicate.write_bytes(path.read_bytes());link.symlink_to(duplicate)
    with pytest.raises(ValueError,match='link target'):g.check()


def test_in_memory_binding_mutation_cannot_change_forcing_query(bound,tmp_path,monkeypatch):
    path,_=own_binding(bound,tmp_path);provider=BoundEnvironment(path)
    provider.binding['effective_climate_epoch']='2026-10-28T16:01:00'
    monkeypatch.setattr(provider,'estimate',lambda *a:pytest.fail('mutated context reached the FMU'))
    with pytest.raises(ValueError,match='in-memory binding'):provider.sample(10.)


def test_change_during_readback_invalidates_result(bound,tmp_path,monkeypatch):
    from phoenix_env_adapter import native_forcing
    path,_=own_binding(bound,tmp_path);link=Link();link.queue=[];closed=[]
    msg=SimpleNamespace(time_boot_ms=10000,lat=400100000,lon=-1053000000,alt=1798657,
        get_type=lambda:'GLOBAL_POSITION_INT',get_srcSystem=lambda:1,get_srcComponent=lambda:1)
    receive=link.recv_match
    link.recv_match=lambda **kw:msg if isinstance(kw.get('type'),list) else receive(**kw)
    link.wait_heartbeat=lambda **kw:msg;link.close=lambda:closed.append(True)
    monkeypatch.setitem(sys.modules,'pymavlink',SimpleNamespace(mavutil=SimpleNamespace(mavlink_connection=lambda endpoint:link)))
    original=native_forcing.apply_wind
    def changed(*a,**kw):
        result=original(*a,**kw);path.unlink();return result
    monkeypatch.setattr(native_forcing,'apply_wind',changed)
    dest=tmp_path/'state.json'
    with pytest.raises(FileNotFoundError):run_bound_runtime(path,dest,mavlink='unit-only',max_updates=1)
    state=json.loads(dest.read_text());assert closed and not state['ok'] and not state['readback_verified']


def test_cached_semantics_cannot_follow_a_different_data_root(bound,tmp_path,monkeypatch):
    path,_=own_binding(bound,tmp_path);guard=context_guard(path)
    monkeypatch.setenv('TDDT_LAYER5_KSTORE_ROOT',str(tmp_path/'another_store'))
    with pytest.raises(ValueError,match='configuration or data address changed'):guard.check()


def test_cached_semantics_cannot_follow_in_memory_configuration_changes(bound,tmp_path):
    path,_=own_binding(bound,tmp_path);guard=context_guard(path)
    guard.config.data['R405_UNAPPROVED_CONFIGURATION_CHANGE']=True
    with pytest.raises(ValueError,match='configuration or data address changed'):guard.check()
