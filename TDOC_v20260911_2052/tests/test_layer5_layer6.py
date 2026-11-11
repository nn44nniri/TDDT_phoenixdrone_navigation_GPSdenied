from __future__ import annotations
import json, sys, os
import pytest
from pathlib import Path
from tddt_tdoc import TDOCConfig,TDOCOrchestrator
from tddt_tdoc.storage import iter_jsonl_auto
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter

ROOT=Path(__file__).parents[1]

_ENV_KEYS=("TDDT_LAYER5_ROOT","TDDT_PTLIB_ROOT","EXT_FMU_SIM_ROOT","TDDT_BOULDER_ROOT")

@pytest.fixture(autouse=True)
def _restore_environment_roots():
    before={k:os.environ.get(k) for k in _ENV_KEYS}
    yield
    for k,v in before.items():
        if v is None: os.environ.pop(k,None)
        else: os.environ[k]=v

def make_fake_layer5(tmp_path:Path)->Path:
    root=tmp_path/"layer5_v20260911_1510"
    pkg=root/"src/layer5"; pkg.mkdir(parents=True)
    (root/"configs").mkdir()
    (root/"configs/configuration_phoenix.yaml").write_text("package:\n  version: 2026.9.11.1510\n")
    (pkg/"__init__.py").write_text('''\n__version__="2026.9.11.1510"\nclass Layer5Config:\n    def __init__(self,path): self.path=path\n    @classmethod\n    def from_yaml(cls,path): return cls(path)\nclass _FakeKStore:
    def __init__(self): self.setd=None
    def load_setd(self): return self.setd or {"ok":False,"reason":"missing"}
class _FakePhoenix:
    hash="fake-phoenix-profile"
    def apply_runtime_validation_certificate(self,cert): return {"ok":True,"certificate_kind":"OFFLINE_MODEL"}
    def mix_physical(self,command):
        a,e,t,r=[float(x) for x in command]
        return {"ok":True,"physical":[-0.08+0.05*(a+e),-0.08+0.05*(-a+e),t,t],"direct_pwm":False}
class Layer5Engine:
    def __init__(self,cfg): self.cfg=cfg; self.kstore=_FakeKStore(); self.offline_count=0; self.phoenix=_FakePhoenix()
    def preflight(self,strict_external=False): return {"ok":True,"strict_external":strict_external}
    def run(self,layer4,learning_mode=None,**kwargs):
        if learning_mode=="OFFLINE_SE": self.offline_count+=1
        if learning_mode=="ONLINE_JSBSIM" and not self.kstore.load_setd().get("ok"):
            return {"ok":True,"learning_mode":learning_mode,"operational_mode":"HOLD","control_action":None,"layer6_package":None,"gates":{"FINAL":False,"lifecycle":{"ok":False,"reasons":["valid_SETD_required_for_online"]}},"knowledge":{"setd":self.kstore.load_setd()},"provenance":{"only_first_mpc_move":False}}
        base={"ok":True,"learning_mode":learning_mode,"operational_mode":"AUTO","snapshot_version":layer4.get("snapshot_version"),"network_commit":False,"model_retraining":False,"direct_pwm":False,"gates":{"FINAL":True},"provenance":{"only_first_mpc_move":True},"knowledge":{"setd":self.kstore.load_setd() if learning_mode=="ONLINE_JSBSIM" else {"ok":False,"reason":"not_loaded_offline"}}}
        ca={"first_move":[0.1,-0.2,0.55,0.0]}
        base["control_action"]=ca
        if learning_mode=="ONLINE_JSBSIM": base["layer6_package"]={"control_action":ca,"device_revalidation":True}
        else: base["layer6_package"]=None
        return base
    def finalize_offline_episode(self,knowledge=None):
        if self.offline_count<1: return {"ok":False,"committed":False,"reason":"no_active_offline_episode"}
        self.kstore.setd={"ok":True,"manifest":{"package_id":"SETD_FAKE_001","episode_id":"E1","experience_count":self.offline_count,"committed":True,"knowledge_quality":{"control_knowledge_valid":True,"phase_control_coverage":{"all_required_control_phases":True,"missing_control_phases":[]}}},"knowledge":{"episode_id":"E1","experience_count":self.offline_count,"knowledge":{"knowledge_version":"v1","knowledge_hash":"kh1","CRP_SARG_TEP_references":{}}}}
        return {"ok":True,"committed":True,"package_id":"SETD_FAKE_001"}
''')
    (pkg/"environment.py").write_text('''\nimport os\nclass PTLibJSBSimAdapter:\n    def __init__(self,config): self.config=config\n    def probe(self): return {"ok":True,"root":os.environ.get("TDDT_PTLIB_ROOT"),"truth_observation_separated":True}\n    def prepare(self): return {"ok":True,"prepared":True}\n    def connect(self): return {"ok":True,"connected":True}\n    def safe_control_write(self,command): return {"ok":True,"command":list(command),"direct_pwm":False,"stream_status":{"running":True,"send_count":1,"last_result":{"release_confirmation":{"ready":True,"released":True,"holding":False,"release_token_consumed":True}}}}\n    def io_snapshot(self): return {"ok":True,"source":"JSBSim-via-ArduPilot","sensor_observation":{"imu":1},"environment_truth":{"x":2}}\n''')
    return root

