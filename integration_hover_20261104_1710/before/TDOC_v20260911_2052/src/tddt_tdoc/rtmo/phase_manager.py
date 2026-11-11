from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import csv

PHASE_ORDER=(
    "on_ground","vertical_takeoff","hover","transition_forward","climb",
    "cruise","turn","approach","transition_back","vertical_landing","touchdown",
)

@dataclass
class MissionPhaseManager:
    """Exact 11-phase TDOC mission FSM with execution- and plant-time-coherent advance.

    Guard timing is learned from the accepted Offline PHASE_HISTORY rather than
    invented in TDOC. Runtime state/goal guards may hold a phase longer; they may
    never skip or reorder phases.  Online phase advance is fail-closed when the
    current plant clock is unavailable or the current transaction lacks realized
    execution evidence.
    """
    phase_order: tuple[str,...]=PHASE_ORDER
    current_index: int=0
    entry_sim_time_s: float|None=None
    offline_min_dwell_s: dict[str,float]|None=None

    @classmethod
    def from_offline_report(cls, phase_history: Path|None) -> "MissionPhaseManager":
        dwell={p:0.0 for p in PHASE_ORDER}
        if phase_history and phase_history.is_file():
            rows=list(csv.DictReader(phase_history.open(newline='',encoding='utf-8')))
            by={p:[] for p in PHASE_ORDER}
            for r in rows:
                ph=str(r.get('phase') or '')
                if ph in by:
                    try: by[ph].append(float(r.get('flight_elapsed_s') or 0.0))
                    except Exception: pass
            for ph, vals in by.items():
                if vals: dwell[ph]=max(0.0,max(vals)-min(vals))
        return cls(offline_min_dwell_s=dwell)

    @property
    def phase(self)->str: return self.phase_order[self.current_index]
    @property
    def complete(self)->bool: return self.current_index==len(self.phase_order)-1

    def token(self, *, cycle:int, sim_time_s:float|None, knowledge_version:str|None=None, mission_version:str="rtmo-v2") -> dict[str,Any]:
        # entry_sim_time is established only from authoritative plant time.
        if self.entry_sim_time_s is None and sim_time_s is not None: self.entry_sim_time_s=float(sim_time_s)
        return {
            "phase_id":self.current_index+1,"phase_name":self.phase,"phase_seq":self.current_index+1,
            "entry_time":self.entry_sim_time_s,"cycle":int(cycle),
            "knowledge_version":knowledge_version,"mission_version":mission_version,
        }

    def can_advance(
        self, *, sim_time_s:float|None, next_capability:bool,
        goal_state:dict[str,Any]|None=None,
        current_state_fresh:bool=False,
        current_state_executable_release:bool=False,
        l4_artifact_valid:bool=False,
        l5_final:bool=False,
        runtime_release:bool=False,
        l6_executed:bool=False,
        actuation_ack:bool=False,
        realized_state_evidence:bool=False,
    ) -> tuple[bool,list[str]]:
        """Return whether the *current* phase may advance.

        P0-1 invariant: historical/durable capability cannot advance a controlled
        phase without current-cycle decision/execution evidence.

        P0-2 invariant: ``simulation_time_s`` is authoritative plant time.  Missing
        plant time never bypasses dwell and never promotes a scheduler/model
        ``reference_time_s`` into freshness evidence.
        """
        reasons=[]
        if self.complete: return False,["already_touchdown"]

        # P0-2: no current plant clock => no phase transition of any kind.  Do not
        # use reference_time_s as a physical-clock substitute.
        if sim_time_s is None:
            reasons.append("simulation_time_missing")
        else:
            sim_time_s=float(sim_time_s)
            if self.entry_sim_time_s is None:
                # First valid plant timestamp establishes the phase-entry epoch;
                # dwell therefore begins at zero rather than being skipped.
                self.entry_sim_time_s=sim_time_s
            dwell=float((self.offline_min_dwell_s or {}).get(self.phase,0.0))
            if (sim_time_s-self.entry_sim_time_s) < dwell:
                reasons.append("offline_min_dwell_not_met")

        if not current_state_fresh:
            reasons.append("current_state_not_fresh")
        if not next_capability:
            reasons.append("next_phase_capability_blocked")

        # P0-1: on_ground is a passive boundary, but leaving it still requires a
        # current Online executable qualification; historical capability alone is
        # not transition authority.
        if self.phase=="on_ground":
            if not current_state_executable_release:
                reasons.append("current_state_executable_release_missing")
        else:
            gates=(
                (l4_artifact_valid,"current_l4_artifact_invalid"),
                (l5_final,"current_l5_final_missing"),
                (runtime_release,"current_runtime_release_missing"),
                (l6_executed,"current_l6_not_executed"),
                (actuation_ack,"current_actuation_ack_missing"),
                (realized_state_evidence,"current_realized_state_evidence_missing"),
            )
            for ok,reason in gates:
                if not ok: reasons.append(reason)

        gs=goal_state or {}
        # Arrival-side phases may only advance after the goal supervisor has armed.
        if self.phase in ("approach","transition_back","vertical_landing") and not bool(gs.get("approach_armed")):
            reasons.append("approach_not_armed")
        if self.phase=="vertical_landing" and not bool(gs.get("ground_referenced")):
            reasons.append("ground_reference_not_ready")
        return (not reasons),reasons

    def advance(self, *, sim_time_s:float|None) -> str:
        if not self.complete:
            self.current_index += 1
            # A new phase may only acquire its entry epoch from a real plant time.
            self.entry_sim_time_s = float(sim_time_s) if sim_time_s is not None else None
        return self.phase
