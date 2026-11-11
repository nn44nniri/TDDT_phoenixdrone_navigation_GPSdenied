from pathlib import Path
from native_fmu import NativeFMU
from drone_config import DroneConfig, DEFAULT_CONFIG
BASE=Path(__file__).resolve().parents[1]
cfg=DroneConfig(DEFAULT_CONFIG)
mesa=NativeFMU(BASE/'fmus'/'MESA_NAV.fmu'); wing=NativeFMU(BASE/'fmus'/'WingMPET.fmu')
try:
    mass=cfg.get('mass.empty_or_flight_mass_kg'); area=cfg.get('geometry.wing_area_m2')
    mesa.set(dt=cfg.get('navigation.imu_dt_s'), imu_ax=0.0, imu_ay=0.0, imu_az=-cfg.get('model.gravity_m_s2'),
             pitot_q=120.0, pressure_pa=90000, pressure_map_ln=11.4076, dem_h=1500)
    wing.set(dt=cfg.get('navigation.imu_dt_s'), mass=mass,S=area,b=cfg.get('geometry.wing_span_m'),
             e=cfg.get('aerodynamics.oswald_efficiency'),CD0=cfg.get('aerodynamics.cd0'),
             CLmax=cfg.get('aerodynamics.cl_max'),vax=cfg.get('speed.cruise_speed_m_s'),
             thrust_cmd=cfg.get('propulsion.maximum_total_thrust_N'),lift_cmd=mass*cfg.get('model.gravity_m_s2'),
             h=1700,terrain_h=1500,h_clear=100,obstacle_distance=500,r_safe=30,mode_electric=1,
             battery_Voc=cfg.get('battery.nominal_voltage_V'),battery_R=cfg.get('battery.internal_resistance_ohm_estimated'),
             battery_Q_Ah=cfg.get('battery.capacity_Ah'),soc=cfg.get('battery.initial_soc'),
             motor_eff=cfg.get('propulsion.motor_efficiency'),inverter_eff=cfg.get('propulsion.inverter_efficiency'),
             prop_eff=cfg.get('propulsion.propeller_efficiency'))
    for slow in range(5):
        for fast in range(100):
            mesa.step(cfg.get('navigation.imu_dt_s'))
            nav=mesa.get('rN','rE','rD','vN','vE','vD','wN','wE','wD')
            wing.set(px=nav['rE'],py=nav['rN'],h=1700-nav['rD'],wind_x=nav['wE'],wind_y=nav['wN'],wind_h=-nav['wD'])
            wing.step(cfg.get('navigation.imu_dt_s'))
        print(slow, mesa.get('rN','rE','agl','obs_score'), wing.get('path_length','energy_J','soc','terrain_margin','objective'))
finally:
    mesa.close(); wing.close()