def make_fake_ptlib(tmp_path:Path)->Path:
    root=tmp_path/"pt_lib_v20260914_1308"
    for rel in ["pt_lib/__init__.py","pt_lib/api.py","pt_lib/fmi_io.py","pt_lib/config.py","pt_lib/controller.py","pt_lib/lifecycle.py","config/pt_lib_sitl.yaml"]:
        p=root/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_text("# fake external PT_LIB\n")
    (root/"pyproject.toml").write_text('[project]\nname="pt-lib"\nversion="0.8.0"\n')
    (root/"resources/terrain/region_v1").mkdir(parents=True,exist_ok=True); (root/"resources/region_v1.geojson").write_text("region"); (root/"resources/terrain/region_v1/terrain_obstacles.tif").write_bytes(b"terrain")
    return root

class Fake2:
    layer_id=2
    calls=[]
    def preflight(self): return {"ok":True}
    def invoke(self,payload):
        self.calls.append(payload)
        p={n:{"twin_id":n,"state":{},"uncertainty":{},"quality":{"score":1},"validity":{"score":1},"reliability":{"prior":1},"context":dict(payload.get("context") or {}),"timestamp":payload.get("reference_time_s",0),"provenance":{}} for n in ("MESA_NAV","WingMPET","MicroMetTwin")}
        applied=bool(payload.get("training_feedback"))
        routes=((payload.get("context") or {}).get("layer7_feedback") or {}); route=(routes.get("Layer2") or {}) if isinstance(routes,dict) else {}; packet=(route.get("packet") or {}) if isinstance(route,dict) else {}
        no_change=bool(packet.get("NoChangeAcknowledgement") is True) if isinstance(packet,dict) else False
        ingress={"present":bool(packet),"consumed":bool(packet),"delta_x_present":bool(packet.get("DeltaX")) if isinstance(packet,dict) else False,
                 "no_change_acknowledged":no_change,"adaptation_applied":bool(packet) and not no_change}
        return {"ok":True,"packets":p,"layer3_runtime_meta":{"layer7_feedback_ingress":ingress},"training_feedback_applied":applied,"control_effect_applied":applied,"training_feedback_hash":(payload.get("training_feedback") or {}).get("feedback_hash"),"direct_layer_ownership":False}
    def close(self):pass
class Fake3:
    layer_id=3
    def preflight(self): return {"ok":True}
    def apply_layer7_feedback(self,routes): return {"ok":True,"applied_transient":[],"deferred_to_contract_context":[]}
    def invoke(self,payload):
        s={"policy_version":"L3-policy-v1","reference_time_s":payload["request"].get("reference_time_s",0),"std":{},"topology":{},"weights":{},"couplings":{},"center_of_consistency":{},"uncertainty_td":{},"guidance":{},"constraints":{},"context":{},"provenance":{"snapshot_kind":"atomic_layer3_network_snapshot","fast_index":1}}
        return {"ok":True,"result":{"snapshot":s},"snapshot":s}
    def close(self):pass
