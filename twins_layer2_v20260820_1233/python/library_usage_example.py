from pathlib import Path
from twins_fmi import TwinsLayer2

ROOT = Path(__file__).resolve().parents[1]
with TwinsLayer2(
    ROOT / "fmus" / "MESA_NAV.fmu",
    ROOT / "fmus" / "WingMPET.fmu",
    config=ROOT / "config" / "phoenix_drone_v3.yaml",
) as twins:
    mesa_result = twins.mesa.evaluate_operation(5, dt=0.01)
    wing_result = twins.wingmpet.evaluate_operation(5, altitude=1000.0, relative_humidity=0.45,
                                                     airspeed=13.0, external_temperature=0.0,
                                                     external_pressure=0.0, external_density=0.0)
    print("MESA_NAV:", mesa_result)
    print("WingMPET:", wing_result)
