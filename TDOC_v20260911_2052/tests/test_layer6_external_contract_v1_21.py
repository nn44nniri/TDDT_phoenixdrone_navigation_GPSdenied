from __future__ import annotations
import os, time
from pathlib import Path
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
from test_layer5_layer6 import make_cfg, make_fake_layer5, make_fake_ptlib, clear_layer5_modules

L6_ROOT=Path(__file__).parents[2]/"layer6_v20260920_0830"


def _enable(cfg):
    cfg.data["layers"]["layer6"]["online"]["require_external_contract"]=True
    os.environ["TDDT_LAYER6_ROOT"]=str(L6_ROOT)


def _canonical_layer5(now=None):
    now=time.time() if now is None else float(now)
    command=[0.1,-0.2,0.55,0.0]
    targets=["left_elevon","right_elevon","left_motor","right_motor"]
    prov={"source_layer":"Layer5","layer6_execution_safety_required":True,"decision_id":"D-L6-1"}
    pkg={
        "command":command,"actuator_targets":targets,
        "execution_constraints":{"direct_pwm":False},
        "safety_policy":{"psf":True,"rta":True,"layer6_device_gate_required":True},
        "apply_time":now,"deadline_time":now+4.0,"decision_confidence":0.9,
        "provenance":prov,"version":"2026.9.11.1510","Source":"Layer5",
        "u_cmd":command,"A_target":targets,"C_exec":{"direct_pwm":False},
        "S_safe":{"psf":True,"rta":True,"layer6_device_gate_required":True},
        "tau_apply":now,"tau_deadline":now+4.0,"rho_decision":0.9,
        "pi_decision":prov,"nu":"2026.9.11.1510",
    }
    pkg["formal_contract"]={k:pkg[k] for k in ("u_cmd","A_target","C_exec","S_safe","tau_apply","tau_deadline","rho_decision","pi_decision","nu")}
    return {"ok":True,"result":{
        "ok":True,"snapshot_version":"v1","decision_id":"D-L6-1",
        "control_action":{"first_move":command},"layer6_package":pkg,"gates":{"FINAL":True}
    }}


def test_native_rejects_reduced_layer5_envelope_before_intake():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter(verified=True)
    p=native_payload()
    del p['layer5_output']['result']['layer6_package']['S_safe']
    r=a.invoke(p)
    assert r['status']=='FAILED' and r['actuation_executed'] is False
    assert 'incomplete Layer5 execution transaction' in r['error']
    assert client.sent==[]



def test_canonical_pilot_envelope_alone_cannot_certify_native_execution():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter(ready=True,verified=False)
    result=a.invoke(native_payload())
    assert result['status']=='FAILED' and not result['actuation_executed']
    assert result['route']=='HOLD'
    assert 'native_execution_model_not_verified' in result['error']
    assert client.sent==[]



def test_native_missing_telemetry_prevents_runtime_execution():
    import pytest
    from native_migration_support import native_adapter, healthy_status
    a,client,_=native_adapter(verified=True)
    bad=healthy_status()
    bad['telemetry_age_s']['HEARTBEAT']=100.0
    with pytest.raises(RuntimeError,match='native telemetry/forcing not ready'):
        a._ingress_from_status(bad)
    assert client.sent==[]
