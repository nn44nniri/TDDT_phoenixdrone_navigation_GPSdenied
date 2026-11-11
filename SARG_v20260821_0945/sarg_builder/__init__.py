"""SARG dataset builder."""
__version__ = "2.1.0"

from .selector import PRIMARY_KEY, SARGSelector, SARGSelectionError, SARGIndexError

__all__ = ["PRIMARY_KEY", "SARGSelector", "SARGSelectionError", "SARGIndexError"]