class Fake4:
    layer_id=4
    def preflight(self):return {"ok":True}
    def invoke(self,payload):return {"ok":True,"result":{"snapshot_version":payload["snapshot"]["policy_version"],"snapshot_hash":"h","candidates":[],"verified_candidates":[],"actuator_command":False,"network_commit":False}}
    def close(self):pass
class Fake7:
    layer_id=7
    def preflight(self):return {"ok":True,"implemented":True,"status":"READY","external_only":True,"registration_descriptor":{"trigger_owner":"TDOC","direct_actuation":False}}
    def invoke(self,payload):
        assert "ExecutionEvidence" in payload and "PredictionEvidence" in payload and "NetworkSnapshot" in payload
        return {"ok":True,"status":"OK","result":{"feedback_layer2":{"DeltaX":{"x":0.1},"nu_new":"v1+1"},"feedback_layer31":{},"feedback_layer32":{},"feedback_layer33":{},"commit":{"status":"TRANSIENT_ONLY"}}}
    def close(self):pass

def make_cfg(tmp_path,l5,pt=None):
    cfg=TDOCConfig.from_yaml(ROOT/"configs/tdoc.yaml")
    # These legacy Layer5/6 contract tests exercise the synchronous adapter route;
    # problem-15 RTMO artifact isolation has dedicated tests and is disabled here.
    cfg.data.setdefault("tdoc",{}).setdefault("rtmo",{}).setdefault("l4_artifacts",{})["fast_path_reuse"]=False
    # These legacy adapter fixtures intentionally emit a reduced pre-formalism
    # Layer6 envelope.  Keep them on the compatibility path; production tdoc.yaml
    # and dedicated formal-contract tests keep require_external_contract=true.
    cfg.data.setdefault("layers",{}).setdefault("layer6",{}).setdefault("online",{})["require_external_contract"]=False
    cfg.data["runtime"]["root"]=str(tmp_path/"runtime")
    fmu=tmp_path/"ardupilot_JSBsim_v20260914_1308/FMU"; fmu.mkdir(parents=True,exist_ok=True)
    for name in ("ArduPilotController.fmu","JSBSimPhoenix.fmu"): (fmu/name).write_bytes(b"fake-fmu")
    b=tmp_path/"boulder_multilayer_map_builder_v20260814_1215"; (b/"region").mkdir(parents=True,exist_ok=True); (b/"outputs/region_v1_map").mkdir(parents=True,exist_ok=True)
    (b/"region/region_v1.geojson").write_text("region"); (b/"outputs/region_v1_map/terrain_obstacles.tif").write_bytes(b"terrain")
    os.environ["TDDT_LAYER5_ROOT"]=str(l5)
    if pt is not None: os.environ["TDDT_PTLIB_ROOT"]=str(pt)
    else: os.environ.pop("TDDT_PTLIB_ROOT",None)
    os.environ["EXT_FMU_SIM_ROOT"]=str(fmu)
    os.environ["TDDT_BOULDER_ROOT"]=str(b)
    return cfg

def clear_layer5_modules():
    for k in list(sys.modules):
        if k=="layer5" or k.startswith("layer5."): sys.modules.pop(k,None)

def test_layer5_exact_root_adapter(tmp_path,monkeypatch):
    clear_layer5_modules(); monkeypatch.delenv("TDDT_LAYER5_ROOT",raising=False)
    l5=make_fake_layer5(tmp_path); cfg=make_cfg(tmp_path,l5)
    a=Layer5Adapter(cfg); pf=a.preflight()
    assert pf["ok"] and pf["version"]=="2026.9.11.1510"
    out=a.invoke({"layer4_result":{"snapshot_version":"v1"},"learning_mode":"OFFLINE_SE","request":{}})
    assert out["ok"] and out["result"]["control_action"]["first_move"][2]==0.55
    assert str(l5) in out["provenance"]["external_root"]
    clear_layer5_modules()

