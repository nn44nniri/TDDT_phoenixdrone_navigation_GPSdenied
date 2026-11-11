import math
from numbers import Real
from .registry import resolve, U
from .models import ConversionResult
from .errors import UnitUnknownError, QuantityMismatchError, DimensionMismatchError, ConversionError

def _map(v, fn):
    def one(x):
        if isinstance(x,bool): raise ConversionError("boolean cannot be converted as a physical measurement",code="OUT_OF_DOMAIN")
        try: x=float(x)
        except (TypeError,ValueError,OverflowError) as exc:
            raise ConversionError("non-numeric measurement",code="OUT_OF_DOMAIN") from exc
        if not math.isfinite(x): raise ConversionError("non-finite measurement",code="OUT_OF_DOMAIN")
        y=fn(x)
        if not math.isfinite(y): raise ConversionError("conversion overflow/non-finite result",code="OUT_OF_DOMAIN")
        return y
    if isinstance(v,(list,tuple)):return type(v)(one(x) for x in v)
    return one(v)

class UnitConverter:
    """Strict quantity-aware converter following TDOC v2 separation rules."""
    def convert(self, value, from_unit, to_unit, *, quantity_kind=None, uncertainty=None, metadata=None):
        src, dst = resolve(from_unit), resolve(to_unit)
        if not src: raise UnitUnknownError(f"Unknown source unit: {from_unit}")
        if not dst: raise UnitUnknownError(f"Unknown destination unit: {to_unit}")
        q = quantity_kind or src.quantity
        if quantity_kind and src.quantity != q:
            raise QuantityMismatchError(f"{from_unit} is registered as {src.quantity}, not {q}")
        if dst.quantity != q:
            raise QuantityMismatchError(f"Cannot convert quantity {q} from {from_unit} to {to_unit} ({dst.quantity})")
        if src.dimension != dst.dimension:
            raise DimensionMismatchError(f"Dimension mismatch: {src.dimension} vs {dst.dimension}")
        def to_canon(x): return (x + src.offset) * src.scale if src.affine else x * src.scale
        def from_canon(x): return x / dst.scale - dst.offset if dst.affine else x / dst.scale
        out = _map(value, lambda x: from_canon(to_canon(x)))
        cclass = "affine" if src.affine or dst.affine else "multiplicative"
        uout = None
        if uncertainty is not None:
            factor = abs(src.scale / dst.scale)
            uout = _map(uncertainty, lambda x: factor*x if x >= 0 else (_ for _ in ()).throw(ConversionError("negative uncertainty",code="UNCERTAINTY_INVALID")))
        return ConversionResult(out, dst.symbol, q, src.canonical, src.symbol, cclass, uout, metadata or {}, {"from":src.symbol,"to":dst.symbol,"quantity_kind":q})

    def canonicalize(self, value, unit, *, quantity_kind=None, uncertainty=None, metadata=None):
        src = resolve(unit)
        if not src: raise UnitUnknownError(f"Unknown unit: {unit}")
        return self.convert(value, unit, src.canonical, quantity_kind=quantity_kind or src.quantity, uncertainty=uncertainty, metadata=metadata)

    def list_units(self, quantity_kind=None):
        return sorted([k for k,v in U.items() if quantity_kind is None or v.quantity == quantity_kind])

def convert(value, from_unit, to_unit, **kwargs):
    return UnitConverter().convert(value, from_unit, to_unit, **kwargs).value
