from __future__ import annotations
import sys
import importlib.util
import math
import threading
from pathlib import Path
from typing import Any
from ..unit_adapter import UnitAdapterBinding
from ..storage import CompressedPersistentWorkspace

class FullFidelityPolicyError(RuntimeError):
    """Retain the rejected model policy evidence without granting execution."""
    def __init__(self, reason, *, step_index, provider_result, replay_evidence=None):
        super().__init__(f"full_fidelity_action_policy_failed:{reason}")
        self.policy_failure={"step_index":int(step_index),"reason":reason,
                             "provider_result":provider_result,"actuation_authority":False}
        if replay_evidence is not None:
            self.policy_failure["replay_evidence"]=replay_evidence


class Layer3Adapter:
    layer_id=3
    def __init__(self,config,unit_adapter:UnitAdapterBinding|None=None,layer2_adapter=None):
        self.config=config; self.root=config.path_value("TDDT_LAYER3_ROOT"); self.engine=None; self.runtime_bridge=None; self.layer3_config=None
        self.unit_adapter=unit_adapter or UnitAdapterBinding(config); self.binding={}; self.layer2_adapter=layer2_adapter; self.native_workspace=None
        self._pending_twin_plant_residual=None; self._last_assimilated_route_hash=None
        self.native_snapshot_sealer=None
        self._boulder_reader_cache=None
        self._boulder_reader_lock=threading.RLock()

    def _boulder_reader(self,cfg):
        """Reuse only immutable map/CRP source lookups, never simulation state.

        Current and isolated runtimes still own separate engines/FMUs. The reader
        contains terrain minimum and (pixel,time-bin) assignment lookups only.
        Changed source paths/files invalidate the reader before the next replay.
        """
        from layer3.boulder_runtime import BoulderRuntimeAdapter
        from ..model_compatibility import _stat
        paths=[Path(cfg.map_root)/name for name in
            ('terrain_obstacles.tif','climate_contexts.tif','magnetic_anomaly.tif')]
        paths.extend(Path(cfg.crp_root)/name for name in
            ('crp_cluster_map.tif','crp_cell_assignments.csv.gz'))
        paths.append(Path(cfg.boulder_root)/'map_mosaic_query.py')
        def identity():
            return tuple((str(p.resolve()),_stat(p) if p.exists() else None) for p in paths)
        with self._boulder_reader_lock:
            key=identity()
            cached=self._boulder_reader_cache
            if cached is not None and cached[0]==key:return cached[1]
            reader=BoulderRuntimeAdapter(cfg)
            if identity()!=key:raise ValueError('Boulder source files changed during reader preparation')
            self._boulder_reader_cache=(key,reader)
            return reader
    def preflight(self):
        req=["src/layer3/__init__.py","src/layer3/tdoc_adapter.py","configs/configuration_boulder.yaml"]
        miss=req if self.root is None else [x for x in req if not (self.root/x).exists()]
        ua=self.unit_adapter.preflight()
        modules={name: bool(importlib.util.find_spec(name)) for name in ("numpy","yaml","pandas","rasterio","pyproj","shapely","plotly")}
        missing_modules=[name for name,ok in modules.items() if not ok]
        ok=bool(self.root and self.root.is_dir() and not miss and ua.get("ok") and not missing_modules)
        return {"ok":ok,"root":str(self.root) if self.root else None,"missing":miss,
                "python_modules":modules,"missing_python_modules":missing_modules,
                "mode":"TDOC-owned two-phase Layer3: plan_network -> runtime couplings using existing Layer2 instances -> Layer3Engine.step",
                "unit_adapter":ua,"alignment_owner":"Layer3.1","adapter_binding_owner":"TDOC/RuntimeScheduler"}
    def _ensure(self):
        if self.engine is not None:return
        if not self.preflight()["ok"]: raise RuntimeError("Layer3/ISO-adapter preflight failed")
        if str(self.root/"src") not in sys.path: sys.path.insert(0,str(self.root/"src"))
        from layer3 import Layer3Config, Layer3Engine, Layer3FMURuntime
        from layer3.contracts import SignalContractRegistry
        from layer3.boulder_runtime import BoulderRuntimeAdapter
        cfg_path=self.root/str(self.config.get("layers.layer3.config","configs/configuration_boulder.yaml"))
        cfg=Layer3Config.from_yaml(cfg_path,package_root=self.root)
        external_path_binding=self.config.bind_layer3_external_paths(cfg,required=True)
        # TDOC is authoritative for which external packages and ISO adapter are admitted.
        # Layer 3.1 remains authoritative for the actual normalization operation.
        self.binding=self.unit_adapter.bind_layer3_config(cfg)
        self.binding["external_paths_from_tdoc_environment"] = external_path_binding
        # Runtime artifacts remain owned by TDOC transaction workspace, not the external Layer-3 package.
        persistent=self.config.runtime_root/"layer3"/"native"; persistent.mkdir(parents=True,exist_ok=True)
        self.native_workspace=CompressedPersistentWorkspace(
            persistent,prefix="tdoc-layer3-",working_parent=self.config.runtime_root/".transient")
        rr=self.native_workspace.working_root
        cfg.paths.ss_kstore=str(rr/"ss_kstore.jsonl"); cfg.paths.setd_kstore=str(rr/"setd_kstore.jsonl"); cfg.paths.provenance_log=str(rr/"provenance.jsonl"); cfg.paths.tdoc_health=str(rr/"tdoc_health.json"); cfg.paths.snapshots_dir=str(rr/"snapshots"); cfg.paths.versions_dir=str(rr/"versions")
        self.layer3_config=cfg; self.engine=Layer3Engine(cfg)
        # Build only the Layer-3 coupling executor object, without calling its constructor.
        # The actual MESA/Wing/Climate instances were already created/invoked by TDOC's Layer2Adapter.
        if self.layer2_adapter is not None:
            self.layer2_adapter._ensure()
            rt=Layer3FMURuntime.__new__(Layer3FMURuntime)
            rt.config=cfg; rt.engine=self.engine; rt.dependencies=self.engine.dependencies; rt.tdoc=self.engine.tdoc
            rt.mesa=self.layer2_adapter.mesa; rt.wing=self.layer2_adapter.wing; rt.climate_twin=self.layer2_adapter.climate_twin; rt._estimate_climate=self.layer2_adapter._estimate
            rt.contracts=SignalContractRegistry(cfg,rt.tdoc)
            rt.boulder=self._boulder_reader(cfg) if bool(cfg.runtime.get("use_boulder_runtime_sampling",True)) else None
            self.runtime_bridge=rt
        self.native_workspace.sync()
    @staticmethod
    def _runtime_gates_and_constraints(req:dict[str,Any], packets:dict[str,Any], *, prior_snapshot:dict[str,Any]|None=None):
        """Derive fail-closed runtime edge gates and explicit Error residual evidence.

        An edge is not allowed to remain CoC-active when its destination operation cannot be
        executed with the available context.  Wing->MESA Error residual uses the published
        Twin validity deficit as a dimensionless diagnostic residual when no explicit
        feasibility scalar is present; provenance is carried in the request context.
        """
        gates=dict(req.get("safety_gates") or {}); constraints=dict(req.get("constraints") or {})
        ctx=dict(req.get("context") or {})
        if not any(k in ctx for k in ("geodetic_origin","origin_wgs84","mesa_geodetic_origin")):
            gates.setdefault("MESA_NAV->MicroMetTwin",False)
        wing=(packets.get("WingMPET") or {}) if isinstance(packets,dict) else {}
        vraw=wing.get("validity")
        valid=(vraw.get("score") if isinstance(vraw,dict) else vraw)
        if valid is None and prior_snapshot:
            valid=((((prior_snapshot.get("std") or {}).get("representations") or {}).get("WingMPET") or {}).get("validity"))
        try:
            residual=max(0.0,min(1.0,1.0-float(valid)))
            er=dict(constraints.get("edge_residuals") or {}); er.setdefault("WingMPET->MESA_NAV",residual); constraints["edge_residuals"]=er
            ctx.setdefault("tdoc_edge_residual_provenance",{})["WingMPET->MESA_NAV"]="1 - WingMPET validity score (dimensionless Error-coupling evidence)"
        except Exception:
            gates.setdefault("WingMPET->MESA_NAV",False)
        return gates,constraints,ctx

    @staticmethod
    def _rebind_mesa_after_layer3_couplings(packets:dict[str,Any], *, reference_time_s:float,
                                             alignment:dict[str,Any]|None, learning_mode:Any,
                                             layer2_adapter=None)->dict[str,Any]:
        """Restore TDOC mission-time authority after Layer-3 coupling FMU updates.

        Layer-3 environmental anchor operations may evaluate MESA again and merge the
        resulting packet into the already TDOC-aligned base packet.  The external
        merge correctly preserves the updated state, but its destination packet also
        carries MESA's local FMI timestamp.  For simulation-backed OFFLINE/ONLINE
        cycles this would silently undo the Layer-2 mission-time binding immediately
        before Layer-3 freshness/CoC evaluation.

        TDOC therefore re-applies the same simulation-only time authority *after*
        coupling execution and before Layer3Engine.step.  No state, validity, quality,
        actuator, or FIELD evidence is modified.
        """
        out=dict(packets or {})
        mesa=out.get("MESA_NAV")
        if not isinstance(mesa,dict) or layer2_adapter is None:
            return out
        mode=str(learning_mode or "").upper()
        if mode not in {"OFFLINE_SE","ONLINE_JSBSIM"} or not bool((alignment or {}).get("applied")):
            return out
        try:
            ref=float(reference_time_s)
            cur=float(mesa.get("timestamp"))
        except (TypeError,ValueError):
            cur=None
        if cur is not None and abs(cur-ref)<=1e-12:
            return out
        rebound=layer2_adapter._bind_mesa_simulation_packet_to_mission_time(
            mesa,reference_time_s=ref,alignment=alignment,learning_mode=mode)
        prov=dict(rebound.get("provenance") or {})
        prov["tdoc_post_layer3_coupling_time_rebind"]={
            "schema":"TDOC-PostLayer3CouplingMissionTimeRebind-v1",
            "simulation_only":True,
            "not_field_evidence":True,
            "reason":"Layer3 coupling destination evaluation may restore local FMI timestamp",
            "mission_reference_time_s":ref,
        }
        rebound["provenance"]=prov
        out["MESA_NAV"]=rebound
        return out

    @staticmethod
    def _rebind_wing_after_layer3_couplings(packets:dict[str,Any], *, reference_time_s:float,
                                             learning_mode:Any, layer2_adapter=None)->dict[str,Any]:
        """Restore mission-time authority when Wing coupling FMU calls overwrite timestamp."""
        out=dict(packets or {})
        wing=out.get("WingMPET")
        if not isinstance(wing,dict) or layer2_adapter is None:
            return out
        mode=str(learning_mode or "").upper()
        if mode not in {"OFFLINE_SE","ONLINE_JSBSIM"}:
            return out
        try:
            ref=float(reference_time_s); cur=float(wing.get("timestamp"))
        except (TypeError,ValueError):
            cur=None
        if cur is not None and abs(cur-ref)<=1e-12:
            return out
        rebound=layer2_adapter._bind_wing_simulation_packet_to_mission_time(
            wing,reference_time_s=ref,learning_mode=mode)
        prov=dict(rebound.get("provenance") or {})
        prov["tdoc_post_layer3_coupling_wing_time_rebind"]={
            "schema":"TDOC-PostLayer3CouplingWingMissionTimeRebind-v1",
            "simulation_only":True,"not_field_evidence":True,
            "reason":"Layer3 Wing coupling destination evaluation may restore local FMI timestamp",
            "mission_reference_time_s":ref,
        }
        rebound["provenance"]=prov; out["WingMPET"]=rebound
        return out

    def _augment_operational_agl(self, context:dict[str,Any], packets:dict[str,Any])->dict[str,Any]:
        """Bind AGL from operational MESA navigation state plus the canonical DEM.

        P0-7 safety invariant: simulator/PT_LIB truth is never used as navigation
        authority here.  If MESA position or DEM coverage cannot be resolved, the
        context records AGL_UNAVAILABLE and Layer4 must fail closed.
        """
        out=dict(context or {})
        authority={
            "schema":"TDOC-Operational-AGL-v1",
            "authority_class":"OPERATIONAL",
            "navigation_source":"MESA_NAV",
            "terrain_source":"TDDT_MAP_ROOT/terrain_obstacles.tif",
            "simulator_truth_used":False,
            "valid":False,
        }
        try:
            mesa=(packets or {}).get("MESA_NAV") or {}
            st=mesa.get("state") or {}
            if all(k in st for k in ("position_n","position_e","position_d")):
                north=float(st["position_n"]); east=float(st["position_e"]); down=float(st["position_d"])
            else:
                cs=(mesa.get("canonical_state") or {})
                pos=cs.get("position_ned_m")
                if not isinstance(pos,(list,tuple)) or len(pos)<3:
                    raise ValueError("mesa_position_unavailable")
                north,east,down=map(float,pos[:3])
            if not all(math.isfinite(v) for v in (north,east,down)):
                raise ValueError("mesa_position_non_finite")
            map_root=self.config.path_value("TDDT_MAP_ROOT")
            dem=(map_root/"terrain_obstacles.tif") if map_root else None
            if dem is None or not dem.exists():
                raise FileNotFoundError("canonical_dem_unavailable")
            import rasterio
            from tddt_tdoc.terrain import sample_bilinear
            with rasterio.open(dem) as ds:
                if not (ds.bounds.left <= east <= ds.bounds.right and ds.bounds.bottom <= north <= ds.bounds.top):
                    raise ValueError("mesa_position_outside_dem_coverage")
                terrain=sample_bilinear(ds,east,north)
                crs=str(ds.crs) if ds.crs else None
            altitude_msl=-down
            agl=altitude_msl-terrain
            if not math.isfinite(agl):
                raise ValueError("derived_agl_non_finite")
            authority.update({
                "valid":True,"reason":"MESA_STD_PLUS_DEM","map_crs":crs,
                "terrain_interpolation":"BILINEAR_PIXEL_CENTRE",
                "position_n_m":north,"position_e_m":east,"position_d_m":down,
                "altitude_msl_m":altitude_msl,"terrain_elevation_m":terrain,"altitude_agl_m":agl,
            })
            out["altitude_agl_m"]=agl
            out["terrain_elevation_m"]=terrain
            out["agl_available"]=True
            out["agl_authority"]=authority
            return out
        except Exception as e:
            authority["reason"]=str(e) or e.__class__.__name__
            out.pop("altitude_agl_m",None)
            out["agl_available"]=False
            out["agl_authority"]=authority
            return out

    def invoke(self,payload:dict[str,Any])->dict[str,Any]:
        self._ensure()
        out=self._invoke_materialized(payload)
        out.setdefault("storage",{})["native_gzip_sync"]=self.native_workspace.sync()
        return out

    def _invoke_materialized(self,payload:dict[str,Any])->dict[str,Any]:
        self._ensure(); req=payload.get("request") or {}; packets=payload["packets"]
        ref=float(req.get("reference_time_s",0.0)); effective_gates,effective_constraints,edge_ctx=self._runtime_gates_and_constraints(req,packets)
        context={**(req.get("context") or {}),**edge_ctx,"stage":req.get("stage","cruise"),"regime_id":req.get("regime_id"),"reference_time_s":ref}
        runtime_couplings={"enabled":False,"execution_order":[],"reason":"TDOC coupling bridge unavailable"}; anchors={}; final_packets=packets; plan=None
        if self.runtime_bridge is not None:
            from layer3 import RuntimeRequest
            allowed=set(RuntimeRequest.__dataclass_fields__)
            rmap={k:v for k,v in req.items() if k in allowed}
            rmap.setdefault("reference_time_s",ref); rmap.setdefault("regime_id",req.get("regime_id")); rmap.setdefault("stage",req.get("stage","cruise")); rmap.setdefault("context",req.get("context") or {})
            rr=RuntimeRequest.from_mapping(rmap)
            meta=dict(payload.get("layer3_runtime_meta") or {})
            plan=self.engine.plan_network(packets,reference_time_s=ref,context=context,safety_gates=effective_gates,constraints=effective_constraints,risks=req.get("risks") or {},objectives=req.get("objectives") or {})
            final_packets,context,anchors,runtime_couplings=self.runtime_bridge.execute_couplings(rr,packets,context,meta,plan)
            mode=str(req.get("learning_mode") or (req.get("context") or {}).get("training_mode") or (req.get("context") or {}).get("learning_mode") or "").upper()
            final_packets=self._rebind_mesa_after_layer3_couplings(
                final_packets,reference_time_s=ref,alignment=meta.get("mesa_simulation_state_alignment"),
                learning_mode=mode,layer2_adapter=self.layer2_adapter)
            final_packets=self._rebind_wing_after_layer3_couplings(
                final_packets,reference_time_s=ref,learning_mode=mode,layer2_adapter=self.layer2_adapter)
            runtime_couplings=dict(runtime_couplings or {})
            runtime_couplings["tdoc_post_coupling_mesa_time_authority"]={
                "schema":"TDOC-PostLayer3CouplingMissionTimeRebind-v1",
                "applied":bool(((final_packets.get("MESA_NAV") or {}).get("provenance") or {}).get("tdoc_post_layer3_coupling_time_rebind")),
                "mission_reference_time_s":ref,
                "simulation_only":True,
                "not_field_evidence":True,
            }
            runtime_couplings["tdoc_post_coupling_wing_time_authority"]={
                "schema":"TDOC-PostLayer3CouplingWingMissionTimeRebind-v1",
                "applied":bool(((final_packets.get("WingMPET") or {}).get("provenance") or {}).get("tdoc_post_layer3_coupling_wing_time_rebind")),
                "mission_reference_time_s":ref,"simulation_only":True,"not_field_evidence":True,
            }
        context=self._augment_operational_agl(context,final_packets)
        runtime_couplings=dict(runtime_couplings or {})
        runtime_couplings["tdoc_operational_agl_authority"]=dict(context.get("agl_authority") or {})
        result=self.engine.step(final_packets,reference_time_s=ref,context=context,regime_id=req.get("regime_id"),stage=req.get("stage","cruise"),prediction=req.get("prediction") or {},action=req.get("action") or {},safety_gates=effective_gates,constraints=effective_constraints,risks=req.get("risks") or {},objectives=req.get("objectives") or {},anchor_results=anchors,runtime_couplings=runtime_couplings,network_plan=plan)
        d=result.to_dict(); snap=d["snapshot"]
        assimilation=self._assimilate_twin_plant_residual(snap,reference_time_s=ref)
        if assimilation.get("applied"):
            # Keep the persisted Fast record and the atomic snapshot semantically
            # identical.  This changes uncertainty only; MESA state remains the
            # navigation authority and no actuator field is introduced.
            fast=d.get("fast_record") or {}
            fast["uncertainty_td"]=dict(snap.get("uncertainty_td") or {})
            fast["twin_plant_residual_assimilation"]=dict(assimilation)
        snap.setdefault("provenance",{})["invocation_owner"]="TDOC/RuntimeScheduler"
        snap["provenance"]["unit_adapter_binding"]={"root":self.binding.get("root"),"version":self.binding.get("version"),"owner":"TDOC/RuntimeScheduler","conversion_owner":"Layer3.1/TDOCAdapter"}
        sealer=getattr(self,'native_snapshot_sealer',None)
        if sealer is not None:
            snap=sealer(snap,final_packets,req)
            d['snapshot']=snap
        health=dict(snap.get("tdoc_health") or {})
        return {"ok":True,"result":d,"snapshot":snap,"tdoc_health":health,"runtime_couplings":runtime_couplings,"packets_after_coupling":final_packets,
                "twin_plant_residual_assimilation":assimilation,
                "unit_adapter_binding":{**self.binding,"health_ok":bool(health.get("ok",True)),"conversions":health.get("conversions",0),"failures":health.get("failures",0)}}

    def _assimilate_twin_plant_residual(self,snapshot:dict[str,Any],*,reference_time_s:float)->dict[str,Any]:
        """Inflate L3/L4 prediction uncertainty from the previous realized plant residual.

        The update is deliberately one-way and covariance-only: it cannot replace
        MESA observations, alter a candidate, assert FINAL, or command an actuator.
        """
        item=self._pending_twin_plant_residual
        if not isinstance(item,dict):
            return {"applied":False,"reason":"no_new_transaction_matched_layer7_residual"}
        route_hash=str(item.get("route_hash") or "")
        if not route_hash or route_hash==self._last_assimilated_route_hash:
            return {"applied":False,"reason":"layer7_residual_already_consumed","route_hash":route_hash or None}
        packet=item.get("packet") or {}; state=packet.get("StateResidualEvidence") or {}
        corrected=packet.get("CorrectedUncertainty") or {}
        try: confidence=max(0.0,min(1.0,float(packet.get("rho_attr",0.0))))
        except (TypeError,ValueError): confidence=0.0
        utd=dict(snapshot.get("uncertainty_td") or {}); ordering=list(utd.get("ordering") or [])
        covariance=[list(row) for row in (utd.get("covariance") or [])]
        n=len(ordering)
        if n==0 or len(covariance)!=n or any(len(row)!=n for row in covariance):
            return {"applied":False,"reason":"layer3_uncertainty_covariance_contract_unavailable","route_hash":route_hash}
        increments={}; mapping={"velocity_n_m_s":"Vg_N","velocity_e_m_s":"Vg_E","velocity_d_m_s":"Vg_D"}
        for source,target in mapping.items():
            try: residual=float(state[source])
            except (KeyError,TypeError,ValueError): continue
            if not math.isfinite(residual) or target not in ordering: continue
            i=ordering.index(target); inc=confidence*residual*residual
            covariance[i][i]=max(0.0,float(covariance[i][i]))+inc; increments[target]=inc
        try: airspeed_variance=float(corrected.get("airspeed_m_s"))
        except (TypeError,ValueError): airspeed_variance=math.nan
        if math.isfinite(airspeed_variance) and airspeed_variance>=0.0:
            for target in ("Va_N","Va_E","Va_D"):
                if target not in ordering: continue
                i=ordering.index(target); before=max(0.0,float(covariance[i][i]))
                covariance[i][i]=max(before,airspeed_variance)
                increments[target]=covariance[i][i]-before
        if not increments:
            return {"applied":False,"reason":"no_compatible_residual_uncertainty_fields","route_hash":route_hash}
        utd["covariance"]=covariance; utd["std"]=[math.sqrt(max(0.0,float(covariance[i][i]))) for i in range(n)]
        snapshot["uncertainty_td"]=utd
        evidence={"schema":"TDOC-TwinPlant-ResidualAssimilation-v1","applied":True,
                  "source_transaction_id":item.get("transaction_id"),"route_hash":route_hash,
                  "base_version":item.get("base_version"),"target_reference_time_s":float(reference_time_s),
                  "attribution_confidence":confidence,"covariance_diagonal_increments":increments,
                  "state_residual_evidence":{str(k):float(v) for k,v in state.items()
                                             if isinstance(v,(int,float)) and math.isfinite(float(v))},
                  "corrected_prediction_diagnostic":dict(packet.get("CorrectedPrediction") or {}),
                  "state_authority":"MESA_STD_UNCHANGED","actuator_authority":False,
                  "direct_jsbsim_state_or_actuator_write":False}
        snapshot.setdefault("guidance",{})["twin_plant_residual_assimilation"]=evidence
        snapshot.setdefault("provenance",{})["twin_plant_residual_assimilation"]=evidence
        self._last_assimilated_route_hash=route_hash
        return evidence


    @staticmethod
    def _validate_full_fidelity_physical_actions(trajectory, physical_actions, bridge_record):
        """Validate the TDOC-owned Phoenix action bridge before isolated replay.

        This gate is intentionally mode-neutral: OFFLINE_SE and ONLINE_JSBSIM use the
        same Layer-4 verification contract, while PT_LIB/Layer-6 remain outside this
        isolated verification path.
        """
        import math
        traj=list(trajectory or [])
        acts=list(physical_actions or [])
        bridge=dict(bridge_record or {})
        horizon=max(0,len(traj)-1)
        reasons=[]
        if not bridge.get("ok"):
            reasons.append("phoenix_bridge_not_ok")
        if horizon <= 0:
            reasons.append("empty_verification_horizon")
        if len(acts) < horizon:
            reasons.append(f"physical_action_horizon_short:{len(acts)}<{horizon}")
        required=("left_motor_cmd","right_motor_cmd","left_elevon_rad","right_elevon_rad")
        checked=[]
        for i,a in enumerate(acts[:horizon]):
            if not isinstance(a,dict):
                reasons.append(f"physical_action_{i}_not_mapping"); continue
            row={}
            for k in required:
                try:
                    v=float(a[k])
                    if not math.isfinite(v): raise ValueError("non_finite")
                    row[k]=v
                except Exception:
                    reasons.append(f"physical_action_{i}_{k}_invalid")
            if len(row)==4:
                if not (0.0 <= row["left_motor_cmd"] <= 1.0 and 0.0 <= row["right_motor_cmd"] <= 1.0):
                    reasons.append(f"physical_action_{i}_motor_out_of_range")
                # Phoenix source-identified elevon joint limit is about +/-0.53 rad;
                # allow a tiny numerical tolerance but never infer/clip a command.
                if abs(row["left_elevon_rad"]) > 0.531 or abs(row["right_elevon_rad"]) > 0.531:
                    reasons.append(f"physical_action_{i}_elevon_out_of_range")
                checked.append(row)
        return {"ok":not reasons,"horizon":horizon,"physical_actions":checked,"reasons":reasons,
                "schema":"TDOC-FullFidelityPhysicalActionContract-v1"}

    @staticmethod
    def _wing27_physical_step(wing, state, action, *, dt, wind_ned, air_density,
                              ground_supported=False, context_id=None, stage_id=None):
        """Execute a numerically qualified WingMPET op27 interval.

        WingMPET's Phoenix motor constants are O(0.02 s), while TDOC's Fast cycle can
        be 1 s.  A single 1-s Euler-like formula evaluation is outside the model's
        integration scale and can create an artificial divergence.  Preserve the
        requested interval but integrate it with <=10-ms physical substeps, which is
        also the IMU cadence in the authoritative Phoenix profile.
        """
        import math
        total=max(1e-6,float(dt)); max_step=0.01
        n=max(1,int(math.ceil(total/max_step))); h=total/n
        current={k:float(v) for k,v in state.items()}; trace=[]; all_feasible=True; last={}
        initial_state=dict(current)
        initial_relative_airspeed=math.sqrt((current["vn"]-float(wind_ned[0]))**2+(current["ve"]-float(wind_ned[1]))**2+(current["vd"]-float(wind_ned[2]))**2)
        for sub in range(n):
            inp={
                "dt":h,
                "pn":current["pn"],"pe":current["pe"],"pd":current["pd"],
                "vn":current["vn"],"ve":current["ve"],"vd":current["vd"],
                "qw":current["qw"],"qx":current["qx"],"qy":current["qy"],"qz":current["qz"],
                "p_rate":current["p_rate"],"q_rate":current["q_rate"],"r_rate":current["r_rate"],
                "omega_left":current["omega_left"],"omega_right":current["omega_right"],"soc":current["soc"],
                "left_motor_cmd":float(action["left_motor_cmd"]),"right_motor_cmd":float(action["right_motor_cmd"]),
                "left_elevon_rad":float(action["left_elevon_rad"]),"right_elevon_rad":float(action["right_elevon_rad"]),
                "wind_n":float(wind_ned[0]),"wind_e":float(wind_ned[1]),"wind_d":float(wind_ned[2]),
                "air_density":float(air_density),
            }
            supported=ground_supported
            operation_code=35 if supported else 27
            if supported: inp["ground_supported"]=1.0
            packet=wing.evaluate_packet(operation_code,inputs=inp,require_all_inputs=True,context_id=context_id,stage_id=stage_id,
                                        source="TDOC/Layer4FullFidelity/physical_action",communication_step=h)
            pdict=packet.to_dict() if hasattr(packet,"to_dict") else dict(packet); out=dict(pdict.get("state") or {}); last=pdict
            current={
                "pn":float(out["next_pn"]),"pe":float(out["next_pe"]),"pd":float(out["next_pd"]),
                "vn":float(out["next_vn"]),"ve":float(out["next_ve"]),"vd":float(out["next_vd"]),
                "qw":float(out["next_qw"]),"qx":float(out["next_qx"]),"qy":float(out["next_qy"]),"qz":float(out["next_qz"]),
                "p_rate":float(out["next_p_rate"]),"q_rate":float(out["next_q_rate"]),"r_rate":float(out["next_r_rate"]),
                "omega_left":float(out["next_omega_left"]),"omega_right":float(out["next_omega_right"]),"soc":float(out["next_soc"]),
            }
            if not all(math.isfinite(v) for v in current.values()):
                raise RuntimeError("WingMPET op27 produced non-finite physical state")
            feasible=bool(float(out.get("vtol_feasible",0.0))>=0.5); all_feasible=all_feasible and feasible
            if sub in (0,n-1): trace.append({"substep":sub+1,"dt_s":h,"vtol_feasible":feasible,
                                              "airspeed_m_s":float(out.get("airspeed",0.0) or 0.0)})
        validity=last.get("validity") or {}; quality=last.get("quality") or {}; out=dict(last.get("state") or {})
        return current,{"operation_code":operation_code,"action_consumed":True,"interval_s":total,"substep_s":h,"substeps":n,
                        "initial_state":initial_state,"wind_ned_m_s":[float(x) for x in wind_ned],"initial_relative_airspeed_m_s":initial_relative_airspeed,
                        "substep_trace":trace,"outputs":out,"validity":validity,"quality":quality,
                        "vtol_feasible":all_feasible,
                        "source":("WingMPET/op35-ground-supported-substepped" if ground_supported else "WingMPET/op27-substepped"),
                        "ground_supported":bool(ground_supported),"direct_pwm":False,"operational_actuation":False,
                        "ground_contact_dynamics_modeled":False,
                        "ground_support_scope":"feasibility_assertion_only" if ground_supported else None}

    @staticmethod
    def _initial_ground_support_evidence(*, mode:str, phase:str, agl_m:float,
                                         agl_ready:bool, physical_state:dict[str,Any])->dict[str,Any]:
        """Classify the held RunIC spool interval without confusing wind with motion.

        Before release, TDOC's canonical RunIC state deliberately represents ground
        velocity as ``air velocity + wind``.  Its NED velocity norm is therefore not a
        ground-contact measurement.  The explicit held state, operational (non-truth)
        AGL, and air-relative speed are the compatible evidence for the one pre-release
        WingMPET ground-supported interval.
        """
        import math
        held=physical_state.get("released") is False
        released=physical_state.get("released") is True
        try:
            airspeed=float(physical_state["airspeed_m_s"])
        except Exception:
            try:
                av=physical_state["air_velocity_ned_m_s"]
                airspeed=math.sqrt(sum(float(av[i])**2 for i in range(3)))
            except Exception:
                airspeed=float("inf")
        try:
            groundspeed=math.sqrt(sum(float(physical_state[k])**2 for k in ("vn","ve","vd")))
        except Exception:
            groundspeed=float("inf")
        # Held RunIC Vg includes wind by construction, whereas released Vg is the
        # live plant motion.  Select the matching observable; neither release state
        # alone nor airspeed alone is accepted as evidence of contact.
        motion_speed=airspeed if held else groundspeed if released else float("inf")
        checks={
            "online_mode":str(mode).upper()=="ONLINE_JSBSIM",
            "vertical_takeoff_phase":str(phase)=="vertical_takeoff",
            "operational_agl_valid":bool(agl_ready),
            "near_ground":bool(agl_ready and math.isfinite(float(agl_m)) and 0.0 <= float(agl_m) <= 1.0),
            "release_state_known":bool(held or released),
            "state_appropriate_motion_stationary":bool(math.isfinite(motion_speed) and motion_speed <= 1.0),
        }
        return {
            "asserted":all(checks.values()),
            "basis":("operational_AGL_plus_held_RunIC_air_relative_speed" if held
                     else "operational_AGL_plus_released_live_ground_speed" if released
                     else "operational_AGL_plus_unknown_release_state"),
            "altitude_agl_m":float(agl_m) if agl_ready else None,
            "agl_authority_valid":bool(agl_ready),
            "simulator_truth_used":False,
            "air_relative_speed_m_s":airspeed if math.isfinite(airspeed) else None,
            "live_ground_speed_m_s":groundspeed if math.isfinite(groundspeed) else None,
            "motion_speed_selected_m_s":motion_speed if math.isfinite(motion_speed) else None,
            "runic_released":physical_state.get("released"),
            "phase":str(phase),
            "checks":checks,
            "step_limited":True,
            "control_release_is_not_contact_evidence":True,
        }

    @staticmethod
    def _safe_workspace_token(value:Any, *, fallback:str) -> str:
        import re, hashlib
        raw=str(value if value not in (None,"") else fallback)
        clean=re.sub(r"[^A-Za-z0-9_.-]+","_",raw).strip("._-") or fallback
        if len(clean)>80:
            clean=clean[:56]+"-"+hashlib.sha256(raw.encode()).hexdigest()[:16]
        return clean

    def _full_fidelity_workspace_identity(self,candidate:dict[str,Any],request:dict[str,Any])->dict[str,str]:
        """Return the immutable namespace for one TDOC-owned FF replay.

        Candidate ids are only unique inside one Layer-4 invocation, so they must
        never own a persistent workspace by themselves.  The namespace binds the
        evidence to run/episode/cycle/phase/candidate and intentionally ignores
        all legacy ``layer4_verification/Cxxxx`` directories.
        """
        ctx=dict(request.get("context") or {})
        rc=dict(ctx.get("rtmo_run_context") or ctx.get("tdoc_run_context") or {})
        run=rc.get("run_id") or ctx.get("tdoc_run_id") or ctx.get("offline_run_id")
        episode=rc.get("episode_id") or request.get("online_episode_id") or ctx.get("offline_episode_id")
        cycle=rc.get("cycle_id") or request.get("online_cycle_in_episode") or ctx.get("offline_cycle_id")
        phase=rc.get("phase_id") or request.get("stage") or ctx.get("flight_phase") or "unknown_phase"
        tx=str(request.get("transaction_id") or "")
        if run in (None,""):
            # A transaction-scoped fallback is deliberately unique rather than
            # falling back to a shared candidate-only directory.
            run="transaction-"+(__import__("hashlib").sha256(tx.encode()).hexdigest()[:16] if tx else "unknown")
        if episode in (None,""): episode="offline-se" if str(request.get("learning_mode") or "").upper()=="OFFLINE_SE" else "unspecified-episode"
        if cycle in (None,""):
            import re
            m=re.search(r"(?:^|[-_])(?:c|cycle)?(\d{1,8})$",tx,re.I)
            cycle=int(m.group(1)) if m else (__import__("hashlib").sha256(tx.encode()).hexdigest()[:12] if tx else "unknown")
        try: cycle_token=f"cycle_{int(cycle):06d}"
        except Exception: cycle_token="cycle_"+self._safe_workspace_token(cycle,fallback="unknown")
        return {
            "run_id":"run_"+self._safe_workspace_token(run,fallback="unknown"),
            "episode_id":"episode_"+self._safe_workspace_token(episode,fallback="unknown"),
            "cycle_id":cycle_token,
            "phase_id":"phase_"+self._safe_workspace_token(phase,fallback="unknown"),
            "candidate_id":"candidate_"+self._safe_workspace_token(candidate.get("candidate_id"),fallback="candidate"),
            "scenario_id":self._safe_workspace_token(candidate.get("scenario_id"),fallback="scenario"),
            "transaction_id":tx,
        }

    @staticmethod
    def _online_ff_initial_velocity(physical_state:dict[str,Any], scenario_wind_ned:list[float])->tuple[dict[str,float],dict[str,Any]]:
        """Preserve inertial velocity when applying an FF wind scenario.

        Ground velocity is a continuous plant state.  Replacing it with
        ``Va_live + W_scenario`` would instantaneously accelerate the isolated
        WingMPET replay whenever its uncertainty scenario differs from the live
        JSBSim wind.  Keep realized Vg and let WingMPET form the scenario-relative
        air velocity from ``Vg_live - W_scenario`` during op27 instead.
        """
        ground={k:float(physical_state[k]) for k in ("vn","ve","vd")}
        wind=[float(v) for v in scenario_wind_ned]
        if len(wind)!=3 or not all(math.isfinite(v) for v in (*ground.values(),*wind)):
            raise ValueError("online_ff_initial_velocity_non_finite")
        live_air=physical_state.get("air_velocity_ned_m_s")
        if not isinstance(live_air,(list,tuple)) or len(live_air)!=3:
            raise ValueError("online_ff_live_air_velocity_contract_missing")
        live_air=[float(v) for v in live_air]
        if not all(math.isfinite(v) for v in live_air):
            raise ValueError("online_ff_live_air_velocity_non_finite")
        scenario_air=[ground["vn"]-wind[0],ground["ve"]-wind[1],ground["vd"]-wind[2]]
        return ground,{
            "inertial_ground_velocity_preserved":True,
            "ground_velocity_source":("online_execution_state/Layer6_GUI_MAVLink"
                if str(physical_state.get("kinematics_source") or "").startswith("Layer6Client.GUI.")
                else "online_execution_state/PT_LIB_JSBSim"),
            "live_air_velocity_ned_m_s":live_air,
            "scenario_wind_ned_m_s":wind,
            "scenario_air_velocity_ned_m_s":scenario_air,
            "wind_application":"WingMPET op27 forcing; no instantaneous inertial-state substitution",
            "scenario_wind_substitution":False,
        }

    def _full_fidelity_persistent_root(self, identity:dict[str,str]):
        return (self.config.runtime_root/"layer4_verification"/"runs"/identity["run_id"]/
                "episodes"/identity["episode_id"]/identity["cycle_id"]/
                identity["phase_id"]/identity["candidate_id"]/"native")

    def full_fidelity_replay(self,candidate:dict[str,Any],snapshot:dict[str,Any],request:dict[str,Any],codec,dt:float,
                             scenario=None,physical_actions=None,bridge_record=None,
                             physical_step_s:float|None=None,
                             physical_action_provider=None)->list[dict[str,Any]]:
        """TDOC-owned full-fidelity replay used by Layer 4 verification.

        A fresh Layer-2 session and a fresh Layer-3 engine are created by TDOC.  The
        validated Phoenix physical trajectory is consumed step-by-step by WingMPET
        op27 before each Layer-3 replay step.  Layer 4 never reverse-invokes Layer 3
        or Layer 2, and this method never invokes Layer 6/PT_LIB.
        """
        from ..native_ground_policy import require_native_contacts
        require_native_contacts(self.config,request)
        self._ensure()
        if self.layer2_adapter is None: raise RuntimeError("Layer2 adapter is required for TDOC full-fidelity replay")
        import copy, math
        from tddt_tdoc.phoenix_frames import fallback_attitude, phase_from_request
        from layer3 import Layer3Config, Layer3Engine, Layer3FMURuntime, RuntimeRequest
        from layer3.contracts import SignalContractRegistry
        from layer3.boulder_runtime import BoulderRuntimeAdapter
        # Retained original IMU records belong to the observed-state packet.
        # Forecasts use the same observed anchor and all semantic state/TEP data;
        # copying the transport history into every hypothetical row adds no input.
        from ..online_imu import compact_replay_context
        request={**request,'context':compact_replay_context(request.get('context') or {})}
        traj=list(candidate.get("trajectory") or []); actions=list(candidate.get("actions") or [])
        contract=self._validate_full_fidelity_physical_actions(traj,physical_actions,bridge_record)
        if not contract["ok"]:
            raise RuntimeError("invalid_full_fidelity_physical_action_contract:"+";".join(contract["reasons"]))
        phys_actions=contract["physical_actions"]
        if not traj: raise RuntimeError("empty_candidate_trajectory")
        replay_dt=float(dt if physical_step_s is None else physical_step_s)
        if not math.isfinite(replay_dt) or replay_dt<=0.0 or replay_dt>float(dt)+1e-12:
            raise RuntimeError("invalid_full_fidelity_execution_segment")
        replay_lane=("COMMIT_SEGMENT" if replay_dt < float(dt)-1e-12 else "FORECAST_HORIZON")

        # Initialize replay from the same physical state used by the OFFLINE plant when
        # available.  Falling back to canonical Layer-4 state is safe for ONLINE/static
        # validation, but must not synthesize an all-zero motor transient in OFFLINE_SE.
        x0=traj[0]
        p0=[float(x0[codec.index[f"mesa_position_ned_m_{i}"]]) for i in range(3)]
        vg0=[float(x0[codec.index[f"Vg_{i}"]]) for i in range(3)]
        heading=float(x0[codec.index["air_heading_rad"]]); gamma=float(x0[codec.index["flightpath_angle_rad"]])
        phase0=phase_from_request(request); att0=fallback_attitude(phase0,yaw_rad=heading,kinematic_pitch_rad=gamma)
        phys={"pn":p0[0],"pe":p0[1],"pd":p0[2],"vn":vg0[0],"ve":vg0[1],"vd":vg0[2],
              "qw":att0["qw"],"qx":att0["qx"],"qy":att0["qy"],"qz":att0["qz"],
              "p_rate":0.0,"q_rate":0.0,"r_rate":0.0,"omega_left":0.0,"omega_right":0.0,"soc":1.0}
        ctx0=(request.get("context") or {})
        mode0=str(request.get("learning_mode") or "").upper()
        observed_climate_projection=ctx0.get("native_climate_time_projection")
        forecast_binding=None
        if observed_climate_projection is not None:
            # An observed source clock is never advanced into a future observation.
            # Only this TDOC-owned isolated prediction session may derive model
            # query times from its immutable accepted anchor.
            if (mode0!="ONLINE_JSBSIM" or
                self.config.get("layers.layer6.online.execution_profile")!="closed_loop_sitl" or
                self.config.get("layers.layer6.online.transport")!="Layer6Client_GUI_MAVLink"):
                raise ValueError("isolated Native prediction clock requires explicit closed_loop_sitl")
            from ..native_time import validate_native_time
            from layer6.binding import context_guard
            observed_climate_projection=copy.deepcopy(validate_native_time(observed_climate_projection))
            observed_time=(request.get("climate") or {}).get("time")
            if (not isinstance(observed_time,dict) or
                observed_time.get("value")!=observed_climate_projection["dataset_timestamp"] or
                observed_time.get("time_reference")!=observed_climate_projection["time_reference"] or
                observed_time.get("time_binding")!=observed_climate_projection):
                raise ValueError("isolated forecast requires the unchanged observed climate time envelope")
            forecast_binding=context_guard(observed_climate_projection["binding_path"]).binding
        if mode0=="ONLINE_JSBSIM":
            # RTMO keeps simulator truth off the MESA observation channel and names
            # it execution-only evidence.  It is still the authoritative initial
            # condition for this isolated WingMPET replay.  Preserve that distinction
            # while avoiding the zero-RPM trajectory fallback.
            ops=(ctx0.get("online_execution_state") or ctx0.get("online_physical_state") or {})
            transport_source=("Layer6_GUI_MAVLink"
                if self.config.get("layers.layer6.online.transport")=="Layer6Client_GUI_MAVLink"
                else "PT_LIB_JSBSim")
            ops_source=("online_execution_state/" if ctx0.get("online_execution_state")
                        else "online_physical_state/")+transport_source
        else:
            ops=(ctx0.get("offline_physical_state") or {})
            ops_source="offline_physical_state"
        physical_initialization={"source":"layer4_trajectory_fallback","ok":True}
        required_phys=("pn","pe","pd","vn","ve","vd","qw","qx","qy","qz","p_rate","q_rate","r_rate","omega_left","omega_right","soc")
        if isinstance(ops,dict) and ops:
            # A partial physical state must never shadow the authoritative OFFLINE plant state.
            # Only accept it when the complete op27 state contract is present.
            if all(k in ops for k in required_phys):
                try:
                    candidate_phys={k:float(ops[k]) for k in required_phys}
                    if not all(math.isfinite(v) for v in candidate_phys.values()): raise ValueError("non_finite")
                    if mode0=="ONLINE_JSBSIM" and isinstance(ops.get("air_velocity_ned_m_s"),(list,tuple)) and len(ops.get("air_velocity_ned_m_s"))==3:
                        # A changed wind scenario changes air-relative velocity, not the
                        # realized inertial velocity state at the replay boundary.
                        # WingMPET receives W_scenario explicitly on every op27 step.
                        w_init=[float(x0[codec.index[f"W_{i}"]]) for i in range(3)]
                        ground_velocity,init_detail=self._online_ff_initial_velocity(ops,w_init)
                        candidate_phys.update(ground_velocity)
                        init_detail={"air_relative_initialization":False,"wind0_ned_m_s":w_init,**init_detail}
                    else:
                        init_detail={"air_relative_initialization":False}
                    phys.update(candidate_phys); physical_initialization={"source":ops_source,"ok":True,"simulation_only":True,"not_field_evidence":True,**init_detail}
                except Exception as exc:
                    raise RuntimeError(f"offline_physical_state_initialization_failed:{exc!r}") from exc
            elif mode0=="OFFLINE_SE" and not isinstance(ctx0.get("offline_plant_state"),dict):
                raise RuntimeError("partial_offline_physical_state_without_offline_plant_state")
            else:
                raise RuntimeError("partial_online_physical_state_contract")
        if physical_initialization["source"]=="layer4_trajectory_fallback" and isinstance(ctx0.get("offline_plant_state"),dict):
            ps=ctx0.get("offline_plant_state") or {}
            try:
                avx=float(ps.get("air_vx",0.0)); avy=float(ps.get("air_vy",0.0)); avh=float(ps.get("air_vh",0.0)); hz=math.hypot(avx,avy)
                yaw=math.atan2(avx,avy) if hz>1e-9 else heading; kinematic_pitch=math.atan2(avh,max(hz,1e-9))
                from ..phoenix_frames import authoritative_or_fallback_attitude
                att=authoritative_or_fallback_attitude(ps,phase0,yaw_rad=yaw,kinematic_pitch_rad=kinematic_pitch)
                w0=[float(x0[codec.index[f"W_{i}"]]) for i in range(3)]
                candidate_phys={"pn":float(ps.get("y",phys["pn"])),"pe":float(ps.get("x",phys["pe"])),"pd":-float(ps.get("h",-phys["pd"])),
                                "vn":avy+w0[0],"ve":avx+w0[1],"vd":-avh+w0[2],"qw":att["qw"],"qx":att["qx"],"qy":att["qy"],"qz":att["qz"],
                                "p_rate":float(ps.get("p_rate",0.0)),"q_rate":float(ps.get("q_rate",0.0)),"r_rate":float(ps.get("r_rate",0.0)),
                                "omega_left":float(ps.get("omega_left",0.0)),"omega_right":float(ps.get("omega_right",0.0)),"soc":float(ps.get("soc",1.0))}
                if not all(math.isfinite(v) for v in candidate_phys.values()): raise ValueError("non_finite")
                phys.update(candidate_phys); physical_initialization={"source":"offline_plant_state_air_plus_wind+Phoenix_phase_attitude","ok":True,"wind0_ned_m_s":w0,"phase":phase0,"attitude_basis":att["basis"]}
            except Exception as exc:
                raise RuntimeError(f"offline_plant_state_initialization_failed:{exc!r}") from exc
        ff_initial_ned={k:float(phys[k]) for k in ("pn","pe","pd")}
        ff_map_origin_ned={"pn":float(p0[0]),"pe":float(p0[1]),"pd":float(p0[2])}
        base_snapshot_ctx=dict(snapshot.get("context") or {})
        base_agl_authority=dict(base_snapshot_ctx.get("agl_authority") or {})
        try:
            base_agl_m=float(base_snapshot_ctx.get("altitude_agl_m"))
            base_agl_ready=bool(math.isfinite(base_agl_m)
                                and base_agl_authority.get("valid") is True
                                and base_agl_authority.get("simulator_truth_used") is not True)
        except Exception:
            base_agl_m=0.0; base_agl_ready=False
        # ``released`` is a control-ownership flag, not contact evidence by itself.
        # In conjunction with operational AGL and the RunIC air-relative speed it is,
        # however, the exact pre-release boundary contract.  Do not use canonical Vg
        # here: while held it intentionally includes the configured wind.
        ground_support_evidence=self._initial_ground_support_evidence(
            mode=mode0,phase=phase0,agl_m=base_agl_m,agl_ready=base_agl_ready,
            physical_state=ops if isinstance(ops,dict) else {})
        initial_ground_support=bool(ground_support_evidence["asserted"])
        session=self.layer2_adapter.open_isolated_session(); rows=[]; consumed_actions=[]
        try:
            cfg=Layer3Config.from_yaml(self.root/str(self.config.get("layers.layer3.config","configs/configuration_boulder.yaml")),package_root=self.root)
            self.config.bind_layer3_external_paths(cfg,required=True)
            self.unit_adapter.bind_layer3_config(cfg)
            identity=self._full_fidelity_workspace_identity(candidate, request)
            persistent=self._full_fidelity_persistent_root(identity)
            persistent.mkdir(parents=True,exist_ok=True)
            ff_workspace=CompressedPersistentWorkspace(
                persistent,prefix="tdoc-l4verify-",working_parent=self.config.runtime_root/".transient")
            rrroot=ff_workspace.working_root
            (rrroot/"workspace_identity.json").write_text(__import__("json").dumps({
                "schema":"TDOC-L4FF-workspace-identity-v2",
                **identity,
                "legacy_candidate_only_workspace_forbidden":True,
                "persistent_root":str(persistent),
            },indent=2,sort_keys=True))
            cid=str(candidate.get("candidate_id","candidate"))
            cfg.paths.ss_kstore=str(rrroot/"ss_kstore.jsonl"); cfg.paths.setd_kstore=str(rrroot/"setd_kstore.jsonl"); cfg.paths.provenance_log=str(rrroot/"provenance.jsonl"); cfg.paths.tdoc_health=str(rrroot/"tdoc_health.json"); cfg.paths.snapshots_dir=str(rrroot/"snapshots"); cfg.paths.versions_dir=str(rrroot/"versions")
            eng=Layer3Engine(cfg)
            runtime_type=Layer3FMURuntime
            contracts_type=SignalContractRegistry
            # These classes are installed only on the freshly created isolated
            # runtime. Request JSON cannot install them on live Layer3, whose
            # observed-clock/override rejection remains unchanged.
            current_forecast_clock=None
            if forecast_binding is not None:
                def prediction_clock(context):
                    if (current_forecast_clock is None or
                        (context or {}).get("tdoc_full_fidelity_prediction_clock")!=current_forecast_clock or
                        (context or {}).get("native_climate_time_projection") is not None):
                        raise ValueError("isolated prediction clock context mismatch")
                    return current_forecast_clock
                class PredictionContracts(SignalContractRegistry):
                    def normalize_climate_request(self,climate,*,context=None,trusted_internal=False):
                        clock=prediction_clock(context)
                        raw=dict(climate or {});item=raw.pop("time",None)
                        if isinstance(item,dict) and "value" in item:
                            envelope=self.tdoc.validate_envelope(item,external=not trusted_internal)
                            if (envelope.get("time_reference")!="MODEL_FORECAST_FROM_ACCEPTED_DATASET" or
                                envelope.get("prediction_time_binding")!=clock):
                                raise ValueError("isolated prediction time envelope mismatch")
                            value=envelope["value"]
                        elif trusted_internal:value=item
                        else:raise ValueError("isolated prediction requires an explicit time envelope")
                        if value!=clock["dataset_timestamp"]:
                            raise ValueError("isolated prediction climate query timestamp changed")
                        out,records=super().normalize_climate_request(raw,context=context,trusted_internal=trusted_internal)
                        out["time"]=value
                        records.append({"signal":"time","prediction_clock":copy.deepcopy(clock),
                                        "UTC_conversion_performed":False,"observed_source_time":False})
                        return out,records
                class PredictionRuntime(Layer3FMURuntime):
                    def _climate_args(self,request,climate_override=None):
                        clock=prediction_clock(request.context)
                        args=super()._climate_args(request,climate_override)
                        from pathlib import Path
                        if Path(args.input).resolve()!=Path(forecast_binding["forcing_file"]).resolve():
                            raise ValueError("isolated prediction forcing differs from accepted binding")
                        # Preserve the accepted naive/offset representation exactly;
                        # the legacy runtime's timezone-stripped intermediate is
                        # never sent to MicroMet.
                        args.time=clock["dataset_timestamp"]
                        return args
                    def _mesa_query(self,request,mesa_packet,K):
                        query,provenance=super()._mesa_query(request,mesa_packet,K)
                        if query is not None:
                            query["time"]=prediction_clock(request.context)["dataset_timestamp"]
                            provenance.update(climate_contract_time_binding="isolated_native_model_forecast",
                                              UTC_conversion_performed=False,observed_source_time=False)
                        return query,provenance
                    @staticmethod
                    def _stamp_prediction(packet,context):
                        clock=prediction_clock(context)
                        if packet.get("timestamp")!=clock["dataset_timestamp"]:
                            raise ValueError("isolated MicroMet prediction timestamp mismatch")
                        return {**packet,"timestamp_reference":"MODEL_FORECAST_FROM_ACCEPTED_DATASET",
                                "prediction_clock":copy.deepcopy(clock)}
                    def collect_base_packets(self,request):
                        packets,context,meta=super().collect_base_packets(request)
                        packets["MicroMetTwin"]=self._stamp_prediction(packets["MicroMetTwin"],request.context)
                        return packets,context,meta
                    def execute_couplings(self,request,*args,**kwargs):
                        packets,context,anchors,meta=super().execute_couplings(request,*args,**kwargs)
                        packets["MicroMetTwin"]=self._stamp_prediction(packets["MicroMetTwin"],request.context)
                        return packets,context,anchors,meta
                runtime_type=PredictionRuntime;contracts_type=PredictionContracts
            rt=runtime_type.__new__(runtime_type); rt.config=cfg; rt.engine=eng; rt.dependencies=eng.dependencies; rt.tdoc=eng.tdoc
            rt.mesa=session["mesa"]; rt.wing=session["wing"]; rt.climate_twin=session["climate_twin"]; rt._estimate_climate=session["estimate"]
            rt.contracts=contracts_type(cfg,rt.tdoc); rt.boulder=self._boulder_reader(cfg) if bool(cfg.runtime.get("use_boulder_runtime_sampling",True)) else None
            for step,x in enumerate(traj[1:],start=1):
                req=copy.deepcopy(request); req.pop("transaction_id",None); req["reference_time_s"]=float(snapshot.get("reference_time_s",0.0)+step*replay_dt)
                if forecast_binding is not None:
                    from layer6.binding import forcing_time,context_guard
                    if context_guard(observed_climate_projection["binding_path"]).binding!=forecast_binding:
                        raise ValueError("isolated prediction binding changed")
                    future=float(req["reference_time_s"])
                    offset=future-float(observed_climate_projection["plant_time_s"])
                    if not math.isfinite(offset) or offset<0:
                        raise ValueError("isolated prediction precedes its observed anchor")
                    dataset_time,elapsed=forcing_time(forecast_binding,future)
                    current_forecast_clock={"schema":"TDOC-isolated-native-prediction-clock-v1",
                        "role":"ISOLATED_MODEL_PREDICTION","observed_source_time":False,
                        "observed_anchor_projection":copy.deepcopy(observed_climate_projection),
                        "forecast_reference_time_s":future,"forecast_offset_from_observed_anchor_s":offset,
                        "mission_elapsed_s":elapsed,"dataset_timestamp":dataset_time,
                        "effective_climate_epoch":forecast_binding["effective_climate_epoch"],
                        "plant_epoch_s":forecast_binding["plant_epoch_s"],
                        "binding_hash":forecast_binding["binding_hash"],"forcing_file_sha256":forecast_binding["forcing_file_sha256"],
                        "clock_domain":observed_climate_projection["clock_domain"],
                        "timezone_status":observed_climate_projection["timezone_status"],
                        "UTC_conversion_performed":False,"first_packet_rebase":False,
                        "simulation_only":True,"actuation_authority":False}
                    req.setdefault("context",{}).pop("native_climate_time_projection",None)
                    req["context"]["tdoc_full_fidelity_prediction_clock"]=copy.deepcopy(current_forecast_clock)
                    time_envelope=copy.deepcopy(req.setdefault("climate",{}).get("time") or {})
                    time_envelope.update(value=dataset_time,time_reference="MODEL_FORECAST_FROM_ACCEPTED_DATASET",
                        prediction_time_binding=copy.deepcopy(current_forecast_clock),source="TDOC isolated model forecast")
                    time_envelope.pop("time_binding",None)
                    req["climate"]["time"]=time_envelope
                # Scenario wind/density are taken from the authoritative Layer-4 state
                # vector for this verification step; the physical state is propagated
                # only by WingMPET op27 using the validated Phoenix action.
                wind=[float(x[codec.index[f"W_{i}"]]) for i in range(3)]
                rho=float(x[codec.index["air_density_kg_m3"]])
                action_row=dict(phys_actions[step-1]); action_generation={
                    "schema":"TDOC-L4FF-action-generation-v1",
                    "mode":"PRECOMPUTED_OPEN_LOOP","step":step,
                }
                if callable(physical_action_provider):
                    mapped_state={
                        "pn":ff_map_origin_ned["pn"]+(float(phys["pn"])-ff_initial_ned["pn"]),
                        "pe":ff_map_origin_ned["pe"]+(float(phys["pe"])-ff_initial_ned["pe"]),
                        "pd":ff_map_origin_ned["pd"]+(float(phys["pd"])-ff_initial_ned["pd"]),
                    }
                    supplied=physical_action_provider(
                        step_index=step-1,
                        physical_state=copy.deepcopy(phys),
                        mapped_state=mapped_state,
                        trajectory_state=copy.deepcopy(x),
                        wind_ned=list(wind),
                    )
                    if not isinstance(supplied,dict) or supplied.get("ok") is not True:
                        reason=(supplied or {}).get("reason") if isinstance(supplied,dict) else None
                        raise FullFidelityPolicyError(reason or "invalid_provider_result",
                            step_index=step-1,provider_result=supplied,replay_evidence={
                                "physical_state":copy.deepcopy(phys),
                                "reference_ground_velocity_ned_m_s":[
                                    float(x[codec.index[f"Vg_{i}"]]) for i in range(3)],
                                "replay_step_s":replay_dt,"replay_lane":replay_lane,
                                "initial_ground_support":ground_support_evidence,
                                "initialization":physical_initialization,
                                "simulation_only":True,"not_field_evidence":True,
                            })
                    provider_bridge=dict(supplied.get("bridge_record") or {})
                    provider_contract=self._validate_full_fidelity_physical_actions(
                        [None,None],[supplied.get("physical_action")],provider_bridge)
                    if not provider_contract.get("ok"):
                        raise RuntimeError("full_fidelity_action_policy_contract_failed:"+
                                           ";".join(provider_contract.get("reasons") or []))
                    action_row=dict(provider_contract["physical_actions"][0])
                    action_generation={
                        "schema":"TDOC-L4FF-action-generation-v1",
                        "mode":"RECEDING_HORIZON_POLICY","step":step,
                        "remaining_horizon_steps":int(supplied.get("remaining_horizon_steps") or 1),
                        "bridge_binding_source":provider_bridge.get("binding_source"),
                        "bridge_reason":provider_bridge.get("reason"),
                        "all_layer5_gates_applied":True,
                    }
                consumed_actions.append(dict(action_row))
                phys,physical_record=self._wing27_physical_step(session["wing"],phys,action_row,dt=replay_dt,
                    wind_ned=wind,air_density=rho,
                    ground_supported=bool(step==1 and initial_ground_support),
                    context_id=str(req.get("regime_id") or ""),stage_id=str(req.get("stage") or "cruise"))
                physical_record["timing_contract"]={
                    "schema":"TDOC-L4-FF-multirate-v3",
                    "replay_lane":replay_lane,
                    "forecast_step_s":float(dt),
                    "replay_step_s":float(replay_dt),
                    "execution_segment_s":float(replay_dt) if replay_lane=="COMMIT_SEGMENT" else None,
                    "plant_transaction_steps":1,
                    "horizon_steps":len(traj)-1,
                    "all_rows_in_lane_gated":True,
                    "first_move_only_committable":replay_lane=="COMMIT_SEGMENT",
                    "forecast_policy_replanned_each_step":bool(callable(physical_action_provider)),
                }
                if step==1:
                    physical_record["initialization"]=dict(physical_initialization)
                    physical_record["ground_support_evidence"]=dict(ground_support_evidence)
                def env(name,value,unit,frame,semantic):
                    item=(req.setdefault("wing_inputs",{}).get(name))
                    if isinstance(item,dict) and "value" in item:
                        item=dict(item); item["value"]=float(value); item["source"]="TDOC/Layer4FullFidelity/WingMPET-op27"; return item
                    return {"value":float(value),"unit":unit,"frame":frame,"semantic":semantic,"source":"TDOC/Layer4FullFidelity/WingMPET-op27"}
                # Layer2 Wing op4 consumes ENU position/air velocity.  Derive the air
                # velocity from physically propagated ground velocity minus NED wind.
                va_ned=[float(phys["vn"])-wind[0],float(phys["ve"])-wind[1],float(phys["vd"])-wind[2]]
                vals={"air_velocity_x":(va_ned[1],"m/s","ENU","air_velocity_x"),"air_velocity_y":(va_ned[0],"m/s","ENU","air_velocity_y"),"air_velocity_h":(-va_ned[2],"m/s","ENU","air_velocity_h"),
                      "state_x":(phys["pe"],"m","ENU","state_x"),"state_y":(phys["pn"],"m","ENU","state_y"),"state_h":(-phys["pd"],"m","ENU","state_h")}
                for name,(value,unit,frame,semantic) in vals.items(): req.setdefault("wing_inputs",{})[name]=env(name,value,unit,frame,semantic)
                req.setdefault("context",{})["layer4_candidate_id"]=cid; req["context"]["tdoc_verification_owner"]="TDOC/RuntimeScheduler"
                # The isolated verification FMI session uses a local clock starting
                # at zero.  Bind packet freshness to this step's mission reference
                # time without changing the FMI dynamics or operational ONLINE time.
                req["context"]["tdoc_full_fidelity_reference_time_s"]=float(req["reference_time_s"])
                req["context"]["layer4_scenario_id"]=str(candidate.get("scenario_id") or "")
                req["context"]["full_fidelity_physical_action_contract"]=contract["schema"]
                req.setdefault("action",{})["layer4_hypothetical_action"]=actions[min(step-1,len(actions)-1)] if actions else []
                req["action"]["phoenix_physical_action"]=dict(phys_actions[step-1])
                if scenario is not None:
                    req.setdefault("prediction",{})["layer4_scenario_parameters"]=getattr(scenario,"parameters",{}) or {}
                    req.setdefault("constraints",{}).update(getattr(scenario,"constraints",{}) or {})
                # Full-fidelity coherence: MESA and WingMPET must represent the same
                # simulated physical state.  This is OFFLINE/verification simulation
                # truth alignment only, never field or GNSS-denied estimator evidence.
                try:
                    # WingMPET ONLINE state is local NED, while MESA and Layer4
                    # objectives use the absolute projected map frame.  Preserve
                    # physical displacement but translate it onto the candidate's
                    # same-cycle absolute MESA origin before the isolated L3 step.
                    mesa_values={"rN":ff_map_origin_ned["pn"]+(float(phys["pn"])-ff_initial_ned["pn"]),
                                 "rE":ff_map_origin_ned["pe"]+(float(phys["pe"])-ff_initial_ned["pe"]),
                                 "rD":ff_map_origin_ned["pd"]+(float(phys["pd"])-ff_initial_ned["pd"]),
                                 "vN":float(phys["vn"]),"vE":float(phys["ve"]),"vD":float(phys["vd"]),
                                 "qw":float(phys["qw"]),"qx":float(phys["qx"]),"qy":float(phys["qy"]),"qz":float(phys["qz"])}
                    if not all(math.isfinite(v) for v in mesa_values.values()): raise ValueError("non_finite")
                    session["mesa"].fmu.set(**mesa_values)
                    req.setdefault("context",{})["offline_mesa_state_alignment"]={
                        "schema":"TDOC-FF-MESA-WingMPET-state-coherence-v1","simulation_only":True,
                        "source":"WingMPET/op27 full-fidelity physical simulation truth",
                        "not_field_evidence":True,"not_gnss_denied_estimator_evidence":True,
                        "position_ned_m":[mesa_values["rN"],mesa_values["rE"],mesa_values["rD"]],
                        "ground_velocity_ned_m_s":[mesa_values["vN"],mesa_values["vE"],mesa_values["vD"]]}
                except Exception as exc:
                    raise RuntimeError(f"full_fidelity_mesa_state_alignment_failed:{exc!r}") from exc
                eg,ec,ectx=self._runtime_gates_and_constraints(req,{"WingMPET": {"validity": ((((snapshot.get("std") or {}).get("representations") or {}).get("WingMPET") or {}).get("validity") or {})}},prior_snapshot=snapshot)
                req["safety_gates"]=eg; req["constraints"]=ec; req["context"]={**(req.get("context") or {}),**ectx}
                allowed=set(RuntimeRequest.__dataclass_fields__); rm={k:v for k,v in req.items() if k in allowed}; rm.setdefault("regime_id",req.get("regime_id")); rm.setdefault("stage",req.get("stage","cruise")); rm.setdefault("context",req.get("context") or {})
                res=rt.step(RuntimeRequest.from_mapping(rm)); sd=res.snapshot.to_dict() if hasattr(res.snapshot,"to_dict") else dict(res.snapshot)
                reps=((sd.get("std") or {}).get("representations") or {}); minv=0.2; validity={t:float((rec or {}).get("validity",1.0)) for t,rec in reps.items()}
                # The isolated Layer3 runtime does not pass through this adapter's
                # normal invoke() boundary, so add the same producer-side operational
                # AGL packet explicitly for every replayed state.  Authority remains
                # MESA_STD + canonical DEM; PT_LIB/JSBSim truth is never used here.
                mesa_replay=(reps.get("MESA_NAV") or {}) if isinstance(reps,dict) else {}
                # ``mesa_values`` is the absolute projected MESA position obtained
                # by translating the isolated WingMPET displacement onto the live
                # map origin.  Re-sample the canonical terrain at every 0.1 s commit
                # replay row: anchoring terrain to the entry cell would hide a DEM
                # boundary/slope inside the committable first segment.
                sd["context"]=self._augment_operational_agl(
                    dict(sd.get("context") or {}),
                    {"MESA_NAV":{"state":{
                        "position_n":mesa_values["rN"],
                        "position_e":mesa_values["rE"],
                        "position_d":mesa_values["rD"],
                    }}},
                )
                replay_authority=dict((sd.get("context") or {}).get("agl_authority") or {})
                if replay_authority.get("valid") is True:
                    replay_authority.update({
                        "schema":"TDOC-FF-Operational-AGL-v2",
                        "reason":"MESA_ALIGNED_WINGMPET_PLUS_DEM_BILINEAR",
                        "navigation_source":"MESA_NAV aligned WingMPET physical replay",
                        "simulation_only":True,"not_field_evidence":True,
                        "commit_segment_terrain_resampled":True,
                    })
                    sd["context"]["agl_authority"]=replay_authority
                action_sha=__import__("hashlib").sha256(__import__("json").dumps(action_row,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")).hexdigest()
                workspace_identity={**dict(identity),"persistent_root":str(persistent),"audit_file":"ff_replay_audit.json.gz"}
                rows.append({"step":step,"mismatch":sd.get("mismatch"),"margin":sd.get("consistency_margin"),"health":sd.get("health"),"policy_version":sd.get("policy_version"),"validity":validity,"stability":sd.get("stability"),"snapshot":sd,
                             "coc_status":sd.get("coc_status"),"consistency_region":sd.get("consistency_region") or {},
                             "coc_missing_expected_observations":sd.get("coc_missing_expected_observations") or [],
                             "coc_unassessed_active_edges":((sd.get("consistency_region") or {}).get("unassessed_edges") or []),
                             "validity_ok":all(v>=minv for v in validity.values()),"physical_replay":physical_record,
                             "physical_action_consumed":True,"physical_action_index":step-1,"physical_action":action_row,
                             "physical_action_sha256":action_sha,
                             "physical_action_generation":action_generation,
                             "physical_action_contract":{"schema":contract.get("schema"),"ok":bool(contract.get("ok")),"horizon":int(contract.get("horizon") or 0)},
                             "physical_mission_safe":bool(physical_record.get("vtol_feasible")),
                             "invocation_owner":"TDOC/RuntimeScheduler",
                             "verification_workspace_identity":workspace_identity})
            return rows
        finally:
            try:
                if 'ff_workspace' in locals():
                    exc=__import__("sys").exc_info()[1]
                    bridge=dict(bridge_record or {})
                    actions=(list(consumed_actions) if consumed_actions
                             else list(bridge.get("physical_actions") or []))
                    action_digest=__import__("hashlib").sha256(__import__("json").dumps(actions,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")).hexdigest() if actions else None
                    bridge_digest=__import__("hashlib").sha256(__import__("json").dumps(bridge,sort_keys=True,separators=(",",":"),default=str).encode("utf-8")).hexdigest()
                    consumed=sum(1 for r in rows if bool(r.get("physical_action_consumed")))
                    audit={
                        "schema":"TDOC-L4FF-replay-audit-v1",
                        "status":"EXCEPTION" if exc is not None else "COMPLETED",
                        "error":repr(exc) if exc is not None else None,
                        "workspace_identity":dict(identity) if 'identity' in locals() else {},
                        "persistent_root":str(persistent) if 'persistent' in locals() else None,
                        "bridge_ready":bool(bridge.get("ok")),
                        "bridge_reason":bridge.get("reason"),
                        "bridge_owner":bridge.get("owner"),
                        "bridge_binding_source":bridge.get("binding_source"),
                        "bridge_verification_mapping":bridge.get("verification_mapping"),
                        "bridge_sha256":bridge_digest,
                        "physical_action_contract":{
                            "schema":contract.get("schema") if 'contract' in locals() else None,
                            "ok":bool(contract.get("ok")) if 'contract' in locals() else False,
                            "horizon":int(contract.get("horizon") or 0) if 'contract' in locals() else 0,
                            "reasons":list(contract.get("reasons") or []) if 'contract' in locals() else [],
                        },
                        "physical_action_count":len(actions),
                        "physical_actions_sha256":action_digest,
                        "replay_row_count":len(rows),
                        "physical_action_consumed_count":consumed,
                        "physical_actions_consumed_all":bool(rows) and consumed==len(rows),
                        "physical_action_generation_mode":("RECEDING_HORIZON_POLICY"
                            if callable(physical_action_provider) else "PRECOMPUTED_OPEN_LOOP"),
                        "replay_sources":sorted({str((r.get("physical_replay") or {}).get("source") or "") for r in rows}),
                        "all_rows_have_workspace_identity":bool(rows) and all(bool(r.get("verification_workspace_identity")) for r in rows),
                        "simulation_only":True,
                        "operational_actuation":False,
                    }
                    (rrroot/"ff_replay_audit.json").write_text(__import__("json").dumps(audit,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8")
                    ff_workspace.close()
            except Exception: pass
            self.layer2_adapter.close_isolated_session(session)

    def apply_layer7_feedback(self, routes:dict[str,Any])->dict[str,Any]:
        """Apply only supported transient Layer-7 corrections; preserve all others as routed evidence."""
        self._ensure(); applied=[]; deferred=[]
        r32=((routes.get("Layer3.2") or {}).get("packet") or {}) if isinstance(routes,dict) else {}
        dk=dict(r32.get("DeltaK") or {})
        if dk:
            self.engine.current_meso_guidance["transient_coupling_delta"]=dk; applied.append("Layer3.2.DeltaK")
        if r32:
            self.engine.current_meso_guidance["layer7_network_feedback"]=r32; deferred.append("Layer3.2.non-DeltaK")
        r33=((routes.get("Layer3.3") or {}).get("packet") or {}) if isinstance(routes,dict) else {}
        if r33:
            self.engine.current_meso_guidance["layer7_temporal_feedback"]=r33; deferred.append("Layer3.3")
        r31=((routes.get("Layer3.1") or {}).get("packet") or {}) if isinstance(routes,dict) else {}
        if r31: deferred.append("Layer3.1")
        r2=(routes.get("Layer2") or {}) if isinstance(routes,dict) else {}
        p2=(r2.get("packet") or {}) if isinstance(r2,dict) else {}
        if p2.get("StateResidualEvidence") or p2.get("CorrectedUncertainty"):
            route_hash=str(r2.get("route_hash") or "")
            if route_hash and route_hash!=self._last_assimilated_route_hash:
                self._pending_twin_plant_residual=dict(r2); applied.append("Layer2.twin_plant_residual_to_L3_uncertainty")
        return {"ok":True,"applied_transient":applied,"deferred_to_contract_context":deferred}

    def commit_layer7(self, adaptation:dict[str,Any], *, authorization:str|None=None)->dict[str,Any]:
        self._ensure()
        out=self.engine.commit_layer7(adaptation,approved=True,authorization=authorization); self.native_workspace.sync(); return out

    def rollback_layer7(self, *, reason:str)->dict[str,Any]:
        self._ensure()
        out=self.engine.rollback(reason=reason); self.native_workspace.sync(); return out

    def close(self):
        try:
            if self.native_workspace: self.native_workspace.close()
        except Exception: pass
        self.runtime_bridge=None; self.engine=None; self.layer3_config=None
        self._boulder_reader_cache=None
