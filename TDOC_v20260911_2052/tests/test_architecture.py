from __future__ import annotations
from pathlib import Path

def test_layer3_adapter_never_uses_layer3_fmu_runtime():
    text=(Path(__file__).parents[1]/"src/tddt_tdoc/adapters/layer3.py").read_text()
    assert "from layer3 import Layer3Config, Layer3Engine" in text
    assert "Layer3FMURuntime(" not in text
    assert "Layer3Engine" in text and ".step(" in text

def test_layer4_disables_reverse_full_fidelity_ownership():
    text=(Path(__file__).parents[1]/"src/tddt_tdoc/adapters/layer4.py").read_text()
    assert '["enabled"]=False' in text
    assert "tdoc_owned_full_fidelity_transaction" in text
    assert "Layer3FMURuntime(" not in text

def test_no_external_binaries_vendored():
    root=Path(__file__).parents[1]
    banned=[]
    for p in root.rglob("*"):
        if p.is_file() and (p.suffix.lower() in {".fmu",".tif",".tiff"} or p.name.endswith((".tar.gz",".tgz"))): banned.append(p)
    assert not banned

def test_formalism_v4_is_packaged_as_documentation():
    p=Path(__file__).parents[1]/"docs/TDDT_Dynamic_NTwin_TDOC_details_FA_v4.md"
    assert p.exists()
    t=p.read_text(encoding="utf-8")
    assert "41.1. قرارداد عمومی مالکیت Trigger" in t
    assert "Owner_{trigger}" in t

def test_iso_adapter_conversion_is_owned_by_layer31_not_duplicated_in_tdoc():
    root=Path(__file__).parents[1]
    orch=(root/"src/tddt_tdoc/orchestrator.py").read_text(encoding="utf-8")
    l3=(root/"src/tddt_tdoc/adapters/layer3.py").read_text(encoding="utf-8")
    assert "UnitConverter(" not in orch
    assert "bind_layer3_config" in l3
    assert '"conversion_owner":"Layer3.1/TDOCAdapter"' in l3

def test_layer5_is_external_and_not_vendored():
    root=Path(__file__).parents[1]
    env=(root/"configs/environment.sh").read_text(encoding="utf-8")
    assert "layer5_v20260911_1510" in env
    assert not (root/"src/layer5").exists()
    a=(root/"src/tddt_tdoc/adapters/layer5.py").read_text(encoding="utf-8")
    assert "Layer5Config" in a and "Layer5Engine" in a
    assert "TDDT_LAYER5_ROOT" in a


def test_layer6_is_online_only_and_offline_bypasses_it():
    from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
    from native_migration_support import native_adapter
    import pytest
    assert Layer6OnlineAdapter.MODES == ('ONLINE_JSBSIM',)
    a,client,_=native_adapter()
    with pytest.raises(ValueError, match='Online-only'):
        a.invoke({'learning_mode':'OFFLINE_SE'})
    assert client.sent == []
    orch=(Path(__file__).parents[1]/'src/tddt_tdoc/orchestrator.py').read_text()
    assert '_route_offline_feedback_to_layer2' in orch



def test_online_layer6_native_transport_does_not_reuse_retired_ptlib():
    from native_migration_support import native_adapter,native_payload
    from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
    a,client,_=native_adapter()
    assert a.SOURCE == 'Layer6/GUI/MAVLink'
    assert all(not hasattr(a,name) for name in ('_make_ptlib','_ptlib_probe','_runtime_snapshot'))
    result=a.invoke(native_payload())
    assert result['status']=='FAILED'
    assert not result['actuation_executed']
    assert 'native_execution_model_not_verified' in result['error']
    assert client.sent==[]


def test_layer7_contract_is_tdoc_owned_and_safe_commit_is_fail_closed():
    root=Path(__file__).parents[1]
    orch=(root/'src/tddt_tdoc/orchestrator.py').read_text()
    l7=(root/'src/tddt_tdoc/adapters/layer7.py').read_text()
    assert '_layer7_validate_candidate' in orch and '_layer7_commit_candidate' in orch and '_layer7_rollback' in orch
    assert 'candidate_bound_external_shadow_validation_not_supplied_fail_closed' in orch
    assert 'route_layer7' in orch and 'VersionedRoute' in orch
    assert 'TDOC/RuntimeScheduler' in l7 and 'external_only' in l7

def test_full_fidelity_never_guesses_layer4_to_phoenix_mapping():
    root=Path(__file__).parents[1]
    l5=(root/"src/tddt_tdoc/adapters/layer5.py").read_text(encoding="utf-8")
    l4=(root/"src/tddt_tdoc/adapters/layer4.py").read_text(encoding="utf-8")
    assert "full_fidelity_action_bridge" in l5
    assert "phoenix.bind_candidate" in l5
    assert "phoenix_physical_allocation_unvalidated" in l5
    assert "phoenix_action_bridge_unvalidated" in l4
    assert "fail_closed_no_mapping_guess" in l4
