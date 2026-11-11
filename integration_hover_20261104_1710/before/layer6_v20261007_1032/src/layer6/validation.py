from __future__ import annotations
from dataclasses import dataclass
import time
from .contracts import DecisionEnvelope

@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: tuple[str,...]

def validate(env: DecisionEnvelope, now: float | None=None) -> ValidationResult:
    now=time.time() if now is None else float(now)
    r=[]; c=env.command
    from .command_correlation import finite
    if any(v is not None and not finite(v) for v in (env.tau_apply,env.tau_deadline)):r.append('nonfinite_packet_time')
    if env.execution_segment is not None:
        try:
            from .segments import contract
            from .command_correlation import finite
            if contract({'execution_segment':env.execution_segment})!=env.execution_segment or not finite(env.tau_deadline):r.append('invalid_execution_segment')
        except (TypeError,ValueError,KeyError):r.append('invalid_execution_segment')
    if not -1 <= c.aileron <= 1: r.append("aileron_out_of_range")
    if not -1 <= c.elevator <= 1: r.append("elevator_out_of_range")
    if not 0 <= c.throttle <= 1: r.append("throttle_out_of_range")
    if not -1 <= c.rudder <= 1: r.append("rudder_out_of_range")
    if env.tau_apply is not None and now < env.tau_apply: r.append("before_tau_apply")
    if env.tau_deadline is not None and now > env.tau_deadline: r.append("after_tau_deadline")
    pd=(env.constraints or {}).get("pilot_domain",{})
    for key,val in (("a",c.aileron),("e",c.elevator),("t",c.throttle),("r",c.rudder)):
        lim=pd.get(key)
        if lim and not float(lim[0]) <= val <= float(lim[1]): r.append(f"constraint_{key}")
    if (env.constraints or {}).get("direct_pwm") is True: r.append("direct_pwm_forbidden")
    for gate in ("psf","rta"):
        if gate in (env.safety or {}) and not bool(env.safety[gate]): r.append(f"safety_{gate}_false")
    return ValidationResult(not r,tuple(r))
