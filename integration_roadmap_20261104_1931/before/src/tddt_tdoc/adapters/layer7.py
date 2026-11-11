from __future__ import annotations
import importlib, math, os, sys
from pathlib import Path
from typing import Any, Callable, Mapping
from ..utils import jsonable, sha256_obj
from ..storage import CompressedPersistentWorkspace


class Layer7Adapter:
    """Exact-root adapter for the external Layer-7 TDOC side module.

    Layer 7 never owns lifecycle of Layers 2--6. TDOC owns invocation, candidate
    validation, safe commit/rollback, and versioned feedback routing.
    """
    layer_id = 7

    def __init__(self, config, *, validation_callback=None, commit_callback=None, rollback_callback=None):
        self.config = config
        self.root = config.path_value("TDDT_LAYER7_ROOT")
        self.expected_package = str(config.get("layers.layer7.expected_package", "layer7_v20260901_0921"))
        self.expected_version = str(config.get("layers.layer7.expected_version", "0.3.0"))
        self.expected_contract = str(config.get("layers.layer7.expected_contract", "tdoc-layer7-v2"))
        self.config_rel = str(config.get("layers.layer7.config", "configs/default.json"))
        self.validation_callback = validation_callback
        self.commit_callback = commit_callback
        self.rollback_callback = rollback_callback
        self.module = None
        self.side = None
        self._native_diagnostic = None
        # Layer-7 external KStore appends a /layer7 suffix to TDDT_KSTORE_ROOT.
        self.kstore_base = (config.path_value("TDDT_LAYER7_KSTORE_BASE") or (config.runtime_root/"layer7/kstore")).resolve()
        self.kstore_root = self.kstore_base / "layer7"
        self.kstore_root.mkdir(parents=True, exist_ok=True)
        self.kstore_workspace = CompressedPersistentWorkspace(
            self.kstore_base,prefix="tdoc-layer7-",working_parent=self.config.runtime_root/".transient")

    def _origin_ok(self, module) -> bool:
        path = getattr(module, "__file__", None)
        if not path or not self.root:
            return False
        try:
            Path(path).resolve().relative_to(self.root.resolve())
            return True
        except Exception:
            return False

    def _required(self):
        return ["src/tddt_layer7/__init__.py", "src/tddt_layer7/tdoc_adapter.py", self.config_rel,
                "schemas/tdoc_layer7_input.schema.json", "docs/TDOC_INTEGRATION.md"]

    def _ensure(self):
        if self.side is not None:
            return
        if not self.root or not self.root.exists():
            raise RuntimeError("TDDT_LAYER7_ROOT is not available")
        if self.root.name != self.expected_package:
            raise RuntimeError(f"Layer7 exact-root mismatch: expected {self.expected_package}, got {self.root.name}")
        existing = sys.modules.get("tddt_layer7")
        if existing is not None and not self._origin_ok(existing):
            raise RuntimeError("preloaded/global tddt_layer7 module is outside TDDT_LAYER7_ROOT")
        src = self.root / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        self.module = importlib.import_module("tddt_layer7")
        if not self._origin_ok(self.module):
            raise RuntimeError("tddt_layer7 import origin is outside TDDT_LAYER7_ROOT")
        cfg = self.module.load_config(self.root / self.config_rel)
        state_root = self.config.path_value("TDDT_LAYER7_STATE_ROOT") or (self.config.runtime_root / "layer7" / "state")
        state_root.mkdir(parents=True, exist_ok=True)
        # The external package resolves its KStore only at construction.  Force its
        # base to TDOC runtime/layer7 and keep JSON/JSONL gzip-compressed at rest.
        previous=os.environ.get("TDDT_KSTORE_ROOT")
        os.environ["TDDT_KSTORE_ROOT"]=str(self.kstore_workspace.working_root)
        try:
            self.side = self.module.Layer7SideModule(
                config=cfg,
                tdoc_validation_callback=self.validation_callback,
                tdoc_commit_callback=self.commit_callback,
                tdoc_rollback_callback=self.rollback_callback,
                state_root=state_root,
                native_diagnostic_validator=self._validate_native_diagnostic,
            )
        finally:
            if previous is None: os.environ.pop("TDDT_KSTORE_ROOT",None)
            else: os.environ["TDDT_KSTORE_ROOT"]=previous
            self.kstore_workspace.sync()

    def preflight(self):
        p = self.root
        missing = ["root"] if not p or not p.exists() else [x for x in self._required() if not (p / x).exists()]
        report = {
            "ok": False, "implemented": True, "root": str(p) if p else None,
            "expected_package": self.expected_package, "expected_version": self.expected_version,
            "expected_contract": self.expected_contract, "missing": missing,
            "external_only": True, "invocation_owner": "TDOC/RuntimeScheduler",
        }
        if missing or not p or p.name != self.expected_package:
            report["status"] = "MISSING_OR_EXACT_ROOT_MISMATCH"
            return report
        try:
            self._ensure()
            desc = dict(self.side.registration_descriptor())
            version = str(getattr(self.module, "__version__", ""))
            api_ok = all(hasattr(self.module, x) for x in ("Layer7SideModule", "Layer7Config", "load_config"))
            contract_ok = bool(desc.get("contract_version") == self.expected_contract and desc.get("trigger_owner") == "TDOC"
                               and desc.get("side_module") is True and desc.get("direct_actuation") is False)
            report.update({
                "ok": bool(api_ok and contract_ok and version == self.expected_version),
                "status": "READY" if api_ok and contract_ok and version == self.expected_version else "API_OR_CONTRACT_MISMATCH",
                "version": version, "api_ok": api_ok, "contract_ok": contract_ok,
                "registration_descriptor": desc,
                "origin": str(Path(self.module.__file__).resolve()),
                "state_root": str((self.config.path_value("TDDT_LAYER7_STATE_ROOT") or (self.config.runtime_root / "layer7/state")).resolve()),
            })
        except Exception as exc:
            report.update({"ok": False, "status": "IMPORT_FAILED", "error": repr(exc)})
        return report

    @staticmethod
    def _finite(v):
        if isinstance(v, bool):
            return None
        try:
            x = float(v)
            return x if math.isfinite(x) else None
        except Exception:
            return None

    @classmethod
    def _flatten_numeric(cls, obj, prefix="", limit=96):
        out = {}
        def walk(x, p):
            if len(out) >= limit:
                return
            v = cls._finite(x)
            if v is not None and p:
                out[p] = v; return
            if isinstance(x, Mapping):
                for k, val in x.items():
                    walk(val, f"{p}.{k}" if p else str(k))
            elif isinstance(x, (list, tuple)):
                for i, val in enumerate(x):
                    walk(val, f"{p}.{i}" if p else str(i))
        walk(obj, prefix)
        return out

    @classmethod
    def _simple_numeric_map(cls, obj):
        out = {}
        if not isinstance(obj, Mapping):
            return out
        for k, val in obj.items():
            x = cls._finite(val)
            if x is not None:
                out[str(k)] = x; continue
            if isinstance(val, Mapping):
                for pick in ("value", "weight", "final", "score", "K", "coupling"):
                    x = cls._finite(val.get(pick))
                    if x is not None:
                        out[str(k)] = x; break
        return out

    @staticmethod
    def _topology_edges(topology):
        if isinstance(topology, list):
            return [list(x[:2]) for x in topology if isinstance(x, (list, tuple)) and len(x) >= 2]
        if not isinstance(topology, Mapping):
            return []
        edges = topology.get("edges") or topology.get("active_edges") or []
        out = []
        for e in edges:
            if isinstance(e, (list, tuple)) and len(e) >= 2:
                out.append([str(e[0]), str(e[1])])
            elif isinstance(e, Mapping):
                a = e.get("source", e.get("from")); b = e.get("target", e.get("to"))
                if a is not None and b is not None:
                    out.append([str(a), str(b)])
        return out

    @classmethod
    def _truth_airspeed(cls, evidence):
        snap = (evidence or {}).get("io_snapshot") or {}
        truth = (snap.get("truth_reference") or {}).get("payload") or {}
        for k in ("airspeed_m_s", "airspeed"):
            v = cls._finite(truth.get(k))
            if v is not None:
                return v, "JSBSim truth_reference"
        groups = snap.get("groups") or {}
        for g in ("AIRSPEED", "VFR_HUD"):
            payload = (groups.get(g) or {}).get("payload") or {}
            for k in ("airspeed", "airspeed_m_s"):
                v = cls._finite(payload.get(k))
                if v is not None:
                    return v, f"PT_LIB sensor group {g}"
        return None, None

    @classmethod
    def _predicted_airspeed(cls, layer4, layer4_root=None):
        if not isinstance(layer4, Mapping):
            return None, None
        pool = layer4.get("verified_candidates") or layer4.get("pareto_front") or layer4.get("candidates") or []
        if not pool:
            return None, None
        c = pool[0] if isinstance(pool[0], Mapping) else {}
        traj = c.get("predicted_trajectory") or c.get("trajectory") or []
        if not traj:
            return None, None
        idx = None
        if layer4_root:
            try:
                import json
                schema = json.loads((Path(layer4_root) / "resources/state_schema.json").read_text())
                names = []
                for f in schema.get("fields", []):
                    name = f["name"]; n = int(f.get("length", 1))
                    names += [name] if n == 1 else [f"{name}_{i}" for i in range(n)]
                idx = names.index("airspeed_m_s")
            except Exception:
                idx = None
        if idx is not None:
            row = traj[1] if len(traj) > 1 else traj[0]
            if isinstance(row, (list, tuple)) and len(row) > idx:
                v = cls._finite(row[idx])
                if v is not None:
                    return v, str(c.get("candidate_id", "candidate"))
        return None, None

    @staticmethod
    def _euler_from_quaternion(qw, qx, qy, qz):
        n=math.sqrt(qw*qw+qx*qx+qy*qy+qz*qz)
        if not math.isfinite(n) or n < 1e-12:
            raise ValueError("invalid prediction quaternion")
        qw,qx,qy,qz=(v/n for v in (qw,qx,qy,qz))
        roll=math.atan2(2.0*(qw*qx+qy*qz),1.0-2.0*(qx*qx+qy*qy))
        pitch=math.asin(max(-1.0,min(1.0,2.0*(qw*qy-qz*qx))))
        yaw=math.atan2(2.0*(qw*qz+qx*qy),1.0-2.0*(qy*qy+qz*qz))
        return roll,pitch,yaw

    @classmethod
    def _matched_prediction_state(cls, layer4, layer5):
        """Return first-move WingMPET state for the candidate actually selected by L5."""
        l5=(layer5.get("result") if isinstance(layer5,Mapping)
            and isinstance(layer5.get("result"),Mapping) else layer5)
        decision=(l5 or {}).get("decision") if isinstance(l5,Mapping) else {}
        selected=str((decision or {}).get("candidate_id") or "")
        if not selected or not isinstance(layer4,Mapping):
            return {},None,{}
        # The bounded Native provider publishes a physical tube on the result,
        # while the WingMPET path publishes a first-move physical replay on the
        # selected candidate. Keep both contracts; never select pool[0].
        prediction=layer4.get('prediction') or {}
        native_request=layer4.get('request') or {}
        if isinstance(prediction,Mapping) and prediction.get('tube'):
            if str(native_request.get('candidate_id') or '') != selected:
                return {},None,{'reason':'selected_candidate_prediction_not_found'}
            segment=cls._finite(native_request.get('commit_segment_s'))
            row=next((r for r in prediction['tube'] if isinstance(r,Mapping)
                      and cls._finite(r.get('offset_s')) is not None and segment is not None
                      and abs(float(r['offset_s'])-segment)<1e-9),None)
            center=(row or {}).get('center') or []
            if len(center)<13 or not all(cls._finite(v) is not None for v in center):
                return {},None,{'reason':'native_first_move_state_incomplete'}
            roll,pitch,yaw=cls._euler_from_quaternion(*center[6:10])
            state=dict(zip(('velocity_n_m_s','velocity_e_m_s','velocity_d_m_s',
                            'roll_rad','pitch_rad','yaw_rad','p_rate_rad_s','q_rate_rad_s','r_rate_rad_s'),
                           [*center[3:6],roll,pitch,yaw,*center[10:13]]))
            return state,selected,{'timing_contract':{'execution_segment_s':segment,'executor_period_s':segment},
                'prediction_source':'selected current Native bounded first-move tube',
                'prediction_sha256':sha256_obj(prediction)}
        pools=[]
        for key in ("candidates","pareto_front","verified_candidates"):
            value=layer4.get(key)
            if isinstance(value,list): pools.extend(value)
        candidate=next((c for c in pools if isinstance(c,Mapping)
                        and str(c.get("candidate_id") or "")==selected),None)
        if candidate is None:
            return {},None,{"reason":"selected_candidate_prediction_not_found","candidate_id":selected}
        verification=candidate.get("verification") or {}
        steps=(verification.get("commit_segment_steps") or verification.get("steps") or [])
        replay=(candidate.get('first_move_prediction') or verification.get("first_move_prediction") or
                ((steps[0].get("physical_replay") or {}) if steps and isinstance(steps[0],Mapping) else {}))
        out=dict(replay.get("outputs") or {})
        try:
            roll,pitch,yaw=cls._euler_from_quaternion(*[float(out[k]) for k in
                ("next_qw","next_qx","next_qy","next_qz")])
            state={
                "velocity_n_m_s":float(out["next_vn"]),
                "velocity_e_m_s":float(out["next_ve"]),
                "velocity_d_m_s":float(out["next_vd"]),
                "roll_rad":roll,"pitch_rad":pitch,"yaw_rad":yaw,
                "p_rate_rad_s":float(out["next_p_rate"]),
                "q_rate_rad_s":float(out["next_q_rate"]),
                "r_rate_rad_s":float(out["next_r_rate"]),
            }
        except (KeyError,TypeError,ValueError):
            return {},None,{"reason":"selected_candidate_first_move_state_incomplete","candidate_id":selected}
        timing=dict(replay.get("timing_contract") or (candidate.get("verification") or {}).get("timing_contract") or {})
        return state,selected,{"timing_contract":timing,
            "predicted_airspeed_m_s":cls._finite(out.get("airspeed")),
            "prediction_source":"selected Layer4 candidate WingMPET first physical replay row"}

    @staticmethod
    def _state_semantics(keys):
        result={}
        for key in keys:
            if key.startswith('velocity_'):
                result[key]={'quantity':'velocity','unit':'m/s','frame':'NED'}
            elif key in ('roll_rad','pitch_rad','yaw_rad'):
                result[key]={'quantity':key[:-4],'unit':'rad','frame':'BODY_FRD_TO_NED'}
            else:
                result[key]={'quantity':'angular_velocity','unit':'rad/s','frame':'BODY_FRD'}
        return result

    @staticmethod
    def _network_snapshot_hash(snapshot, layer4, layer4_root=None):
        """Use the producer's canonical fields, never the whole JSON wrapper.

        The Layer4 hash algorithm is independent of its policy configuration.
        Resolve that implementation from the exact configured source package and
        compare its independently computed value with the actual Layer4 result.
        """
        declared=(layer4 or {}).get('snapshot_hash')
        if declared is None:
            # Compatibility for standalone non-operational contract fixtures.
            return str(snapshot.get('snapshot_hash') or sha256_obj(snapshot))
        root=Path(layer4_root or os.environ.get('TDDT_LAYER4_ROOT','')).resolve()
        if not (root/'src/layer4/snapshot.py').is_file():
            raise ValueError('exact Layer4 snapshot implementation root required')
        src=str(root/'src')
        if src not in sys.path:sys.path.insert(0,src)
        module=importlib.import_module('layer4.snapshot')
        try:Path(module.__file__).resolve().relative_to(root)
        except ValueError as exc:raise ValueError('Layer4 canonical hash implementation origin mismatch') from exc
        canonical=module.Layer3SnapshotAdapter(None).canonical_hash(snapshot)
        metadata=snapshot.get('snapshot_hash') or (snapshot.get('provenance') or {}).get('snapshot_hash')
        if declared!=canonical or metadata is not None and metadata!=canonical:
            raise ValueError('Layer3/Layer4 canonical snapshot hash mismatch')
        return canonical

    @classmethod
    def _realized_execution_state(cls, execution):
        """Extract post-step PT_LIB state without routing simulator truth into MESA."""
        if (execution or {}).get("source")=="Layer6/GUI/MAVLink":
            ex=execution or {}; st=ex.get("current_state") or {}
            att=st.get("attitude") or {}; rates=st.get("body_rates") or {}; obs=ex.get("x_obs") or {}
            vals={"roll_rad":cls._finite(att.get("roll_rad")), "pitch_rad":cls._finite(att.get("pitch_rad")),
                  "yaw_rad":cls._finite(att.get("yaw_rad")),
                  "p_rate_rad_s":cls._finite(rates.get("roll_rate_rad_s")),
                  "q_rate_rad_s":cls._finite(rates.get("pitch_rate_rad_s")),
                  "r_rate_rad_s":cls._finite(rates.get("yaw_rate_rad_s")),
                  **{k:cls._finite(obs.get(k)) for k in ("velocity_n_m_s","velocity_e_m_s","velocity_d_m_s")}}
            return (vals if all(v is not None for v in vals.values()) else {}),cls._finite(ex.get("plant_time_s")),{
                "state_source":"Layer6 GUI post-command telemetry","simulation_only":True,"not_navigation_authority":True}
        ex=dict(execution or {}); wr=ex.get("write_result") or {}
        candidates=[]
        if isinstance(wr,Mapping):
            latch=wr.get("latch_result") or {}; stream=wr.get("stream_status") or {}
            for rec in (latch,stream.get("last_result") or {},wr):
                if not isinstance(rec,Mapping): continue
                rel=rec.get("release_confirmation") or {}
                state=((rel.get("bridge") or {}).get("current_state") if isinstance(rel,Mapping) else None)
                if isinstance(state,Mapping): candidates.append(state)
        snap=ex.get("io_snapshot") or {}
        truth=((snap.get("truth_reference") or {}).get("payload") if isinstance(snap,Mapping) else None)
        if isinstance(truth,Mapping): candidates.append(truth)
        for state in candidates:
            vel=state.get("velocity")
            att=state.get("attitude")
            rates=state.get("angular_rates")
            if not (isinstance(vel,(list,tuple)) and len(vel)>=3
                    and isinstance(att,(list,tuple)) and len(att)>=3
                    and isinstance(rates,(list,tuple)) and len(rates)>=3):
                continue
            values={
                "velocity_n_m_s":cls._finite(vel[0]),"velocity_e_m_s":cls._finite(vel[1]),
                "velocity_d_m_s":cls._finite(vel[2]),"roll_rad":cls._finite(att[0]),
                "pitch_rad":cls._finite(att[1]),"yaw_rad":cls._finite(att[2]),
                "p_rate_rad_s":cls._finite(rates[0]),"q_rate_rad_s":cls._finite(rates[1]),
                "r_rate_rad_s":cls._finite(rates[2]),
            }
            if all(v is not None for v in values.values()):
                return {k:float(v) for k,v in values.items()},cls._finite(state.get("timestamp")),{
                    "state_source":"PT_LIB release-confirmation post-step state",
                    "simulation_only":True,"not_navigation_authority":True}
        return {},None,{"reason":"post_step_realized_state_unavailable"}

    @classmethod
    def build_input(cls, *, transaction_id, version, reference_time_s, execution_evidence,
                    layer4_result, layer5_result, network_snapshot, request, layer4_root=None):
        """Construct the strict Layer-7 contract from one version-consistent TDOC transaction.

        ONLINE_JSBSIM evidence is explicitly classified as simulation. It can close the
        feedback loop but cannot authorize persistent operational adaptation in Layer 7.
        """
        ex = dict(execution_evidence or {})
        native=ex.get('source')=='Layer6/GUI/MAVLink'
        raw_cmd=ex.get('command') or ex.get('u_cmd_pilot') or ex.get('u_cmd') or []
        cmd = list(raw_cmd.values()) if isinstance(raw_cmd,Mapping) else list(raw_cmd)
        axes = ["aileron", "elevator", "throttle", "rudder"]
        if isinstance(raw_cmd,Mapping):cmd=[raw_cmd.get(k) for k in axes]
        u_cmd = {axes[i]: float(cmd[i]) for i in range(min(4, len(cmd))) if cls._finite(cmd[i]) is not None}
        # PT_LIB write result is an acknowledgement; use explicit applied values only when present.
        wr = ex.get("write_result") or {}
        applied = dict(ex.get("u_applied_pilot") or ex.get('u_applied') or {}) if native else {}
        if isinstance(wr, Mapping):
            for i, a in enumerate(axes):
                for k in (a, f"applied_{a}", f"{a}_norm"):
                    if k in wr and cls._finite(wr[k]) is not None:
                        applied[a] = float(wr[k]); break
        observation_only=bool(ex.get("hold_observation_only"))
        if not applied and u_cmd and not observation_only and ex.get("source")!="Layer6/GUI/MAVLink":
            applied = dict(u_cmd)  # command acknowledgement fallback only for an actual command path.

        y_real = {}; y_hat = {}; sigma = {}; semantics = {}
        av_real, av_source = cls._truth_airspeed(ex)
        av_pred, av_pred_source = cls._predicted_airspeed(layer4_result, layer4_root)
        if av_real is not None and av_pred is not None:
            y_real["airspeed_m_s"] = av_real; y_hat["airspeed_m_s"] = av_pred; sigma["airspeed_m_s"] = 1.0
            semantics["airspeed_m_s"] = {"quantity":"true_airspeed", "unit":"m/s", "frame":"airmass-relative"}

        snap = dict(network_snapshot or {})
        snapshot_hash=cls._network_snapshot_hash(snap,layer4_result,layer4_root)
        std = cls._flatten_numeric(snap.get("std") or {}, limit=96)
        weights = cls._simple_numeric_map(snap.get("weights") or {})
        couplings = cls._simple_numeric_map(snap.get("couplings") or {})
        reps = ((snap.get("std") or {}).get("representations") or {}) if isinstance(snap.get("std"), Mapping) else {}
        validity={}; quality={}; reliability={}; freshness={}
        for name, rep in reps.items():
            if not isinstance(rep, Mapping): continue
            for dest, key, default in ((validity,"validity",1.0),(quality,"quality",1.0),(reliability,"reliability",0.5),(freshness,"freshness",1.0)):
                val=rep.get(key)
                if isinstance(val, Mapping): val=val.get("score", val.get("value"))
                fv=cls._finite(val)
                if fv is not None: dest[str(name)]=fv
        # Required property may be empty, but use known active twins when metadata is sparse.
        for name in ("MESA_NAV","WingMPET","MicroMetTwin"):
            if name in weights:
                validity.setdefault(name,1.0); quality.setdefault(name,1.0); reliability.setdefault(name,0.5); freshness.setdefault(name,1.0)

        mismatch = cls._finite(snap.get("mismatch"))
        if mismatch is None:
            mismatch = cls._finite((snap.get("center_of_consistency") or {}).get("cost") if isinstance(snap.get("center_of_consistency"), Mapping) else None) or 0.0
        margin = cls._finite(snap.get("consistency_margin")) or 0.0
        unc_values = cls._flatten_numeric(snap.get("uncertainty_td") or {}, limit=64)
        uncertainty = max([abs(v) for v in unc_values.values()] or [0.0])
        predicted_state,matched_candidate,predicted_state_provenance=cls._matched_prediction_state(
            layer4_result,layer5_result)
        realized_state,plant_ts,realized_state_provenance=cls._realized_execution_state(ex)
        rate_sync=(wr.get("rate_sync") or ex.get("rate_sync") or {}) if isinstance(wr,Mapping) else {}
        execution_tx=str(rate_sync.get("transaction_id") or (ex.get('provenance') or {}).get('transaction_id') or "") if isinstance(rate_sync,Mapping) else ""
        timing=dict(predicted_state_provenance.get("timing_contract") or {})
        segment_proof=ex.get('segment_closure_proof') or {}
        temporal={}
        state_pair_ok=bool(predicted_state and realized_state and execution_tx==str(transaction_id))
        if state_pair_ok:
            try:
                segment=float(timing["execution_segment_s"])
                executor=float(timing.get("executor_period_s",segment))
                state_pair_ok=bool(math.isfinite(segment) and segment>0.0 and abs(segment-executor)<=1e-12)
                if native:
                    bounds=ex.get('native_execution_duration_bounds_s') or {}
                    start=ex.get('execution_start_plant_time_bounds_s') or (segment_proof.get('first_event') or {}).get('plant_time_bounds') or {}
                    lower,upper=cls._finite(bounds.get('lower_s')),cls._finite(bounds.get('upper_s'))
                    first,last=cls._finite(start.get('lower_s')),cls._finite(start.get('upper_s'))
                    state_pair_ok=bool(state_pair_ok and all(v is not None for v in (lower,upper,first,last,plant_ts))
                                       and 0<lower<=upper<=segment+1e-9 and 0<=first<=last)
                    if state_pair_ok:
                        target={'lower_s':first+segment,'upper_s':last+segment}
                        distance=max(target['lower_s']-plant_ts,plant_ts-target['upper_s'],0.)
                        state_pair_ok=distance<=.05
                        temporal={'execution_duration_bounds_s':dict(bounds),
                                  'prediction_target_plant_time_bounds_s':target,
                                  'plant_time_s':plant_ts,'target_plant_timestamp':None,
                                  'timing_scope':'bounded first-move diagnostic; no exact exposure assumed'}
            except Exception:
                state_pair_ok=False
        if not state_pair_ok:
            predicted_state={}; realized_state={}
            # Incomparable physical evidence must reject. A command norm is not
            # an observed physical output and cannot replace the missing pair.
            y_real={};y_hat={};sigma={};semantics={}
        elif av_real is not None and cls._finite(predicted_state_provenance.get("predicted_airspeed_m_s")) is not None:
            y_real={"airspeed_m_s":float(av_real)}
            y_hat={"airspeed_m_s":float(predicted_state_provenance["predicted_airspeed_m_s"])}
            sigma={"airspeed_m_s":1.0}
            semantics={"airspeed_m_s":{"quantity":"true_airspeed","unit":"m/s","frame":"airmass-relative"}}
        if state_pair_ok and not y_real:
            # Both producers expose these physical coordinates with fixed units.
            y_real=dict(realized_state);y_hat=dict(predicted_state)
            sigma={k:1.0 for k in y_hat}
            semantics=cls._state_semantics(y_hat)
        ts = float(plant_ts if plant_ts is not None else reference_time_s)
        obs_id=f"{transaction_id}:observation"; dec_id=f"{transaction_id}:decision"; cmd_id=f"{transaction_id}:command"
        context=(segment_proof.get('context') or {})
        if native and context.get('command_id'):cmd_id=context['command_id']
        feedback_id=f'{transaction_id}:actuator-feedback'
        if native:
            source_meta=(segment_proof.get('snapshot') or {}).get('telemetry_metadata') or {}
            obs_id=(source_meta.get('SCALED_IMU') or {}).get('wire_sha256') or obs_id
            feedback_id=(source_meta.get('SERVO_OUTPUT_RAW') or {}).get('wire_sha256') or feedback_id
        native_meta={}
        if native:
            native_meta={'native_pilot_execution_requested':True,'command_domain':'layer6_pilot',
                'transaction_id':str(transaction_id),'candidate_id':matched_candidate,
                'snapshot_hash':snapshot_hash,
                'command_id':cmd_id,'flight_mode':(ex.get('current_state') or {}).get('mode') or (segment_proof.get('snapshot') or {}).get('mode'),
                'session_id':segment_proof.get('session_id') or (ex.get('provenance') or {}).get('session_id'),
                'native_session_id':(ex.get('provenance') or {}).get('native_session_id'),
                'command_correlation':(ex.get('provenance') or {}).get('command_correlation'),
                'intake_ack':(ex.get('provenance') or {}).get('intake_ack'),
                'segment_proof_sha256':sha256_obj(segment_proof),
                'state_semantics':cls._state_semantics(predicted_state),**temporal}
        l5 = layer5_result.get("result") if isinstance(layer5_result, Mapping) and isinstance(layer5_result.get("result"), Mapping) else layer5_result
        l5 = dict(l5 or {})
        decision = l5.get("decision") or {}
        explanation = l5.get("explanation") or {}
        hints = dict((request or {}).get("layer7_hints") or {})
        hints.setdefault("source_quality", min(quality.values()) if quality else 1.0)
        hints.setdefault("source_validity", min(validity.values()) if validity else 1.0)
        hints.setdefault("source_freshness", min(freshness.values()) if freshness else 1.0)
        hints.setdefault("source_credibility", min(reliability.values()) if reliability else 0.5)
        hints.setdefault("structural_error", abs(float(mismatch)))
        hints.setdefault("maturity", 0.0)
        hints.setdefault("authorized", False); hints.setdefault("identity_ok", False); hints.setdefault("policy_ok", False); hints.setdefault("trace_ok", True)
        for twin in weights: hints.setdefault("weight_bounds", {}).setdefault(twin, [0.0,1.0])
        for edge in couplings: hints.setdefault("coupling_bounds", {}).setdefault(edge, [0.0,1.0])
        context = snap.get("context") or {}
        if isinstance(context, Mapping):
            if context.get("crp_id") is not None: hints.setdefault("crp_id", str(context.get("crp_id")))
            if context.get("sarg_id") is not None: hints.setdefault("sarg_id", str(context.get("sarg_id")))

        return {
            "transaction_id": str(transaction_id), "v": str(version),
            "ExecutionEvidence": {
                "y_real": y_real, "u_cmd": u_cmd, "u_applied": applied,
                "x_obs": realized_state, "d_real": {}, "actuator_status": {"write_ack": bool(wr and wr.get("executed",True)), "direct_pwm": False, "observation_only": observation_only},
                "timestamp": ts, "planned_timestamp": None if native else ts, "version": str(version), "evidence_type": "simulation",
                "observation_id": obs_id, "input_observation_id": obs_id,
                "decision_id": dec_id, "correlates_to_decision_id": dec_id,
                "command_id": cmd_id, "correlates_to_actuator_command_id": cmd_id,
                "actuator_feedback_id": feedback_id,
                "output_semantics": semantics,
                "provenance": {"source":str(ex.get("source") or "Layer6/PT_LIB/JSBSim"), "evidence_hash":ex.get("evidence_hash"),
                               "truth_source":av_source, "applied_control_source":"none_observation_only" if observation_only else ("encoded MAVLink MANUAL_CONTROL write; FCU actuator output telemetry retained separately" if native else "PT_LIB explicit write result"),
                               "classification":"simulation_validation_fixture" if ex.get("validation_only") else "simulation_not_field", "observation_only":observation_only, "validation_only":bool(ex.get("validation_only")),
                               "state_transaction_match":state_pair_ok,"state_transaction_id":execution_tx,
                               "realized_state":realized_state_provenance,
                               **native_meta,'observation_kind':'physical_state' if realized_state else 'unavailable'},
            },
            "PredictionEvidence": {
                "y_hat_td": y_hat, "x_hat_td": predicted_state, "xi_hat": {}, "sigma_pred": sigma,
                "jc_pred": float(mismatch), "margin_pred": float(margin), "timestamp": ts, "version": str(version),
                "output_semantics": semantics,
                "provenance": {"source":"Layer4/Layer5 matched prediction", "candidate":matched_candidate or av_pred_source,
                               "state_transaction_match":state_pair_ok,"target_plant_timestamp":ts,
                               **predicted_state_provenance,**native_meta,
                               'prediction_kind':'trans_domain_model_state' if predicted_state else 'unavailable'},
            },
            "NetworkSnapshot": {
                "std": std, "weights": weights, "couplings": couplings, "topology": cls._topology_edges(snap.get("topology")),
                "coc_cost": float(mismatch), "margin": float(margin), "uncertainty": float(uncertainty),
                "guidance": jsonable(snap.get("guidance") or {}), "constraints": jsonable(snap.get("constraints") or {}),
                "epsilon": {}, "omega": {}, "hidden_loop_lci": cls._simple_numeric_map(snap.get("hidden_loop") or {}),
                "twin_validity": validity, "twin_quality": quality, "twin_freshness": freshness, "twin_reliability": reliability,
                "timestamp": ts, "version": str(version), "content_hash": snapshot_hash, "boundary_safe": True,
                "metadata": {"stability_ok": (snap.get("stability") or {}).get("stable", True) if isinstance(snap.get("stability"), Mapping) else True,
                             "source_snapshot_hash": sha256_obj(snap)},
            },
            "DecisionTrace": {
                "real_cost": None, "feasible_reference_cost": None,
                "constraint_violations": 0 if (decision.get("approval") is not False) else 1,
                "decision_confidence": cls._finite(decision.get("confidence", explanation.get("decision_confidence"))),
                "timestamp": ts, "version": str(version), "provenance": {"source":"Layer5", "decision_id":decision.get("decision_id")},
            },
            "AdaptationHints": hints,
        }

    def prepare_native_diagnostic(self, payload, *, execution_evidence, layer4_result,
                                  layer5_result, network_snapshot):
        """Bind one direct L6 result to one diagnostic invocation in this process.

        No JSON flag installs this capability. Recheck the actual bounded segment,
        encoded MAVLink witnesses, observation freshness and all projected values.
        This permission has no persistent-learning or actuation authority.
        """
        self._native_diagnostic=None
        try:
            from layer6.segments import validate_result
            from layer6.status import validate_observation_status
            from layer6.command_correlation import verified_send_bounds
            ex=execution_evidence;proof=ex['segment_closure_proof'];status=proof['snapshot']
            tx=payload['transaction_id'];ctx=proof['context'];instance=ctx['instance_id']
            policy=ex['execution_segment_policy'];provenance=ex['provenance']
            if (ex.get('source')!='Layer6/GUI/MAVLink' or ex.get('evidence_type')!='simulation'
                or not validate_result(proof,tx,instance,policy,before_session=proof['session_id'])
                or not validate_observation_status(status)['ok'] or verified_send_bounds(status,tx) is None):
                raise ValueError('invalid current Native segment/observation proof')
            stop=ex['latch_cancel_ack'];boot=provenance['native_session_id']
            if (not isinstance(boot,str) or len(boot)!=32
                or provenance['intake_ack'].get('native_session_id')!=boot
                or (status.get('status_quality') or {}).get('native_session_id')!=boot
                or stop.get('cancelled') is not True or stop.get('command_instance_id')!=instance
                or stop.get('transaction_id')!=tx):
                raise ValueError('Native session/cancellation identity mismatch')
            ep=payload['ExecutionEvidence'];pp=payload['PredictionEvidence'];meta=ep['provenance']
            state,stamp,_=self._realized_execution_state(ex)
            projected={'source':'Layer6/GUI/MAVLink','current_state':{
                'attitude':{k+'_rad':status['attitude'][k] for k in ('roll','pitch','yaw')},
                'body_rates':{k+'_rate_rad_s':status['attitude'][k+'_rate'] for k in ('roll','pitch','yaw')}},
                'x_obs':{k:status[k] for k in ('velocity_n_m_s','velocity_e_m_s','velocity_d_m_s')},
                'plant_time_s':status['plant_time_s']}
            actual,actual_ts,_=self._realized_execution_state(projected)
            requested=ctx['requested_gui_state']
            expected_cmd=dict(zip(('aileron','elevator','throttle','rudder'),
                                  [requested[k] for k in ('roll','pitch','throttle','yaw')]))
            l5=layer5_result.get('result') if isinstance(layer5_result.get('result'),Mapping) else layer5_result
            package=l5.get('layer6_package') or {}
            if ((l5.get('gates') or {}).get('FINAL') is not True
                or package.get('transaction_id')!=tx
                or package.get('u_cmd')!=[expected_cmd[k] for k in ('aileron','elevator','throttle','rudder')]):
                raise ValueError('current Layer5 FINAL/package identity mismatch')
            predicted,candidate,pred_meta=self._matched_prediction_state(layer4_result,layer5_result)
            segment=float(pred_meta['timing_contract']['execution_segment_s'])
            start=proof['first_event']['plant_time_bounds']
            target={k:float(start[k])+segment for k in ('lower_s','upper_s')}
            if (state!=actual or ep['x_obs']!=actual or stamp!=actual_ts or ep['timestamp']!=actual_ts
                or ep['u_cmd']!=expected_cmd or ep['u_applied']!=status['applied_pilot_command']
                or ep['command_id']!=ctx['command_id'] or meta['session_id']!=proof['session_id']
                or ep['actuator_feedback_id']!=status['telemetry_metadata']['SERVO_OUTPUT_RAW']['wire_sha256']
                or ep['observation_id']!=status['telemetry_metadata']['SCALED_IMU']['wire_sha256']
                or meta['command_correlation']!=status['command_correlation']
                or meta['segment_proof_sha256']!=sha256_obj(proof)
                or meta['native_session_id']!=boot or meta['flight_mode']!=status['mode']
                or meta['candidate_id']!=provenance['candidate_id']
                or meta['snapshot_hash']!=provenance['snapshot_hash']
                or meta['snapshot_hash']!=payload['NetworkSnapshot']['content_hash']
                or meta['snapshot_hash']!=self._network_snapshot_hash(network_snapshot,layer4_result,
                    self.config.path_value('TDDT_LAYER4_ROOT'))
                or candidate!=meta['candidate_id'] or pp['x_hat_td']!=predicted
                or meta['prediction_target_plant_time_bounds_s']!=target
                or pp['provenance']['prediction_target_plant_time_bounds_s']!=target
                or segment!=policy['max_duration_s']
                or meta['state_transaction_match'] is not True
                or pp['provenance']['state_transaction_match'] is not True
                or meta['execution_duration_bounds_s']!=proof['duration_bounds_s']):
                raise ValueError('Native diagnostic projection differs from direct execution evidence')
            if set(ep['y_real'])==set(actual):
                if ep['y_real']!=actual or pp['y_hat_td']!=predicted:
                    raise ValueError('Native physical output projection mismatch')
            elif set(ep['y_real'])=={'airspeed_m_s'}:
                if (ep['y_real']['airspeed_m_s']!=status['airspeed_m_s']
                    or pp['y_hat_td']['airspeed_m_s']!=pred_meta.get('predicted_airspeed_m_s')):
                    raise ValueError('Native airspeed output projection mismatch')
            else:
                raise ValueError('Native diagnostic has no comparable physical output')
            self._ensure()
            inp=self.module.Layer7SideModule.input_from_dict(payload)
            self._native_diagnostic=sha256_obj(inp)
            return {'ok':True,'scope':'MODEL_PREDICTION_NATIVE_EXECUTION_DIAGNOSTIC',
                    'persistent_learning_allowed':False}
        except (KeyError,TypeError,ValueError,AttributeError) as exc:
            return {'ok':False,'reason':str(exc),'persistent_learning_allowed':False}

    def _validate_native_diagnostic(self, inp):
        return self._native_diagnostic is not None and sha256_obj(inp)==self._native_diagnostic

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure()
        try:
            out = self.side.process(payload)
        finally:
            self._native_diagnostic=None
        sync=self.kstore_workspace.sync()
        result=jsonable(out)
        accepted=result.get('evidence_ok') is True and result.get('mode')!='REJECT'
        has_feedback=any(result.get(k) for k in ('feedback_layer2','feedback_layer31','feedback_layer32','feedback_layer33'))
        return {"ok": accepted, "status": "OK" if accepted else "REJECTED", "result": result,
                'feedback_evidence_valid':accepted,'no_change':bool(accepted and not has_feedback
                    and (result.get('commit') or {}).get('status') in ('NO_CHANGE','TRANSIENT_ONLY')),
                "registration_descriptor": self.side.registration_descriptor(),
                "provenance": {"invocation_owner":"TDOC/RuntimeScheduler", "external_root":str(self.root),
                               "external_version":getattr(self.module,"__version__",None),
                               "kstore_root":str(self.kstore_root),"kstore_compressed_at_rest":True,"kstore_sync":sync}}

    def close(self):
        try: self.kstore_workspace.close()
        except Exception: pass
        self.side = None
        self.module = None
