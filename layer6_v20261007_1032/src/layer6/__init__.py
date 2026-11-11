"""TDDT Layer-6 digital-pilot execution gateway."""
from .client import Layer6Ack, Layer6Client, Layer6ClientError, Layer6RemoteError, Layer6TimeoutError

__version__ = "2026.10.7.0937"
__all__ = [
    "Layer6Ack", "Layer6Client", "Layer6ClientError", "Layer6RemoteError", "Layer6TimeoutError",
]