def test_offline_feedback_is_built_from_layer5_and_bypasses_layer6(tmp_path,monkeypatch):
    clear_layer5_modules(); monkeypatch.delenv("TDDT_LAYER5_ROOT",raising=False); monkeypatch.delenv("TDDT_PTLIB_ROOT",raising=False)
    l5=make_fake_layer5(tmp_path); cfg=make_cfg(tmp_path,l5); a5=Layer5Adapter(cfg); a6=Layer6OnlineAdapter(cfg,a5)
    o5=a5.invoke({"layer4_result":{"snapshot_version":"v1"},"learning_mode":"OFFLINE_SE","request":{}})
    fb=a5.offline_training_feedback(o5,version="v1")
    assert fb["route"]=="LAYER2_TRAINING_FEEDBACK"
    assert fb["training_feedback"]["source"]=="Layer5/OFFLINE_SE"
    assert fb["training_feedback"]["destination"]=="Layer2"
    assert fb["training_feedback"]["actuator_settings"]["values"]==[0.1,-0.2,0.55,0.0]
    assert a6.preflight()["offline_in_path"] is False
    try:
        a6.invoke({"layer5_output":o5,"learning_mode":"OFFLINE_SE","version":"v1"})
        assert False, "Layer6 must reject OFFLINE_SE"
    except ValueError as exc:
        assert "online-only" in str(exc).lower()
    clear_layer5_modules()

def test_native_final_without_controller_predictor_never_submits():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter(ready=True,verified=False)
    out=a.invoke(native_payload())
    assert out['status']=='FAILED' and out['route']=='HOLD'
    assert out['actuation_executed'] is False
    assert 'native_execution_model_not_verified' in out['error']
    assert client.sent==[]


def test_tdoc_offline_closes_direct_layer5_to_layer2_loop(tmp_path,monkeypatch):
    clear_layer5_modules(); monkeypatch.delenv("TDDT_LAYER5_ROOT",raising=False); monkeypatch.delenv("TDDT_PTLIB_ROOT",raising=False)
    l5=make_fake_layer5(tmp_path); cfg=make_cfg(tmp_path,l5); f2=Fake2(); f2.calls=[]
    o=TDOCOrchestrator(cfg); o.adapters[2]=f2; o.adapters[3]=Fake3(); o.adapters[4]=Fake4()
    try:r=o.run_cycle({"reference_time_s":1.0,"learning_mode":"OFFLINE_SE","context":{}}).to_dict()
    finally:o.close(); clear_layer5_modules()
    assert r["status"]=="OK" and r["conformance"]["A_offline_loop_closure"] is True
    assert len(f2.calls)==2 and f2.calls[1]["training_feedback"]["source"]=="Layer5/OFFLINE_SE"
    assert r["downstream_layers"]["6"]["status"]=="NOT_IN_OFFLINE_PATH"
    assert r["conformance"]["A_offline_bypasses_layer6"] is True
    events=[]
    for pth in sorted((tmp_path/"runtime/events/invocations/chunks").glob("chunk_*.jsonl.gz")): events.extend(iter_jsonl_auto(pth))
    invokes=[e["destination"] for e in events if e["event_type"]=="Invoke"]
    assert invokes==["Layer2","Layer3","Layer4","Layer5","Layer2"]

def test_native_unverified_final_cannot_trigger_layer7_execution():
    from native_migration_support import native_adapter,native_payload
    from tddt_tdoc.orchestrator import _layer7_trigger_eligibility
    a,client,_=native_adapter()
    output=a.invoke(native_payload())
    assert not output.get('execution_evidence')
    gate=_layer7_trigger_eligibility(layer6_output=output,execution_evidence={},
                                    layer7_preflight={'ok':True},request={})
    assert not gate['realized_execution'] and not gate['eligible']
    assert client.sent==[]


def test_native_hold_observation_requires_no_control_write():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter()
    out=a.invoke(native_payload(final=False))
    assert out['status']=='OBSERVATION_ONLY' and not out['actuation_executed']
    assert out['execution_evidence']['hold_observation_only'] is True
    assert not out['execution_evidence'].get('execution_confirmed',False)
    assert client.sent==[]



