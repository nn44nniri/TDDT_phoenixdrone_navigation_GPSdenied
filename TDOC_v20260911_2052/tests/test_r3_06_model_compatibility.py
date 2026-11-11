"""Versioned model compatibility without altering accepted Offline knowledge."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc import model_compatibility as mc
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.rtmo.artifact_gate import RTMOL4ArtifactGate

TDOC=Path(__file__).resolve().parents[1]
BUNDLE=TDOC.parent


@pytest.fixture(scope='module')
def current():
    return mc.capture_model_dependencies(TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml'))


def changed(current,key):
    b=deepcopy(current);b['artifacts'][key]['sha256']='0'*64
    b['model_id']=mc.digest({k:v for k,v in b.items() if k!='model_id'})
    return b


def test_current_identity_is_complete_stable_and_has_no_flight_authority(current):
    again=mc.capture_model_dependencies(TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml'))
    assert current==again and mc.valid_dependencies(current)
    assert len(current['artifacts'])>=100
    gate=mc.certificate_compatibility({'model_dependencies':current},again)
    assert gate['ok'] and not gate['flight_authority'] and not gate['current_state_replaced']
    assert mc.compatibility_policy()['cross_model_equivalences']==[]


@pytest.mark.parametrize('key',['layer2_profile','layer5_profile','mesa_nav_fmu','wingmpet_fmu',
    'micromet_fmu','native_aircraft','packaged_aircraft','reset_geometry_template','native_bridge',
    'arduplane_binary','jsbsim_binary','ardupilot_default_parameters','pilot_configuration','flightstack_profile',
    'layer4/multifidelity.py','layer5/phoenix.py','tdoc/adapters/layer3.py'])
def test_each_changed_dependency_rejects_previous_certificate(current,key):
    assert not mc.certificate_compatibility({'model_dependencies':current},changed(current,key))['ok']


@pytest.mark.parametrize('certificate',[{}, {'model_dependencies':{}}, {'model_dependencies':{'model_id':'forged'}}])
def test_missing_and_forged_dependencies_reject(current,certificate):
    assert not mc.certificate_compatibility(certificate,current)['ok']


def test_tampered_binding_and_changed_during_qualification_reject(current):
    bad=deepcopy(current);bad['artifacts']['wingmpet_fmu']['sha256']='0'*64
    assert not mc.certificate_compatibility({'model_dependencies':bad},current)['ok']
    with pytest.raises(ValueError,match='during certificate qualification'):
        mc.qualify_new_certificate({'ok':True},current,changed(current,'wingmpet_fmu'))


@pytest.mark.parametrize('artifact',['arduplane_binary','jsbsim_binary','native_aircraft','wingmpet_fmu'])
def test_capture_observes_actual_changed_file_bytes(current,artifact,tmp_path,monkeypatch):
    # Redirect only this read to a private mutated copy. Production remains intact.
    rel={'arduplane_binary':'layer6_v20261007_1032/FlightStack/ardupilot/build/sitl/bin/arduplane',
         'jsbsim_binary':'layer6_v20261007_1032/FlightStack/jsbsim/build/src/JSBSim',
         'native_aircraft':'layer6_v20261007_1032/FlightStack/ardupilot/Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml',
         'wingmpet_fmu':'twins_layer2_v20260820_1233/fmus/WingMPET.fmu'}[artifact]
    target=BUNDLE/rel;original=Path.read_bytes
    altered=tmp_path/'modified';altered.write_bytes(original(target)+b'\nR3.06-test-mutation')
    # A private installed bundle may reference the same immutable artifact through
    # a symlink. Mutate the resolved file read in this process for every alias.
    monkeypatch.setattr(Path,'read_bytes',lambda p:original(altered) if p.resolve()==target.resolve() else original(p))
    try:after=mc.capture_model_dependencies(TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml'))
    except ValueError as exc:
        assert artifact=='native_aircraft' and 'deployment mismatch' in str(exc)
    else:assert not mc.certificate_compatibility({'model_dependencies':current},after)['ok']


def test_l5_rechecks_cached_certificate_and_revokes_tracker(current,monkeypatch):
    a=Layer5Adapter.__new__(Layer5Adapter)
    a.config=SimpleNamespace(get=lambda k,d=None:'Layer6Client_GUI_MAVLink' if k.endswith('transport') else d)
    p=SimpleNamespace(nominal_tracker_authorized=True,runtime_certificate={'accepted':True})
    a.engine=SimpleNamespace(phoenix=p);a._verification_model_certificate={'ok':True,'model_dependencies':current}
    live=[current];monkeypatch.setattr(mc,'capture_model_dependencies',lambda cfg:live[0])
    assert a._certificate_model_gate(a._verification_model_certificate)['ok']
    live[0]=changed(current,'jsbsim_binary')
    result=a._wingmpet_verification_mix([0.,0.,0.,0.])
    assert not result['ok'] and result['reason']=='certificate_model_dependencies_mismatch'
    assert not p.nominal_tracker_authorized and not p.runtime_certificate['accepted']
    assert a._verification_model_certificate is None


def test_l5_external_autoload_validator_rejects_legacy_and_changed_model(current):
    from layer5 import Layer5Config
    from layer5.phoenix import PhoenixSpecialization
    cfg=Layer5Config.from_yaml(BUNDLE/'layer5_v20260911_1510/configs/configuration_phoenix.yaml')
    cfg.model_dependencies_provider=lambda:current
    p=PhoenixSpecialization(cfg)
    result=p.apply_runtime_validation_certificate({'schema':'Layer5-Phoenix-offline-model-certificate-v2','ok':True})
    assert not result['ok'] and not p.nominal_tracker_authorized
    assert 'model_dependencies_missing_or_changed' in str(result)


def test_rtmo_fast_artifact_rechecks_physical_identity(current):
    live=[current];g=RTMOL4ArtifactGate(model_dependencies_provider=lambda:live[0])
    result={'verified_candidates':[{'candidate_id':'C'}],
            'candidates':[{'candidate_id':'C','verification':{'model_dependencies':current}}]}
    assert g._is_verified(result)
    g._artifact={'metadata':{},'result':result}
    live[0]=changed(current,'arduplane_binary')
    assert not g._is_verified(result)
    gate=g.acquire(snapshot={},request={},orchestration_version='v1',reference_time_s=0.)
    assert not gate.ok and gate.reason=='artifact_model_dependencies_missing_or_changed'


def test_legacy_handoff_certificate_is_rejected_without_modification(current,tmp_path):
    # A regression fixture must not assume that the user's live prior is legacy.
    hp=tmp_path/'offline_latest.json.gz'
    hp.write_bytes(gzip.compress(json.dumps({'source_transaction_id':'legacy'}).encode()))
    cp=tmp_path/'legacy.json.gz'
    cp.write_bytes(gzip.compress(json.dumps({'layer_status':{'4':{'evidence':{
        'offline_model_control':{'certificate':{'schema':'Layer5-Phoenix-offline-model-certificate-v2','ok':True}}
    }}}}).encode()))
    raw=hp.read_bytes()
    hm=json.loads(gzip.decompress(raw))
    cp=tmp_path/(hm['source_transaction_id']+'.json.gz');cycle_raw=cp.read_bytes()
    cycle=json.loads(gzip.decompress(cycle_raw))
    cert=cycle['layer_status']['4']['evidence']['offline_model_control']['certificate']
    gate=mc.certificate_compatibility(cert,current)
    assert not gate['ok'] and gate['reason']=='certificate_model_dependencies_missing'
    assert hp.read_bytes()==raw and cp.read_bytes()==cycle_raw


def mf_case(tmp_path):
    from layer4 import Layer4Config
    from layer4.snapshot import Layer3SnapshotAdapter
    from layer4.multifidelity import CertifiedMultiFidelityManager
    from layer4.models import Candidate
    root=BUNDLE/'layer4_v20260907_0608'
    cfg=Layer4Config.from_yaml(root/'configs/configuration_boulder.yaml')
    cfg.data.setdefault('verification',{})['certificate_bank_path']=str(tmp_path/'bank.jsonl')
    snap=Layer3SnapshotAdapter(cfg).adapt(json.loads((root/'examples/layer3_snapshot_example.json').read_text()))
    snap.context={**(snap.context or {}),'flight_phase':'cruise'}
    c=Candidate('C','S',snap.version,snap.hash,1.,[[0.]*19],[[[0.]*19 for _ in range(19)]],[{}],[[0.]*5],{'mission':0.})
    return CertifiedMultiFidelityManager(cfg),snap,c


def test_positive_bank_issuance_and_reuse_live_model_guard(current,tmp_path):
    m,s,c=mf_case(tmp_path);live=[current];m.model_dependencies_provider=lambda:live[0]
    v={'verified':True,'candidate_verified':True,'executed':True}
    assert m.build(c,s,None,'m','p',v) is None
    v['model_dependencies']=current
    cert=m.build(c,s,None,'m','p',v);assert cert is not None
    assert not cert['flight_authorized'] and m.find(c,s,None,'m','p')==cert
    live[0]=changed(current,'native_bridge')
    assert m.find(c,s,None,'m','p') is None
    assert m.build(c,s,None,'m','p',v) is None
    assert m.capability()==[]


def test_negative_bank_requires_same_live_model(current,tmp_path):
    m,s,c=mf_case(tmp_path);live=[current];m.model_dependencies_provider=lambda:live[0]
    v={'executed':True,'attempted':True,'verified':False,'outcome_status':'ATTEMPTED_AND_REJECTED'}
    assert m.build_rejection(c,s,None,'m','p',v) is None
    v['model_dependencies']=current
    cert=m.build_rejection(c,s,None,'m','p',v);assert cert is not None
    assert m.find_rejection(c,s,None,'m','p')==cert
    live[0]=changed(current,'ardupilot_default_parameters')
    assert m.find_rejection(c,s,None,'m','p') is None


def test_provider_error_fails_closed(current,tmp_path):
    m,s,c=mf_case(tmp_path)
    def broken():raise FileNotFoundError('missing native model')
    m.model_dependencies_provider=broken
    assert m.build(c,s,None,'m','p',{'verified':True,'candidate_verified':True,'executed':True,'model_dependencies':current}) is None


def test_rtmo_bank_capability_rejects_old_or_changed_dependencies(current,tmp_path):
    from tddt_tdoc.rtmo.capability_gate import _certificate_bank_guard
    m,s,c=mf_case(tmp_path)
    m.model_dependencies_provider=lambda:current
    cert=m.build(c,s,None,'m','p',{'verified':True,'candidate_verified':True,'executed':True,'model_dependencies':current})
    assert cert is not None
    l4={'cruise':{'model_hashes':['m'],'parameter_hashes':['p'],'verified_reference_times':[s.reference_time_s]}}
    assert _certificate_bank_guard(m.path,l4,lambda:current)['cruise']['certificate_contract_ready']
    assert not _certificate_bank_guard(m.path,l4,lambda:changed(current,'wingmpet_fmu'))['cruise']['certificate_contract_ready']


def test_corrupt_packaged_reset_template_rejects(tmp_path,monkeypatch):
    import shutil
    from tddt_tdoc import phoenix_geometry as pg
    target=tmp_path/'r3_02_v1';shutil.copytree(pg.aligned_root(),target)
    p=target/'reset.xml';p.write_text(p.read_text().replace('89','88'))
    monkeypatch.setattr(pg,'aligned_root',lambda:target)
    with pytest.raises(ValueError,match='Reset template hash mismatch'):
        pg.validate_native_geometry(TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml'))


def test_stale_embedded_fmu_profile_rejects(monkeypatch):
    original=mc.ZipFile.read
    def read(z,name,*args,**kwargs):
        raw=original(z,name,*args,**kwargs)
        return raw+b'\n# stale' if str(z.filename).endswith('WingMPET.fmu') else raw
    monkeypatch.setattr(mc.ZipFile,'read',read)
    with pytest.raises(ValueError,match='stale embedded Phoenix profile: wingmpet_fmu'):
        mc.capture_model_dependencies(TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml'))


def test_layer6_bound_session_rejects_changed_model_before_execution(current,monkeypatch):
    import time
    from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
    a=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    a.config=SimpleNamespace();a._option=lambda k,d=None:True
    a._environment_binding={'binding_id':'B','forcing_file_sha256':'F','model_dependencies':current}
    s={'environment_contract':{'ok':True,'binding_id':'B','forcing_file_sha256':'F','updated_monotonic':time.monotonic()}}
    live=[current];monkeypatch.setattr(mc,'capture_model_dependencies',lambda cfg:live[0])
    assert a._environment_ready(s)
    live[0]=changed(current,'native_aircraft')
    assert not a._environment_ready(s)


def test_dynamic_reset_origin_never_grants_origin_binding_or_changes_model_id(current,monkeypatch):
    import xml.etree.ElementTree as ET
    from tddt_tdoc.phoenix_geometry import validate_native_geometry
    original=ET.parse
    def parse(path,*args,**kwargs):
        tree=original(path,*args,**kwargs)
        if str(path).endswith('ardupilot/Tools/autotest/aircraft/phoenix_v3/reset.xml'):
            tree.getroot().find('psi').text='12'
        return tree
    monkeypatch.setattr(ET,'parse',parse)
    cfg=TDOCConfig.from_yaml(TDOC/'configs/tdoc.yaml')
    assert not validate_native_geometry(cfg)['reset_origin_binding_verified']
    assert mc.capture_model_dependencies(cfg)==current
