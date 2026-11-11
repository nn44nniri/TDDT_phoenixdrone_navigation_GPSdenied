from __future__ import annotations
import importlib
import sys
import os
import json
import hashlib
from pathlib import Path
from typing import Any, Callable
from .config import Layer5Config

class Layer2VirtualEnvironmentAdapter:
    """A2^virt: explicit three-Twin OFFLINE_SE execution boundary.

    Domain-specific semantic operation mappings remain registered/versioned outside
    Layer 5.  Once registered, all three Twin calls are executed for each offline step.
    """
    REQUIRED=("WingMPET","MESA_NAV","Climate")
    def __init__(self): self._steps:dict[str,Callable[[dict[str,Any],dict[str,Any]],Any]]={}; self.calls=0
    def register(self,twin:str,fn:Callable[[dict[str,Any],dict[str,Any]],Any]):
        if twin not in self.REQUIRED: raise ValueError(twin)
        if not callable(fn): raise TypeError("adapter must be callable")
        self._steps[twin]=fn
    def ready(self)->bool: return all(k in self._steps for k in self.REQUIRED)
    def status(self)->dict[str,Any]: return {"ready":self.ready(),"registered":sorted(self._steps),"required":list(self.REQUIRED),"calls":self.calls,"layer6_call":False,"jsbsim_call":False}
    def rollout(self,action_package:dict[str,Any],contexts:dict[str,dict[str,Any]]|None=None)->dict[str,Any]:
        if not self.ready(): raise RuntimeError("all three Layer-2 virtual Twin adapters must be registered")
        contexts=contexts or {}; self.calls+=1
        return {k:self._steps[k](dict(action_package),dict(contexts.get(k,{}))) for k in self.REQUIRED}

