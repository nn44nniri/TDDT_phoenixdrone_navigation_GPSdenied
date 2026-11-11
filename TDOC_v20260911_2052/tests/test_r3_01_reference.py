"""Reference identity, claim limits and packaging relocation; no native execution."""
import json
import shutil

import pytest

from tddt_tdoc.phoenix_reference import EXPECTED_FILES, inspect_reference, reference_root


@pytest.fixture
def pinned(tmp_path):
    target=tmp_path/'reference';shutil.copytree(reference_root(),target)
    return target


def test_original_reference_inventory_has_four_feet_and_exact_reset():
    result=inspect_reference()
    assert result['ok']
    assert [c['name'] for c in result['contacts']]==['TAIL_FOOT_L1','TAIL_FOOT_L2','TAIL_FOOT_R1','TAIL_FOOT_R2']
    assert [c['location']['xyz'] for c in result['contacts']]==[
        [.14,-.28,.06],[.14,-.28,-.06],[.14,.28,.06],[.14,.28,-.06]]
    assert all(c['type']=='STRUCTURE' and c['location']['unit']=='M' for c in result['contacts'])
    assert result['reset']['altitude']=={'value':.08,'unit':'M'}
    assert result['reset']['theta']=={'value':89.,'unit':'DEG'}
    assert not result['ground_load_bearing_verified'] and not result['flight_ready']
    assert not result['active_installation'] and not result['actuation_authority']


def test_relocated_reference_is_read_only_and_keeps_identity(pinned):
    before={p.name:p.read_bytes() for p in pinned.iterdir()}
    assert inspect_reference(pinned)==inspect_reference()
    assert before=={p.name:p.read_bytes() for p in pinned.iterdir()}


@pytest.mark.parametrize('role',['aircraft','reset'])
def test_tampered_reference_is_rejected_without_changing_other_files(pinned,role):
    path=pinned/EXPECTED_FILES[role][0]
    data=path.read_bytes();path.write_bytes(data.replace(b'0.08',b'0.09',1) if role=='reset' else data.replace(b'TAIL_FOOT_L1',b'BELLY_GUARD1',1))
    assert not inspect_reference(pinned)['ok']
    assert not inspect_reference(pinned)['actuation_authority']


@pytest.mark.parametrize('role',['aircraft','reset'])
def test_missing_reference_member_is_rejected(pinned,role):
    (pinned/EXPECTED_FILES[role][0]).unlink()
    assert not inspect_reference(pinned)['ok']


def test_rehashing_manifest_cannot_promote_or_replace_reference(pinned):
    path=pinned/'REFERENCE_MANIFEST.json';manifest=json.loads(path.read_text())
    manifest['claims']['ground_load_bearing_verified']=True
    path.write_text(json.dumps(manifest))
    result=inspect_reference(pinned)
    assert not result['ok'] and result['reason']=='reference manifest hash mismatch'
    assert not result['ground_load_bearing_verified']


def test_reference_symlink_substitution_is_rejected(pinned,tmp_path):
    path=pinned/EXPECTED_FILES['reset'][0];external=tmp_path/'external.xml'
    path.rename(external);path.symlink_to(external)
    assert not inspect_reference(pinned)['ok']


def test_swapped_xml_pair_is_rejected(pinned):
    a=pinned/EXPECTED_FILES['aircraft'][0];b=pinned/EXPECTED_FILES['reset'][0]
    first,second=a.read_bytes(),b.read_bytes();a.write_bytes(second);b.write_bytes(first)
    assert not inspect_reference(pinned)['ok']
