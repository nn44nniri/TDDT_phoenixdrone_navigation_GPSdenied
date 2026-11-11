import warnings
import numpy as np
from pathlib import Path

def test_mask_check_precedes_float_conversion():
    text=(Path(__file__).resolve().parents[1]/"python/terrain_truth.py").read_text()
    block=text[text.index("def _read_nearest"):text.index("def _offset_xy")]
    assert block.index("np.ma.is_masked(cell)") < block.index("value = float(cell)")