class PTLibJSBSimAdapter:
    """Stateful ONLINE-only A6 FMI adapter for pt_lib_v20260909_0932.

    The operational path follows PT_LIB's documented public ``PTLibFmiClient``
    sequence.  ArduPilotController.fmu remains the actuator/mixer owner and PT_LIB
    remains the FMI master coupling it to JSBSimPhoenix.fmu.  OFFLINE_SE never
    constructs this adapter.  No direct PWM or direct JSBSim actuator injection is
    exposed.
    """
    def __init__(self,config:Layer5Config,ptlib_config:str|Path|None=None):
        self.config=config; self.root=config.env_path("TDDT_PTLIB_ROOT"); self.fmu_root=config.env_path("EXT_FMU_SIM_ROOT")
        self.client=None; self.controller=None; self.lifecycle=None; self.error=None; self.prepared=False; self.connected=False
        self.ptlib_config=Path(ptlib_config).resolve() if ptlib_config else None
        self.background_receive=None; self.truth_stream=None
        self.external_control_ready=None; self.control_preparation=None
        self.import_origins:dict[str,str]={}; self.origin_verified=False
        self.initial_state_record=None; self.resource_binding=None
    def _required_files(self)->list[Path]:
        if not self.root: return []
        return [self.root/"pt_lib/__init__.py", self.root/"pt_lib/api.py", self.root/"pt_lib/config.py", self.root/"pt_lib/controller.py",
                self.root/"pt_lib/lifecycle.py", self.root/"pt_lib/fmi_io.py", self.root/"config/pt_lib_sitl.yaml"]

    @staticmethod
    def _sha256(path:Path)->str:
        h=hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda:fh.read(1024*1024),b""): h.update(block)
        return h.hexdigest()

    def _external_fmu_probe(self)->dict[str,Any]:
        root=self.fmu_root or self.config.env_path("EXT_FMU_SIM_ROOT")
        expected=("ArduPilotController.fmu","JSBSimPhoenix.fmu")
        missing=[name for name in expected if not root or not (root/name).is_file()]
        return {"ok":bool(root and root.is_dir() and not missing),"root":str(root) if root else None,
                "environment_variable":"EXT_FMU_SIM_ROOT","authoritative":True,"expected_files":list(expected),"missing":missing,
                "transport":"FMI2-CoSimulation","container_runtime_required":False}

    def _resource_binding_probe(self)->dict[str,Any]:
        """Prove PT_LIB region/terrain assets are the canonical Boulder assets."""
        if not self.root: return {"ok":False,"reason":"ptlib_root_unavailable"}
        boulder=self.config.env_path("TDDT_BOULDER_ROOT")
        pairs=[
            ("region_geojson",self.root/"resources/region_v1.geojson",(boulder/"region/region_v1.geojson") if boulder else None),
            ("terrain_raster",self.root/"resources/terrain/region_v1/terrain_obstacles.tif",(boulder/"outputs/region_v1_map/terrain_obstacles.tif") if boulder else None),
        ]
        rows=[]; ok=True
        for name,pt_path,boulder_path in pairs:
            exists=bool(pt_path.is_file() and boulder_path and boulder_path.is_file())
            ph=self._sha256(pt_path) if pt_path.is_file() else None
            bh=self._sha256(boulder_path) if boulder_path and boulder_path.is_file() else None
            match=bool(exists and ph==bh); ok=ok and match
            rows.append({"asset":name,"ptlib_path":str(pt_path),"boulder_path":str(boulder_path) if boulder_path else None,
                         "ptlib_sha256":ph,"boulder_sha256":bh,"byte_identical":match})
        return {"ok":bool(ok),"schema":"PTLIB-Boulder-resource-binding-v1","boulder_root":str(boulder) if boulder else None,
                "policy":"PT_LIB runtime consumes its packaged resources only when byte-identical to canonical Boulder region/terrain",
                "assets":rows}

    def _is_under_root(self,path:Path)->bool:
        try:
            path.resolve().relative_to(self.root.resolve()); return True
        except Exception:
            return False

    def _load(self):
        if not self.root or not self.root.exists(): raise RuntimeError("TDDT_PTLIB_ROOT unavailable")
        self.root=self.root.resolve()
        missing=[str(p) for p in self._required_files() if not p.is_file()]
        if missing:
            raise RuntimeError("TDDT_PTLIB_ROOT invalid/incomplete; missing: "+", ".join(missing))
        # Fail closed if a pt_lib module was already imported from anywhere other than
        # the exact external root selected by TDDT_PTLIB_ROOT.  This prevents an
        # installed/global or accidentally vendored copy from becoming an implicit fallback.
        for name,module in list(sys.modules.items()):
            if name=="pt_lib" or name.startswith("pt_lib."):
                origin=getattr(module,"__file__",None)
                if origin and not self._is_under_root(Path(origin)):
                    raise RuntimeError(f"pt_lib module already loaded outside TDDT_PTLIB_ROOT: {name} -> {origin}")
        root_s=str(self.root)
        if root_s in sys.path: sys.path.remove(root_s)
        sys.path.insert(0,root_s)
        pkg=importlib.import_module("pt_lib")
        api_mod=importlib.import_module("pt_lib.api")
        cfg_mod=importlib.import_module("pt_lib.config")
        ctl_mod=importlib.import_module("pt_lib.controller")
        life_mod=importlib.import_module("pt_lib.lifecycle")
        mods={"pt_lib":pkg,"pt_lib.api":api_mod,"pt_lib.config":cfg_mod,"pt_lib.controller":ctl_mod,"pt_lib.lifecycle":life_mod}
        origins={}
        for name,module in mods.items():
            origin=getattr(module,"__file__",None)
            if not origin or not self._is_under_root(Path(origin)):
                raise RuntimeError(f"pt_lib import escaped TDDT_PTLIB_ROOT: {name} -> {origin}")
            origins[name]=str(Path(origin).resolve())
        self.import_origins=origins; self.origin_verified=True
        PTLibFmiClient=api_mod.PTLibFmiClient
        cpath=self.ptlib_config or (self.root/"config/pt_lib_sitl.yaml")
        if not cpath.is_file() or not self._is_under_root(cpath):
            raise RuntimeError("PT_LIB config must resolve inside TDDT_PTLIB_ROOT")
        # The public façade owns Config loading and FMI-master construction.
        client=PTLibFmiClient(cpath); cfg=client.shell.cfg; ctl=client.controller; life=client.shell.lifecycle
        # PT_LIB interactive shell binds these hooks explicitly. Layer5 is a
        # non-interactive orchestrator, so bind the same live-truth/release hooks
        # here as a compatibility guard for PT_LIB revisions that do not self-bind.
        hooks={
            "_simulator_readiness_refresher":"refresh_bridge_status",
            "_simulator_truth_stream_status":"bridge_truth_stream_status",
            "_simulator_release_authorizer":"authorize_bridge_release",
            "_simulator_release_clearer":"clear_bridge_release_authorization",
            "_simulator_handoff_authorizer":"authorize_bridge_handoff",
            "_simulator_handoff_clearer":"clear_bridge_handoff_authorization",
            "_simulator_recovery_activator":"activate_bridge_recovery",
            "_simulator_recovery_clearer":"clear_bridge_recovery",
        }
        for key,name in hooks.items():
            fn=getattr(life,name,None)
            if callable(fn): cfg[key]=fn
        self.client=client; self.controller=ctl; self.lifecycle=life
        self.resource_binding=self._resource_binding_probe()
        if not self.resource_binding.get("ok"):
            raise RuntimeError(f"PT_LIB/Boulder resource binding failed: {self.resource_binding}")
        return cfg
    def probe(self)->dict[str,Any]:
        try:
            if not self.root or not self.root.exists(): return {"ok":False,"root":str(self.root) if self.root else None,"reason":"root_unavailable"}
            expected=self._required_files(); fmu=self._external_fmu_probe(); resources=self._resource_binding_probe()
            pkg_version=None
            try:
                txt=(self.root/"pt_lib/__init__.py").read_text(encoding="utf-8")
                import re
                m=re.search(r'__version__\s*=\s*["\']([^"\']+)',txt); pkg_version=m.group(1) if m else None
            except Exception: pass
            return {"ok":bool(all(p.is_file() for p in expected) and fmu.get("ok") and resources.get("ok")),"root":str(self.root),
                    "expected_files":[str(p) for p in expected],"version":pkg_version,"expected_version":"0.8.0",
                    "external_only":True,"environment_variable":"TDDT_PTLIB_ROOT","fmu":fmu,"resource_binding":resources,
                    "origin_verified":self.origin_verified,"import_origins":dict(self.import_origins),
                    "api":["PTLibFmiClient.prepare","PTLibFmiClient.set_initial_geospatial_state","PTLibFmiClient.connect",
                           "PTLibFmiClient.precheck","PTLibFmiClient.set_mode","PTLibFmiClient.set_home","PTLibFmiClient.arm",
                           "PTLibFmiClient.enable_gnss_denied","PTLibFmiClient.acquire_trim","PTLibFmiClient.set_environment",
                           "PTLibFmiClient.send_pilot_command","PTLibFmiClient.read_state","PTLibFmiClient.read_actuator_feedback","PTLibFmiClient.release_control"],
                    "prepared":self.prepared,"connected":self.connected,"truth_observation_separated":True,
                    "persistent_session":True,"transport":"FMI2-CoSimulation","container_runtime_required":False,
                    "live_truth_hook_bound":bool(self.controller is not None and callable(getattr(self.controller,"cfg",{}).get("_simulator_readiness_refresher")))}
        except Exception as e: return {"ok":False,"reason":repr(e)}
    @staticmethod
    def _operation_ok(stage:str,result:Any)->tuple[bool,str|None]:
        """Fail closed only on explicit PT_LIB negative readiness evidence.

        PT_LIB versions may return different mapping shapes, so absence of an optional
        readiness field is not treated as failure. Explicit false/FAILED/NOT_READY is.
        """
        if isinstance(result,dict):
            for key in ("ok","success","ready"):
                if key in result and result.get(key) is False:
                    return False,f"{stage}:{key}=false"
            state=str(result.get("status") or result.get("state") or "").strip().upper()
            if state in {"FAILED","FAIL","ERROR","NOT_READY","UNAVAILABLE","DISCONNECTED","TIMEOUT"}:
                return False,f"{stage}:status={state}"
            if stage=="prepare" and result.get("prepared") is False:
                return False,"prepare:prepared=false"
            if stage=="connect" and result.get("connected") is False:
                return False,"connect:connected=false"
        return True,None
    def _apply_public_initial_state(self)->dict[str,Any]:
        if getattr(self,"client",None) is None: return {"applied":False,"reason":"public_client_unavailable"}
        if self.connected: return {"applied":False,"reason":"already_connected"}
        if isinstance(self.initial_state_record,dict): return {**self.initial_state_record,"reused":True}
        cfg=self.client.shell.cfg; rt=cfg.get("_runtime_payload") or {}; sim=rt.get("simulation") or {}; mission=rt.get("mission") or {}
        start=mission.get("airborne_control_start") or mission.get("start") or {}
        profile=rt.get("airframe_profile") or {}; seed=((profile.get("simulation_assumptions") or {}).get("analytic_release_seed") or {})
        lat=float(start.get("latitude")); lon=float(start.get("longitude")); alt=float(sim.get("initial_alt_msl_m",start.get("altitude_msl_m")))
        heading=float(sim.get("heading_deg") or 0.0); airspeed=float(seed.get("airspeed_m_s",13.5))
        rec=self.client.set_initial_geospatial_state(latitude_deg=lat,longitude_deg=lon,altitude_msl_m=alt,heading_deg=heading,initial_airspeed_m_s=airspeed)
        self.initial_state_record={"applied":True,"source":"PT_LIB runtime payload from validated Boulder/PT_LIB resources","record":rec}
        return dict(self.initial_state_record)

    def prepare(self):
        if self.lifecycle is None: self._load()
        if not self.prepared:
            r=self.client.prepare() if self.client is not None else self.lifecycle.prepare(); ok,reason=self._operation_ok("prepare",r)
            if not ok:
                self.prepared=False
                raise RuntimeError(f"PT_LIB prepare did not establish runtime readiness: {reason}; result={r!r}")
            self.prepared=True; return r
        return {"ok":True,"already_prepared":True}
    def connect(self):
        if self.controller is None: self._load()
        if not self.connected:
            initial=self._apply_public_initial_state() if self.client is not None else {"applied":False,"reason":"legacy_adapter"}
            r=self.client.connect() if self.client is not None else self.controller.connect_session(); ok,reason=self._operation_ok("connect",r)
            if not ok:
                self.connected=False
                raise RuntimeError(f"PT_LIB connect did not establish MAVLink/runtime readiness: {reason}; result={r!r}")
            self.connected=True
            # Non-interactive TDOC/edge callers need persistent consumers while
            # Layer2-5 compute. Otherwise MAVLink ages out and TDOC restarts a healthy
            # ArduPilot/JSBSim session on every supervisory cycle.
            if hasattr(self.controller,"start_background_receive_session"):
                self.background_receive=self.controller.start_background_receive_session()
            if self.lifecycle is not None and hasattr(self.lifecycle,"start_bridge_truth_stream"):
                self.truth_stream=self.lifecycle.start_bridge_truth_stream()
            if isinstance(r,dict):
                r=dict(r); r["initial_state"]=initial; r["background_receive"]=self.background_receive; r["environment_truth_stream"]=self.truth_stream
            return r
        out={"ok":True,"already_connected":True}
        if self.controller is not None and hasattr(self.controller,"background_receive_status_session"):
            out["background_receive"]=self.controller.background_receive_status_session()
        if self.lifecycle is not None and hasattr(self.lifecycle,"bridge_truth_stream_status"):
            out["environment_truth_stream"]=self.lifecycle.bridge_truth_stream_status()
        return out
    def prepare_control_session(self)->dict[str,Any]:
        """Execute PT_LIB v0.8 public FMI preparation order exactly once.

        Order: prepare -> initial state -> connect -> precheck -> FBWA -> HOME ->
        normal ARM (bounded SITL force fallback only when configured) -> GNSS-denied
        -> trim -> environment.  Physics remains supervised by PT_LIB/FMI and all
        pilot commands continue to flow through ArduPilot.
        """
        if self.controller is None: self._load()
        if isinstance(self.control_preparation,dict) and self.control_preparation.get("ready"):
            return {**self.control_preparation,"reused":True}
        if not self.prepared: self.prepare()
        if not self.connected: self.connect()
        if getattr(self,"client",None) is None:
            fn=getattr(self.controller,"external_control_prepare_session",None)
            if not callable(fn): fn=getattr(self.controller,"external_control_ready_session",None)
            if not callable(fn):
                self.control_preparation={"ok":False,"ready":False,"reason":"ptlib_external_control_prepare_api_unavailable"}; return self.control_preparation
            out=fn(background_poll=True); self.control_preparation=out if isinstance(out,dict) else {"ok":False,"ready":False,"reason":"invalid_external_control_prepare_result"}
            if self.control_preparation.get("ready"): self.external_control_ready=dict(self.control_preparation)
            return self.control_preparation
        seq=[]
        def add(name,value): seq.append({"step":name,"result":value}); return value
        try:
            pre=add("precheck",self.client.precheck(timeout_s=20.0))
            if not pre.get("ready"): raise RuntimeError(f"precheck_not_ready:{pre}")
            mode=add("set_mode_FBWA",self.client.set_mode("FBWA"))
            if not mode.get("confirmed"): raise RuntimeError(f"FBWA_not_confirmed:{mode}")
            rt=self.client.shell.cfg.get("_runtime_payload") or {}; mission=rt.get("mission") or {}; ground=mission.get("ground_start") or mission.get("start") or {}
            home=add("set_home",self.client.set_home(latitude_deg=float(ground.get("latitude")),longitude_deg=float(ground.get("longitude")),altitude_msl_m=float(ground.get("altitude_msl_m",0.0))))
            arm=add("arm_normal",self.client.arm(force=False,timeout_s=20.0))
            if not arm.get("armed"):
                safety=self.client.shell.cfg.get("safety") or {}; allow=bool(safety.get("sitl_only",True) and safety.get("allow_force_arm_sitl",False))
                if not allow: raise RuntimeError(f"normal_arm_not_confirmed:{arm}")
                arm=add("arm_force_sitl_fallback",self.client.arm(force=True,timeout_s=20.0))
            if not arm.get("armed"): raise RuntimeError(f"arming_not_confirmed:{arm}")
            gnss=add("enable_gnss_denied",self.client.enable_gnss_denied(timeout_s=30.0))
            if not gnss.get("ready"):
                gnss=add("enable_gnss_denied_retry",self.client.enable_gnss_denied(timeout_s=30.0))
            if not gnss.get("ready"): raise RuntimeError(f"gnss_denied_not_ready:{gnss}")
            trim=add("acquire_trim",self.client.acquire_trim(timeout_s=3.0))
            if not trim.get("ready"): raise RuntimeError(f"trim_not_ready:{trim}")
            sim=rt.get("simulation") or {}; env=add("set_environment",self.client.set_environment(wind_ned_m_s=sim.get("wind_ned_m_s",[0.0,0.0,0.0]),source="TDOC_ONLINE_INITIAL_ENV"))
            status=add("status",self.client.status())
            self.control_preparation={"ok":True,"ready":True,"schema":"TDOC-PTLIB-FMI-public-preparation-v1","transport":"FMI2-CoSimulation",
                                      "sequence":seq,"initial_state":self.initial_state_record,"home":home,"arm":arm,"gnss_denied":gnss,"trim":trim,
                                      "environment":getattr(env,"__dict__",env),"status":status,"direct_pwm":False,"mixer_owner":"ArduPilotController.fmu"}
        except Exception as exc:
            self.control_preparation={"ok":False,"ready":False,"schema":"TDOC-PTLIB-FMI-public-preparation-v1","reason":"public_ptlib_preparation_failed","error":repr(exc),"sequence":seq}
        if self.control_preparation.get("ready"): self.external_control_ready=dict(self.control_preparation)
        return self.control_preparation

    def commission_control_path(self, phase_throttle_requests:dict[str,Any]|None=None)->dict[str,Any]:
        """Run demo-aligned preparation followed by bounded held-SITL commissioning."""
        prep=self.prepare_control_session()
        if not isinstance(prep,dict) or not prep.get("ready"):
            return {"ok":False,"reason":"ptlib_demo_aligned_control_preparation_failed","preparation":prep}
        fn=getattr(self.controller,"phoenix_control_commission_session",None)
        if not callable(fn): return {"ok":False,"reason":"ptlib_phoenix_control_commission_unavailable","preparation":prep}
        out=fn(background_poll=True,phase_throttle_requests=phase_throttle_requests)
        result=out if isinstance(out,dict) else {"ok":False,"reason":"invalid_ptlib_commission_result","value":out}
        if isinstance(result,dict): result=dict(result); result.setdefault("preparation",prep)
        return result

    def ensure_external_control_ready(self)->dict[str,Any]:
        if isinstance(self.external_control_ready,dict) and self.external_control_ready.get("ready") and self.connected:
            return {**self.external_control_ready,"reused":True}
        out=self.prepare_control_session()
        self.external_control_ready=out if isinstance(out,dict) else {"ok":False,"ready":False,"reason":"invalid_external_control_ready_result"}
        return self.external_control_ready

    def abort_control_session(self,reason:str="tdoc_preflight_abort")->dict[str,Any]:
        if self.controller is None: return {"ok":True,"reason":reason,"no_session":True}
        if getattr(self,"client",None) is not None:
            try: out=self.client.release_control(); out={"ok":True,"reason":reason,"public_release":out}
            except Exception as exc: out={"ok":False,"reason":reason,"error":repr(exc)}
        else:
            fn=getattr(self.controller,"external_control_abort_session",None)
            out=fn(background_poll=True,reason=reason) if callable(fn) else {"ok":False,"reason":"ptlib_external_control_abort_api_unavailable"}
        self.external_control_ready=None; self.control_preparation=None
        return out if isinstance(out,dict) else {"ok":False,"reason":"invalid_external_control_abort_result","value":out}

    def safe_control_write(self,command:list[float],*,decision_id:str|None=None,transaction_id:str|None=None,rate_hz:float|None=None,ttl_s:float=8.0)->dict[str,Any]:
        """Latch one normalized pilot-domain command through the public PT_LIB FMI facade.

        ArduPilotController.fmu remains the sole mixer/actuator authority.  This method
        never exposes PWM or direct JSBSim actuator writes.  The legacy controller API
        is retained only as an explicit compatibility fallback for older PT_LIB roots;
        pt_lib_v20260909_0932 always takes the public ``send_pilot_command`` branch.
        """
        if len(command)!=4: raise ValueError("Phoenix pilot-domain command must be [a,e,t,r]")
        a,e,t,r=[float(x) for x in command]
        if not (-1<=a<=1 and -1<=e<=1 and 0<=t<=1 and -1<=r<=1): raise ValueError("pilot-domain command outside bounds")
        if self.controller is None: self._load()
        if not self.connected: self.connect()
        ready=self.ensure_external_control_ready()
        if isinstance(ready,dict) and (ready.get("ok") is False or ready.get("ready") is False):
            raise RuntimeError(f"PT_LIB external control session not ready: {ready}")
        public=getattr(getattr(self,"client",None),"send_pilot_command",None)
        if callable(public):
            return public(aileron=a,elevator=e,throttle=t,rudder=r,decision_id=decision_id,transaction_id=transaction_id,rate_hz=rate_hz,ttl_s=float(ttl_s))
        legacy=getattr(self.controller,"tdoc_control_stream_update_session",None)
        if callable(legacy):
            return legacy(a,e,t,r,decision_id=decision_id,transaction_id=transaction_id,rate_hz=rate_hz,ttl_s=float(ttl_s),release_on_stale=True)
        raise RuntimeError("PT_LIB pilot-command API unavailable")
    def io_snapshot(self)->dict[str,Any]:
        if self.controller is None: self._load()
        if not self.connected: self.connect()
        if getattr(self,"client",None) is not None:
            # Layer6 invokes this on the Online control hot path.  PT_LIB already
            # owns persistent FMI truth and MAVLink receive streams, so re-run the
            # same health/freshness gates without a new commissioning observation
            # window.  No readiness check is bypassed; only the blocking wait is.
            status=self.client.status(observation_s=0.0); snap=dict(status.get("io") or {})
            snap["public_state"]=self.client.read_state(); snap["public_actuator_feedback"]=self.client.read_actuator_feedback(); snap["fmi_health"]=status.get("fmi")
            snap["transport"]="FMI2-CoSimulation"; snap["docker_used"]=False
            return snap
        return self.controller.io_snapshot_session()
    def step(self,command:list[float])->dict[str,Any]:
        if not self.prepared: self.prepare()
        if not self.connected: self.connect()
        write=self.safe_control_write(command); snap=self.io_snapshot()
        obs=snap.get("sensor_observation") or snap.get("observation") or snap.get("sensors")
        truth=snap.get("truth_reference") or snap.get("environment_truth")
        events=snap.get("simulator_event") or snap.get("events")
        return {"actuator_command":write,"sensor_observation":obs,"environment_truth":truth,"simulator_event":events,"raw_io_snapshot":snap,
                "semantic_invariant":"environment_truth != sensor_observation","persistent_session":True}
    def close(self):
        if self.lifecycle is not None and hasattr(self.lifecycle,"stop_bridge_truth_stream"):
            try: self.lifecycle.stop_bridge_truth_stream()
            except Exception: pass
        if self.controller is not None:
            try:
                if hasattr(self.controller,"stop_background_receive_session"): self.controller.stop_background_receive_session()
            except Exception: pass
        if getattr(self,"client",None) is not None:
            try:self.client.close()
            except Exception:pass
        elif self.controller is not None:
            try:
                if hasattr(self.controller,"disconnect_session"): self.controller.disconnect_session()
                elif hasattr(self.controller,"close_session"): self.controller.close_session()
            except Exception: pass
        self.background_receive=None; self.truth_stream=None; self.external_control_ready=None; self.control_preparation=None; self.connected=False

# TDOC_RATE_SYNC_PATCH_v20260906_1115
from .tdoc_stream_bridge import install_environment_bridge as _install_tdoc_stream_bridge
_install_tdoc_stream_bridge(PTLibJSBSimAdapter)
