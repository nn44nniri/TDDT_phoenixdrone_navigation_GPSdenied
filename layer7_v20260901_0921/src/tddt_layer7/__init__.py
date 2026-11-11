"""TDDT Layer 7: Edge Gaussian/Bayesian Runtime Adaptation & Commit.

The package is intentionally a TDOC-managed side module.  It never owns
actuator execution and never owns the lifecycle of Layers 2--6.
"""
from .config import Layer7Config, load_config
from .models import (
    EvidenceType, AdaptationMode, ExecutionEvidence, PredictionEvidence,
    NetworkSnapshot, DecisionTrace, AdaptationHints, OfflineErrorPacket, TDOCInput,
    Layer7Output, CandidateUpdate, RouteDecision,
)
from .pipeline import Layer7Pipeline
from .tdoc_adapter import Layer7SideModule, TDOCModuleManifest

__all__ = [
    "Layer7Config", "load_config", "EvidenceType", "AdaptationMode",
    "ExecutionEvidence", "PredictionEvidence", "NetworkSnapshot",
    "DecisionTrace", "AdaptationHints", "OfflineErrorPacket", "TDOCInput", "Layer7Output",
    "CandidateUpdate", "RouteDecision", "Layer7Pipeline",
    "Layer7SideModule", "TDOCModuleManifest",
]
__version__ = "0.3.0"
