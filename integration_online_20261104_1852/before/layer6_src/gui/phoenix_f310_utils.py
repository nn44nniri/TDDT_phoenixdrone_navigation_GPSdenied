from __future__ import annotations
import math

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def shape_axis(raw: float, deadzone: float, expo: float, invert: bool = False) -> float:
    x = -raw if invert else raw
    x = clamp(x, -1.0, 1.0)
    a = abs(x)
    if a <= deadzone:
        return 0.0
    n = (a - deadzone) / (1.0 - deadzone)
    y = (1.0 - expo) * n + expo * (n ** 3)
    return math.copysign(y, x)

def trigger_fraction(raw: float, released_value: float = -1.0) -> float:
    raw = clamp(raw, -1.0, 1.0)
    if released_value <= -0.5:
        return clamp((raw + 1.0) / 2.0, 0.0, 1.0)
    return clamp(raw, 0.0, 1.0)