def test_native_validation_request_is_diagnostic_only():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter()
    p=native_payload(final=False);p['request']={'online_observation_only':True}
    out=a.invoke(p)
    assert out['status']=='OBSERVATION_ONLY' and not out['actuation_executed']
    assert out['execution_evidence']['validation_only'] is True
    assert client.sent==[]


def test_offline_model_prior_alone_never_authorizes_native_layer7():
    from native_migration_support import native_adapter,native_payload
    a,client,l5=native_adapter(ready=True,verified=False)
    result=a.commission_online_control()
    assert result['ok'] is True and result['actuation_executed'] is False
    online=a.invoke(native_payload())
    assert not online['ok'] and not online['actuation_executed']
    assert 'native_execution_model_not_verified' in online['error']
    assert l5.calls==['tx-1'] and client.sent==[]



def test_unavailable_offline_prior_blocks_native_before_submit():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter(ready=False,verified=False)
    online=a.invoke(native_payload())
    assert online['status']=='FAILED' and not online['actuation_executed']
    assert 'readiness rejected' in online['error']
    assert client.sent==[]



def test_native_hold_never_treats_unavailable_gui_as_observation():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter()
    a._status=lambda **kw: (_ for _ in ()).throw(RuntimeError('GUI unavailable'))
    out=a.invoke(native_payload(final=False))
    assert out['status']=='FAILED' and not out['actuation_executed']
    assert 'GUI unavailable' in out['error']
    assert client.sent==[]



def test_native_metadata_only_snapshot_is_not_measured_telemetry():
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    with pytest.raises(RuntimeError,match='telemetry/forcing not ready'):
        a._ingress_from_status({'ok':True,'status':'READY','mavlink_connected':True})
    assert client.sent==[]


def test_native_session_reset_cancels_latched_transaction_once():
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    a._active_tx='tx-1'
    out=a.reset_online_session('TEST_RESET','episode-2')
    assert out['ok'] and out['session_generation']==1
    assert len(client.cancelled)==1 and client.cancelled[0]['transaction_id']=='tx-1'
    assert client.closed and a._client is None and not a._session_open


def test_native_observation_reuses_gui_client_without_execution():
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    a._status();a._status()
    assert client.snapshot_calls==0  # fixture _status is inert and does not launch anything
    # Real online GUI status polling must keep the service client reusable.
    a._status=lambda **kwargs:a._client.gui_status(**kwargs)
    assert a._status()['plant_time_s']==1. and a._status()['plant_time_s']==1.
    assert client.snapshot_calls==2 and client.sent==[]



def test_native_stale_heartbeat_is_rejected_without_restart():
    from native_migration_support import native_adapter,healthy_status
    a,client,_=native_adapter()
    stale=healthy_status();stale['telemetry_age_s']['HEARTBEAT']=10.
    assert not a._fresh(stale)
    assert a._client is client and client.sent==[]



def test_native_fresh_telemetry_requires_monotonic_and_bounded_age():
    from native_migration_support import native_adapter,healthy_status
    a,client,_=native_adapter()
    healthy=healthy_status();assert a._fresh(healthy)
    bad=healthy_status();bad['telemetry_age_s']['ATTITUDE']=-.01
    assert not a._fresh(bad)
    bad2=healthy_status();bad2['telemetry_age_s']['VFR_HUD']=float('nan')
    assert not a._fresh(bad2)
    assert client.sent==[]


class _NativePhoenixClaimStub:
    """Simulation-scoped claim metadata for tests, NEVER an operational certificate."""
    def claim_limits(self):
        return {
            "vtol_autopilot_controller_validated": False,
            "true_zero_speed_hover_dynamic_validated": False,
            "aerodynamics_full_envelope_validated": False,
            "qlpv_nonzero_correction_ready": False,
            "real_aircraft_flight_validated": False,
        }

    def claim_scope(self):
        return {
            "schema": "Layer5-Phoenix-claim-scope-v1",
            "status": "CLAIM_LIMITED_TO_SITL",
            "scope_ceiling": "SITL_SIMULATION_ONLY",
            "beyond_sitl_claims_allowed": False,
            "commissioning_may_upgrade_claim_limits": False,
            "claim_limits": self.claim_limits(),
        }


