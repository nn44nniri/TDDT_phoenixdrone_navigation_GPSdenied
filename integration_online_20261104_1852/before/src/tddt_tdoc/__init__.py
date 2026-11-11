"""TDDT Trans-Domain Orchestration Core (TDOC)."""
from .config import TDOCConfig
from .models import CycleResult, InvocationEvent, LayerRegistration, Readiness
from .orchestrator import TDOCOrchestrator
from .unit_adapter import UnitAdapterBinding

__all__ = ["TDOCConfig", "TDOCOrchestrator", "UnitAdapterBinding", "CycleResult", "InvocationEvent", "LayerRegistration", "Readiness"]
__version__ = "2026.9.11.2052"
