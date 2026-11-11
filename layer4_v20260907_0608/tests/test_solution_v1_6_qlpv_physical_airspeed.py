from __future__ import annotations

from types import SimpleNamespace
import numpy as np

from layer4 import Layer4Config, AtomicSnapshot
from layer4.state import StateCodec
from layer4.qlpv import QLPVModel
from layer4.uq import SquareRootCubatureUQ
from layer4.engine import Layer4Engine
from layer4.vtol import VTOLProfile


def _cfg(root):
    return Layer4Config.from_yaml(root / "configs/configuration_boulder.yaml")


def _snapshot(mode="transition_forward", airspeed=0.0):
    return AtomicSnapshot(
        version="p0-6-test", reference_time_s=0.0, hash="p0-6-test-hash",
        std={"representations":{"WingMPET":{"canonical_state":{"airspeed_m_s":float(airspeed)},"validity":1.0}}},
        center_of_consistency={}, mismatch=0.0, weights={}, couplings={}, topology={"active_edges":[]},
        consistency_margin=1.0, uncertainty_td={}, guidance={}, constraints={},
        context={"flight_mode":mode,"altitude_agl_m":100.0}, stability={"stable":True}, provenance={}, slow_policy={},
    )


def test_qlpv_step_projects_scalar_physical_airspeed_nonnegative(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    x=np.zeros(codec.dim); x[codec.index["airspeed_m_s"]]=0.1
    a=np.zeros(model.action_dim); a[-1]=-2.0
    d=np.zeros(model.disturbance_dim)
    y,meta=model.step(x,a,d,{}, {}, {"active_edges":[]})
    assert y[codec.index["airspeed_m_s"]] == 0.0
    proj=meta["physical_state_projection"]
    assert proj["active"] is True
    assert proj["fields"]["airspeed_m_s"]["raw"] < 0.0
    assert proj["fields"]["airspeed_m_s"]["projected"] == 0.0


def test_qlpv_rollout_never_emits_negative_scalar_airspeed(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    H=cfg.horizon; x=np.zeros(codec.dim); x[codec.index["airspeed_m_s"]]=0.0
    U=np.zeros((H,model.action_dim)); U[:,-1]=-2.0
    D=np.zeros((H,model.disturbance_dim)); W=[{} for _ in range(H)]; K=[{} for _ in range(H)]; G={"active_edges":[]}
    X,_=model.rollout(x,U,D,W,K,G)
    assert np.all(X[:,codec.index["airspeed_m_s"]] >= 0.0)


def test_affine_uq_mean_uses_same_physical_airspeed_projection(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec); uq=SquareRootCubatureUQ(cfg,codec,model)
    x=np.zeros(codec.dim); x[codec.index["airspeed_m_s"]]=0.1
    C=np.eye(codec.dim)*1e-6; a=np.zeros(model.action_dim); a[-1]=-2.0; d=np.zeros(model.disturbance_dim)
    m,_=uq.propagate_step(x,C,a,d,{}, {}, {"active_edges":[]})
    assert m[codec.index["airspeed_m_s"]] == 0.0


def test_transition_forward_action_projection_disallows_negative_delta_airspeed_below_target(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    eng=Layer4Engine.__new__(Layer4Engine); eng.model=model; eng.codec=codec; eng.feas=SimpleNamespace(vtol=VTOLProfile(cfg))
    x=np.zeros(codec.dim); x[codec.index["airspeed_m_s"]]=0.0
    lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(cfg.horizon,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(cfg.horizon,1))
    lo2,hi2,meta=eng._physical_action_bounds(_snapshot("transition_forward",0.0),x,lo,hi,{"empty_intersection":False})
    aidx=model.meta["action_order"].index("delta_airspeed_m_s")
    assert np.all(lo2[:,aidx] >= 0.0)
    assert np.all(hi2[:,aidx] >= lo2[:,aidx])
    assert meta["physical_airspeed_action_constraint"]["applied"] is True
    U=np.tile(np.asarray([0,0,0,0,-2.0],float),(cfg.horizon,1))
    Up=np.minimum(np.maximum(U,lo2),hi2)
    assert np.all(Up[:,aidx] >= 0.0)


def test_non_transition_phases_keep_existing_negative_delta_airspeed_authority(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    eng=Layer4Engine.__new__(Layer4Engine); eng.model=model; eng.codec=codec; eng.feas=SimpleNamespace(vtol=VTOLProfile(cfg))
    x=np.zeros(codec.dim); x[codec.index["airspeed_m_s"]]=13.0
    lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(cfg.horizon,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(cfg.horizon,1))
    lo2,_,meta=eng._physical_action_bounds(_snapshot("cruise",13.0),x,lo,hi,{"empty_intersection":False})
    aidx=model.meta["action_order"].index("delta_airspeed_m_s")
    assert np.allclose(lo2[:,aidx],-2.0)
    assert meta["physical_airspeed_action_constraint"]["applied"] is False


def test_transition_forward_above_terminal_target_does_not_forbid_deceleration(root):
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    eng=Layer4Engine.__new__(Layer4Engine); eng.model=model; eng.codec=codec; eng.feas=SimpleNamespace(vtol=VTOLProfile(cfg))
    x=np.zeros(codec.dim); x[codec.index["airspeed_m_s"]]=4.0
    lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(cfg.horizon,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(cfg.horizon,1))
    lo2,_,meta=eng._physical_action_bounds(_snapshot("transition_forward",4.0),x,lo,hi,{"empty_intersection":False})
    aidx=model.meta["action_order"].index("delta_airspeed_m_s")
    assert np.allclose(lo2[:,aidx],-2.0)
    assert meta["physical_airspeed_action_constraint"]["applied"] is False


def test_hover_delta_thrust_has_source_derived_vertical_prediction_authority(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    assert model.vertical_collective_model is not None
    assert model.meta["runtime_vertical_collective_model"]["full_fidelity_still_required"] is True
    model.set_context({"flight_mode":"hover"})
    x=np.zeros(codec.dim)
    x[codec.index["Vg_2"]]=-3.3; x[codec.index["Va_2"]]=-3.3
    d=np.zeros(model.disturbance_dim); hold=np.zeros(model.action_dim)
    brake=hold.copy(); brake[model.meta["action_order"].index("delta_thrust_norm")]=-0.1
    y_hold,_=model.step(x,hold,d,{}, {}, {"active_edges":[]})
    y_brake,_=model.step(x,brake,d,{}, {}, {"active_edges":[]})
    assert y_brake[codec.index["Vg_2"]] > y_hold[codec.index["Vg_2"]]
    assert y_brake[codec.index["mesa_position_ned_m_2"]] > y_hold[codec.index["mesa_position_ned_m_2"]]


def test_hover_collective_delta_is_bounded_to_physical_motor_domain(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    eng=Layer4Engine.__new__(Layer4Engine); eng.model=model; eng.codec=codec; eng.feas=SimpleNamespace(vtol=VTOLProfile(cfg))
    x=np.zeros(codec.dim); lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(cfg.horizon,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(cfg.horizon,1))
    lo2,hi2,meta=eng._physical_action_bounds(_snapshot("hover",0.0),x,lo,hi,{"empty_intersection":False})
    tidx=model.meta["action_order"].index("delta_thrust_norm")
    equilibrium=model.vertical_collective_model["hover_equilibrium_motor_norm"]
    assert np.all(lo2[:,tidx] >= -equilibrium-1e-12)
    assert np.all(hi2[:,tidx] <= 1.0-equilibrium+1e-12)
    assert meta["physical_hover_collective_action_constraint"]["applied"] is True


def test_online_vertical_delta_intersects_layer5_commissioned_envelope(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    eng=Layer4Engine.__new__(Layer4Engine); eng.model=model; eng.codec=codec; eng.feas=SimpleNamespace(vtol=VTOLProfile(cfg))
    bounds=[{"lower":0.0,"upper":0.063} for _ in range(cfg.horizon)]
    eng.runtime_action_envelope={
        "schema":"Layer5-to-Layer4-commissioned-action-envelope-v1","ok":True,"applied":True,
        "phase":"vertical_takeoff","action":"delta_thrust_norm","per_step_delta_bounds":bounds,
        "contract_sha256":"test-contract","not_actuator_command":True,"actuation_authority":False,
        "layer4_full_fidelity_required":True,"layer5_final_required":True,
        "layer6_executor_gate_required":True,"ardupilot_mixer_authoritative":True,
        "direct_jsbsim_actuation":False,
    }
    x=np.zeros(codec.dim); lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(cfg.horizon,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(cfg.horizon,1))
    lo2,hi2,meta=eng._physical_action_bounds(_snapshot("vertical_takeoff",0.0),x,lo,hi,{"empty_intersection":False})
    tidx=model.meta["action_order"].index("delta_thrust_norm")
    assert np.allclose(lo2[:,tidx],0.0)
    assert np.allclose(hi2[:,tidx],0.063)
    assert meta["empty_intersection"] is False
    assert meta["layer5_commissioned_action_constraint"]["applied"] is True


def test_invalid_online_layer5_envelope_fails_closed(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    eng=Layer4Engine.__new__(Layer4Engine); eng.model=model; eng.codec=codec; eng.feas=SimpleNamespace(vtol=VTOLProfile(cfg))
    eng.runtime_action_envelope={"ok":False,"reason":"certificate_missing"}
    x=np.zeros(codec.dim); lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(cfg.horizon,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(cfg.horizon,1))
    _,_,meta=eng._physical_action_bounds(_snapshot("vertical_takeoff",0.0),x,lo,hi,{"empty_intersection":False})
    assert meta["empty_intersection"] is True
    assert meta["layer5_commissioned_action_constraint"]["reason"]=="invalid_layer5_action_envelope"


def test_hover_collective_feedforward_does_not_invalidate_terminal_error_certificate(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    model.set_context({"flight_mode":"hover"})
    from layer4.tube import PeriodicTubeMPC
    tube=PeriodicTubeMPC(cfg,model)
    cert=tube.terminal_certificate([{} for _ in range(cfg.horizon)],[{} for _ in range(cfg.horizon)],{"active_edges":[]})
    assert cert["certified"] is True
    assert cert["spectral_radius_per_step"] < 1.0


def test_vertical_takeoff_delta_thrust_has_source_derived_prediction_authority(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    model.set_context({"flight_mode":"vertical_takeoff"})
    x=np.zeros(codec.dim); d=np.zeros(model.disturbance_dim); hold=np.zeros(model.action_dim)
    climb=hold.copy(); climb[model.meta["action_order"].index("delta_thrust_norm")]=0.1
    y_hold,m_hold=model.step(x,hold,d,{}, {}, {"active_edges":[]})
    y_climb,m_climb=model.step(x,climb,d,{}, {}, {"active_edges":[]})
    assert y_climb[codec.index["Vg_2"]] < y_hold[codec.index["Vg_2"]]
    assert y_climb[codec.index["mesa_position_ned_m_2"]] < y_hold[codec.index["mesa_position_ned_m_2"]]
    assert m_hold["nominal_vertical_collective"]["applied"] is True
    assert m_climb["nominal_vertical_collective"]["full_fidelity_still_required"] is True


def test_vertical_takeoff_collective_delta_uses_physical_motor_domain(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); codec=StateCodec(cfg.package_root/cfg.get("qlpv.state_schema_file")); model=QLPVModel(cfg,codec)
    eng=Layer4Engine.__new__(Layer4Engine); eng.model=model; eng.codec=codec; eng.feas=SimpleNamespace(vtol=VTOLProfile(cfg))
    x=np.zeros(codec.dim); lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(cfg.horizon,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(cfg.horizon,1))
    lo2,hi2,meta=eng._physical_action_bounds(_snapshot("vertical_takeoff",0.0),x,lo,hi,{"empty_intersection":False})
    tidx=model.meta["action_order"].index("delta_thrust_norm"); equilibrium=model.vertical_collective_model["hover_equilibrium_motor_norm"]
    assert np.all(lo2[:,tidx] >= -equilibrium-1e-12)
    assert np.all(hi2[:,tidx] <= 1.0-equilibrium+1e-12)
    assert meta["physical_hover_collective_action_constraint"]["phase"] == "vertical_takeoff"


def test_hover_reference_seed_inverts_source_model_without_bypassing_gates(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); eng=Layer4Engine(cfg)
    snap=_snapshot("hover",0.0)
    snap.context.update({"altitude_agl_m":13.5,"phase_target_agl_m":12.24})
    eng.model.set_context(snap.context)
    x0=eng.codec.encode(snap); x0[eng.codec.index["mesa_position_ned_m_2"]]=-1800.0; x0[eng.codec.index["Vg_2"]]=-2.7
    H=cfg.horizon
    case={"D":np.zeros((H,eng.model.disturbance_dim)),"W":[{} for _ in range(H)],
          "K":[{} for _ in range(H)],"topology":{"active_edges":[]}}
    lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(H,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(H,1))
    lo,hi,_=eng._physical_action_bounds(snap,x0,lo,hi,{"empty_intersection":False})
    U,meta=eng._vertical_reference_seed(snap,x0,case,lo,hi)
    tidx=eng.model.meta["action_order"].index("delta_thrust_norm")
    assert meta["available"] is True
    assert meta["not_actuator_command"] is True
    assert meta["full_fidelity_still_required"] is True
    assert np.all(U[:,tidx] >= lo[:,tidx]) and np.all(U[:,tidx] <= hi[:,tidx])
    assert abs(meta["predicted_agl_m"][-1]-12.24) < 1e-9


def test_hover_reference_seed_is_not_injected_into_takeoff(root, monkeypatch):
    twins=root.parent / "twins_layer2_v20260820_1233"
    monkeypatch.setenv("TDDT_TWINS_ROOT",str(twins))
    cfg=_cfg(root); eng=Layer4Engine(cfg); snap=_snapshot("vertical_takeoff",0.0)
    snap.context.update({"altitude_agl_m":2.0,"phase_target_agl_m":12.24})
    x0=eng.codec.encode(snap); H=cfg.horizon
    case={"D":np.zeros((H,eng.model.disturbance_dim)),"W":[{} for _ in range(H)],
          "K":[{} for _ in range(H)],"topology":{"active_edges":[]}}
    lo=np.tile(np.asarray(cfg.get("qlpv.action_lower"),float),(H,1)); hi=np.tile(np.asarray(cfg.get("qlpv.action_upper"),float),(H,1))
    U,meta=eng._vertical_reference_seed(snap,x0,case,lo,hi)
    assert U is None
    assert meta["reason"] == "not_source_modelled_hover"
