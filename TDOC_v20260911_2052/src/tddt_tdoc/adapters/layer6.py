from __future__ import annotations

import copy
from collections import deque
import hashlib
import importlib
import math
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

from ..utils import sha256_obj
from ..layer6_bridge import Layer6Bridge
from ..layer6_diagnostics import Layer6PackageError, inspect_client_api


class _NativeIMUHistoryReader:
    """Read-only transport retention across slow L2/L4 work; never a current-state substitute."""

    def __init__(self,client,status,*,capacity=8192):
        from layer6.status import validate_observation_status
        if not validate_observation_status(status)['ok']:
            raise ValueError('fresh Native observation required before history retention')
        self.client=client;self.session=status['session_id'];self.domain=status['monotonic_domain_id']
        self.native_session=(status.get('status_quality') or {}).get('native_session_id')
        self.target=(status['target_system'],status['target_component'])
        self.anchor=status['telemetry_metadata']['SCALED_IMU']['history_sequence']
        self.anchor_plant_time_s=status['imu']['plant_time_s']
        self.anchor_wire_sha256=status['imu']['wire_sha256']
        if type(self.anchor) is not int or self.anchor<1 or type(capacity) is not int or capacity<1:
            raise ValueError('invalid Native history cursor/capacity')
        self.capacity=capacity;self.records=deque();self.cursor=self.anchor-1
        self.evicted_through=0;self.last_primary=None;self.last_source=None;self.last_receipt=None
        self.error=None;self.condition=threading.Condition();self.stopping=threading.Event();self.thread=None

    def _accept_page(self,page,after,through):
        from layer6.telemetry_history import SCHEMA
        if (not isinstance(page,dict) or page.get('schema')!=SCHEMA or page.get('ok') is not True
            or page.get('session_id')!=self.session or page.get('after_seq')!=after
            or page.get('channels')!=['SCALED_IMU'] or page.get('retention_gap') is not False
            or page.get('lost_unfiltered_samples')!=0 or type(page.get('through_seq')) is not int
            or page['through_seq']<after or through is not None and page['through_seq']!=through
            or type(page.get('has_more')) is not bool or type(page.get('next_after_seq')) is not int):
            raise ValueError('Native retained-history page lost, changed session, or invalid cursor')
        records=page.get('samples');contracts=page.get('sample_contracts')
        if not isinstance(records,list) or not isinstance(contracts,list) or len(records)!=len(contracts):
            raise ValueError('Native raw history/contracts missing')
        cutoff=page['through_seq'];next_cursor=page['next_after_seq']
        if (page['has_more'] and (not records or not after<next_cursor<=cutoff or next_cursor!=records[-1].get('history_sequence'))
            or not page['has_more'] and next_cursor!=cutoff):
            raise ValueError('Native history pagination did not advance to its declared cutoff')
        origin_hash=sha256_obj(page);accepted=[];previous_sequence=self.last_primary
        previous_source=self.last_source;previous_receipt=self.last_receipt
        for raw,contract in zip(records,contracts):
            seq=raw.get('history_sequence');stamp=raw.get('source_time') or {};receipt=raw.get('timestamp_monotonic')
            normalized=contract.get('normalized') or {};wire=bytes.fromhex(raw.get('wire_hex',''))
            if (type(seq) is not int or not after<seq<=cutoff
                or previous_sequence is None and seq!=self.anchor
                or previous_sequence is not None and (seq<=previous_sequence or raw.get('previous_channel_sequence')!=previous_sequence)
                or raw.get('type')!='SCALED_IMU' or raw.get('source')!='MAVLink/SCALED_IMU'
                or raw.get('session_id')!=self.session or raw.get('monotonic_domain_id')!=self.domain
                or (raw.get('source_system'),raw.get('source_component'))!=self.target
                or raw.get('valid') is not True or not wire or hashlib.sha256(wire).hexdigest()!=raw.get('wire_sha256')
                or previous_sequence is None and raw.get('wire_sha256')!=self.anchor_wire_sha256
                or stamp.get('field')!='time_boot_ms' or stamp.get('unit')!='ms' or stamp.get('clock_domain')!='FCU_BOOT'
                or type(stamp.get('value')) is not int or not 0<=stamp['value']<2**32
                or previous_source is not None and stamp['value']<=previous_source
                or not isinstance(receipt,(int,float)) or isinstance(receipt,bool) or not math.isfinite(receipt)
                or receipt<0 or previous_receipt is not None and receipt<previous_receipt
                or (raw.get('freshness_basis') or {}).get('clock_invalid') is not False
                or normalized.get('available') is not True or normalized.get('frame')!='BODY_FRD'
                or normalized.get('source_clock_domain')!='FCU_BOOT'
                or contract.get('history_sequence')!=seq or contract.get('wire_sha256')!=raw.get('wire_sha256')):
                raise ValueError('Native retained IMU wire/source/progression mismatch')
            accepted.append((copy.deepcopy(raw),copy.deepcopy(contract),origin_hash))
            previous_sequence=seq;previous_source=stamp['value'];previous_receipt=receipt
        with self.condition:
            for row in accepted:
                if len(self.records)>=self.capacity:self.evicted_through=self.records.popleft()[0]['history_sequence']
                self.records.append(row)
            self.last_primary=previous_sequence;self.last_source=previous_source;self.last_receipt=previous_receipt
            self.cursor=next_cursor;self.condition.notify_all()
        return cutoff,page['has_more']

    def poll_once(self):
        """One bounded GUI snapshot; original sample clocks and receipts are untouched."""
        through=None
        for _ in range(64):
            if self.stopping.is_set():return
            after=self.cursor
            page=self.client.telemetry_history(after_seq=after,limit=64,channels=['SCALED_IMU'],
                session_id=self.session,through_seq=through,timeout=.2)
            if page is None:
                raise TimeoutError('Native history transport response timed out')
            through,more=self._accept_page(page,after,through)
            if not more:return
        raise ValueError('Native history acquisition exceeded bounded page budget')

    def _run(self):
        timeouts=0
        try:
            while not self.stopping.is_set():
                try:
                    self.poll_once()
                    timeouts=0
                except TimeoutError:
                    # Retry the original cursor. No sample, receipt or clock is
                    # synthesized; the next actual page must still prove that
                    # no source samples were evicted or changed in the interval.
                    timeouts+=1
                    if timeouts>=3:
                        raise RuntimeError('Native history transport unavailable after 3 consecutive timeouts')
                self.stopping.wait(.1)
        except Exception as exc:
            if not self.stopping.is_set():
                with self.condition:self.error=repr(exc);self.condition.notify_all()
        finally:self.client.close()

    def start(self):
        self.thread=threading.Thread(target=self._run,name='TDOC-Native-IMU-retention',daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread is not None:self.thread.join(timeout=1.)
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError('Native history reader did not stop within its bounded query budget')

    def window(self,after_seq,through_seq,*,timeout=1.):
        if type(after_seq) is not int or type(through_seq) is not int or not 0<=after_seq<=through_seq:
            raise ValueError('invalid retained Native history window')
        end=time.monotonic()+timeout
        with self.condition:
            while self.cursor<through_seq and self.error is None and not self.stopping.is_set():
                remaining=end-time.monotonic()
                if remaining<=0:raise RuntimeError('Native retained-history reader has not reached current observation')
                self.condition.wait(remaining)
            if self.error:raise RuntimeError('Native history retention failed: '+self.error)
            if self.stopping.is_set():raise RuntimeError('Native history retention stopped')
            if after_seq<self.evicted_through:raise ValueError('Native primary IMU history evicted; no interval repair')
            rows=[copy.deepcopy(row) for row in self.records if after_seq<row[0]['history_sequence']<=through_seq]
        if after_seq<through_seq and (not rows or rows[0][0].get('previous_channel_sequence') not in (after_seq,None if after_seq==0 else after_seq)
                or rows[-1][0]['history_sequence']!=through_seq):
            raise ValueError('Native primary IMU history does not cover the exact MESA/current interval')
        pages=[];cursor=after_seq
        for start in range(0,len(rows),64):
            selected=rows[start:start+64];more=start+64<len(rows)
            pages.append({'schema':'Layer6-telemetry-history-v1','type':'telemetry_history','ok':True,
                'session_id':self.session,'after_seq':cursor,'through_seq':through_seq,'channels':['SCALED_IMU'],
                'retention_gap':False,'lost_unfiltered_samples':0,'samples':[row[0] for row in selected],
                'sample_contracts':[row[1] for row in selected],'has_more':more,
                'next_after_seq':selected[-1][0]['history_sequence'] if more else through_seq,
                'limit':64,'retention_origin':'TDOC-process-local-primary-ring',
                'original_gui_page_sha256':list(dict.fromkeys(row[2] for row in selected)),
                'guidance_state_qualified':False})
            cursor=pages[-1]['next_after_seq']
        return pages

    def source_window(self,start_s,end_s,*,timeout=1.):
        """Inclusive real source witnesses for an acknowledged plant interval."""
        if (not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (start_s,end_s))
            or not 0<=start_s<=end_s):
            raise ValueError('invalid Native clock continuity interval')
        deadline=time.monotonic()+timeout
        with self.condition:
            while (self.last_source is None or self.last_source*.001+1e-8<end_s) and self.error is None:
                remaining=deadline-time.monotonic()
                if remaining<=0 or self.stopping.is_set():raise RuntimeError('Native retained clock interval unavailable')
                self.condition.wait(remaining)
            if self.error:raise RuntimeError('Native history retention failed: '+self.error)
            first=next((r[0] for r in self.records if abs(r[0]['source_time']['value']*.001-start_s)<=1e-8),None)
            last=next((r[0] for r in self.records if abs(r[0]['source_time']['value']*.001-end_s)<=1e-8),None)
            if first is None or last is None:raise ValueError('Native clock interval evicted or has no exact source witnesses')
            after=first.get('previous_channel_sequence') or 0;through=last['history_sequence']
        return after,self.window(after,through,timeout=timeout)


