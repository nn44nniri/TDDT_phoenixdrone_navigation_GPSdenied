#!/usr/bin/env python3
"""Static safety regression for production TEP builder entry points."""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "python" / "build_tddt_layer2_tep_v2.py"
GEN = ROOT / "python" / "build_tddt_layer2_tep.py"
V3 = ROOT / "python" / "build_tddt_layer2_tep_v3.py"

v2 = V2.read_text(encoding="utf-8")
gen = GEN.read_text(encoding="utf-8")
v3 = V3.read_text(encoding="utf-8")

# All files must parse.
for p in (V2, GEN, V3):
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))

# Historical v2 must not retain the reduced schema or a production OUT writer.
assert "UOUT=" not in v2.replace(" ", "")
assert "resources'/'tddt_layer2'/'tep" not in v2
assert "build_tddt_layer2_tep_v3" in v2

# Generic wrapper must also bypass v2.
assert "build_tddt_layer2_tep_v3" in gen
assert "build_tddt_layer2_tep_v2" not in gen

# Current production builder must retain the required expanded outputs.
required = {
    "temperature_local", "relative_humidity_local", "wind_speed_terrain",
    "wind_direction_terrain_deg", "surface_pressure_pa", "shortwave_radiation",
    "wind_u_3dw", "wind_v_3dw", "diagnostic_vertical_wind", "wind_speed_3d",
    "air_density",
}
for name in required:
    assert repr(name) in v3 or f'"{name}"' in v3

print("PASS: legacy/generic entry points cannot downgrade the production TEP schema")
