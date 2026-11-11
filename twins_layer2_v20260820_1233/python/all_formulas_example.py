from pathlib import Path
from formula_fmi import FormulaFMU
R=Path(__file__).resolve().parents[1]
with FormulaFMU(R/'fmus'/'WingMPET.fmu') as fmu:
    rho=fmu.evaluate_formula('wingmpet_op05_air_density', altitude=1000.0,
        external_temperature=0.0, external_pressure=0.0, external_density=0.0,
        relative_humidity=0.45, airspeed=25.0)
    print('rho=',rho)
    print(fmu.evaluate_operation(9, air_density=rho, airspeed=25, mass=5,
        wing_area=.8, cd0=.03, induced_drag_factor=.06, cl_max=1.4,
        bank_angle=.1, flightpath_angle=0, lift_command=0))
with FormulaFMU(R/'fmus'/'MESA_NAV.fmu') as fmu:
    print(fmu.evaluate_operation(14,residual=2.0,innovation_variance=1.5,
        nis_gate=6.0,student_nu=4.0))