class Layer6OnlineAdapter:
    """TDOC-owned Online boundary into the public native Layer6 API.

    GUI is the only MAVLink owner. Layer5 FINAL is the only command authority.
    Package/API preflight is read-only; optional ground ARM has separate Layer5 authority.
    """
    layer_id = 6
    MODES = ("ONLINE_JSBSIM",)
    SOURCE = "Layer6/GUI/MAVLink"

    def __init__(self, config, layer5_adapter):
        self.config = config
        self.layer5 = layer5_adapter
        self._client = None
        self._session_open = False
        self._owns_stack = False
        self._episode_id = None
        self._session_generation = 0
        self._origin = None
        self._active_tx = None
        self._environment_local = threading.local()

    @property
    def _environment_status_witness(self):
        return getattr(getattr(self,'_environment_local',None),'witness',None)

    @_environment_status_witness.setter
    def _environment_status_witness(self,value):
        if not hasattr(self,'_environment_local'):self._environment_local=threading.local()
        self._environment_local.witness=value

    def _reject_environment(self,code,**details):
        self._environment_rejection={'code':code,'checked_monotonic':time.monotonic(),**details}
        return False

    @property
    def _environment_rejection(self):
        return getattr(getattr(self,'_environment_local',None),'rejection',None)

    @_environment_rejection.setter
    def _environment_rejection(self,value):
        if not hasattr(self,'_environment_local'):self._environment_local=threading.local()
        self._environment_local.rejection=value

    def _option(self, name, default=None):
        return self.config.get("layers.layer6.online." + name, default)

    def _module(self):
        root = self.config.path_value("TDDT_LAYER6_ROOT")
        if not root or not root.is_dir():
            raise Layer6PackageError("package_root_unavailable", actual_root=str(root) if root else None)
        expected = self._option("expected_package", "layer6_v20261007_1032")
        if root.name != expected:
            raise Layer6PackageError("package_root_mismatch", expected_root_name=expected, actual_root_name=root.name)
        src = str(root / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        for name in ("layer6", "layer6.client", "layer6.contracts", "layer6.validation", "layer6.feedback"):
            module = importlib.import_module(name)
            origin = getattr(module, "__file__", None)
            try:
                if origin is None:
                    raise ValueError("module has no file origin")
                Path(origin).resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise Layer6PackageError("module_origin_mismatch", module=name,
                    expected_under=str(root.resolve()), actual_origin=str(origin) if origin else None) from exc
        return sys.modules["layer6.client"]

    def preflight(self):
        from ..diagnostics import event
        root = None
        required = ["src/layer6/client.py", "src/layer6/contracts.py", "src/layer6/validation.py",
                    "src/layer6/feedback.py", "src/run_flightstack.py", "scripts/run_layer6_stack.sh",
                    "FlightStack/ardupilot", "FlightStack/jsbsim", "FlightStack/models/phoenix_v3"]
        required_api = ("submit", "cancel", "native_session_status", "segment_status", "gui_status", "ensure_mavlink", "wait_fcu_ready", "build_layer7_input", "close")
        expected_package = self._option("expected_package", "layer6_v20261007_1032")
        expected_version = self._option("expected_version", "2026.10.7.0937")
        failures = []
        probe = {"schema": "TDOC-Layer6-preflight-diagnostics-v1", "ok": False, "root": None,
                 "external_only": True, "expected_package": expected_package, "actual_package": None,
                 "expected_version": expected_version, "actual_version": None, "version": None,
                 "version_match": False, "missing": [], "api_inspected": False, "api_ok": False,
                 "missing_api": [], "signature_mismatches": [], "api_inspection_errors": [],
                 "actual_signatures": {}, "required_api": list(required_api),
                 "transport": "Layer6Client_GUI_MAVLink", "runtime_started": False,
                 "failures": failures}
        stage = "root"

        def failed(code, **details):
            failures.append({"code": code, **details})

        try:
            stage="ground_policy"
            from ..native_ground_policy import require_native_contacts
            try:require_native_contacts(self.config)
            except ValueError as exc:
                raise Layer6PackageError('ground_contact_policy_rejected',reason=str(exc)) from exc
            stage="root"
            root = self.config.path_value("TDDT_LAYER6_ROOT")
            probe["root"] = str(root) if root else None
            probe["actual_package"] = root.name if root else None
            if not root or not root.is_dir():
                raise Layer6PackageError("package_root_unavailable", actual_root=probe["root"])
            if root.name != expected_package:
                failed("package_root_mismatch", expected=expected_package, actual=root.name)
            try:
                root.resolve().relative_to(self.config.package_root.resolve())
                probe["external_only"] = False
            except ValueError:
                pass
            if not probe["external_only"]:
                failed("layer6_must_be_external", actual_root=str(root))
            probe["missing"] = [r for r in required if not (root/r).exists()]
            if probe["missing"]:
                failed("missing_package_paths", paths=probe["missing"])
            stage = "metadata"
            try:
                version = tomllib.loads((root/"pyproject.toml").read_text())["project"]["version"]
                probe["version"] = probe["actual_version"] = version
                probe["version_match"] = version == expected_version
                if not probe["version_match"]:
                    failed("version_mismatch", expected=expected_version, actual=version)
            except Exception as exc:
                failed("version_metadata_error", expected=expected_version, actual=None, error=repr(exc))
            stage = "api"
            # Never inspect a rejected package location. A version mismatch alone
            # does not hide independent API defects at the configured source root.
            if root.name == expected_package and probe["external_only"]:
                module = self._module()
                probe.update(inspect_client_api(getattr(module, "Layer6Client", None), required_api))
                if probe["missing_api"]:
                    failed("missing_api", methods=probe["missing_api"])
                if probe["signature_mismatches"]:
                    failed("signature_mismatch", mismatches=probe["signature_mismatches"])
                if probe["api_inspection_errors"]:
                    failed("api_inspection_error", errors=probe["api_inspection_errors"])
            probe["ok"] = not failures and probe["api_ok"] and probe["version_match"]
        except Exception as exc:
            probe["error"] = repr(exc)
            probe["error_stage"] = stage
            failed(exc.code if isinstance(exc, Layer6PackageError) else "api_import_error" if stage == "api" else "preflight_error",
                   stage=stage, error=repr(exc), **(exc.details if isinstance(exc, Layer6PackageError) else {}))
        probe["failure_reasons"] = list(dict.fromkeys(f["code"] for f in failures))
        report = {"ok": probe["ok"], "online_ready": probe["ok"], "implemented": True,
                "status": "READY" if probe["ok"] else "ONLINE_NOT_READY", "layer6_client": probe,
                "reason": probe["failure_reasons"][0] if failures else "package_api_ready",
                "failure_reasons": probe["failure_reasons"], "flight_ready": False, "actuation_executed": False,
                "modes": list(self.MODES), "offline_in_path": False,
                "readiness_scope": "PACKAGE_API_ONLY; live GUI/FCU readiness checked during session preparation",
                "ownership": "TDOC/RuntimeScheduler -> Layer6Client -> GUI -> MAVLink"}
        event("layer6.preflight", result=report)
        return report

    def _get_client(self):
        if self._client is None:
            probe = self.preflight()
            if not probe["ok"]:
                raise RuntimeError(f"Layer6 preflight failed: {probe}")
            module = self._module()
            self._client = module.Layer6Client(
                endpoint=self._option("endpoint", "127.0.0.1:14661"),
                gui_endpoint=self._option("gui_endpoint", "127.0.0.1:14660"),
                timeout=float(self._option("execution_confirmation_timeout_s", 2.0)),
                start_if_needed=False,
                flightstack_dir=str(self.config.path_value("TDDT_LAYER6_ROOT")/"FlightStack"),
                startup_timeout=float(self._option("startup_timeout_s", 30.0)))
        return self._client

    @staticmethod
    def _finite(value):
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    def _fresh(self, status):
        if not isinstance(status, dict) or status.get("mavlink_connected") is not True:
            return False
        if 'schema' in status or 'telemetry_metadata' in status:
            from layer6.status import validate_observation_status
            if not validate_observation_status(status)['ok']:return False
        ages = status.get("telemetry_age_s") or {}
        limit = float(self._option("telemetry_max_age_s", 2.0))
        heartbeat_limit = float(self._option("heartbeat_max_age_s", 2.0))
        if self._option("require_body_attitude_quaternion",False):
            age=ages.get("ATTITUDE_QUATERNION")
            if not (self._finite(age) and 0<=float(age)<=limit
                    and (status.get("attitude") or {}).get("source")=="MAVLink/ATTITUDE_QUATERNION"
                    and (status.get("attitude") or {}).get("frame")=="BODY_FRD_TO_NED"):
                return False
        return (self._finite(ages.get("HEARTBEAT"))
                and 0 <= float(ages["HEARTBEAT"]) <= heartbeat_limit
                and all(self._finite(ages.get(name)) and 0 <= float(ages[name]) <= limit
                        for name in ("ATTITUDE", "VFR_HUD", "GLOBAL_POSITION_INT")))

    def prepare_online_transport_session(self):
        from ..diagnostics import event
        stage="ground_policy"
        try:
            from ..native_ground_policy import require_native_contacts
            require_native_contacts(self.config)
            stage="binding"
            event("layer6.prepare.begin",transport="Layer6Client_GUI_MAVLink")
            from ..native_environment import prepare_binding
            require_env=self._option("require_bound_environment",False)
            if require_env:
                self._module()
                from layer6.native_session import active_binding
                reused=active_binding(self.config)
                if reused:
                    self._environment_binding_path,self._environment_binding=reused
                else:
                    self._environment_binding=prepare_binding(self.config,manifest_path=getattr(self,"offline_knowledge_manifest",None))
                    self._environment_binding_path=self.config.path_value('TDDT_LAYER6_ROOT',required=True)/'runtime/tdoc_environment_binding.json'
            stage="stack_start"
            client = self._get_client()
            if self._option("start_if_needed", True) and not client.stack_responding():
                client.ensure_stack_started(timeout=float(self._option("startup_timeout_s", 30.0)))
                self._owns_stack = bool(getattr(client,'_owned_native_session',None))
            stage="mavlink_connect"
            event("layer6.prepare.stage",stage=stage)
            client.ensure_mavlink(timeout=float(self._option("startup_timeout_s", 30.0)))
            stage="fcu_ready"
            event("layer6.prepare.stage",stage=stage)
            status = client.wait_fcu_ready(timeout=float(self._option("startup_timeout_s", 30.0)))
            if not self._fresh(status):
                raise RuntimeError("fresh FCU telemetry unavailable")
            stage="forcing_confirmation"
            event("layer6.prepare.stage",stage=stage,status=status)
            if require_env:
                # Launcher/Adapter must consume this exact ID. Never replace it
                # after FCU startup or silently rebase climate to connection time.
                end=time.monotonic()+float(self._option("startup_timeout_s",30.0))
                self._forcing_status_diagnostics=[]
                while time.monotonic()<end:
                    poll_started=time.monotonic()
                    status,environment_ready=self._acquire_environment_status(timeout=.2)
                    self._forcing_status_diagnostics.append({'elapsed_s':time.monotonic()-poll_started,'received':status is not None,
                        'reason':(status or {}).get('reason'),'environment_reason':((status or {}).get('environment_contract') or {}).get('error')})
                    if environment_ready: break
                else: raise RuntimeError("native environment forcing not confirmed; start stack after prepare_native_layer6_session.py")
            stage="live_channels"
            event("layer6.prepare.stage",stage=stage,status=status)
            if self._option("require_live_channels",False):
                end=time.monotonic()+float(self._option("startup_timeout_s",30.0))
                stable=0;previous=None;last_error="live channels unavailable"
                while time.monotonic()<end:
                    channels_valid=False
                    try:
                        status,environment_ready=self._acquire_environment_status(timeout=.2)
                        if not environment_ready:
                            raise ValueError('fresh native telemetry/forcing required')
                        if self._option("require_fcu_estimator_ready",False):
                            from ..native_control_binding import fcu_estimator_ready
                            if not fcu_estimator_ready(status,require_horizontal_position=getattr(self,'readiness_requires_horizontal_position',True)):
                                raise ValueError("FCU estimator/pre-arm health not ready: "+str({"ekf_status":(status or {}).get("ekf_status"),"fcu_health":(status or {}).get("fcu_health")}))
                        from layer6.status import CORE_CHANNELS,validate_observation_status
                        if not validate_observation_status(status,required=(*CORE_CHANNELS,'SERVO_OUTPUT_RAW','SYS_STATUS'))['ok']:
                            raise ValueError('fresh native channels required')
                        plant=float(status['plant_time_s'])
                        mode=str(status.get("mode") or "").upper()
                        if mode in {"", "INITIALISING", "INITIALIZING", "UNKNOWN", "NONE"}:
                            raise ValueError("FCU is not ready")
                        stable=stable+1 if previous is not None and plant>previous else 1
                        previous=plant
                        channels_valid=True
                        # Channel continuity and current forcing are independent
                        # checks. An in-progress wind write cannot release startup;
                        # it also must not erase already observed fresh telemetry.
                        live=self._ingress_from_status(status)
                        if stable>=3:break
                    except (RuntimeError,ValueError,TypeError,KeyError) as exc:
                        if not channels_valid:stable=0
                        last_error=str(exc)
                    time.sleep(0.03)
                else:raise RuntimeError("native live-channel readiness timeout: "+last_error)
            native_status=client.native_session_status()
            if not native_status.get('process_ready') or native_status.get('session_id')!=(status.get('status_quality') or {}).get('native_session_id'):
                raise RuntimeError('native process/session readiness mismatch')
            self._session_open = True
            self._start_native_history_reader(status)
            if float(self.config.get('tdoc.rtmo.initial_navigation.ground_calibration_seconds',0.) or 0.)>0:
                status=self._prepare_ground_calibration(status)
                native_status=client.native_session_status()
            # Observed origin is diagnostic; configured origin validation remains TDOC's responsibility.
            self._origin = {k: status.get(k) for k in ("latitude_deg", "longitude_deg", "altitude_msl_m")}
            self._origin.update(heading_deg=status.get("heading_deg"), initial_airspeed_m_s=status.get("airspeed_m_s"))
            if require_env:
                known=dict(self._environment_binding["mission_origin"])
                from ..unit_adapter import UnitAdapterBinding
                unit=UnitAdapterBinding(self.config);unit.activate()
                from tdoc_unit_converter import adapters as iso
                reference=status
                if getattr(self,'origin_validation_physical',False):
                    from ..native_control_binding import native_physical_origin_reference
                    reference=native_physical_origin_reference(native_status,status)
                observed=iso.wgs84_to_ecef(math.radians(float(reference["latitude_deg"])),math.radians(float(reference["longitude_deg"])),float(reference["altitude_msl_m"]))
                target=iso.wgs84_to_ecef(math.radians(known["latitude_deg"]),math.radians(known["longitude_deg"]),known["altitude_msl_m"])
                if math.sqrt(sum((float(a)-float(b))**2 for a,b in zip(observed,target)))>float(self._option("origin_tolerance_m",50.0)):
                    raise ValueError("live FCU validation position does not match surveyed mission origin")
                self._origin.update(known)
                self._origin["initial_airspeed_m_s"]=0.0
            return {"ok": True, "status": "READY", "transport": "Layer6Client_GUI_MAVLink",
                    "native_session":native_status,"flight_ready":False,
                    "session_generation": self._session_generation, "startup_actuation": False,
                    "live_channels_verified":bool(self._option("require_live_channels",False)),
                    "ready": True, "gnss_denied_validated": False, "gps_role":"validation_reference_only",
                    "environment_contract":status.get("environment_contract") or {}, "current_gui_state": status,
                    "semantics": "Connection readiness only; Layer5 safety/certification and GNSS-denied deployment validation remain required"}
        except Exception as exc:
            self._session_open=False
            cleanup_error=None
            try:self._stop_native_history_reader()
            except Exception as stop_error:cleanup_error=repr(stop_error)
            if self._client is not None and getattr(self._client,'_owned_native_session',None):
                self._owns_stack=True
                try:self._client.stop_stack();self._owns_stack=False
                except Exception as stop_error:cleanup_error=repr(stop_error)
            event("layer6.prepare.failed",stage=stage,error=repr(exc),traceback=True)
            return {"ok": False, "status": "NOT_READY", "stage":stage,"error": repr(exc), "cleanup_error":cleanup_error,
                    "last_gui_state":locals().get('status'),"forcing_status_diagnostics":getattr(self,'_forcing_status_diagnostics',None),
                    "environment_rejection":getattr(self,'_environment_rejection',None),"actuation_executed": False}

    def prepare_online_control_session(self):
        out = self.prepare_online_transport_session()
        if not out.get("ok"):
            return out
        readiness = self.layer5.online_control_readiness()
        blockers=list(readiness.get("flight_release_blockers") or [])
        if blockers or readiness.get("nominal_control_ready") is not True:
            # Transport readiness is not flight readiness. Do not ARM and enter
            # a mission that this provider already knows it cannot authorize.
            from ..diagnostics import event
            reason=blockers[0] if blockers else "layer5_nominal_control_not_ready"
            event("layer6.prepare.control_blocked",reason=reason,
                  flight_release_blockers=blockers,actuation_executed=False)
            return {**out,"ok":False,"ready":False,"status":"NOT_READY",
                    "transport_ready":True,"stage":"control_prediction",
                    "layer5":readiness,"reason":reason,
                    "startup_actuation":False,"actuation_executed":False}
        if self._option("ground_arm_bootstrap",False):
            out["ground_arm"]={"ok":False,"status":"DEFERRED_TO_CURRENT_D_EXEC",
                "reason":"ground_arm_requires_current_verified_D_exec",
                "actuation_executed":False}
        return {**out, "ok": bool(readiness.get("ok")),
                "status": "READY" if readiness.get("ok") else "NOT_READY", "layer5": readiness,
                "reason": "ready" if readiness.get("ok") else "layer5_control_certification_required",
                "startup_actuation":False,
                "semantics": "Readiness only; ARM and other actuation require a current verified Layer5 D_exec through invoke"}

    def _prepare_ground_arm(self):
        """Retired status-only bootstrap; ARM requires a verified D_exec.

        Do not emit preparation commands before authority is established. The
        regular invoke path verifies current Layer5/provider evidence and then
        submits the complete decision through Layer6Client.
        """
        raise RuntimeError("ground_arm_requires_current_verified_D_exec")

    def commission_online_control(self):
        binding=self.layer5.ensure_native_control_binding()
        return {"ok":bool(binding.get("ok")),"status":"READY" if binding.get("ok") else "NOT_READY",
                "reason":"accepted_offline_model_prior_loaded" if binding.get("ok") else "accepted_offline_model_prior_required",
                "binding":binding,"actuation_executed":False,
                "semantics":"Read-only accepted Offline model prior; physical execution remains separately checked"}

    def reset_online_session(self, reason="manual_reset", episode_id=None):
        errors = []
        try:self._stop_native_history_reader()
        except Exception as exc:errors.append(repr(exc))
        if self._client is not None:
            try:
                self._cancel_active(reason)
            except Exception as exc:
                errors.append(repr(exc))
            if self._owns_stack and self._option("stop_owned_stack_on_close", True):
                try:self._client.stop_stack()
                except Exception as exc:
                    errors.append(repr(exc))
                    return {"ok":False,"reason":reason,"errors":errors,"cleanup_pending":True,"ownership_retained":True}
            self._client.close()
        self._client = None
        self._session_open = False
        self._owns_stack = False
        self._origin = None
        self._ground_calibration = None
        self._episode_id = episode_id
        self._session_generation += 1
        return {"ok": not errors, "reason": reason, "episode_id": episode_id,
                "session_generation": self._session_generation, "errors": errors}

    def begin_online_episode(self, episode_id):
        # Workflow validates the transport before opening its first episode.
        # Adopt that still-unused session; later episode boundaries must reset.
        if (self._session_open and self._episode_id is None
                and self._active_tx is None):
            self._episode_id=str(episode_id)
            return {"ok":True,"reason":"prepared_first_session_adopted",
                    "episode_id":self._episode_id,"session_generation":self._session_generation,
                    "startup_actuation":False,"session_restarted":False}
        return self.reset_online_session("online_episode_start", str(episode_id))

    def end_online_episode(self, episode_id=None):
        return self.reset_online_session("online_episode_end", episode_id or self._episode_id)

    def abort_online_control_session(self, reason="tdoc_online_control_abort"):
        # Do not synthesize an ARM/DISARM or flight command from the transport adapter.
        # The Layer6 service enforces the packet deadline and owns the active latch.
        return self.reset_online_session(reason, self._episode_id)

    def _environment_ready(self,status):
        """Strict direct consumer: rehash bound inputs/model on every call."""
        try:
            checked=self._check_environment_binding()
            return self._environment_state_ready(status,checked)
        except Exception as exc:
            self._environment_rejection=repr(exc)
            return False

    def _check_environment_binding(self):
        """Perform expensive integrity work before requesting a fresh GUI sample.

        This is invoked anew for every acquisition. The returned object is local
        evidence for that acquisition, never a serialized authorization flag.
        """
        if not self._option('require_bound_environment',False):return None
        expected=copy.deepcopy(getattr(self,'_environment_binding',{}))
        if expected.get('schema')=='TDOC-native-environment-binding-v2':
            from layer6.binding import context_guard
            from ..native_binding import _stat
            path=getattr(self,'_environment_binding_path',None)
            if path is None:path=self.config.path_value('TDDT_LAYER6_ROOT',required=True)/'runtime/tdoc_environment_binding.json'
            paths=[Path(row['path']) for row in expected['input_files']]
            paths.append(Path(path))
            before={p:_stat(p) for p in paths}
            if context_guard(path).binding!=expected:
                raise ValueError('Native bound publication/model identity mismatch')
            if any(_stat(p)!=value for p,value in before.items()):
                raise ValueError('Native bound input changed during acquisition guard')
            return {'expected':expected,'path':Path(path),'stats':before,
                    'configuration':sha256_obj(self.config.data)}
        from ..model_compatibility import capture_model_dependencies,certificate_compatibility
        if not certificate_compatibility(expected,capture_model_dependencies(self.config))['ok']:
            raise ValueError('Native legacy model identity mismatch')
        return {'expected':expected}

    def _environment_state_ready(self,status,checked):
        """Check live identity/clock/readback/freshness after the GUI snapshot.

        No model/input file bytes are re-read here. Stat/publication checks detect
        a changed bound input during the same short acquisition; the next
        acquisition must repeat full byte and model verification.
        """
        from ..native_ground_policy import reject_fixture_environment
        reject_fixture_environment((status or {}).get('environment_contract'))
        if not self._option("require_bound_environment",False): return True
        env=(status or {}).get("environment_contract") or {}
        expected=(checked or {}).get('expected') or {}
        if expected!=getattr(self,'_environment_binding',{}):
            return self._reject_environment('BINDING_IDENTITY_CHANGED')
        if expected.get('schema')=='TDOC-native-environment-binding-v2':
            try:
                from layer6.binding import validate_wind_application,consumption_identity,forcing_time
                from layer6.timebase import validate_clock_mapping
                from ..native_binding import read_binding,_stat
                if read_binding(checked['path'])!=expected:
                    return self._reject_environment('BINDING_PUBLICATION_CHANGED')
                if sha256_obj(self.config.data)!=checked['configuration']:
                    return self._reject_environment('CONFIGURATION_CHANGED')
                changed=[str(p) for p,value in checked['stats'].items() if _stat(p)!=value]
                if changed:return self._reject_environment('BOUND_INPUT_CHANGED',paths=changed)
                differing=[k for k,v in consumption_identity(expected).items() if env.get(k)!=v]
                if differing:return self._reject_environment('CONSUMPTION_IDENTITY_MISMATCH',fields=differing)
                if env.get('ok') is not True:
                    return self._reject_environment('ENVIRONMENT_APPLICATION_PENDING',reason=env.get('reason'))
                try:validate_clock_mapping(expected,env)
                except Exception as exc:
                    # Diagnose expiry without accepting or retiming the old
                    # mapping. Historical validation only distinguishes a sound
                    # publication that expired in transport from a corrupt one.
                    try:
                        validate_clock_mapping(expected,env,now=env['updated_monotonic'])
                        age=time.monotonic()-env['clock_mapping']['observation']['received_monotonic_s']
                        if age>1.:
                            return self._reject_environment('CLOCK_OBSERVATION_STALE',age_s=age,limit_s=1.)
                    except Exception:pass
                    return self._reject_environment('CLOCK_MAPPING_REJECTED',error=repr(exc))
                try:validate_wind_application(env)
                except Exception as exc:return self._reject_environment('WIND_READBACK_REJECTED',error=repr(exc),
                    updated_age_s=time.monotonic()-float(env.get('updated_monotonic',0)),
                    sampled_age_s=time.monotonic()-float(env.get('sampled_monotonic',0)))
                if forcing_time(expected,env['plant_time_s'])[0]!=env['simulation_time_dataset']:
                    return self._reject_environment('FORCING_TIME_MISMATCH')
            except Exception as exc:
                return self._reject_environment('ENVIRONMENT_VALIDATION_EXCEPTION',error=repr(exc))
        age=time.monotonic()-float(env.get("updated_monotonic",0))
        if env.get('ok') is not True:return self._reject_environment('ENVIRONMENT_NOT_READY',reason=env.get('reason'))
        if not 0<=age<=1.:return self._reject_environment('ENVIRONMENT_STALE',age_s=age,limit_s=1.)
        if not expected.get('binding_id') or env.get('binding_id')!=expected['binding_id']:
            return self._reject_environment('BINDING_ID_MISMATCH')
        if env.get('forcing_file_sha256')!=expected.get('forcing_file_sha256'):
            return self._reject_environment('FORCING_HASH_MISMATCH')
        self._environment_rejection=None
        return True

    def _acquire_environment_status(self,*,include_imu_samples=False,timeout=.1):
        self._environment_status_witness=None
        checked=self._check_environment_binding()
        status=self._get_client().gui_status(timeout=timeout,include_imu_samples=include_imu_samples)
        ready=self._environment_state_ready(status,checked) and self._fresh(status)
        if ready:
            self._environment_status_witness={'status':status,'digest':sha256_obj(status),
                'checked':checked,'acquired_monotonic':time.monotonic()}
        return status,ready

    def validated_environment_status(self,status):
        """Validate the exact locally acquired object without a duplicate full hash.

        Caller-supplied dictionaries, copies, modified objects and expired samples
        use the strict direct validator. Actual observation freshness and live
        readback/publication checks are always evaluated again.
        """
        witness=getattr(self,'_environment_status_witness',None)
        if witness is not None and status is witness['status']:
            if sha256_obj(status)!=witness['digest']:
                return self._reject_environment('ACQUIRED_STATUS_MUTATED')
            age=time.monotonic()-witness['acquired_monotonic']
            if not 0<=age<=.15:
                # Rehashing the complete deployment here would only make the
                # same aged observation older. Caller must acquire a new sample.
                return self._reject_environment('ACQUISITION_WITNESS_EXPIRED',age_s=age,limit_s=.15)
            if not self._environment_state_ready(status,witness['checked']):return False
            if not self._fresh(status):
                from layer6.status import validate_observation_status
                check=validate_observation_status(status)
                return self._reject_environment('OBSERVATIONS_REJECTED_AT_CONSUMPTION',reason=check.get('reason'))
            return True
        return self._environment_ready(status) and self._fresh(status)

    def _bridge(self):
        return Layer6Bridge(self._get_client(),fresh=self._fresh,confirmed=self._confirmed)

    def _cancel_active(self, reason):
        ack=None
        if self._active_tx is None:return None
        if self._client is not None:
            ack=self._bridge().cancel(transaction_id=self._active_tx,reason=reason)
            raw=getattr(ack,'raw',None) or {}
            if getattr(ack,'ok',False) is not True or raw.get('cancelled') is not True:
                if raw.get('reason')!='no_matching_latch':raise RuntimeError('Layer6 cancellation unconfirmed')
        self._active_tx=None
        return ack

    def current_online_status(self):
        """Decision/readiness observation; lifecycle startup is owned by TDOC.

        Package preflight and Offline readiness inspection must never start the
        Native stack as a side effect of querying the Layer5 status callback.
        """
        if not self._session_open:
            raise RuntimeError("native_transport_not_prepared")
        return self._status()

    def _status(self, *, include_imu_samples=False):
        if not self._session_open:
            result = self.prepare_online_transport_session()
            if not result.get("ok"):
                raise RuntimeError(str(result))
        end=time.monotonic()+float(self._option("telemetry_resample_timeout_s",0.4))
        status=None
        while True:
            # One request must be allowed to finish the GUI's integrity prework.
            # Abandoning every RPC at 100 ms created a queue of stale request IDs.
            # This is a transport wait within the same existing acquisition budget,
            # never a telemetry-age or command-deadline extension.
            remaining=max(.01,end-time.monotonic())
            status,ready=self._acquire_environment_status(timeout=min(.5,remaining),include_imu_samples=include_imu_samples)
            if ready:return status
            if time.monotonic()>=end:
                env=(status or {}).get("environment_contract") or {}
                raise RuntimeError("native telemetry/forcing not ready: ages="+str((status or {}).get("telemetry_age_s"))
                                   +" forcing_ok="+str(env.get("ok"))+" reason="+str(env.get("reason"))
                                   +" validation_rejection="+str(getattr(self,'_environment_rejection',None)))
            time.sleep(0.01)

    @staticmethod
    def _state(status):
        att = status.get("attitude") or {}
        imu = status.get("imu") or {}
        valid=bool(imu)
        if 'schema' in status or 'telemetry_metadata' in status:
            from layer6.status import validate_observation_status
            valid=validate_observation_status(status)['ok']
        return {"runtime_ready":1.0 if valid else 0.0,
                **{k:imu.get(k) for k in ("accel_x_m_s2","accel_y_m_s2","accel_z_m_s2")},
                "imu_frame":imu.get("frame"),"imu_source":imu.get("source"),
                "imu_time_s":imu.get("plant_time_s"),
                **{k: status.get(k) for k in ("latitude_deg", "longitude_deg", "altitude_msl_m",
                "airspeed_m_s", "velocity_n_m_s", "velocity_e_m_s", "velocity_d_m_s")},
                "roll_rad": att.get("roll"), "pitch_rad": att.get("pitch"), "yaw_rad": att.get("yaw"),
                "gyro_x_rad_s": imu.get("gyro_x_rad_s"), "gyro_y_rad_s": imu.get("gyro_y_rad_s"),
                "gyro_z_rad_s": imu.get("gyro_z_rad_s"), "sim_time_s": status.get("plant_time_s")}

    @staticmethod
    def _snapshot(status):
        """Compatibility representation of measured GUI telemetry for existing TDOC consumers."""
        att = status.get("attitude") or {}
        def scaled(key, factor):
            value = status.get(key)
            return None if value is None else float(value)*factor
        ages = status.get("telemetry_age_s") or {}
        groups = {
            "ATTITUDE": ("ATTITUDE", {"roll": att.get("roll"), "pitch": att.get("pitch"), "yaw": att.get("yaw"),
                         "rollspeed": att.get("roll_rate"), "pitchspeed": att.get("pitch_rate"), "yawspeed": att.get("yaw_rate")}),
            "AIRSPEED": ("VFR_HUD", {"airspeed": status.get("airspeed_m_s"), "groundspeed": status.get("groundspeed_m_s"),
                         "climb": status.get("vertical_speed_m_s"), "heading": status.get("heading_deg")}),
            "POSITION": ("GLOBAL_POSITION_INT", {"lat": scaled("latitude_deg", 1e7), "lon": scaled("longitude_deg", 1e7),
                         "alt": scaled("altitude_msl_m", 1000), "relative_alt": scaled("relative_altitude_m", 1000)}),
            "VELOCITY": ("GLOBAL_POSITION_INT", {"vx": scaled("velocity_n_m_s", 100), "vy": scaled("velocity_e_m_s", 100), "vz": scaled("velocity_d_m_s", 100)}),
            "ACTUATOR": ("SERVO_OUTPUT_RAW", status.get("servo_output_raw") or {}),
        }
        ready=True
        if 'schema' in status or 'telemetry_metadata' in status:
            from layer6.status import validate_observation_status
            ready=validate_observation_status(status)['ok']
        return {"ready": ready, "source": "Layer6 GUI reconstructed telemetry",
                **{k:status.get(k) for k in ("latitude_deg","longitude_deg","altitude_msl_m","relative_altitude_m")},
                "sim_time_s": status.get("plant_time_s"), "plant_time_s": status.get("plant_time_s"),
                "groups": {k: {"payload": data, "source_message": message, "age_s": ages.get(message)}
                           for k, (message, data) in groups.items()}}

    def _ingress_from_status(self,status):
        if isinstance(status,dict) and ('schema' in status or 'telemetry_metadata' in status):
            from layer6.status import CORE_CHANNELS,validate_observation_status
            check=validate_observation_status(status,required=(*CORE_CHANNELS,'SERVO_OUTPUT_RAW','SYS_STATUS'))
            if not check['ok']:raise ValueError('native observations rejected: '+check['reason'])
        if not self.validated_environment_status(status) or not self._fresh(status):
            env=(status or {}).get("environment_contract") or {}
            raise RuntimeError("native telemetry/forcing not ready: ages="+str((status or {}).get("telemetry_age_s"))
                               +" forcing_ok="+str(env.get("ok"))+" reason="+str(env.get("reason"))
                               +" validation_rejection="+str(getattr(self,'_environment_rejection',None)))
        ages=status.get("telemetry_age_s") or {}
        for name,limit in (("SERVO_OUTPUT_RAW",float(self._option("telemetry_max_age_s",2.0))),
                           ("SYS_STATUS",0.5)):
            if not self._finite(ages.get(name)) or not 0<=float(ages[name])<=limit:
                raise ValueError("fresh native "+name+" required")
        state=self._state(status)
        if state['runtime_ready']!=1.0:raise ValueError('native observations expired during ingress validation')
        required=("latitude_deg","longitude_deg","altitude_msl_m","airspeed_m_s",
                  "velocity_n_m_s","velocity_e_m_s","velocity_d_m_s","roll_rad","pitch_rad","yaw_rad","sim_time_s")
        if not all(self._finite(state.get(k)) for k in required):
            raise ValueError("complete native telemetry and plant clock required")
        imu_age=(status.get("telemetry_age_s") or {}).get("SCALED_IMU")
        if not self._finite(imu_age) or not 0<=float(imu_age)<=float(self._option("imu_max_age_s",0.15)):
            raise ValueError("fresh BODY_FRD SCALED_IMU required for Online MESA")
        if state.get("imu_frame")!="BODY_FRD" or state.get("imu_source")!="MAVLink/SCALED_IMU" or not all(self._finite(state.get(k)) for k in ("accel_x_m_s2","accel_y_m_s2","accel_z_m_s2","gyro_x_rad_s","gyro_y_rad_s","gyro_z_rad_s","imu_time_s")):
            raise ValueError("incomplete/non-finite native IMU sample")
        state["battery_soc"]=(float((status.get("battery") or {})["remaining_percent"])/100.0
            if self._finite((status.get("battery") or {}).get("remaining_percent")) and 0<=float(status["battery"]["remaining_percent"])<=100 else None)
        if state["battery_soc"] is None:
            raise ValueError("valid native battery state required")
        actuator=self.layer5.native_actuator_feedback(status)
        if not actuator.get("ok"):
            raise ValueError("native actuator feedback unavailable: "+str(actuator.get("reason")))
        return {"ok": True, "source": self.SOURCE, "state": state, "actuator_feedback": actuator["feedback"],
                "gui_state":status,"imu_samples":status.get("imu_samples") or [],
                "released": True, "simulation_time_s": status.get("plant_time_s"),
                "plant_time_authority": "ArduPilot telemetry time_boot_ms",
                "initialization_contract": dict(self._origin or {}), "environment_contract": status.get("environment_contract") or {},
                "environment_clock_mapping": (status.get("environment_contract") or {}).get("clock_mapping"),
                "navigation_authority": "MESA_STD", "truth_injection_to_mesa": False,
                "simulation_only": True, "not_field_evidence": True, "actuation_authority": "ArduPilot SITL"}

    def _start_native_history_reader(self,status):
        if self.config.get('layers.layer6.online.execution_profile')!='closed_loop_sitl':return
        self._native_health(status)
        existing=getattr(self,'_native_history_reader',None)
        if existing is not None:
            if (existing.session!=status.get('session_id') or existing.native_session!=(status.get('status_quality') or {}).get('native_session_id')
                or existing.error or existing.stopping.is_set()):
                raise RuntimeError('Native history retention belongs to a failed/different session')
            return
        # Separate public client/socket: the control/status client remains sequential.
        client=self._module().Layer6Client(endpoint=self._option('endpoint','127.0.0.1:14661'),
            gui_endpoint=self._option('gui_endpoint','127.0.0.1:14660'),timeout=.2,start_if_needed=False)
        try:reader=_NativeIMUHistoryReader(client,status)
        except Exception:
            client.close();raise
        self._native_history_reader=reader;reader.start()

    def _stop_native_history_reader(self):
        reader=getattr(self,'_native_history_reader',None)
        if reader is not None:
            reader.stop();self._native_history_reader=None

    def _prepare_ground_calibration(self,status):
        from ..online_imu import prepare_native_imu_history,prepare_ground_calibration
        from ..native_control_binding import ground_contact
        duration=float(self.config.get('tdoc.rtmo.initial_navigation.ground_calibration_seconds',5.))
        if not .5<=duration<=15.:raise ValueError('ground calibration duration outside bounded commissioning window')
        statuses=[status];start=float(status['plant_time_s']);reader=self._native_history_reader
        deadline=time.monotonic()+duration+10.
        while status['plant_time_s']-start<duration+.15:
            if time.monotonic()>=deadline:raise RuntimeError('ground calibration source-time timeout')
            status=self._status()
            if status.get('armed') is not False or status.get('throttle')!=0. or not ground_contact(status):
                raise ValueError('ground calibration interrupted by vehicle lifecycle change')
            statuses.append(status);time.sleep(.03)
        after=statuses[0]['telemetry_metadata']['SCALED_IMU']['history_sequence']
        through=status['telemetry_metadata']['SCALED_IMU']['history_sequence']
        pages=reader.window(after,through)
        history=prepare_native_imu_history(pages,expected_session_id=reader.session,
            expected_monotonic_domain_id=reader.domain,target_system=reader.target[0],target_component=reader.target[1],after_seq=after)
        self._ground_calibration=prepare_ground_calibration(history,statuses,duration_s=duration)
        return self._status()

    def online_history_ingress(self,*,history_after_seq=None,history_session_id=None):
        """Layer2-only acquisition, including the first interval since validated startup."""
        reader=getattr(self,'_native_history_reader',None)
        if reader is None:
            return {'ok':False,'reason':'native_history_reader_not_started'}
        if history_after_seq is None and history_session_id is None:
            history_after_seq=reader.anchor;history_session_id=reader.session
        result=self.online_runtime_ingress(history_after_seq=history_after_seq,history_session_id=history_session_id)
        if result.get('ok'):
            result['native_imu_history_anchor']={'history_sequence':reader.anchor,'plant_time_s':reader.anchor_plant_time_s,
                'session_id':reader.session,'monotonic_domain_id':reader.domain}
            result['history_anchor_plant_time_s']=reader.anchor_plant_time_s
            result['history_anchor_sequence']=reader.anchor
        return result

    @staticmethod
    def _history_tail_covers(history,status):
        """Cheap continuity witness after decoding; does not retime either interval."""
        current=status.get('imu') or {};through=history.through_seq
        if current.get('history_sequence')==through:return True
        if type(current.get('history_sequence')) is not int or current['history_sequence']<through:return False
        base=history.samples[-1] if history.samples else next(
            (sample for sample in status.get('imu_samples',[]) if sample.get('history_sequence')==through),None)
        if base is None:return False
        previous=base.get('plant_time_s');cursor=through
        for sample in status.get('imu_samples') or []:
            seq=sample.get('history_sequence')
            if type(seq) is not int:return False
            if seq<=through:continue
            stamp=sample.get('plant_time_s');delta=sample.get('source_time_delta_ms')
            if (seq<=cursor or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
                                     for v in (previous,stamp,delta))
                or stamp<=previous or abs((stamp-previous)*1000.-delta)>1e-5):return False
            previous=stamp;cursor=seq
        return cursor==current['history_sequence'] and previous==current.get('plant_time_s')

    def online_runtime_ingress(self,*,history_after_seq=None,history_session_id=None):
        acquired_started=time.monotonic();final_status_returned=None;status=None
        acquisition_stages={}
        def identity(value):
            return (value.get('session_id'),value.get('monotonic_domain_id'),
                (value.get('status_quality') or {}).get('native_session_id'),
                value.get('target_system'),value.get('target_component'))
        def timing():
            now=time.monotonic();receipt=((status or {}).get('imu') or {}).get('timestamp_monotonic')
            return {'scope':'acquisition_diagnostic_only','elapsed_s':now-acquired_started,
                'stages_s':dict(acquisition_stages),
                'post_final_snapshot_s':None if final_status_returned is None else now-final_status_returned,
                'source_receipt_age_s':now-float(receipt) if self._finite(receipt) else None,
                'source_receipt_retimed':False}
        try:
            prepared=None;pages=None
            mark=time.monotonic();probe=self._status()
            acquisition_stages['initial_status']=time.monotonic()-mark
            mark=time.monotonic();self._native_health(probe)
            acquisition_stages['component_health']=time.monotonic()-mark
            if history_after_seq is not None or history_session_id is not None:
                if (type(history_after_seq) is not int or history_after_seq<1
                    or not isinstance(history_session_id,str) or len(history_session_id)!=32):
                    raise ValueError('Native MESA history requires its exact successful source cursor/session')
                before=probe
                reader=getattr(self,'_native_history_reader',None)
                if (reader is None or reader.session!=history_session_id or before.get('session_id')!=history_session_id
                    or before.get('monotonic_domain_id')!=reader.domain
                    or (before.get('status_quality') or {}).get('native_session_id')!=reader.native_session):
                    raise ValueError('Native history reader/current observation session mismatch')
                through=before['telemetry_metadata']['SCALED_IMU']['history_sequence']
                mark=time.monotonic();pages=reader.window(history_after_seq,through)
                from ..online_imu import prepare_native_imu_history
                prepared=prepare_native_imu_history(pages,expected_session_id=history_session_id,
                    expected_monotonic_domain_id=reader.domain,target_system=reader.target[0],
                    target_component=reader.target[1],after_seq=history_after_seq)
                acquisition_stages['history_prepare']=time.monotonic()-mark
                # Decode/normalize the archived interval BEFORE the final acquisition.
                # No historical receipt timestamp is changed or treated as current.
                probe=before
            for attempt in range(8):
                # /proc/window scans are integrity prework. Validate the actual
                # prior identity, then acquire NEW current observations afterwards.
                # The complete component check ran once for this acquisition.
                # Catch-up repeats source/session checks, not the same process scan.
                expected_identity=identity(probe)
                mark=time.monotonic();status=self._status(include_imu_samples=True)
                acquisition_stages['final_status']=acquisition_stages.get('final_status',0.)+time.monotonic()-mark
                final_status_returned=time.monotonic()
                if identity(status)!=expected_identity:
                    raise ValueError('Native identity changed after component-health verification')
                if prepared is not None and (status.get('session_id')!=reader.session or status.get('monotonic_domain_id')!=reader.domain
                    or (status.get('status_quality') or {}).get('native_session_id')!=reader.native_session):
                    raise ValueError('Native session changed after retained-history preparation')
                if prepared is not None and not self._history_tail_covers(prepared,status):
                    if attempt==7:raise RuntimeError('Native history catch-up exceeded bounded final-observation attempts')
                    # Retain the verified prefix and add only actual source records.
                    from ..online_imu import extend_native_imu_history
                    mark=time.monotonic();next_pages=reader.window(prepared.through_seq,status['telemetry_metadata']['SCALED_IMU']['history_sequence'])
                    prepared=extend_native_imu_history(prepared,next_pages)
                    acquisition_stages['history_catchup']=acquisition_stages.get('history_catchup',0.)+time.monotonic()-mark
                    pages.extend(next_pages);probe=status
                    continue
                try:
                    ingress=self._ingress_from_status(status)
                except RuntimeError:
                    rejection=getattr(self,'_environment_rejection',None) or {}
                    code=rejection.get('code') if isinstance(rejection,dict) else None
                    transient=(code in ('ACQUISITION_WITNESS_EXPIRED','ENVIRONMENT_STALE','ENVIRONMENT_APPLICATION_PENDING','CLOCK_OBSERVATION_STALE')
                        or code=='OBSERVATIONS_REJECTED_AT_CONSUMPTION' and 'stale' in rejection.get('reason','')
                        or code=='WIND_READBACK_REJECTED' and 'stale' in rejection.get('error',''))
                    if not transient or attempt==7:raise
                    acquisition_stages['expired_observations_discarded']=acquisition_stages.get('expired_observations_discarded',0)+1
                    probe=status
                    continue
                break
            else:raise RuntimeError('Native observation acquisition exhausted its bounded budget')
            if getattr(self,'_ground_calibration',None) is not None:
                ingress['native_ground_calibration']=self._ground_calibration
            if prepared is not None:
                ingress['native_imu_history']=prepared;ingress['imu_history_pages']=pages
            self._last_native_ingress_timing=timing()
            ingress['native_acquisition_timing']=self._last_native_ingress_timing
            return ingress
        except Exception as exc:
            self._last_native_ingress_timing=timing()
            return {"ok":False,"reason":"layer6_gui_ingress_not_ready","error":repr(exc),
                    'native_acquisition_timing':self._last_native_ingress_timing}

    def _native_health(self,status):
        if self.config.get('layers.layer6.online.execution_profile')!='closed_loop_sitl':
            return None
        from layer6.native_session import session_health
        sid=(status.get('status_quality') or {}).get('native_session_id')
        health=session_health(sid)
        if health.get('ok') is not True:
            raise RuntimeError('native components unavailable: '+str(health.get('reason')))
        self._last_native_health=health
        return health

    def _retained_ack_clock_samples(self,payload,status):
        reader=getattr(self,'_native_history_reader',None)
        if (reader is None or reader.session!=status.get('session_id') or reader.domain!=status.get('monotonic_domain_id')
            or reader.native_session!=(status.get('status_quality') or {}).get('native_session_id')):
            raise ValueError('Native acknowledged clock/history session mismatch')
        start=(payload.get('network_snapshot') or {}).get('reference_time_s')
        end=status.get('plant_time_s')
        after,pages=reader.source_window(start,end)
        from ..online_imu import prepare_native_imu_history,native_imu_history_samples
        prepared=prepare_native_imu_history(pages,expected_session_id=reader.session,
            expected_monotonic_domain_id=reader.domain,target_system=reader.target[0],target_component=reader.target[1],after_seq=after)
        samples=native_imu_history_samples(prepared)
        if not samples or abs(samples[0]['plant_time_s']-start)>1e-8 or abs(samples[-1]['plant_time_s']-end)>1e-8:
            raise ValueError('Native retained clock samples do not prove the acknowledged interval')
        return [s for s in samples if start-1e-8<=s['plant_time_s']<=end]

    def _evidence(self, packet, status, ack=None, layer7_input=None):
        result = layer7_input or self._get_client().build_layer7_input(packet, status=status, ack=ack)
        evidence = dict(result["ExecutionEvidence"])
        evidence.update(schema="TDOC-L6-online-native-execution-evidence-v1", source=self.SOURCE, destination="Layer7", command=(packet.get("layer6_package") or packet).get("u_cmd"),
                        io_snapshot=self._snapshot(status), simulation_only=True, not_field_evidence=True,
                        session={"generation": self._session_generation, "episode_id": self._episode_id},
                        plant_time_s=status.get("plant_time_s"), live_runtime_evidence=True,
                        rate_sync={"transaction_id": packet.get("transaction_id")})
        evidence['environment_clock_mapping']=(status.get('environment_contract') or {}).get('clock_mapping')
        health=self._native_health(status)
        if health is not None:evidence['native_session_health']=health
        evidence["evidence_hash"] = sha256_obj(evidence)
        return evidence

    def invoke(self, payload: dict[str, Any]):
        if str(payload.get("learning_mode") or "ONLINE_JSBSIM").upper() != "ONLINE_JSBSIM":
            raise ValueError("Layer6 is Online-only; Offline routes Layer5 -> TDOC -> Layer2")
        env = payload.get("layer5_output") or {}
        l5 = env.get("result") if isinstance(env.get("result"), dict) else env
        tx = payload.get("transaction_id")
        submission_attempted=False
        try:
            if (l5.get("gates") or {}).get("FINAL") is not True:
                cancelled=self._cancel_active("Layer5_FINAL_false")
                status = self._status()
                evidence = self._evidence({"transaction_id": tx}, status)
                from layer6.timeout_hold import hold_outcome
                evidence['hold_outcome']=hold_outcome(reason='Layer5_FINAL_false',cancel_ack=cancelled)
                evidence["hold_observation_only"] = True
                evidence["validation_only"] = bool((payload.get("request") or {}).get("online_observation_only"))
                evidence["evidence_hash"] = sha256_obj({k:v for k,v in evidence.items() if k!="evidence_hash"})
                return {"ok": True, "status": "OBSERVATION_ONLY", "actuation_executed": False,
                        "reason": "layer5_FINAL_not_authorized", "execution_evidence": evidence}
            readiness=self.layer5.online_control_readiness()
            if not readiness.get("ok"):
                raise ValueError(f"Layer5 current Online control readiness rejected: {readiness.get('blockers')}")
            # TDOC's infrastructure/mapping readiness is not authority to actuate.
            # The present WingMPET provider cannot predict ArduPilot's stateful
            # native pilot controller. A future real provider must implement an
            # independent verifier on the Layer5 adapter; declarative booleans or
            # an external FINAL flag alone are not native execution evidence.
            verifier=getattr(self.layer5,"verify_native_execution_evidence",None)
            verified=(verifier(l5,transaction_id=tx) if callable(verifier)
                      else {"ok":False,"reason":"native_pilot_controller_prediction_unavailable"})
            contract_verified=bool(isinstance(verified,dict) and verified.get('execution_contract_verified') is True
                and self.config.get('layers.layer6.online.execution_profile')=='closed_loop_sitl'
                and l5.get('native_mission_binding'))
            if not isinstance(verified,dict) or verified.get("ok") is not True or not (verified.get("native_execution_model_matched") is True or contract_verified):
                reason=(verified.get("reason") if isinstance(verified,dict) else None) or "native_execution_model_not_verified"
                raise ValueError("native_execution_model_not_verified: "+str(reason))
            package = l5.get("layer6_package")
            if not isinstance(package, dict):
                raise ValueError("Layer5 FINAL requires complete layer6_package")
            packet = {"transaction_id": tx, "layer6_package": copy.deepcopy(package)}
            p = packet["layer6_package"]
            # Existing Layer5 aliases are normalized only at this boundary.
            for canonical, alias in (("u_cmd", "command"), ("C_exec", "execution_constraints"),
                                     ("S_safe", "safety_policy"), ("tau_apply", "apply_time"), ("tau_deadline", "deadline_time")):
                if canonical not in p and alias in p:
                    p[canonical] = p[alias]
            # Never repair a mismatched Layer5 transaction by overwriting it.
            # The packet must remain bound to the decision that was verified.
            if p.get("transaction_id") is not None and p["transaction_id"] != tx:
                raise ValueError("Layer5/Layer6 transaction identity mismatch")
            chosen=(l5.get("decision") or {}).get("candidate_id") if isinstance(l5.get("decision"),dict) else None
            if chosen is not None and p.get("candidate_id") is not None and str(chosen)!=str(p["candidate_id"]):
                raise ValueError("Layer5/Layer6 candidate identity mismatch")
            p["transaction_id"] = tx
            packet["execution_rate_hz"] = float(self._option("execution_rate_hz", 30.0))
            if l5.get('schema')=='Layer5-native-execution-result-v1' or contract_verified:
                if verified.get('package_sha256')!=sha256_obj(p):
                    raise ValueError('Native package changed after independent verification')
                expected_segment=float(self.config.get('tdoc.rtmo.l4_artifacts.commit_segment_s',0.1))
                if (p.get('execution_segment') or {}).get('max_duration_s')!=expected_segment:
                    raise ValueError('Native certified segment differs from TDOC execution policy')
                if p.get('execution_rate_hz')!=packet['execution_rate_hz']:
                    raise ValueError('Native certified execution cadence differs from transport policy')
            if not tx or any(p.get(k) is None for k in ("u_cmd", "tau_apply", "tau_deadline", "C_exec", "S_safe")):
                raise ValueError("incomplete Layer5 execution transaction")
            if not all(self._finite(p[k]) for k in ("tau_apply", "tau_deadline")) or float(p["tau_deadline"]) <= float(p["tau_apply"]):
                raise ValueError("invalid execution time window")
            if not isinstance(p["S_safe"],dict) or any(p["S_safe"].get(k) is not True for k in ("psf","rta")):
                raise ValueError("explicit Layer5 PSF/RTA evidence required")
            for canonical,alias in (("u_cmd","command"),("C_exec","execution_constraints"),
                                    ("S_safe","safety_policy"),("tau_apply","apply_time"),("tau_deadline","deadline_time")):
                if alias in p and p[alias] != p[canonical]:
                    raise ValueError(f"conflicting execution contract aliases: {canonical}/{alias}")
            intent=p.get("execution_intent") or {}
            mode=intent.get("requested_pilot_mode") or p.get("mode")
            if mode and mode not in {"QHOVER","FBWA","QSTABILIZE","QLOITER"}:
                raise ValueError("unsupported Layer6 pilot mode")
            if any(action not in {"ARM","DISARM","THROTTLE_ZERO","NEUTRAL_ATTITUDE"} for action in (intent.get("discrete") or [])):
                raise ValueError("unsupported Layer6 discrete action")
            self._module()
            from layer6.contracts import DecisionEnvelope
            from layer6.validation import validate
            verdict = validate(DecisionEnvelope.from_dict(packet))
            if not verdict.ok:
                raise ValueError(f"Layer6 validation rejected: {verdict.reasons}")
            before = self._status()
            self._native_health(before)
            segment=float(self.config.get("tdoc.rtmo.l4_artifacts.commit_segment_s",0.1))
            packet["execution_segment_s"]=segment
            self._active_tx=tx
            submission_attempted=True
            exchange=self._bridge().exchange(packet,before=before,
                timeout_s=float(self._option("execution_confirmation_timeout_s",2.0)),segment_s=segment,
                prediction_evidence=payload.get("prediction_evidence") or payload.get("layer4_result"),
                network_snapshot=payload.get("network_snapshot"))
            status=exchange["status"]; ack=exchange["ack"]
            evidence = self._evidence(packet,status,ack,exchange["layer7_input"])
            # Retain measured clock continuity through the decision/ack interval.
            # The acknowledged state stays unchanged; samples are not navigation
            # observations, interpolation or execution authority.
            try:
                if self.config.get('layers.layer6.online.execution_profile')=='closed_loop_sitl':
                    evidence['plant_clock_samples']=self._retained_ack_clock_samples(payload,status)
                    evidence['plant_clock_retention_origin']='TDOC-process-local-primary-ring'
                else:
                    clock_status=self._status(include_imu_samples=True)
                    evidence["plant_clock_samples"]=[sample for sample in (clock_status.get("imu_samples") or [])
                        if float(sample.get("plant_time_s",float("inf")))<=float(status["plant_time_s"])]
            except Exception as clock_error:
                # Keep the actual execution ACK; missing continuity evidence still
                # rejects the RTMO clock grant downstream.
                evidence["plant_clock_samples"]=[]
                evidence["plant_clock_sample_error"]=repr(clock_error)
            evidence["write_result"] = {"executed": True, **(status.get("applied_pilot_command") or {})}
            evidence["evidence_hash"] = sha256_obj({k:v for k,v in evidence.items() if k!="evidence_hash"})
            return {"ok": True, "status": "EXECUTED", "route": "LAYER7_EXECUTION_EVIDENCE",
                    "actuation_executed": True, "execution_evidence": evidence,
                    "layer7_input":{**exchange["layer7_input"],"ExecutionEvidence":evidence}}
        except Exception as exc:
            cancelled=None;cancel_error=None
            try:cancelled=self._cancel_active("Layer6_rejected_or_timeout")
            except Exception as stop_error:cancel_error=repr(stop_error)
            from layer6.timeout_hold import hold_outcome
            return {"ok": False, "status": "FAILED", "route": "HOLD", "actuation_executed": None if submission_attempted else False,
                    "submission_attempted":submission_attempted,"execution_confirmation":"UNCONFIRMED",
                    "hold_outcome":hold_outcome(reason='Layer6_rejected_or_timeout',cancel_ack=cancelled,cancel_error=cancel_error),
                    "reason": "layer6_execution_not_confirmed", "error": repr(exc)}

    def _confirmed(self, status, before, package, tx):
        if not self._fresh(status) or status.get("applied_transaction_id") != tx:
            return False
        if 'command_correlation' in status or status.get('schema')=='Layer6-GUI-status-v1':
            from layer6.command_correlation import verified_send_bounds
            if verified_send_bounds(status,tx) is None:return False
        sent = status.get("command_first_sent_monotonic")
        if not self._finite(sent) or float(sent) <= float(before.get("manual_sent_monotonic") or 0):
            return False
        times = status.get("telemetry_received_monotonic") or {}
        if not all(self._finite(times.get(name)) and float(times[name]) >= float(sent)
                   for name in ("ATTITUDE", "SERVO_OUTPUT_RAW")):
            return False
        applied=status.get("applied_pilot_command") or {}
        if any(not self._finite(applied.get(k)) or abs(float(applied[k])-float(v))>0.0011
               for k,v in zip(("aileron","elevator","throttle","rudder"),package["u_cmd"])):
            return False
        if float(package["u_cmd"][2]) > 0 and status.get("armed") is not True:
            return False
        intent = package.get("execution_intent") or {}
        mode = intent.get("requested_pilot_mode") or package.get("mode")
        if mode and status.get("mode") != mode:
            return False
        discrete = intent.get("discrete") or []
        if "ARM" in discrete and status.get("armed") is not True:
            return False
        if "DISARM" in discrete and status.get("armed") is not False:
            return False
        return True

    def close(self):
        self.reset_online_session("adapter_close", self._episode_id)


Layer6DualModeAdapter = Layer6OnlineAdapter
