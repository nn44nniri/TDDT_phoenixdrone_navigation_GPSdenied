from __future__ import annotations

from typing import Any


def _normalize_command(command: Any) -> list[float]:
    if isinstance(command, dict):
        for key in ("first_move", "pilot_command", "control", "command"):
            v = command.get(key)
            if isinstance(v, (list, tuple)) and len(v) >= 4:
                command = v
                break
    if not isinstance(command, (list, tuple)) or len(command) < 4:
        raise ValueError("pilot-domain command must contain [aileron,elevator,throttle,rudder]")
    a,e,t,r = [float(x) for x in command[:4]]
    return [max(-1.0,min(1.0,a)), max(-1.0,min(1.0,e)), max(0.0,min(1.0,t)), max(-1.0,min(1.0,r))]


def install_environment_bridge(adapter_cls: type) -> None:
    if getattr(adapter_cls, "_tdoc_stream_bridge_v1", False):
        return

    original_safe = getattr(adapter_cls, "safe_control_write", None)
    original_close = getattr(adapter_cls, "close", None)

    def configure_tdoc_rate_sync(self, cfg: dict[str, Any] | None) -> dict[str, Any]:
        self._tdoc_rate_sync = dict(cfg or {})
        return dict(self._tdoc_rate_sync)

    def tdoc_rate_sync_status(self) -> dict[str, Any]:
        ctl = getattr(self, "controller", None)
        fn = getattr(ctl, "tdoc_control_stream_status_session", None) if ctl is not None else None
        stream = fn() if callable(fn) else {"running": False, "reason": "ptlib_stream_api_unavailable"}
        return {"configured": dict(getattr(self, "_tdoc_rate_sync", {}) or {}), "stream": stream}

    def safe_control_write(self, command: Any, *args: Any, **kwargs: Any):
        cfg = dict(getattr(self, "_tdoc_rate_sync", {}) or {})
        enabled = bool(cfg.get("enabled", True))
        ctl = getattr(self, "controller", None)
        client = getattr(self, "client", None)
        update = getattr(ctl, "tdoc_control_stream_update_session", None) if ctl is not None else None
        public_send = getattr(client, "send_pilot_command", None) if client is not None else None
        if not enabled or (not callable(public_send) and not callable(update)):
            if callable(original_safe):
                return original_safe(self, command, *args, **kwargs)
            raise RuntimeError("PT_LIB external-control stream unavailable and no legacy safe_control_write exists")
        cmd = _normalize_command(command)
        if callable(public_send):
            out = public_send(
                aileron=cmd[0], elevator=cmd[1], throttle=cmd[2], rudder=cmd[3],
                decision_id=cfg.get("decision_id"), transaction_id=cfg.get("transaction_id"),
                rate_hz=cfg.get("ptlib_control_hz"), ttl_s=cfg.get("command_ttl_s"),
                execution_authorization=cfg.get("execution_authorization"),
            )
        else:
            out = update(
                *cmd,
                decision_id=cfg.get("decision_id"),
                transaction_id=cfg.get("transaction_id"),
                rate_hz=cfg.get("ptlib_control_hz"),
                ttl_s=cfg.get("command_ttl_s"),
                execution_authorization=cfg.get("execution_authorization"),
                release_on_stale=bool(cfg.get("release_on_stale", True)),
                max_consecutive_failures=max(1, int(cfg.get("max_consecutive_stream_failures", 5))),
            )
        # PT_LIB exposes immutable evidence for the synchronous latch separately
        # from ``last_result``, which is live worker state and can legitimately be
        # replaced by a later observation-only refresh.
        first = (out.get("latch_result") or out.get("last_result") or {}) if isinstance(out, dict) else {}
        release = (first.get("release_confirmation") or {}) if isinstance(first, dict) else {}
        return {
            "ok": bool(out.get("running", False)) and bool(release.get("ready", True)),
            "command_sent": True,
            "streaming": True,
            "synchronous_first_send": True,
            "pilot_command": cmd,
            "rate_sync": dict(cfg),
            "stream_status": out,
            "latch_result": first,
            "release_confirmation": release,
            "release_confirmed": bool(release.get("ready", False)) if release else None,
            "direct_pwm": False,
            "mixer_owner": "ArduPilotController.fmu",
            "transport": "FMI2-CoSimulation",
            "public_api_used": bool(callable(public_send)),
        }

    def stop_tdoc_control_stream(self, reason: str = "layer5_stop") -> dict[str, Any]:
        ctl = getattr(self, "controller", None)
        fn = getattr(ctl, "tdoc_control_stream_stop_session", None) if ctl is not None else None
        if not callable(fn):
            return {"ok": True, "skipped": True, "reason": "stream_api_unavailable"}
        return fn(release=True, reason=reason)

    def close(self, *args: Any, **kwargs: Any):
        try:
            stop_tdoc_control_stream(self, "layer5_adapter_close")
        except Exception:
            pass
        if callable(original_close):
            return original_close(self, *args, **kwargs)
        return None

    adapter_cls.configure_tdoc_rate_sync = configure_tdoc_rate_sync
    adapter_cls.tdoc_rate_sync_status = tdoc_rate_sync_status
    adapter_cls.safe_control_write = safe_control_write
    adapter_cls.stop_tdoc_control_stream = stop_tdoc_control_stream
    adapter_cls.close = close
    adapter_cls._tdoc_stream_bridge_v1 = True