def test_layer5_online_control_readiness_fails_closed_on_unvalidated_phoenix(tmp_path,monkeypatch):
    clear_layer5_modules(); monkeypatch.delenv("TDDT_LAYER5_ROOT",raising=False)
    l5=make_fake_layer5(tmp_path); cfg=make_cfg(tmp_path,l5); a=Layer5Adapter(cfg); a._ensure()
    assert cfg.get("layers.layer6.online.transport") == "Layer6Client_GUI_MAVLink"
    # No valid Offline model prior: mock this dependency without changing any
    # actual handoff, KStore, runtime certificate or FlightStack configuration.
    monkeypatch.setattr(Layer5Adapter, "ensure_native_control_binding",
                        lambda self: {"ok":False,"reason":"verified_offline_model_prior_required"})
    class Phoenix(_NativePhoenixClaimStub):
        L4_ACTION_ORDER=["delta_thrust_norm","delta_lift_norm","delta_pitch_rad","delta_yaw_rate_rad_s","delta_airspeed_m_s"]
        LOGICAL_ORDER=["aileron_domain","elevator_domain","throttle_domain","rudder_domain"]
        def status(self): return {"mapping_validated":False,"environment_authorized":"FORWARD_SITL"}
        def correction_validation_gates(self): return {"mixer":False,"servo_dynamics":False,"esc_mapping":False,"transition":False,"nominal_control":False,"qlpv_nonzero_correction":False}
    a.engine.phoenix=Phoenix()
    r=a.online_control_readiness()
    assert r['ok'] is False and r['status']=='NOT_READY'
    assert r['readiness_scope']=='NATIVE_INTERFACE_AND_ACCEPTED_MODEL_PRIOR'
    assert r['reason']=='verified_offline_model_prior_required'
    assert r['mapping_status']['mapping_validated'] is False
    assert r['nominal_control_ready'] is False
    assert r['qlpv_nonzero_correction_ready'] is False
    assert r['execution_mapping_certified'] is False
    assert r['mission_executability_authority'] is False
    assert r['current_phase_release_authority'] is False
    assert r['phase_execution_capability']['actuation_authority'] is False
    assert 'native_pilot_controller_prediction_unavailable' in r['flight_release_blockers']
    assert r['claim_scope']['beyond_sitl_claims_allowed'] is False
    assert r['claim_limits']['real_aircraft_flight_validated'] is False
    clear_layer5_modules()


def test_native_correction_gates_cannot_authorize_pilot_controller(tmp_path,monkeypatch):
    """Even with a test-only accepted model prior, native flight stays blocked."""
    clear_layer5_modules(); monkeypatch.delenv("TDDT_LAYER5_ROOT",raising=False)
    l5=make_fake_layer5(tmp_path); cfg=make_cfg(tmp_path,l5); a=Layer5Adapter(cfg); a._ensure()
    assert cfg.get("layers.layer6.online.transport") == "Layer6Client_GUI_MAVLink"
    # Only the model-prior/interface gate is simulated here. It cannot
    # substitute for native controller prediction or physical verification.
    monkeypatch.setattr(Layer5Adapter, "ensure_native_control_binding",
                        lambda self: {"ok":True,"source":"TEST_ONLY_ACCEPTED_MODEL_PRIOR",
                                      "actuation_authority":False,"execution_mapping_certified":False})
    class Phoenix(_NativePhoenixClaimStub):
        L4_ACTION_ORDER=["a","b","c","d","e"]
        LOGICAL_ORDER=["aileron_domain","elevator_domain","throttle_domain","rudder_domain"]
        def status(self): return {"mapping_validated":True,"environment_authorized":"FORWARD_SITL"}
        def correction_validation_gates(self): return {"mixer":True,"servo_dynamics":False,"esc_mapping":False,"transition":False,"nominal_control":True,"qlpv_nonzero_correction":False}
    a.engine.phoenix=Phoenix()
    r=a.online_control_readiness()
    assert r['ok'] is True and r['status']=='READY'   # Accepted prior / interface only.
    assert r['readiness_scope']=='NATIVE_INTERFACE_AND_ACCEPTED_MODEL_PRIOR'
    assert r['native_model_prior']['source']=='TEST_ONLY_ACCEPTED_MODEL_PRIOR'
    assert r['mapping_status']['mapping_validated'] is True
    assert r['nominal_control_ready'] is False
    assert r['qlpv_nonzero_correction_ready'] is False
    assert r['execution_mapping_certified'] is False
    assert r['mission_executability_authority'] is False
    assert r['current_phase_release_authority'] is False
    assert r['phase_execution_capability']['actuation_authority'] is False
    assert r['flight_release_blockers']==['native_pilot_controller_prediction_unavailable']
    assert r['claim_scope']['scope_ceiling']=='SITL_SIMULATION_ONLY'
    assert r['claim_scope']['beyond_sitl_claims_allowed'] is False
    assert r['claim_limits']['vtol_autopilot_controller_validated'] is False
    clear_layer5_modules()


