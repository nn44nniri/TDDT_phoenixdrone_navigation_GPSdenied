from pathlib import Path
from unittest.mock import patch

from sarg_builder.fmu import FormulaFMU


def test_optional_symbol_error_is_recognized():
    text = "lib.so: undefined symbol: fmi2DeSerializeFMUstate"
    optional = (
        "fmi2GetFMUstate", "fmi2SetFMUstate", "fmi2FreeFMUstate",
        "fmi2SerializedFMUstateSize", "fmi2SerializeFMUstate",
        "fmi2DeSerializeFMUstate",
    )
    assert [name for name in optional if name in text] == ["fmi2DeSerializeFMUstate"]


def test_mandatory_symbol_is_not_classified_optional():
    text = "lib.so: undefined symbol: fmi2DoStep"
    optional = (
        "fmi2GetFMUstate", "fmi2SetFMUstate", "fmi2FreeFMUstate",
        "fmi2SerializedFMUstateSize", "fmi2SerializeFMUstate",
        "fmi2DeSerializeFMUstate",
    )
    assert not [name for name in optional if name in text]
