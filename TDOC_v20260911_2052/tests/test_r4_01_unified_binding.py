"""R4.01 binding identity/epoch contracts, never a live-environment flight test."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import pytest

from tddt_tdoc import native_binding as nb
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_environment import prepare_binding

TDOC=Path(__file__).resolve().parents[1]


def config():
    c=TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml');resolve=c.path_value
    # These tests explicitly audit the preserved package read-only. No Layer5
    # engine/working KStore is constructed; publication always targets tmp_path.
    c.path_value=lambda key,required=False:TDOC/'runtime/layer5/kstore' if key=='TDDT_LAYER5_KSTORE_ROOT' else resolve(key,required=required)
    return c


@pytest.fixture(scope='module')
def binding():return nb.build_native_binding(config(),plant_epoch_s=17.25)


def seal(binding):
    binding['identity_hashes']=nb._identity_parts(binding)
    binding['context_sha256']=nb.digest(binding['identity_hashes'])
    binding['binding_hash']=nb.digest({k:v for k,v in binding.items() if k!='binding_hash'})
    return binding


def test_binding_is_one_complete_content_bound_context(binding,tmp_path):
    assert nb.validate_binding(binding)==binding
    assert binding['schema']==nb.SCHEMA and binding['revision']==nb.REVISION
    assert set(binding['identity_hashes'])=={'model_dependencies','knowledge','forcing','origin','time_contract','ground_contact_policy'}
    assert nb.verify_binding_inputs(binding,config())['ok']
    p=tmp_path/'binding.json';p.write_text(json.dumps(binding))
    assert nb.read_binding(p)==binding
    assert binding['knowledge']['setd_package_id'].startswith('SETD_')
    assert len(binding['knowledge']['verified_package_files'])==17
    assert binding['origin']['initialization']['initialization_only']
    assert not binding['execution_environment_verified'] and not binding['actuation_authority']


def test_accepted_naive_epoch_is_preserved_without_utc_or_host_conversion(binding):
    hm=json.loads(gzip.decompress((TDOC/'runtime/knowledge/handoff/offline_latest.json.gz').read_bytes()))
    epoch=hm['reuse_contract']['climate_time_alignment']['effective_climate_epoch']
    assert binding['effective_climate_epoch']==epoch=='2026-10-28T16:00:00'
    t=binding['time_contract']
    assert t['plant_epoch_s']==17.25 and t['mission_elapsed_epoch_s']==0.
    assert t['clock_domain']=='ACCEPTED_DATASET_NAIVE' and not t['utc_mapping_verified']
    assert t['observed_monotonic_s'] is None and not t['runtime_clock_synchronization_verified']
    assert not t['online_epoch_rebased']
    assert binding['forcing']['metadata']['timezone_request_selectors']==['auto']


def test_separate_preparations_have_unique_ids_but_identical_context(binding):
    second=nb.build_native_binding(config(),plant_epoch_s=17.25)
    assert binding['binding_id']!=second['binding_id']
    assert binding['context_sha256']==second['context_sha256']
    assert binding['binding_hash']!=second['binding_hash']


@pytest.mark.parametrize('key',['schema','revision','binding_id','prepared_at_utc','binding_hash','model_id',
    'handoff_hash','forcing_file_sha256','effective_climate_epoch','plant_epoch_s','context_sha256'])
def test_corrupt_top_level_fields_reject(binding,key):
    corrupt=deepcopy(binding);corrupt[key]='corrupt'
    with pytest.raises(ValueError):nb.validate_binding(corrupt)


@pytest.mark.parametrize('key',['knowledge','forcing','origin','time_contract','model_dependencies'])
def test_unhashed_nested_changes_reject(binding,key):
    corrupt=deepcopy(binding);corrupt[key]['added_corruption']=True
    with pytest.raises(ValueError,match='content hash'):nb.validate_binding(corrupt)


@pytest.mark.parametrize('key',['actuation_authority','execution_environment_verified'])
def test_rehashed_preparation_cannot_claim_execution(binding,key):
    corrupt=deepcopy(binding);corrupt[key]=True
    with pytest.raises(ValueError,match='cannot grant execution'):nb.validate_binding(seal(corrupt))


def test_rehashed_origin_must_keep_cg_and_datum_equation(binding):
    corrupt=deepcopy(binding);corrupt['origin']['initialization']['altitude_msl_m']+=1.
    with pytest.raises(ValueError,match='origin/CG geometry'):nb.validate_binding(seal(corrupt))


def test_rehashed_naive_clock_cannot_be_labeled_utc(binding):
    corrupt=deepcopy(binding)
    for d in [corrupt['time_contract'],corrupt['forcing']]:d.update(clock_domain='UTC_OFFSET_AWARE',utc_mapping_verified=True)
    with pytest.raises(ValueError,match='clock authority unsupported'):nb.validate_binding(seal(corrupt))


@pytest.mark.parametrize('value',[True,-1.,float('nan'),float('inf'),'0'])
def test_invalid_plant_anchor_rejects_before_publication(value,tmp_path):
    p=tmp_path/'binding.json'
    with pytest.raises(ValueError,match='plant epoch'):prepare_binding(config(),plant_epoch_s=value,output_path=p)
    assert not p.exists()


def test_atomic_publication_has_exact_readback_and_preserves_inputs(binding,tmp_path):
    p=tmp_path/'binding.json';before={r['path']:hashlib.sha256(Path(r['path']).read_bytes()).hexdigest() for r in binding['input_files']}
    result=prepare_binding(config(),plant_epoch_s=17.25,output_path=p)
    assert nb.read_binding(p)==result and nb.verify_binding_inputs(result,config())['ok']
    assert not list(tmp_path.glob('*.tmp'))
    assert all(hashlib.sha256(Path(k).read_bytes()).hexdigest()==v for k,v in before.items())


def test_output_cannot_overwrite_preserved_forcing(binding):
    path=Path(binding['forcing_file']);raw=path.read_bytes()
    with pytest.raises(ValueError,match='overwrite an accepted input'):prepare_binding(config(),output_path=path)
    assert path.read_bytes()==raw


def test_legacy_binding_cannot_be_mistaken_for_v2(binding):
    old=deepcopy(binding);old['schema']='TDOC-native-environment-binding-v1'
    with pytest.raises(ValueError,match='schema/revision'):nb.validate_binding(old)


def test_changed_model_during_construction_prevents_publication(binding,tmp_path,monkeypatch):
    original=nb.capture_model_dependencies;calls=[]
    def capture(c):
        now=original(c);calls.append(True)
        if len(calls)>1:now['model_id']='different'
        return now
    monkeypatch.setattr(nb,'capture_model_dependencies',capture)
    p=tmp_path/'binding.json'
    with pytest.raises(ValueError,match='model changed during binding construction'):prepare_binding(config(),output_path=p)
    assert not p.exists()


def test_input_change_detected_without_rewriting_file(binding,monkeypatch):
    original=Path.read_bytes;path=Path(binding['forcing_file'])
    monkeypatch.setattr(Path,'read_bytes',lambda p:original(p)+b'\nchanged' if p==path else original(p))
    with pytest.raises(ValueError,match='input hash mismatch'):nb.verify_binding_inputs(binding,config())


def test_rehashed_origin_does_not_match_current_surveyed_context(binding):
    corrupt=deepcopy(binding);corrupt['mission_origin']['latitude_deg']+=.001
    seal(corrupt)
    with pytest.raises(ValueError,match='semantic context no longer current'):
        nb.verify_binding_inputs(corrupt,config())


def test_missing_input_index_cannot_hide_files(binding):
    corrupt=deepcopy(binding);corrupt['input_files']=[];seal(corrupt)
    with pytest.raises(ValueError,match='input index incomplete'):
        nb.verify_binding_inputs(corrupt,config())


@pytest.mark.parametrize('raw',[b'point,time\np,2026-10-28T16:00\np,2026-10-28T16:00\n',
    b'point,time\np,2026-10-28T16:00\np,2026-10-28T15:00\n',
    b'point,time\np,2026-10-28T16:00\np,2026-10-28T17:00+00:00\n'])
def test_forcing_clock_structure_must_be_unambiguous(tmp_path,raw):
    p=tmp_path/'forcing.csv';p.write_bytes(raw)
    contract={'forcing_file':str(p),'forcing_file_sha256':hashlib.sha256(raw).hexdigest(),
        'effective_climate_epoch':'2026-10-28T16:00:00','forcing_start':'2026-10-28T16:00:00','forcing_end':'2026-10-28T17:00:00'}
    with pytest.raises(ValueError):nb._forcing(contract,nb._Inputs())


def test_offset_aware_forcing_keeps_source_offset_without_changing_samples(tmp_path):
    p=tmp_path/'forcing.csv';raw=b'point,time\np,2026-10-28T16:00-06:00\np,2026-10-28T17:00-06:00\n';p.write_bytes(raw)
    contract={'forcing_file':str(p),'forcing_file_sha256':hashlib.sha256(raw).hexdigest(),
        'effective_climate_epoch':'2026-10-28T16:00-06:00','forcing_start':'2026-10-28T16:00-06:00','forcing_end':'2026-10-28T17:00-06:00'}
    forcing,epoch=nb._forcing(contract,nb._Inputs())
    assert forcing['utc_mapping_verified'] and forcing['clock_domain']=='UTC_OFFSET_AWARE'
    assert epoch.isoformat()=='2026-10-28T16:00:00-06:00' and p.read_bytes()==raw
    assert forcing['time_reference']['effective_epoch_exact']==contract['effective_climate_epoch']


def test_missing_or_changed_setd_component_rejects_publication(binding,monkeypatch,tmp_path):
    target=Path(binding['knowledge']['verified_package_files'][1]['path']);original=Path.read_bytes
    def read(p):
        raw=original(p)
        return gzip.compress(gzip.decompress(raw)+b'\ncorrupt') if p==target else raw
    monkeypatch.setattr(Path,'read_bytes',read)
    with pytest.raises(ValueError,match='SETD component hash'):prepare_binding(config(),output_path=tmp_path/'binding.json')
    assert not (tmp_path/'binding.json').exists()


def test_failed_atomic_replace_preserves_existing_binding(tmp_path,monkeypatch):
    import tddt_tdoc.native_environment as env
    p=tmp_path/'binding.json';p.write_bytes(b'previous accepted binding')
    def fail(*args):raise OSError('isolated publication failure')
    monkeypatch.setattr(env.os,'replace',fail)
    with pytest.raises(OSError,match='publication failure'):prepare_binding(config(),output_path=p)
    assert p.read_bytes()==b'previous accepted binding'
    assert not list(tmp_path.glob('*.tmp'))