def test_native_hold_evidence_is_non_actuating_even_with_validation_request():
    from native_migration_support import native_adapter,native_payload
    a,client,_=native_adapter()
    p=native_payload(final=False);p['request']={'online_observation_only':True}
    out=a.invoke(p)
    ex=out['execution_evidence']
    assert ex['validation_only'] is True and ex['hold_observation_only'] is True
    assert not ex.get('execution_confirmed',False)
    assert not out['actuation_executed'] and client.sent==[]



def test_full_fidelity_bridge_does_not_treat_layer4_abstract_actions_as_qlpv_correction(tmp_path,monkeypatch):
    clear_layer5_modules(); monkeypatch.delenv("TDDT_LAYER5_ROOT",raising=False)
    l5=make_fake_layer5(tmp_path); cfg=make_cfg(tmp_path,l5); a=Layer5Adapter(cfg); a._ensure()
    seen={}
    class Phoenix:
        LOGICAL_ORDER=['a','e','t','r']; PHYSICAL_ACTUATORS=['le','re','lm','rm']
        def status(self): return {'nominal_control_ready':True}
        def bind_candidate(self,c): return [[0.1,0.2,0.3,0.0]], {'ok':True,'source':'tracker'}
        def physical_trajectory_feasibility(self,seq,correction_requested=False): seen['correction']=correction_requested; return {'ok':True,'reasons':[]}
        def mix_physical(self,u): return {'ok':True,'physical':[0.1,-0.1,0.3,0.3]}
    a.engine.phoenix=Phoenix()
    out=a.full_fidelity_action_bridge({'actions':[[9,8,7,6,5]]})
    assert out['ok'] is True and seen['correction'] is False
    clear_layer5_modules()

def test_native_commission_is_read_only_model_prior_check():
    from native_migration_support import native_adapter
    a,client,_=native_adapter(ready=True)
    out=a.commission_online_control()
    assert out['status']=='READY' and out['binding']['status']=='TEST_PRIOR_ONLY'
    assert out['actuation_executed'] is False and client.sent==[]
    a2,client2,_=native_adapter(ready=False)
    other=a2.commission_online_control()
    assert not other['ok'] and not other['actuation_executed'] and client2.sent==[]



def test_native_transport_ready_does_not_override_control_prediction_blocker():
    from native_migration_support import native_adapter
    a,client,_=native_adapter(ready=True)
    a.prepare_online_transport_session=lambda:{'ok':True,'ready':True,'status':'READY','startup_actuation':False}
    result=a.prepare_online_control_session()
    assert not result['ok'] and result['status']=='NOT_READY'
    assert result['transport_ready'] is True
    assert result['reason']=='native_pilot_controller_prediction_unavailable'
    assert result['actuation_executed'] is False and client.sent==[]


def test_native_session_reset_preserves_cwd_and_closes_only_client():
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    cwd=Path.cwd()
    a.reset_online_session('no_external_logs')
    assert Path.cwd()==cwd
    assert client.closed and not client.sent
    assert a._client is None



