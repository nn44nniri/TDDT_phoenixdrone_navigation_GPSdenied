"""The file bridge delegates execution; serialized decisions have no authority."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('completed,fresh',[(True,True),(False,True),(True,False)])
def test_execute_uses_current_workflow_and_rejects_failed_or_old_results(tmp_path,monkeypatch,completed,fresh):
    path=Path(__file__).parents[1]/'scripts/bridge_layer5_layer6_layer7.py'
    spec=importlib.util.spec_from_file_location('live_bridge_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    cfg=SimpleNamespace(package_root=tmp_path,runtime_root=tmp_path/'runtime',path=tmp_path/'configs/tdoc.yaml')
    monkeypatch.setattr(module.TDOCConfig,'from_yaml',lambda _:cfg)
    monkeypatch.setattr(module,'OfflineKnowledgeHandoff',lambda _:SimpleNamespace(
        validate_online_start=lambda:{'ok':True,'manifest_path':str(tmp_path/'handoff.json.gz')}))
    summary=tmp_path/'reports/online/ONLINE_JSBSIM_SUMMARY.json';summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({'online_training_contract':{'contract_ok':True}}))
    import os
    os.utime(summary,(1,1))
    payload={'transaction_id':'archived-tx','layer5_output':{'never_execute':'archived-command'},
        'layer4_result':{'archived':True},'network_snapshot':{'archived':True},
        'request':{'mesa_operation':5,'wing_operation':4,'stage':'on_ground'}}
    request=tmp_path/'request.json';request.write_text(json.dumps(payload))
    output=tmp_path/'result.json'
    def run(command,*,cwd):
        assert cwd==tmp_path and '--skip-offline' in command and '--online-training' in command
        actual=json.loads(Path(command[command.index('--request')+1]).read_text())
        assert actual==payload['request'] and 'layer5_output' not in actual
        if fresh:
            summary.write_text(json.dumps({'online_training_contract':{'contract_ok':completed}}))
        return SimpleNamespace(returncode=0 if completed else 2)
    monkeypatch.setattr(module.subprocess,'run',run)
    monkeypatch.setattr(module.sys,'argv',['bridge','--request',str(request),'--output',str(output),'--execute'])
    rc=module.main();result=json.loads(output.read_text())
    assert result['ok'] is (completed and fresh)
    assert rc==(0 if completed and fresh else 2)
    assert result['archived_decision_replayed'] is False
