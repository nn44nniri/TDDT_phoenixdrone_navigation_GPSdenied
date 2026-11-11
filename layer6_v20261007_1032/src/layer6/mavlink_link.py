"""Production GUI MAVLink link, importable without a Qt application."""
from __future__ import annotations
import time
from collections import deque
from typing import Dict,Any,Optional
from pymavlink import mavutil
from .telemetry import TelemetryReceiver,empty_channel_status

def clamp(v,lo,hi):return max(lo,min(hi,v))

class MavlinkLink:
    def __init__(self, cfg: Dict[str, Any], override_connection: Optional[str] = None):
        mcfg = cfg["mavlink"]
        self.connection_string = override_connection or str(mcfg["connection"])
        self.source_system = int(mcfg.get("source_system", 255))
        self.source_component = int(mcfg.get("source_component", 191))
        self.heartbeat_timeout = float(mcfg.get("heartbeat_timeout_seconds", 10))
        self.master = None
        self.target_system = 0
        self.target_component = 0
        self.telemetry_receiver = None
        self.telemetry_requests = {}
        self.status_observations = {}
        self.command_correlation = None
        from .segments import SegmentExecution
        self.segments=SegmentExecution(self)
        self.telemetry_history_capacity=mcfg.get('telemetry_history_capacity',4096)
        from .telemetry_history import TelemetryHistory
        TelemetryHistory(self.telemetry_history_capacity)  # Validate before connecting.
        self.armed: Optional[bool] = None
        self.mode = "—"
        # Physical/landing evidence used by Layer-6 demo and Layer-7 feedback.
        self.landed_state: Optional[str] = None
        self.groundspeed_m_s: Optional[float] = None
        self.relative_altitude_m: Optional[float] = None
        self.vertical_speed_m_s: Optional[float] = None
        self.last_statustext: Optional[str] = None
        self.last_command_ack: Optional[dict] = None
        # Stateful reverse path (B6/R6): retain a bounded telemetry history while
        # exporting only the reconstructed current state to Layer-7 callers.
        self.telemetry_history = deque(maxlen=4096)
        self.telemetry_latest: Dict[str, dict] = {}
        self.attitude = {"roll": None, "pitch": None, "yaw": None,
                         "roll_rate": None, "pitch_rate": None, "yaw_rate": None}
        self.airspeed_m_s: Optional[float] = None
        self.heading_deg: Optional[float] = None
        self.latitude_deg: Optional[float] = None
        self.longitude_deg: Optional[float] = None
        self.altitude_msl_m: Optional[float] = None
        self.battery = {"voltage_v": None, "current_a": None, "remaining_percent": None}
        self.servo_output_raw: Dict[str, Optional[int]] = {}
        self.last_manual_pilot = {"aileron": 0.0, "elevator": 0.0, "throttle": 0.0, "rudder": 0.0}

    def connect(self) -> None:
        self.master = mavutil.mavlink_connection(
            self.connection_string,
            source_system=self.source_system,
            source_component=self.source_component,
            autoreconnect=True,
        )
        hb = self.master.wait_heartbeat(timeout=self.heartbeat_timeout)
        if hb is None:
            raise TimeoutError(f"no HEARTBEAT within {self.heartbeat_timeout:.1f}s")
        if int(hb.autopilot)!=mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA:
            raise ValueError('ArduPilot heartbeat required; no telemetry source fallback')
        self.target_system = int(hb.get_srcSystem())
        self.target_component = int(hb.get_srcComponent())
        from .telemetry_source import native_source_contract
        self.telemetry_source_contract=native_source_contract()
        self.telemetry_receiver=TelemetryReceiver(self.target_system,self.target_component,
            history_capacity=self.telemetry_history_capacity,source_contract=self.telemetry_source_contract)
        self.master.target_system=self.target_system;self.master.target_component=self.target_component
        self.telemetry_history.clear();self.telemetry_latest.clear()
        self.status_observations.clear()
        from .command_correlation import CommandCorrelation
        self.command_correlation=CommandCorrelation(self.telemetry_receiver.history.session_id,
            self.telemetry_receiver.contracts.domain,ordered_stream=isinstance(self.master,mavutil.mavtcp),
            source_verified=self.telemetry_source_contract['deployment_verified'])
        from .segments import SegmentExecution
        self.segments=SegmentExecution(self)
        self.battery={k:None for k in self.battery};self.servo_output_raw={}
        self._update_heartbeat(hb)
        # A GNSS-denied FCU cannot discover its home from a disabled receiver.
        # Install the surveyed mission origin once, through this same GUI link;
        # this is initialization, never a recurring navigation observation.
        import os
        binding_path=os.environ.get('TDDT_NATIVE_BINDING_FILE')
        if binding_path:
            from .binding import context_guard
            origin=context_guard(binding_path).binding['mission_origin']
            self.master.mav.command_long_send(self.target_system,self.target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_HOME,0,0,0,0,0,
                float(origin['latitude_deg']),float(origin['longitude_deg']),
                float(origin['terrain_elevation_msl_m']))
        from .status import observe_status
        observe_status(self,hb)
        self.request_telemetry()

    def request_telemetry(self):
        from .telemetry import REQUEST_HZ,dialect
        from .status import STATUS_REQUEST_HZ
        for name,hz in {**REQUEST_HZ,**STATUS_REQUEST_HZ}.items():
            msgid=getattr(dialect,'MAVLINK_MSG_ID_'+name)
            request={'message_id':msgid,'requested_hz':hz,'interval_us':int(1e6/hz),
                'sent_monotonic':time.monotonic(),'ack_is_sample':False,'sent':False}
            try:
                self.master.mav.command_long_send(self.target_system,self.target_component,
                    mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,0,msgid,request['interval_us'],0,0,0,0,0)
                request['sent']=True
            except Exception as exc:request['error']=str(exc)
            self.telemetry_requests[name]=request

    def received_channels(self):
        return self.telemetry_receiver.snapshot() if self.telemetry_receiver is not None else empty_channel_status()

    def _update_heartbeat(self, msg) -> None:
        try:
            self.armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            mapping = self.master.mode_mapping() if self.master else {}
            reverse = {v: k for k, v in (mapping or {}).items()}
            self.mode = reverse.get(int(msg.custom_mode), str(int(msg.custom_mode)))
        except Exception:
            pass

    def send_manual(self, pitch: int, roll: int, throttle: int, yaw: int, *, command_context=None) -> None:
        if not self.master:
            return
        pitch_i=int(clamp(pitch, -1000, 1000)); roll_i=int(clamp(roll, -1000, 1000))
        throttle_i=int(clamp(throttle, 0, 1000)); yaw_i=int(clamp(yaw, -1000, 1000))
        from .command_correlation import verified_mavlink_send
        pre=None
        if command_context is not None:
            self.drain()
            candidate=self.telemetry_receiver.latest.get('SCALED_IMU')
            if candidate is not None:
                contract=self.telemetry_receiver.contracts.evaluate(candidate)
                if contract['normalized']['available'] and contract['freshness']['fresh']:pre=candidate
        started=time.monotonic()
        if not self.segments.before_write(command_context,pre):return False
        message=self.master.mav.manual_control_encode(self.target_system,pitch_i,roll_i,throttle_i,yaw_i,0)
        try:wire=verified_mavlink_send(self.master,message)
        except Exception:
            if self.command_correlation is not None:self.command_correlation.failed_send()
            raise
        completed=time.monotonic()
        self.last_manual_pilot = {
            "aileron": roll_i/1000.0, "elevator": pitch_i/1000.0,
            "throttle": throttle_i/1000.0, "rudder": yaw_i/1000.0,
        }
        if self.command_correlation is not None:
            nonce=self.command_correlation.record_send(command_context,self.last_manual_pilot,wire,started,completed,
                write_verified=True,pre=pre)
            self.segments.sent(command_context)
            if nonce is not None:
                try:
                    verified_mavlink_send(self.master,self.master.mav.timesync_encode(0,nonce))
                    self.command_correlation.barrier_sent(nonce)
                except Exception as exc:
                    self.command_correlation.records[self.command_correlation.current]['barrier_request_error']=str(exc)[:200]

    def set_mode(self, name: str) -> bool:
        if not self.master:
            return False
        mapping = self.master.mode_mapping() or {}
        name = name.upper()
        if name not in mapping:
            return False
        self.master.mav.set_mode_send(
            self.target_system,
            mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            mapping[name],
        )
        return True

    def arm(self) -> None:
        self._arm(False, True)

    def disarm(self) -> None:
        self._arm(False, False)

    def _arm(self, force: bool, arm: bool) -> None:
        if not self.master:
            return
        self.master.mav.command_long_send(
            self.target_system,
            self.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1 if arm else 0,
            21196 if force else 0,
            0, 0, 0, 0, 0,
        )

    def _record_telemetry(self, msg_type: str, fields: dict) -> None:
        rec={"type": str(msg_type), "timestamp_monotonic": time.monotonic(), "fields": dict(fields)}
        self.telemetry_history.append(rec); self.telemetry_latest[str(msg_type)] = rec

    def drain(self) -> None:
        if not self.master:
            return
        while True:
            msg = self.master.recv_match(blocking=False)
            if msg is None:
                break
            t = msg.get_type()
            if self.telemetry_receiver is None or not self.telemetry_receiver.accepts(msg):continue
            if t=='TIMESYNC' and self.command_correlation is not None:
                self.command_correlation.timesync_reply(msg);self.segments.timesync(msg)
            from .status import observe_status
            observation=observe_status(self,msg)
            if observation is not None and not observation['valid']:continue
            sample=self.telemetry_receiver.ingest(msg)
            observed=sample if sample is not None else observation
            if observed is not None and self.command_correlation is not None:
                c=self.telemetry_receiver.contracts.evaluate(observed)
                self.command_correlation.observe(observed,fresh=c['freshness']['fresh'],
                    clock_valid=c['freshness']['reason'] in ('fresh_local_observation','stale'))
                self.segments.observe(observed)
            if sample is not None:
                # Raw channel records are separate from the legacy display state.
                self.telemetry_history.append(sample);self.telemetry_latest[t]=sample
                if not sample['valid']:
                    if t=='SYS_STATUS':self.battery={k:None for k in self.battery}
                    elif t=='SERVO_OUTPUT_RAW':self.servo_output_raw={}
                    continue
            if t == "HEARTBEAT":
                self._record_telemetry(t, {"custom_mode": int(getattr(msg,"custom_mode",0)), "base_mode": int(getattr(msg,"base_mode",0))})
                self._update_heartbeat(msg)
            elif t == "EXTENDED_SYS_STATE":
                landed = int(getattr(msg, "landed_state", 0))
                names = {
                    int(getattr(mavutil.mavlink, "MAV_LANDED_STATE_UNDEFINED", 0)): "UNDEFINED",
                    int(getattr(mavutil.mavlink, "MAV_LANDED_STATE_ON_GROUND", 1)): "ON_GROUND",
                    int(getattr(mavutil.mavlink, "MAV_LANDED_STATE_IN_AIR", 2)): "IN_AIR",
                    int(getattr(mavutil.mavlink, "MAV_LANDED_STATE_TAKEOFF", 3)): "TAKEOFF",
                    int(getattr(mavutil.mavlink, "MAV_LANDED_STATE_LANDING", 4)): "LANDING",
                }
                self.landed_state = names.get(landed, str(landed))
                self._record_telemetry(t, {"landed_state": self.landed_state})
            elif t == "ATTITUDE":
                vals={
                    "roll": float(getattr(msg,"roll",0.0)), "pitch": float(getattr(msg,"pitch",0.0)), "yaw": float(getattr(msg,"yaw",0.0)),
                    "roll_rate": float(getattr(msg,"rollspeed",0.0)), "pitch_rate": float(getattr(msg,"pitchspeed",0.0)), "yaw_rate": float(getattr(msg,"yawspeed",0.0)),
                }
                self.attitude.update(vals); self._record_telemetry(t, vals)
            elif t == "VFR_HUD":
                vals={}
                try: self.groundspeed_m_s = float(msg.groundspeed); vals["groundspeed_m_s"]=self.groundspeed_m_s
                except Exception: pass
                try: self.vertical_speed_m_s = float(msg.climb); vals["vertical_speed_m_s"]=self.vertical_speed_m_s
                except Exception: pass
                try: self.airspeed_m_s = float(msg.airspeed); vals["airspeed_m_s"]=self.airspeed_m_s
                except Exception: pass
                try: self.heading_deg = float(msg.heading); vals["heading_deg"]=self.heading_deg
                except Exception: pass
                self._record_telemetry(t, vals)
            elif t == "GLOBAL_POSITION_INT":
                vals={}
                try: self.relative_altitude_m = float(msg.relative_alt) / 1000.0; vals["relative_altitude_m"]=self.relative_altitude_m
                except Exception: pass
                try: self.latitude_deg = float(msg.lat) / 1e7; vals["latitude_deg"]=self.latitude_deg
                except Exception: pass
                try: self.longitude_deg = float(msg.lon) / 1e7; vals["longitude_deg"]=self.longitude_deg
                except Exception: pass
                try: self.altitude_msl_m = float(msg.alt) / 1000.0; vals["altitude_msl_m"]=self.altitude_msl_m
                except Exception: pass
                self._record_telemetry(t, vals)
            elif t == "SYS_STATUS":
                vals={}
                try:
                    v=float(msg.voltage_battery); self.battery["voltage_v"] = None if v < 0 or v==65535 else v/1000.0; vals["voltage_v"]=self.battery["voltage_v"]
                except Exception: pass
                try:
                    c=float(msg.current_battery); self.battery["current_a"] = None if c < 0 else c/100.0; vals["current_a"]=self.battery["current_a"]
                except Exception: pass
                try:
                    r=int(msg.battery_remaining); self.battery["remaining_percent"] = None if r < 0 else r; vals["remaining_percent"]=self.battery["remaining_percent"]
                except Exception: pass
                # Raw SYS_STATUS and health masks were already retained above.
            elif t == "SERVO_OUTPUT_RAW":
                vals={}
                for i in range(1,17):
                    name=f"servo{i}_raw"
                    value=sample['fields'].get(name)
                    vals[name]=int(value) if value is not None else None
                self.servo_output_raw=vals
            elif t == "COMMAND_ACK":
                self.last_command_ack = {
                    "command": int(getattr(msg, "command", -1)),
                    "result": int(getattr(msg, "result", -1)),
                }
                print(f"[MAV][ACK] command={getattr(msg,'command','?')} result={getattr(msg,'result','?')}", flush=True)
            elif t == "STATUSTEXT":
                self.last_statustext = str(getattr(msg,'text',''))
                print(f"[FCU] {self.last_statustext}", flush=True)
        self.segments.check()