def test_native_confirmation_correlates_transaction_clock_and_pilot_command():
    import time
    from native_migration_support import native_adapter,healthy_status
    a,client,_=native_adapter()
    sent=time.monotonic()-.001
    before={'manual_sent_monotonic':sent-.01}
    cmd={'u_cmd':[0.,0.,0.,0.],'execution_intent':{'requested_pilot_mode':'QHOVER','discrete':[]}}
    actual=healthy_status('tx-1',sent)
    assert a._confirmed(actual,before,cmd,'tx-1')
    assert not a._confirmed(actual,before,cmd,'tx-OTHER')
    before_future={'manual_sent_monotonic':sent+1}
    assert not a._confirmed(actual,before_future,cmd,'tx-1')
    actual['applied_pilot_command']['throttle']=.2
    assert not a._confirmed(actual,before,cmd,'tx-1')
    assert client.sent==[]



def test_native_layer6_does_not_expose_retired_fmi_probe():
    from native_migration_support import native_adapter
    a,client,_=native_adapter()
    assert not hasattr(a,'_ptlib_probe')
    assert not hasattr(a,'_make_ptlib')
    assert a.SOURCE=='Layer6/GUI/MAVLink' and client.sent==[]


def test_native_ingress_keeps_navigation_and_truth_authorities_separate():
    from native_migration_support import native_adapter,healthy_status
    a,client,_=native_adapter()
    result=a._ingress_from_status(healthy_status())
    assert result['ok'] and result['navigation_authority']=='MESA_STD'
    assert result['truth_injection_to_mesa'] is False
    assert result['source']=='Layer6/GUI/MAVLink'
    assert result['not_field_evidence'] is True and client.sent==[]



def test_layer7_not_triggered_on_unverified_native_final():
    from native_migration_support import native_adapter,native_payload
    from tddt_tdoc.orchestrator import _layer7_trigger_eligibility
    a,client,_=native_adapter()
    out=a.invoke(native_payload())
    gate=_layer7_trigger_eligibility(layer6_output=out,execution_evidence={},layer7_preflight={'ok':True},request={})
    assert not gate['eligible'] and not gate['realized_execution']
    assert client.sent==[]


def test_native_diagnostic_hold_cannot_claim_layer7_commit():
    from native_migration_support import native_adapter,native_payload
    from tddt_tdoc.orchestrator import _layer7_trigger_eligibility
    a,client,_=native_adapter()
    p=native_payload(final=False)
    out=a.invoke(p)
    gate=_layer7_trigger_eligibility(layer6_output=out,execution_evidence=out['execution_evidence'],
                                    layer7_preflight={'ok':True},request={})
    assert not gate['realized_execution'] and not gate['eligible']
    assert client.sent==[]


def test_offline_wingmpet_certificate_is_not_native_flight_proof(tmp_path,monkeypatch):
    # This old fixture was constructed without __init__ or a transport config.
    from types import SimpleNamespace
    clear_layer5_modules();monkeypatch.delenv('TDDT_LAYER5_ROOT',raising=False)
    l5=make_fake_layer5(tmp_path);cfg=make_cfg(tmp_path,l5);a=Layer5Adapter(cfg);a._ensure()
    a.config=SimpleNamespace(get=lambda key,default=None:
        'OFFLINE_SE' if key=='layers.layer6.online.transport' else default)
    class Phoenix:elevon_max=0.53
    a.engine.phoenix=Phoenix()
    a._verification_model_certificate={
        'ok':True,'simulation_only':True,
        'mixer':{'elevon_matrix_rad_per_norm':[[.02,.02],[-.02,.02]],'trim_rad':[-.08,-.08]},
        'trim':{'pitch_request_norm':0.,'throttle_request_norm':.3},
        'throttle_path':{'qualified_delta_from_operating_request_norm':[-.1,.1],
                         'trim_norm':.3,'gain_motor_norm_per_pilot_delta':1.}}
    out=a._wingmpet_verification_mix([0.,0.,.3,0.])
    assert out['ok'] and out['physical']==[-.08,-.08,.3,.3]
    bad=a._wingmpet_verification_mix([0.,0.,.41,0.])
    assert not bad['ok'] and bad['reason']=='wingmpet_verification_throttle_outside_qualified_neighborhood'
    clear_layer5_modules()

