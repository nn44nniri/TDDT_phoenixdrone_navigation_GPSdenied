from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass(frozen=True)
class UnitDef:
    symbol: str
    quantity: str
    dimension: str
    scale: float = 1.0
    offset: float = 0.0
    canonical: str = ""
    affine: bool = False

@dataclass
class ConversionResult:
    value: Any
    unit: str
    quantity_kind: str
    canonical_unit: str
    source_unit: str
    conversion_class: str
    uncertainty: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    audit: Dict[str, Any] = field(default_factory=dict)
