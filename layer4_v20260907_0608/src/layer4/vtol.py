from __future__ import annotations
from pathlib import Path
import yaml


class VTOLProfile:
    """Read-only Phoenix VTOL constraint adapter; no external configuration is copied into Layer 4."""

    # Mission-phase names are owned by TDOC.  Layer-4's Phoenix constraint
    # profile historically used shorter aerodynamic mode names.  Keep the
    # translation explicit and deliberately narrow. Mission phases with a
    # dedicated phase-aware contract are normalized to their aerodynamic regime;
    # the requested phase name is still preserved in limits()/corridor metadata.
    _MODE_ALIASES = {
        "vertical_takeoff": "takeoff",
        # solution_v1 P0-5: forward transition is a transition aerodynamic regime,
        # but it owns a dedicated progress-dependent airspeed contract below.  It
        # must never fall through to the fixed-wing cruise stall floor.
        "transition_forward": "transition",
        # Problem #23: TDOC's transition-back mission phase is still a
        # transition aerodynamic regime.  It must not fall through to the
        # fixed-wing cruise stall floor.
        "transition_back": "transition",
        # Problem #24: terminal vertical landing is a ground-referenced VTOL
        # regime; it must not inherit cruise stall/clearance semantics.
        "vertical_landing": "landing",
    }

    def __init__(self, config):
        self.config = config
        self.data = {}
        self.source = None
        self._load()

    def _load(self):
        root = self.config.env_path("TDDT_TWINS_ROOT")
        p = (root / "config/phoenix_drone_v3.yaml") if root else None
        if p and p.exists():
            self.data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            self.source = str(p)

    def _get(self, path, default):
        cur = self.data
        for t in path.split('.'):
            if isinstance(cur, dict) and t in cur:
                cur = cur[t]
            else:
                return default
        return cur

    @classmethod
    def normalize_mode(cls, mode: str) -> str:
        requested = str(mode or "cruise").strip().lower()
        return cls._MODE_ALIASES.get(requested, requested)

    def terrain_corridor(self, mode: str, entry_agl_m: float, *, step_index: int = 0, step_count: int = 1, context=None):
        """Return phase-aware AGL corridor without weakening obstacle/plant gates.

        Priority-1 v2: the generic en-route terrain floor is not physically valid
        during ground-referenced vertical takeoff or low-altitude hover.  Landing
        keeps the already-established terminal corridor.  When an explicit takeoff
        target is available, the reference corridor progresses linearly toward it;
        otherwise the safe fallback only forbids ground penetration/material descent.
        """
        requested=str(mode or "cruise").strip().lower()
        cmode=self.normalize_mode(requested)
        ctx=context if isinstance(context,dict) else {}
        entry=max(float(entry_agl_m),0.0)
        if cmode == "takeoff":
            floor=max(float(self.config.get("vtol.vertical_takeoff_min_agl_m",0.0)),0.0)
            down_tol=max(float(self.config.get("vtol.vertical_takeoff_max_downward_excursion_m",0.25)),0.0)
            band=max(float(self.config.get("vtol.vertical_takeoff_corridor_tolerance_m",2.0)),0.0)
            target=None
            for k in ("takeoff_target_agl_m","phase_target_agl_m","target_agl_m"):
                if ctx.get(k) is not None:
                    try: target=float(ctx.get(k)); break
                    except Exception: pass
            if target is not None and target >= entry:
                n=max(int(step_count),1); frac=min(max((int(step_index)+1)/n,0.0),1.0)
                ref=entry+frac*(target-entry)
                return {"policy":"GROUND_REFERENCED_TAKEOFF_CORRIDOR","lower_m":max(floor,ref-band),"upper_m":ref+band,"reference_m":ref,"target_m":target}
            return {"policy":"GROUND_REFERENCED_TAKEOFF_CORRIDOR","lower_m":max(floor,entry-down_tol),"upper_m":None,"reference_m":entry,"target_m":target}
        if cmode == "hover":
            target=None
            for k in ("hover_target_agl_m","phase_target_agl_m","target_agl_m"):
                if ctx.get(k) is not None:
                    try: target=float(ctx.get(k)); break
                    except Exception: pass
            if target is None: target=entry
            tol=max(float(self.config.get("vtol.hover_agl_tolerance_m",3.0)),0.0)
            floor=max(float(self.config.get("vtol.hover_min_agl_m",0.0)),0.0)
            return {"policy":"HOVER_TARGET_AGL_CORRIDOR","lower_m":max(floor,target-tol),"upper_m":target+tol,"reference_m":target,"target_m":target}
        if cmode == "landing":
            floor=float(self.config.get("vtol.vertical_landing_min_agl_m",0.0))
            up=max(float(self.config.get("vtol.vertical_landing_max_upward_excursion_m",1.0)),0.0)
            return {"policy":"TERMINAL_GROUND_CORRIDOR","lower_m":floor,"upper_m":max(entry,floor)+up,"reference_m":entry,"target_m":floor}
        return {"policy":"ENROUTE_TERRAIN_FLOOR","lower_m":float(self.config.get("feasibility.min_terrain_clearance_m",30.0)),"upper_m":None,"reference_m":None,"target_m":None}

    def airspeed_corridor(
        self,
        mode: str,
        entry_airspeed_m_s: float,
        *,
        step_index: int = 0,
        step_count: int = 1,
        context=None,
        terminal_min_override_m_s=None,
    ):
        """Return a phase-aware scalar-airspeed corridor.

        solution_v1 P0-5: ``transition_forward`` must be allowed to enter from
        hover/near-zero airspeed and build airspeed across the prediction horizon.
        A single static 3 m/s floor on every prediction step is therefore invalid.

        The required terminal floor is the existing transition minimum.  A higher
        phase target may be supplied by authoritative context.  Reachability is
        checked against the already-configured qLPV ``delta_airspeed_m_s`` action
        bound and Layer-4 time step; no new actuator or aerodynamic authority is
        invented here.  Full-fidelity WingMPET verification remains mandatory.
        """
        requested = str(mode or "cruise").strip().lower()
        lim = self.limits(requested)
        entry = max(float(entry_airspeed_m_s), 0.0)
        n = max(int(step_count), 1)
        i = min(max(int(step_index), 0), n)
        frac = float(i) / float(n)

        if requested != "transition_forward":
            lower = max(float(lim["min_airspeed_m_s"]), 0.0)
            return {
                "policy": "STATIC_AIRSPEED_LIMIT",
                "lower_m_s": lower,
                "upper_m_s": float(lim["max_airspeed_m_s"]),
                "reference_m_s": None,
                "entry_m_s": entry,
                "terminal_min_m_s": lower,
                "terminal_target_m_s": lower,
                "progress": frac,
                "required_accel_m_s2": 0.0,
                "model_accel_limit_m_s2": None,
                "reachable": True,
            }

        ctx = context if isinstance(context, dict) else {}
        terminal_min = float(
            lim.get(
                "terminal_min_airspeed_m_s",
                self.config.get("vtol.min_airspeed_by_mode_m_s.transition", 3.0),
            )
        )
        if terminal_min_override_m_s is not None:
            try:
                terminal_min = max(terminal_min, float(terminal_min_override_m_s))
            except Exception:
                pass
        terminal_min = max(terminal_min, 0.0)

        target = None
        for key in (
            "transition_forward_terminal_airspeed_m_s",
            "phase_target_airspeed_m_s",
            "target_airspeed_m_s",
        ):
            if ctx.get(key) is not None:
                try:
                    target = float(ctx.get(key))
                    break
                except Exception:
                    pass
        if target is None:
            target = terminal_min
        # The corridor is a minimum-speed contract, not a command to preserve the
        # exact entry speed.  Once transition entry is already above the terminal
        # floor, safe deceleration must remain admissible (the qLPV action contract
        # already permits it).  Pinning the target to ``entry`` made harmless speed
        # variation below that exact value infeasible for the whole horizon.
        target = max(float(target), terminal_min)

        # Ramp upward only when acceleration is required.  If entry is already
        # above the terminal target, the minimum corridor is the target itself;
        # using the exact entry value at step zero is intrinsically infeasible
        # after robust tube tightening (entry - uncertainty < entry) and also
        # turns a minimum-speed contract into an unintended speed-hold command.
        reference = (
            entry + frac * (target - entry)
            if entry < target
            else target
        )
        lower = max(reference, 0.0)
        upper = float(lim["max_airspeed_m_s"])

        dt = max(float(self.config.get("edge.dt_s", 1.0)), 1e-9)
        total_time = float(n) * dt
        required_accel = max((target - entry) / total_time, 0.0)
        action_upper = self.config.get("qlpv.action_upper", []) or []
        model_accel_limit = None
        try:
            # action_order[-1] is delta_airspeed_m_s in scenario_templates.json.
            model_accel_limit = max(float(action_upper[-1]), 0.0) / dt
        except Exception:
            model_accel_limit = None
        reachable = bool(
            model_accel_limit is None
            or required_accel <= model_accel_limit + 1e-12
        )
        return {
            "policy": "FORWARD_TRANSITION_PROGRESS_CORRIDOR",
            "lower_m_s": lower,
            "upper_m_s": upper,
            "reference_m_s": reference,
            "entry_m_s": entry,
            "terminal_min_m_s": terminal_min,
            "terminal_target_m_s": target,
            "progress": frac,
            "required_accel_m_s2": required_accel,
            "model_accel_limit_m_s2": model_accel_limit,
            "reachable": reachable,
        }

    def limits(self, mode: str):
        requested_mode = str(mode or "cruise").strip().lower()
        constraint_mode = self.normalize_mode(requested_mode)
        stall = float(self._get("speed.stall_speed_m_s", self.config.get("feasibility.min_airspeed_m_s", 9.1)))
        vmax = float(self._get("speed.maximum_speed_m_s", self.config.get("feasibility.max_airspeed_m_s", 18.0)))
        terminal_min = None
        airspeed_policy = "STATIC_AIRSPEED_LIMIT"
        if requested_mode == "transition_forward":
            # P0-5: entry from hover may legitimately be near zero.  The 3 m/s
            # transition floor is a *terminal/progress* condition, not a step-0
            # lower bound.  Feasibility/FF verification use airspeed_corridor().
            amin = 0.0
            terminal_min = float(self.config.get("vtol.min_airspeed_by_mode_m_s.transition", 3.0))
            airspeed_policy = "FORWARD_TRANSITION_PROGRESS_CORRIDOR"
        elif constraint_mode in ("hover", "takeoff", "landing"):
            amin = float(self.config.get(f"vtol.min_airspeed_by_mode_m_s.{constraint_mode}", 0.0))
        elif constraint_mode == "transition":
            amin = float(self.config.get("vtol.min_airspeed_by_mode_m_s.transition", 3.0))
        else:
            amin = max(stall, float(self.config.get("vtol.min_airspeed_by_mode_m_s.cruise", stall)))
        wind = (
            float(self._get("mission.maximum_hover_wind_m_s", 5.0))
            if constraint_mode in ("hover", "takeoff", "landing")
            else float(self._get("mission.maximum_cruise_wind_m_s", 8.0))
        )
        gust = float(self._get("mission.maximum_gust_m_s", 10.0))
        out = {
            "mode": requested_mode,
            "constraint_mode": constraint_mode,
            "min_airspeed_m_s": amin,
            "max_airspeed_m_s": vmax,
            "max_wind_m_s": wind,
            "max_gust_m_s": gust,
            "airspeed_policy": airspeed_policy,
            "source": self.source or "Layer4 fallback",
        }
        if terminal_min is not None:
            out["terminal_min_airspeed_m_s"] = terminal_min
        return out
