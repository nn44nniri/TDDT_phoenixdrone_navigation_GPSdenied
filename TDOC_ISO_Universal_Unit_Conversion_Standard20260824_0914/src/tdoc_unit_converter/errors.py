class ConversionError(ValueError):
    code = "CONVERSION_ERROR"
    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code

class UnitUnknownError(ConversionError): code = "UNIT_UNKNOWN"
class QuantityMismatchError(ConversionError): code = "QUANTITY_MISMATCH"
class DimensionMismatchError(ConversionError): code = "DIMENSION_MISMATCH"
class ReferenceRequiredError(ConversionError): code = "REFERENCE_REQUIRED"
class FrameMismatchError(ConversionError): code = "FRAME_MISMATCH"
class VerticalReferenceError(ConversionError): code = "VERTICAL_REFERENCE_MISMATCH"
class OutOfDomainError(ConversionError): code = "OUT_OF_DOMAIN"

class CRSMismatchError(ConversionError): code = "CRS_MISMATCH"
class DatumMismatchError(ConversionError): code = "DATUM_MISMATCH"
class QuaternionConventionError(ConversionError): code = "QUATERNION_CONVENTION_MISSING"
class TimeReferenceError(ConversionError): code = "TIME_REFERENCE_MISMATCH"
class AirDataCalibrationError(ConversionError): code = "AIRDATA_CALIBRATION_MISSING"
class PressureTypeMismatchError(ConversionError): code = "PRESSURE_TYPE_MISMATCH"
class GridConvergenceError(ConversionError): code = "GRID_CONVERGENCE_MISSING"
class LogReferenceError(ConversionError): code = "LOG_REFERENCE_MISSING"


class EulerSequenceError(ConversionError): code = "EULER_SEQUENCE_MISSING"
class DataSemanticError(ConversionError): code = "DATA_SEMANTIC_MISMATCH"
