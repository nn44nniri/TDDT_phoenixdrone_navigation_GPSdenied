from __future__ import annotations

import time
from typing import Any


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _get(config: Any, path: str, default: Any = None) -> Any:
    fn = getattr(config, 'get', None)
    if callable(fn):
        try:
            v = fn(path, default)
            return default if v is None else v
        except TypeError:
            try:
                v = fn(path)
                return default if v is None else v
            except Exception:
                return default
        except Exception:
            return default
    return default


def _native_ptlib_rate(pt: Any, default_hz: float) -> float:
    ctl = getattr(pt, 'controller', None)
    fn = getattr(ctl, 'control_boundary_rate_hz', None) if ctl is not None else None
    if callable(fn):
        try:
            v = float(fn())
            if v > 0:
                return v
        except Exception:
            pass
    return float(default_hz)


def resolve_rate_sync(config: Any, pt: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if isinstance(state, dict) else {}
    enabled = bool(_get(config, 'rate_sync.enabled', True))
    mode = str(_get(config, 'rate_sync.mode', 'auto')).strip().lower()
    if mode not in {'auto', 'manual'}:
        mode = 'auto'

    now = time.monotonic()
    prev = state.get('last_invoke_monotonic')
    if prev is not None:
        dt = max(1e-6, now - float(prev))
        alpha = float(_get(config, 'rate_sync.auto.ewma_alpha', 0.25))
        old = state.get('decision_period_ewma_s')
        state['decision_period_ewma_s'] = dt if old is None else (alpha * dt + (1.0 - alpha) * float(old))
    state['last_invoke_monotonic'] = now

    rtmo_enabled = bool(_get(config, 'tdoc.rtmo.enabled', False))
    if mode == 'manual':
        tdoc_hz = max(1e-6, float(_get(config, 'rate_sync.manual.tdoc_decision_hz', 1.0)))
        ptlib_hz = max(0.5, float(_get(config, 'rate_sync.manual.ptlib_control_hz', 10.0)))
        ttl_s = max(0.5, float(_get(config, 'rate_sync.manual.command_ttl_s', 8.0)))
        source = 'manual'
    else:
        default_native = float(_get(config, 'rate_sync.auto.default_ptlib_hz', 10.0))
        native = _native_ptlib_rate(pt, default_native)
        lo = float(_get(config, 'rate_sync.auto.min_ptlib_hz', 5.0))
        hi = float(_get(config, 'rate_sync.auto.max_ptlib_hz', 20.0))
        ptlib_hz = _clip(native, lo, hi)
        configured_fast_dt = float(_get(config, 'tdoc.rtmo.realtime.max_control_period_s', 0.1)) if rtmo_enabled else float(_get(config, 'scheduler.fast_dt_s', 1.0))
        requested_tdoc_hz = 1.0 / max(1e-6, configured_fast_dt)
        observed_period = state.get('decision_period_ewma_s')
        if observed_period is None:
            tdoc_hz = requested_tdoc_hz
            period_for_ttl = 1.0 / max(1e-6, tdoc_hz)
        else:
            observed_hz = 1.0 / max(1e-6, float(observed_period))
            # A target faster than measured computation is not claimed as achieved.
            tdoc_hz = min(requested_tdoc_hz, observed_hz)
            period_for_ttl = max(float(observed_period), 1.0 / max(1e-6, requested_tdoc_hz))
        if rtmo_enabled:
            # RTMO command lease is derived from the intended fast tick and never
            # expanded to hide slow TDOC computation. A missed lease therefore fails
            # stale/closed instead of silently holding a many-second command.
            hold_ticks=max(1,int(_get(config,'tdoc.rtmo.command_lease.hold_ticks',3)))
            ttl_s=hold_ticks*configured_fast_dt
            source='rtmo_fast_tick_lease+ptlib_native_boundary_rate'
        else:
            factor = float(_get(config, 'rate_sync.auto.command_ttl_factor', 2.0))
            ttl_lo = float(_get(config, 'rate_sync.auto.min_command_ttl_s', 4.0))
            ttl_hi = float(_get(config, 'rate_sync.auto.max_command_ttl_s', 15.0))
            ttl_s = _clip(factor * period_for_ttl, ttl_lo, ttl_hi)
            source = 'ptlib_native_boundary_rate+tdoc_measured_cadence'

    override_timeout_hint = float(_get(config, 'rate_sync.safeguards.rc_override_timeout_hint_s', 3.0))
    minimum_refreshes = max(2.0, float(_get(config, 'rate_sync.safeguards.minimum_refreshes_per_override_timeout', 6.0)))
    min_from_timeout = minimum_refreshes / max(0.1, override_timeout_hint)
    if mode == 'auto':
        ptlib_hz = max(ptlib_hz, min_from_timeout)

    ratio = ptlib_hz / max(1e-6, tdoc_hz)
    return {
        'schema': 'TDOC-PTLIB-rate-sync-v2' if rtmo_enabled else 'TDOC-PTLIB-rate-sync-v1',
        'enabled': enabled,
        'mode': mode,
        'source': source,
        'tdoc_decision_hz': tdoc_hz,
        'tdoc_decision_period_s': 1.0 / max(1e-6, tdoc_hz),
        'ptlib_control_hz': ptlib_hz,
        'ptlib_control_period_s': 1.0 / max(1e-6, ptlib_hz),
        'command_ttl_s': ttl_s,
        # Under transaction-clocked FMI operation this TTL is the independent
        # executor-health watchdog.  It is deliberately not enlarged to match a
        # slow planner wall time: PT_LIB may renew it only from a fresh, frozen-
        # plant observation while the exact Layer5/Layer6 authorization remains
        # latched.  A new plant-time grant still requires a new L6 transaction.
        'ttl_semantics': ('executor_health_watchdog' if rtmo_enabled else 'command_age_deadman'),
        'transaction_clocked_watchdog': bool(rtmo_enabled),
        'require_execution_authorization_for_watchdog': bool(rtmo_enabled),
        'rate_ratio_ptlib_to_tdoc': ratio,
        'release_on_stale': bool(_get(config, 'rate_sync.safeguards.release_on_stale', True)),
        'max_consecutive_stream_failures': max(1, int(_get(config, 'rate_sync.safeguards.max_consecutive_stream_failures', 5))),
        'preserve_commissioned_session': bool(_get(config, 'rate_sync.safeguards.preserve_commissioned_session', True)),
        'decision_period_ewma_s': state.get('decision_period_ewma_s'),
        'direct_pwm': False,
        'semantics': ('RTMO executor watchdog; only fresh frozen-plant health evidence may retain the last Layer5 FINAL latch, and only a new L6 transaction grants plant time' if rtmo_enabled else 'TDOC decides; PT_LIB refreshes the latest authorized pilot-domain command until TTL'),
    }
