from pathlib import Path
import math
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from twins_fmi import DroneConfig, FormulaFMU, NativeFMU, TwinsLayer2

mesa_path = ROOT / "fmus" / "MESA_NAV.fmu"
wing_path = ROOT / "fmus" / "WingMPET.fmu"
config_path = ROOT / "config" / "phoenix_drone_v3.yaml"

cfg = DroneConfig(config_path)
assert cfg.resolve("mass") > 0
assert cfg.resolve("wing_area") > 0

# Low-level independent lifecycle and batch real access.
with NativeFMU(mesa_path, instance_name="mesa_low") as mesa:
    mesa.set_values({"dt": 0.01, "imu_gx": 0.01, "imu_gy": 0.0, "imu_gz": 0.0,
                     "imu_ax": 0.0, "imu_ay": 0.0, "imu_az": -9.80665})
    mesa.step(0.01)
    q = mesa.get_values(["qw", "qx", "qy", "qz"])
    assert all(math.isfinite(v) for v in q.values())
    mesa.reset()
    assert mesa.t == 0.0

# Formula catalog and configuration-aware operation.
with FormulaFMU(wing_path, config=cfg, instance_name="wing_formula") as wing:
    assert len(wing.list_operations()) == len(wing.catalog['operations']) == 22
    result = wing.evaluate_operation(5, altitude=1000.0, relative_humidity=0.45,
                                     external_temperature=0.0, external_pressure=0.0,
                                     external_density=0.0)
    assert result and all(math.isfinite(v) for v in result.values())

# Two FMUs alive simultaneously, with independent time and state.
with TwinsLayer2(mesa_path, wing_path, config=cfg) as twins:
    assert twins.mesa.component != twins.wingmpet.component
    mesa_out = twins.mesa.evaluate_operation(5, dt=0.01)
    wing_out = twins.wingmpet.evaluate_operation(5, altitude=500.0, relative_humidity=0.3,
                                                 external_temperature=0.0, external_pressure=0.0,
                                                 external_density=0.0)
    assert mesa_out and wing_out
    assert twins.mesa.t > 0 and twins.wingmpet.t > 0
    twins.mesa.reset()
    assert twins.mesa.t == 0.0 and twins.wingmpet.t > 0.0

print("ALL_LIBRARY_INTERFACE_TESTS_PASSED")
