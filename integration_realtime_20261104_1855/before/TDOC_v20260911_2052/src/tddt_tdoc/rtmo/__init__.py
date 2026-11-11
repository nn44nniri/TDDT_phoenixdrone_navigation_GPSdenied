"""Real-Time Mission Orchestration (RTMO) complement for TDOC.

RTMO is orchestration-only: it does not replace Layer2..Layer7, PT_LIB,
ArduPilot, or JSBSim and never creates synthetic Layer4/Layer5 capability.
"""
from .phase_manager import PHASE_ORDER, MissionPhaseManager
from .capability_gate import CapabilityManifest, build_capability_manifest
from .capability_registry import LiveCapabilityRegistry, load_live_capability_registry
from .execution_gate import execution_gate_trace
from .goal_supervisor import GoalSupervisor
from .run_context import RunContext
from .realtime_scheduler import RTClock
from .artifact_gate import RTMOL4ArtifactGate

__all__ = [
    "PHASE_ORDER", "MissionPhaseManager", "CapabilityManifest",
    "build_capability_manifest", "LiveCapabilityRegistry", "load_live_capability_registry", "execution_gate_trace", "GoalSupervisor",
    "RunContext", "RTClock", "RTMOL4ArtifactGate",
]
