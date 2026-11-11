"""Public Layer-6 client and shared demo implementation.

All external AI/TDDT callers and the bundled ``demo_layer6.py`` use this module.
Commands always enter through the canonical Layer-6 intake service, then follow
service -> DigitalPilotExecutor -> Virtual Pilot -> GUI -> MAVLink -> ArduPilot.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import json
import math
import random
import socket
import time
import uuid
from typing import Any, Mapping, Sequence

try:
    from .virtual_pilot import VirtualPilotClient
except ImportError:  # allow direct: python3 src/layer6/client.py
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from layer6.virtual_pilot import VirtualPilotClient


class Layer6ClientError(RuntimeError):
    """Base client-side error."""


class Layer6TimeoutError(Layer6ClientError):
    """Raised when the Layer-6 intake service does not ACK in time."""


class Layer6RemoteError(Layer6ClientError):
    """Raised when Layer-6 rejects a submitted decision."""


@dataclass(frozen=True)
class Layer6Ack:
    ok: bool
    transaction_id: str | None = None
    error: str | None = None
    raw: Mapping[str, Any] | None = None


PHASES = [
    ("ground", "QHOVER", (0.00, 0.00, 0.00, 0.00), ("THROTTLE_ZERO",), 1.0),
    ("arm", "QHOVER", (0.00, 0.00, 0.00, 0.00), ("ARM",), 2.0),
    ("vertical_takeoff", "QHOVER", (0.03, -0.02, 0.62, 0.02), (), 3.0),
    ("hover", "QHOVER", (0.02, 0.01, 0.50, 0.02), (), 2.0),
    ("vtol_translation", "QHOVER", (0.05, -0.18, 0.54, 0.02), (), 3.0),
    ("transition", "FBWA", (0.04, -0.08, 0.61, 0.01), (), 3.0),
    ("cruise", "FBWA", (0.10, 0.01, 0.58, 0.04), (), 5.0),
    ("approach", "FBWA", (0.04, 0.05, 0.43, 0.01), (), 3.0),
    ("back_transition", "QHOVER", (0.03, 0.06, 0.48, 0.01), (), 3.0),
    ("vtol_approach", "QHOVER", (0.03, 0.10, 0.43, 0.02), (), 3.0),
    ("final_hover", "QHOVER", (0.00, 0.00, 0.50, 0.00), (), 2.0),
    ("descent", "QHOVER", (0.00, 0.00, 0.38, 0.00), (), 3.0),
    ("touchdown", "QHOVER", (0.00, 0.00, 0.00, 0.00), ("THROTTLE_ZERO",), 3.0),
    ("disarm", "QHOVER", (0.00, 0.00, 0.00, 0.00), ("DISARM",), 2.0),
]


def _runner_module():
    try:
        return importlib.import_module("run_flightstack")
    except Exception as exc:
        raise Layer6ClientError(
            "run_flightstack.py is not importable. Run from the Layer-6 package source tree "
            "or add its src directory to PYTHONPATH."
        ) from exc


def _fmt_status(st: Mapping[str, Any] | None) -> str:
    if not st:
        return "GUI status unavailable"
    return (
        f"mavlink={st.get('mavlink_connected')} sys={st.get('target_system')} "
        f"mode={st.get('mode')} armed={st.get('armed')} thr={st.get('throttle')} "
        f"landed={st.get('landed_state')} gs={st.get('groundspeed_m_s')} "
        f"rel_alt={st.get('relative_altitude_m')} vz={st.get('vertical_speed_m_s')} "
        f"ack={st.get('last_command_ack')} statustext={st.get('last_statustext')!r} "
        f"endpoint={st.get('mavlink_endpoint')}"
    )


def _ground_safe(st: Mapping[str, Any] | None) -> bool:
    if not st or not st.get("mavlink_connected"):
        return False
    landed = str(st.get("landed_state") or "").upper()
    if landed == "ON_GROUND":
        return True
    if landed in {"IN_AIR", "TAKEOFF", "LANDING"}:
        return False
    try:
        gs = st.get("groundspeed_m_s")
        alt = st.get("relative_altitude_m")
        vz = st.get("vertical_speed_m_s")
        return (
            gs is not None and float(gs) <= 0.8
            and alt is not None and float(alt) <= 0.8
            and vz is not None and abs(float(vz)) <= 0.6
        )
    except Exception:
        return False


class Layer6Client:
    """Canonical external API for AI/TDDT and the bundled Layer-6 demo.

    ``start_if_needed=True`` is the library equivalent of the CLI switch
    ``--start-if-needed``.  It starts the three-terminal stack only when the GUI
    control endpoint is not already responding.
    """

    def __init__(
        self,
        endpoint: str = "127.0.0.1:14661",
        *,
        timeout: float = 2.0,
        gui_endpoint: str = "127.0.0.1:14660",
        start_if_needed: bool = False,
        flightstack_dir: str | None = None,
        terminal: str | None = None,
        startup_check: float = 3.0,
        lifecycle_debug: bool = False,
        startup_timeout: float = 20.0,
    ):
        host, port = endpoint.rsplit(":", 1)
        self.endpoint = (host, int(port))
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")
        gh, gp = gui_endpoint.rsplit(":", 1)
        self._gui = VirtualPilotClient(gh, int(gp))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self.timeout)
        self._lifecycle = {
            "flightstack_dir": flightstack_dir,
            "terminal": terminal,
            "startup_check": float(startup_check),
            "debug": bool(lifecycle_debug),
        }
        self._owned_native_session=None
        if start_if_needed:
            try:self.ensure_stack_started(timeout=startup_timeout)
            except Exception:
                try:
                    if self._owned_native_session:self.stop_stack()
                finally:self.close()
                raise

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            self._gui.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Layer6Client":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ----- stack lifecycle -------------------------------------------------
    def stack_responding(self, timeout: float = 0.4) -> bool:
        return self.gui_status(timeout=timeout) is not None

    def native_session_status(self):
        """Local process ownership/readiness; never a flight release."""
        r=_runner_module();result=r.status()
        result['owned_by_client']=bool(getattr(self,'_owned_native_session',None)==result.get('session_id'))
        return result

    def start_stack(self) -> Mapping[str, Any]:
        r = _runner_module()
        result=r.start(
            self._lifecycle["flightstack_dir"],
            verbose=self._lifecycle["debug"],
            debug=self._lifecycle["debug"],
            terminal=self._lifecycle["terminal"],
            startup_check=self._lifecycle["startup_check"],
        )
        if isinstance(result,dict) and result.get('created'):
            self._owned_native_session=result['session_id']
        return result

    def ensure_stack_started(self, timeout: float = 20.0) -> Mapping[str, Any]:
        st = self.gui_status(timeout=0.35)
        if st is not None:
            return st
        print("[L6CLIENT] Layer-6 stack/GUI not detected; starting it ...", flush=True)
        self.start_stack()
        end = time.monotonic() + float(timeout)
        last = None
        while time.monotonic() < end:
            last = self.gui_status(timeout=min(0.5, max(0.05, end - time.monotonic())))
            if last is not None:
                print("[L6CLIENT] Layer-6 GUI is responding.", flush=True)
                return last
            time.sleep(0.2)
        raise Layer6TimeoutError(
            f"Layer-6 was started but GUI did not answer within {timeout:.1f}s"
        )

    def stop_stack(self, *, verbose: bool | None = None) -> Mapping[str, Any]:
        if not getattr(self,'_owned_native_session',None):
            raise Layer6ClientError('native stack is not owned by this client')
        r = _runner_module()
        result=r.stop(verbose=self._lifecycle["debug"] if verbose is None else bool(verbose),session_id=self._owned_native_session)
        self._owned_native_session=None
        return result

    def restart_stack(self) -> Mapping[str, Any]:
        if not getattr(self,'_owned_native_session',None):raise Layer6ClientError('native stack is not owned by this client')
        r = _runner_module()
        result=r.restart(
            flightstack_dir=self._lifecycle["flightstack_dir"],
            verbose=self._lifecycle["debug"],
            debug=self._lifecycle["debug"],
            terminal=self._lifecycle["terminal"],
            startup_check=self._lifecycle["startup_check"],
            session_id=self._owned_native_session,
        )
        self._owned_native_session=result['session_id']
        self._intake_instances={};self._cancel_pending={};self._cancel_session_id=uuid.uuid4().hex;self._cancel_sequence=0
        return result

    # ----- GUI / FCU status ------------------------------------------------
    def gui_status(self, timeout: float = 0.8, *, include_imu_samples: bool = False) -> Mapping[str, Any] | None:
        """Read versioned real observations; IMU history is optional and bounded."""
        return self._gui.status(timeout=timeout,include_imu_samples=include_imu_samples)

    def telemetry_history(self, *, after_seq: int = 0, limit: int = 16,
                          channels: Sequence[str] | None = None, session_id: str | None = None,
                          through_seq: int | None = None, timeout: float = 0.8) -> Mapping[str, Any] | None:
        """Read bounded raw history; None means timeout, not an empty valid history.

        Continue with session_id/next_after_seq/through_seq from the first page.
        Source clocks, gaps and unknown values are preserved, never repaired.
        Calls sharing a client must be sequential (the GUI socket is shared).
        """
        return self._gui.telemetry_history(after_seq=after_seq,limit=limit,channels=channels,
            session_id=session_id,through_seq=through_seq,timeout=timeout)
    def segment_status(self, *, transaction_id, command_instance_id, timeout=.8):
        """Read local segment completion; this endpoint grants no environment/flight readiness."""
        return self._gui.segment_status(transaction_id=transaction_id,command_instance_id=command_instance_id,timeout=timeout)

    def build_layer7_input(
        self,
        layer5_packet: Mapping[str, Any],
        *,
        ack: Layer6Ack | None = None,
        status: Mapping[str, Any] | None = None,
        prediction_evidence: Any = None,
        network_snapshot: Any = None,
    ) -> Mapping[str, Any]:
        """Build canonical Layer7Input from the latest reconstructed L6 state.

        Layer-6 never fabricates L4 PredictionEvidence or L3 NetworkSnapshot; callers
        may pass those objects through when available.
        """
        from .feedback import build_layer7_input
        st = status if status is not None else self.gui_status(timeout=0.8)
        raw_ack = dict(ack.raw or {}) if ack is not None else {}
        tx = ack.transaction_id if ack is not None else None
        return build_layer7_input(
            layer5_packet, st, transaction_id=tx, intake_ack=raw_ack,
            prediction_evidence=prediction_evidence, network_snapshot=network_snapshot,
        )

    def request_mavlink_connect(self) -> None:
        self._gui.connect_mavlink()

    def ensure_mavlink(self, timeout: float = 12.0) -> Mapping[str, Any]:
        st = self.gui_status(timeout=0.8)
        if st is None:
            raise Layer6TimeoutError(
                "Layer-6 GUI did not answer on UDP 127.0.0.1:14660. "
                "Start the stack or use --start-if-needed."
            )
        print(f"[DEMO][GUI] {_fmt_status(st)}", flush=True)
        if not st.get("mavlink_connected"):
            print("[DEMO][MAV] GUI is not connected; requesting MAVLink Connect ...", flush=True)
            self.request_mavlink_connect()
            st = self._gui.wait_for(lambda x: bool(x.get("mavlink_connected")), timeout=timeout, interval=0.2)
            if not st or not st.get("mavlink_connected"):
                raise Layer6TimeoutError(
                    "GUI could not establish MAVLink to ArduPilot within the timeout. "
                    f"Last GUI status: {_fmt_status(st)}"
                )
        print(f"[DEMO][MAV] connected: {_fmt_status(st)}", flush=True)
        return st

    def wait_for_status(self, predicate, *, timeout: float = 8.0, interval: float = 0.15):
        return self._gui.wait_for(predicate, timeout=timeout, interval=interval)

    def wait_fcu_ready(self, timeout: float = 20.0, stable_samples: int = 3) -> Mapping[str, Any]:
        """Wait until ArduPilot has left INITIALISING and heartbeat state is stable.

        This is intentionally stronger than merely seeing ``mavlink_connected=True``.
        Immediately after SITL startup the GUI can receive a heartbeat while ArduPilot
        still reports INITIALISING; mode changes issued in that window can be ignored.
        """
        end = time.monotonic() + float(timeout)
        stable = 0
        last_key = None
        last = None
        next_log = 0.0
        not_ready_modes = {"", "NONE", "UNKNOWN", "INITIALISING", "INITIALIZING"}
        while time.monotonic() < end:
            last = self.gui_status(timeout=min(0.5, max(0.05, end-time.monotonic()))) or last
            mode = str((last or {}).get("mode") or "").upper()
            ready = bool(
                last
                and last.get("mavlink_connected")
                and int(last.get("target_system") or 0) > 0
                and mode not in not_ready_modes
                and last.get("armed") is not None
            )
            if ready and ('schema' in last or 'telemetry_metadata' in last):
                from .status import validate_observation_status
                ready=validate_observation_status(last)['ok']
            if ready:
                key = (int(last.get("target_system") or 0), mode, bool(last.get("armed")))
                if key == last_key:
                    stable += 1
                else:
                    last_key = key
                    stable = 1
                if stable >= max(1, int(stable_samples)):
                    print(f"[DEMO][FCU] ready: {_fmt_status(last)}", flush=True)
                    return last
            else:
                stable = 0
                last_key = None
            now = time.monotonic()
            if now >= next_log:
                print(f"[DEMO][FCU] waiting for ArduPilot readiness: {_fmt_status(last)}", flush=True)
                next_log = now + 1.0
            time.sleep(0.2)
        raise Layer6TimeoutError(
            "ArduPilot remained in startup/not-ready state; no flight command was sent. "
            f"Last GUI/FCU status: {_fmt_status(last)}"
        )

    # ----- canonical intake ------------------------------------------------
    def _native_intake_id(self):
        owned=getattr(self,'_owned_native_session',None)
        if owned:return owned
        if self.endpoint[1]!=14661:return None
        from pathlib import Path
        path=Path(__file__).resolve().parents[2]/'runtime/flightstack_state.json'
        if not path.exists():return None
        from .native_session import check_session
        record=json.loads(path.read_text());folder=record.get('session_dir')
        if not folder:return None
        return check_session(folder)[1]['session_id']

    def submit(self, packet: Mapping[str, Any], *, timeout: float | None = None) -> Layer6Ack:
        from .command_correlation import valid_tx
        obj=json.loads(json.dumps(dict(packet),allow_nan=False));inner=obj.get('layer6_package',obj)
        if not isinstance(inner,dict):raise Layer6ClientError('invalid layer6_package')
        outer_tx=obj.get('transaction_id');inner_tx=inner.get('transaction_id')
        if outer_tx is not None and inner_tx is not None and outer_tx!=inner_tx:raise Layer6ClientError('transaction_id mismatch')
        tx=inner_tx if inner_tx is not None else outer_tx if outer_tx is not None else f'external-{uuid.uuid4()}'
        if not valid_tx(tx):raise Layer6ClientError('invalid transaction_id')
        obj['transaction_id']=inner['transaction_id']=tx
        request_id=uuid.uuid4().hex;obj['intake_request_id']=request_id
        native_id=self._native_intake_id()
        if native_id:
            if obj.get('native_session_id') not in (None,native_id):raise Layer6ClientError('stale native boot packet')
            obj['native_session_id']=native_id
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        budget=self.timeout if timeout is None else float(timeout)
        if not math.isfinite(budget) or budget<=0:raise ValueError('invalid submission timeout')
        peer=(socket.gethostbyname(self.endpoint[0]),self.endpoint[1]);deadline=time.monotonic()+budget
        previous = self._sock.gettimeout()
        reply=None
        try:
            self._sock.sendto(payload, self.endpoint)
            while time.monotonic()<deadline:
                self._sock.settimeout(max(.000001,deadline-time.monotonic()))
                try:data,source=self._sock.recvfrom(65535)
                except socket.timeout:break
                if source!=peer:continue
                try:reply=json.loads(data.decode('utf-8'))
                except (ValueError,UnicodeError):continue
                if isinstance(reply,dict) and reply.get('transaction_id')==tx and reply.get('intake_request_id')==request_id:break
            else:reply=None
            if not isinstance(reply,dict) or reply.get('transaction_id')!=tx or reply.get('intake_request_id')!=request_id:
                raise Layer6TimeoutError(f'no matching ACK from Layer-6 intake at {self.endpoint[0]}:{self.endpoint[1]}')
        finally:
            self._sock.settimeout(previous)
        ack = Layer6Ack(
            ok=bool(reply.get("ok")),
            transaction_id=reply.get("transaction_id"),
            error=reply.get("error"),
            raw=reply,
        )
        if not ack.ok:
            raise Layer6RemoteError(ack.error or "Layer-6 rejected the decision")
        if native_id and reply.get('native_session_id')!=native_id:raise Layer6ClientError('intake native boot mismatch')
        from .cancellation import token
        if token(reply.get('command_instance_id')):
            instances=getattr(self,'_intake_instances',{})
            instances[tx]=reply['command_instance_id']
            while len(instances)>128:instances.pop(next(iter(instances)))
            self._intake_instances=instances
        return ack

    def cancel(self, *, transaction_id=None, reason="TDOC_HOLD", timeout=None) -> Layer6Ack:
        """Stop the matching service/GUI generation; no FCU stabilization claim.

        None snapshots the service's current target, never broadcasts a GUI
        release. Missing/nonmatching targets report cancelled=False.
        """
        from .command_correlation import valid_tx
        from .cancellation import token,reason_text,SCHEMA,OP_SCHEMA
        if transaction_id is not None and not valid_tx(transaction_id):raise ValueError('invalid cancellation transaction')
        if not reason_text(reason):raise ValueError('invalid cancellation reason')
        budget=self.timeout if timeout is None else timeout
        if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not math.isfinite(budget) or budget<=0:raise ValueError('invalid cancel timeout')
        instance=getattr(self,'_intake_instances',{}).get(transaction_id)
        pending=getattr(self,'_cancel_pending',None)
        if pending is None:self._cancel_pending=pending={}
        operation_key=(transaction_id,instance)
        request=pending.get(operation_key)
        if request is None:
            if len(pending)>=128:raise Layer6ClientError('unresolved cancellation capacity; target outcome remains unknown')
            if not getattr(self,'_cancel_session_id',None):self._cancel_session_id=uuid.uuid4().hex
            self._cancel_sequence=getattr(self,'_cancel_sequence',0)+1
            request={'type':'cancel','transaction_id':transaction_id,'reason':reason,'intake_request_id':uuid.uuid4().hex,
                     'operation_schema':OP_SCHEMA,'cancel_session_id':self._cancel_session_id,'cancel_sequence':self._cancel_sequence}
            if instance:request['command_instance_id']=instance
            native_id=self._native_intake_id()
            if native_id:request['native_session_id']=native_id
            pending[operation_key]=request
        nonce=request['intake_request_id']
        peer=(socket.gethostbyname(self.endpoint[0]),self.endpoint[1]);deadline=time.monotonic()+budget;old=self._sock.gettimeout()
        reply=None
        try:
            self._sock.sendto(json.dumps(request,separators=(',',':')).encode(),self.endpoint)
            while time.monotonic()<deadline:
                self._sock.settimeout(max(.000001,deadline-time.monotonic()))
                try:data,source=self._sock.recvfrom(65535)
                except socket.timeout:break
                if source!=peer:continue
                try:candidate=json.loads(data.decode())
                except (ValueError,UnicodeError):continue
                if (isinstance(candidate,dict) and candidate.get('type')=='cancel_ack'
                    and candidate.get('intake_request_id')==nonce and candidate.get('requested_transaction_id')==transaction_id
                    and candidate.get('operation_schema')==OP_SCHEMA
                    and candidate.get('cancel_session_id')==request['cancel_session_id']
                    and type(candidate.get('cancel_sequence')) is int
                    and candidate.get('cancel_sequence')==request['cancel_sequence']):
                    reply=candidate;break
            if reply is None:raise Layer6TimeoutError('no matching Layer-6 cancellation ACK')
        finally:self._sock.settimeout(old)
        if reply.get('ok') is not True:raise Layer6RemoteError(reply.get('error') or reply.get('reason') or 'cancellation not confirmed')
        if request.get('native_session_id') and reply.get('native_session_id')!=request['native_session_id']:raise Layer6ClientError('cancel native boot mismatch')
        if reply.get('cancelled') is True:
            gui=reply.get('gui_ack') or {}
            if (reply.get('latch_cancelled') is not True or reply.get('gui_cancelled') is not True
                or reply.get('schema')!=SCHEMA or gui.get('schema')!=SCHEMA or not token(gui.get('gui_session_id'))
                or not token(reply.get('service_session_id')) or type(reply.get('idempotent_replay')) is not bool
                or (reply.get('bound_transaction_id'),reply.get('bound_command_instance_id'))!=(reply.get('transaction_id'),reply.get('command_instance_id'))
                or reply.get('idempotent_replay') is True and reply.get('completion_semantics')!='original_operation_completed'
                or not valid_tx(reply.get('transaction_id')) or not token(reply.get('command_instance_id'))
                or transaction_id is not None and reply['transaction_id']!=transaction_id
                or instance is not None and reply['command_instance_id']!=instance
                or gui.get('ok') is not True or gui.get('cancelled') is not True or gui.get('replay_fence_installed') is not True
                or (gui.get('transaction_id'),gui.get('command_instance_id'))!=(reply['transaction_id'],reply['command_instance_id'])):
                raise Layer6ClientError('incomplete/mismatched targeted cancellation evidence')
        pending.pop(operation_key,None)
        return Layer6Ack(ok=True,transaction_id=reply.get('transaction_id'),raw=reply)

    def submit_command(
        self,
        u_cmd: Sequence[float],
        *,
        mode: str | None = None,
        discrete: Sequence[str] = (),
        transaction_id: str | None = None,
        ttl_s: float = 5.0,
        tau_apply: float | None = None,
        safety: Mapping[str, Any] | None = None,
        constraints: Mapping[str, Any] | None = None,
        confidence: float | None = None,
        provenance: Mapping[str, Any] | None = None,
        version: str = "external-client-v2",
        execution_rate_hz: float | None = None,
    ) -> Layer6Ack:
        if len(u_cmd) != 4:
            raise ValueError("u_cmd must be [aileron,elevator,throttle,rudder]")
        ttl_s = float(ttl_s)
        if ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        now = time.time()
        c_exec = dict(constraints or {})
        c_exec.setdefault("pilot_domain", {"a": [-1, 1], "e": [-1, 1], "t": [0, 1], "r": [-1, 1]})
        c_exec.setdefault("direct_pwm", False)
        packet: dict[str, Any] = {
            "transaction_id": transaction_id or f"external-{uuid.uuid4()}",
            "u_cmd": [float(x) for x in u_cmd],
            "C_exec": c_exec,
            "S_safe": dict(safety or {}),
            "tau_apply": now if tau_apply is None else float(tau_apply),
            "tau_deadline": now + ttl_s,
            "pi_decision": dict(provenance or {}),
            "nu": version,
            "execution_intent": {
                "requested_pilot_mode": mode,
                "discrete": [str(x) for x in discrete],
            },
        }
        if confidence is not None:
            packet["rho_decision"] = float(confidence)
        if execution_rate_hz is not None:
            rate = float(execution_rate_hz)
            if rate <= 0 or rate > 200:
                raise ValueError("execution_rate_hz must be in (0, 200]")
            packet["execution_rate_hz"] = rate
        return self.submit(packet)

    # ----- shared demo implementation -------------------------------------
    @staticmethod
    def _jitter(value: float, amount: float, rng: random.Random, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, value + rng.uniform(-amount, amount)))

    def _hold_and_verify(
        self,
        seconds: float,
        *,
        expected_mode: str | None = None,
        expected_armed: bool | None = None,
        verify_timeout: float = 8.0,
    ) -> Mapping[str, Any] | None:
        end = time.monotonic() + max(0.0, float(seconds))
        last = None
        while time.monotonic() < end:
            last = self.gui_status(timeout=0.2) or last
            time.sleep(0.1)
        if expected_mode is None and expected_armed is None:
            return self.gui_status(timeout=0.5) or last

        def good(st):
            if not st or not st.get("mavlink_connected"):
                return False
            if expected_mode is not None and str(st.get("mode") or "").upper() != expected_mode.upper():
                return False
            if expected_armed is not None and st.get("armed") is not bool(expected_armed):
                return False
            return True

        st = self.wait_for_status(good, timeout=verify_timeout)
        if not good(st):
            wants = []
            if expected_mode is not None:
                wants.append(f"mode={expected_mode}")
            if expected_armed is not None:
                wants.append(f"armed={expected_armed}")
            raise RuntimeError(
                "ArduPilot did not confirm " + ", ".join(wants)
                + f". Last GUI/FCU status: {_fmt_status(st)}"
            )
        print(f"[DEMO][VERIFY] confirmed {_fmt_status(st)}", flush=True)
        return st

    def _verify_mode_with_retry(
        self,
        *,
        mode: str,
        command: Sequence[float],
        discrete: Sequence[str],
        hold_s: float,
        verify_timeout: float,
        transaction_prefix: str,
        rate_hz: float,
        retries: int = 3,
    ) -> Mapping[str, Any]:
        """Confirm a requested flight mode, re-submitting through canonical intake if needed."""
        last = None
        for attempt in range(1, max(1, int(retries)) + 1):
            if attempt > 1:
                print(f"[DEMO][MODE] retry {attempt}/{retries}: requesting {mode}", flush=True)
                self.submit_command(
                    command, mode=mode, discrete=discrete,
                    transaction_id=f"{transaction_prefix}-mode-retry-{attempt}",
                    ttl_s=max(2.0, hold_s + verify_timeout + 2.0),
                    safety={"psf": True, "rta": True},
                    constraints={"direct_pwm": False},
                    provenance={"source": "demo_layer6", "reason": "mode_retry"},
                    version="demo-client-v5", execution_rate_hz=rate_hz,
                )
            last = self.wait_for_status(
                lambda st: bool(st and st.get("mavlink_connected")
                    and str(st.get("mode") or "").upper() == mode.upper()),
                timeout=verify_timeout, interval=0.15,
            )
            if last and str(last.get("mode") or "").upper() == mode.upper():
                print(f"[DEMO][VERIFY] confirmed {_fmt_status(last)}", flush=True)
                return last
        raise RuntimeError(
            f"ArduPilot did not confirm mode={mode} after {retries} canonical retries. "
            f"Last GUI/FCU status: {_fmt_status(last)}"
        )

    def _wait_for_ground(self, timeout: float) -> Mapping[str, Any]:
        end = time.monotonic() + float(timeout)
        last = None
        next_log = 0.0
        while time.monotonic() < end:
            last = self.gui_status(timeout=0.2) or last
            if _ground_safe(last):
                print(f"[DEMO][LAND] ground confirmed: {_fmt_status(last)}", flush=True)
                return last
            now = time.monotonic()
            if now >= next_log:
                print(f"[DEMO][LAND] waiting for ground evidence: {_fmt_status(last)}", flush=True)
                next_log = now + 1.0
            time.sleep(0.1)
        raise RuntimeError(
            "Touchdown was commanded but ArduPilot did not provide safe ground evidence before DISARM. "
            f"Last GUI/FCU status: {_fmt_status(last)}. DISARM was not forced."
        )

    def _verify_arm_with_retry(
        self,
        *,
        command: Sequence[float],
        hold_s: float,
        verify_timeout: float,
        transaction_prefix: str,
        rate_hz: float,
        retries: int = 3,
        retry_delay: float = 1.0,
    ) -> Mapping[str, Any]:
        """Confirm ARM using normal retries through the canonical Layer-6 intake.

        No force-arm is used. The first ARM request is the phase transaction already
        submitted by the caller. Further attempts are fresh Layer-6 transactions so
        the discrete ARM action is emitted again by the GUI/MAVLink path.
        """
        retries = max(1, int(retries))
        last = None
        end = time.monotonic() + max(0.0, float(hold_s))
        while time.monotonic() < end:
            last = self.gui_status(timeout=0.25) or last
            if last and last.get("armed") is True:
                print(f"[DEMO][VERIFY] confirmed {_fmt_status(last)}", flush=True)
                return last
            time.sleep(0.1)

        for attempt in range(1, retries + 1):
            last = self.wait_for_status(
                lambda st: bool(st and st.get("mavlink_connected") and st.get("armed") is True),
                timeout=verify_timeout, interval=0.15,
            ) or last
            if last and last.get("armed") is True:
                print(f"[DEMO][VERIFY] confirmed {_fmt_status(last)}", flush=True)
                return last

            print(
                f"[DEMO][ARM] attempt {attempt}/{retries} not confirmed; "
                f"ACK={(last or {}).get('last_command_ack')} "
                f"STATUSTEXT={(last or {}).get('last_statustext')!r}", flush=True,
            )
            if attempt < retries:
                time.sleep(max(0.0, float(retry_delay)))
                print(f"[DEMO][ARM] retry {attempt+1}/{retries}: normal ARM request", flush=True)
                self.submit_command(
                    command, mode="QHOVER", discrete=["ARM"],
                    transaction_id=f"{transaction_prefix}-arm-retry-{attempt+1}",
                    ttl_s=max(2.0, verify_timeout + retry_delay + 2.0),
                    safety={"psf": True, "rta": True},
                    constraints={"direct_pwm": False},
                    provenance={"source": "demo_layer6", "reason": "arm_retry"},
                    version="demo-client-v6", execution_rate_hz=rate_hz,
                )

        raise RuntimeError(
            "ArduPilot remained disarmed after repeated normal ARM requests. "
            "Force-arm was not used. "
            f"Last GUI/FCU status: {_fmt_status(last)}"
        )

    def _verify_disarm_with_retry(self, timeout: float, retries: int) -> Mapping[str, Any]:
        last = self.gui_status(timeout=0.5)
        if last and last.get("armed") is False:
            print(f"[DEMO][VERIFY] confirmed {_fmt_status(last)}", flush=True)
            return last
        if not _ground_safe(last):
            last = self._wait_for_ground(max(3.0, timeout))
        for attempt in range(1, max(1, int(retries)) + 1):
            if not _ground_safe(last):
                raise RuntimeError(
                    "Ground evidence was lost before DISARM; refusing to force-disarm. "
                    f"Last GUI/FCU status: {_fmt_status(last)}"
                )
            print(f"[DEMO][DISARM] normal request attempt {attempt}/{retries}", flush=True)
            self.submit_command(
                [0, 0, 0, 0], mode="QHOVER", discrete=["DISARM"],
                transaction_id=f"demo-disarm-retry-{attempt}", ttl_s=max(2.0, timeout + 1.0),
                safety={"psf": True, "rta": True}, constraints={"direct_pwm": False}, version="demo-client-v4",
            )
            end = time.monotonic() + float(timeout)
            while time.monotonic() < end:
                last = self.gui_status(timeout=0.2) or last
                if last and last.get("armed") is False:
                    print(f"[DEMO][VERIFY] confirmed {_fmt_status(last)}", flush=True)
                    return last
                time.sleep(0.1)
        extra = ""
        if last:
            if last.get("last_statustext"):
                extra += f" FCU: {last.get('last_statustext')}"
            if last.get("last_command_ack"):
                extra += f" ACK: {last.get('last_command_ack')}"
        raise RuntimeError(
            "ArduPilot remained armed after repeated normal DISARM requests while ground-safe. "
            "Force-disarm was intentionally not used." + extra + f" Last status: {_fmt_status(last)}"
        )

    def run_demo_sequence(
        self,
        *,
        seed: int = 42,
        time_scale: float = 1.0,
        rate_hz: float = 30.0,
        connect_timeout: float = 12.0,
        verify_timeout: float = 8.0,
        landing_timeout: float = 20.0,
        arm_retries: int = 3,
        disarm_retries: int = 3,
    ) -> None:
        # rate_hz is retained for CLI/API compatibility. The service owns its
        # configured execution cadence (30 Hz by default).
        if rate_hz <= 0:
            raise ValueError("rate_hz must be > 0")
        rng = random.Random(seed)
        self.ensure_mavlink(connect_timeout)
        # A MAVLink heartbeat can arrive while ArduPilot still reports INITIALISING.
        # Do not send the first QHOVER request until the FCU has reached a stable mode.
        self.wait_fcu_ready(timeout=max(20.0, connect_timeout, verify_timeout), stable_samples=3)
        for i, (name, mode, nominal, discrete, duration) in enumerate(PHASES):
            zero = name in {"ground", "arm", "touchdown", "disarm"}
            a = 0.0 if zero else self._jitter(nominal[0], 0.03, rng)
            e = 0.0 if zero else self._jitter(nominal[1], 0.03, rng)
            t = 0.0 if zero else self._jitter(nominal[2], 0.025, rng, 0.0, 1.0)
            r = 0.0 if zero else self._jitter(nominal[3], 0.025, rng)
            hold_s = max(0.0, duration * time_scale)
            print(
                f"[DEMO] {name:18s} mode={mode:10s} "
                f"u={[round(x, 3) for x in (a, e, t, r)]} discrete={discrete}", flush=True,
            )
            self.submit_command(
                [a, e, t, r], mode=mode, discrete=discrete,
                transaction_id=f"demo-{i:02d}-{name}",
                ttl_s=max(2.0, hold_s + verify_timeout + 2.0),
                safety={"psf": True, "rta": True}, constraints={"direct_pwm": False},
                provenance={"source": "demo_layer6", "phase": name}, version="demo-client-v4",
                execution_rate_hz=rate_hz,
            )
            expected_mode = mode if name in {"ground", "transition", "back_transition"} else None
            if expected_mode is not None:
                # Hold first, then verify mode with canonical re-submit retries.
                self._hold_and_verify(hold_s, verify_timeout=verify_timeout)
                self._verify_mode_with_retry(
                    mode=expected_mode, command=[a, e, t, r], discrete=discrete,
                    hold_s=hold_s, verify_timeout=verify_timeout,
                    transaction_prefix=f"demo-{i:02d}-{name}", rate_hz=rate_hz, retries=3,
                )
            elif name == "arm":
                self._verify_arm_with_retry(
                    command=[a, e, t, r], hold_s=hold_s, verify_timeout=verify_timeout,
                    transaction_prefix=f"demo-{i:02d}-{name}", rate_hz=rate_hz,
                    retries=arm_retries,
                )
            else:
                self._hold_and_verify(hold_s, verify_timeout=verify_timeout)
            if name == "touchdown":
                self._wait_for_ground(landing_timeout)
            elif name == "disarm":
                self._verify_disarm_with_retry(verify_timeout, disarm_retries)
        print("[DEMO] sequence complete with ArduPilot execution verification", flush=True)


def build_demo_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Python TDDT Layer-6 demo implemented entirely via Layer6Client")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--time-scale", type=float, default=1.0)
    p.add_argument("--rate-hz", type=float, default=30.0)
    p.add_argument("--connect-timeout", type=float, default=12.0, help="seconds to wait for GUI MAVLink connection")
    p.add_argument("--verify-timeout", type=float, default=8.0, help="seconds to wait for ArduPilot mode/arm evidence")
    p.add_argument("--landing-timeout", type=float, default=20.0, help="seconds to wait for ground evidence")
    p.add_argument("--arm-retries", type=int, default=3, help="normal ARM retries; force-arm is never automatic")
    p.add_argument("--disarm-retries", type=int, default=3, help="normal DISARM retries; force-disarm is never automatic")
    lifecycle = p.add_mutually_exclusive_group()
    lifecycle.add_argument("--restart-at-end", action="store_true", help="restart complete Layer-6/FlightStack after successful demo")
    lifecycle.add_argument("--stop-at-end", action="store_true", help="stop Layer-6/FlightStack after successful demo")
    p.add_argument("--start-if-needed", action="store_true", help="start the complete Layer-6/FlightStack automatically when it is not already running")
    p.add_argument("--flightstack-dir")
    p.add_argument("--terminal", choices=("gnome-terminal", "xfce4-terminal", "konsole", "xterm", "x-terminal-emulator"))
    p.add_argument("--restart-debug", action="store_true")
    p.add_argument("--startup-check", type=float, default=3.0)
    return p


def demo_main(argv: Sequence[str] | None = None) -> int:
    args = build_demo_parser().parse_args(argv)
    if args.time_scale < 0:
        raise SystemExit("--time-scale must be >= 0")
    if args.rate_hz <= 0:
        raise SystemExit("--rate-hz must be > 0")
    if args.connect_timeout <= 0 or args.verify_timeout <= 0 or args.landing_timeout <= 0:
        raise SystemExit("timeouts must be > 0")
    if args.arm_retries < 1:
        raise SystemExit("--arm-retries must be >= 1")
    if args.disarm_retries < 1:
        raise SystemExit("--disarm-retries must be >= 1")

    with Layer6Client(
        start_if_needed=args.start_if_needed,
        flightstack_dir=args.flightstack_dir,
        terminal=args.terminal,
        startup_check=args.startup_check,
        lifecycle_debug=args.restart_debug,
        startup_timeout=max(20.0, args.connect_timeout),
    ) as client:
        client.run_demo_sequence(
            seed=args.seed, time_scale=args.time_scale, rate_hz=args.rate_hz,
            connect_timeout=args.connect_timeout, verify_timeout=args.verify_timeout,
            landing_timeout=args.landing_timeout, arm_retries=args.arm_retries,
            disarm_retries=args.disarm_retries,
        )
        if args.restart_at_end:
            print("[DEMO] invoking Layer-6 restart() through Layer6Client ...", flush=True)
            client.restart_stack()
            print("[DEMO] Layer-6 restart completed", flush=True)
        elif args.stop_at_end:
            print("[DEMO] invoking Layer-6 stop() through Layer6Client ...", flush=True)
            client.stop_stack()
            print("[DEMO] Layer-6 stop completed", flush=True)
    return 0


def build_client_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Submit one command through Layer6Client")
    p.add_argument("--endpoint", default="127.0.0.1:14661")
    p.add_argument("--start-if-needed", action="store_true", help="start Layer-6/FlightStack automatically if GUI is not running")
    p.add_argument("--flightstack-dir")
    p.add_argument("--terminal", choices=("gnome-terminal", "xfce4-terminal", "konsole", "xterm", "x-terminal-emulator"))
    p.add_argument("--startup-check", type=float, default=3.0)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--mode", default="QHOVER")
    p.add_argument("--u", nargs=4, type=float, metavar=("AIL", "ELE", "THR", "RUD"), default=[0.0, 0.0, 0.0, 0.0])
    p.add_argument("--discrete", action="append", default=[])
    p.add_argument("--ttl", type=float, default=5.0)
    return p


def client_main(argv: Sequence[str] | None = None) -> int:
    a = build_client_parser().parse_args(argv)
    with Layer6Client(
        a.endpoint, start_if_needed=a.start_if_needed, flightstack_dir=a.flightstack_dir,
        terminal=a.terminal, startup_check=a.startup_check, lifecycle_debug=a.debug,
    ) as c:
        ack = c.submit_command(a.u, mode=a.mode, discrete=a.discrete, ttl_s=a.ttl)
        print(json.dumps(dict(ack.raw or {}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(client_main())
