"""Physically meaningful deterministic defaults for FMI verification scenarios.

These values are *scenario priors*, not measured truth. Aircraft-specific values are
still resolved first from DroneConfig; this module supplies only runtime/environment
quantities that are not configuration parameters.
"""
from __future__ import annotations
import math


def scenario_value(model: str, name: str, index: int = 1) -> float:
    m=model.upper().replace('-','_'); n=name.lower()
    # Generic interface/contract vectors: bounded neutral values.
    if n.startswith('contract_value_'): return 0.7
    if n.startswith('problem_value_'): return 0.64
    if n.startswith('guarantee_'): return 1.0
    if n in {'data_value','solver_value','final_value'}: return 1.0
    if n.startswith('unused_'): return 0.0

    # Time / synchronization
    if n in {'dt','flow_dt'}: return 0.01 if m=='MESA_NAV' else 0.02
    if 'timestamp' in n: return 10.0
    if n=='event_age': return 0.05
    if n=='late_max': return 0.20
    if n=='future_tolerance': return 0.05


    # Phoenix v3 VTOL extension inputs (runtime verification priors, not field truth)
    if n in {'pn','pe','pd'}: return {'pn':0.0,'pe':0.0,'pd':-100.0}[n]
    if n in {'vn','ve','vd'}: return {'vn':13.0,'ve':0.0,'vd':0.0}[n]
    if n in {'qw','qx','qy','qz'}: return 1.0 if n=='qw' else 0.0
    if n in {'p_rate','q_rate','r_rate'}: return 0.0
    if n in {'omega_left','omega_right'}: return 560.0
    if n in {'left_motor_cmd','right_motor_cmd','motor_collective'}: return 0.62
    if n in {'left_motor_norm','right_motor_norm'}: return 0.62
    if n in {'left_elevon_rad','right_elevon_rad','elevon_collective'}: return 0.0
    if n in {'wind_n','prior_wind_n','wind_obs_n'}: return 3.0
    if n in {'wind_e','prior_wind_e','wind_obs_e'}: return 1.0
    if n in {'wind_d','prior_wind_d','wind_obs_d'}: return 0.0
    if n in {'sample_age_s'}: return 0.02
    if n in {'max_age_s'}: return 0.5
    if n in {'variance_n','variance_e','variance_d'}: return 0.25
    if n in {'process_variance_n','process_variance_e','process_variance_d'}: return 0.02
    if n in {'prior_variance_n','prior_variance_e','prior_variance_d'}: return 1.0
    if n in {'bias_n','bias_e','bias_d'}: return 0.0
    if n=='command_min_speed': return 12.0
    if n=='min_lambda': return 0.0
    if n in {'uncertainty_scalar'}: return 0.5
    if n in {'quality_prior','validity_prior','reliability_prior','freshness_score','completeness_score','innovation_score','physical_score','numerical_score','anchor_score','support_score'}: return 0.95
    if n=='novelty_score': return 0.05
    if n=='evidence_count': return 64.0
    if n=='battery_voltage': return 11.1
    if n=='battery_current': return 8.0
    if n=='battery_telemetry_valid': return 0.0
    if n=='total_thrust': return 6.5
    if n=='vx': return 8.0
    if n=='vz': return 0.0
    if n=='pitch': return 0.4
    if n=='omega_sum': return 1120.0

    # Common environment / coordinates
    if n in {'altitude','msl_altitude','state_h','h','target_h','goal_h'}: return 1000.0
    if n in {'terrain_height','terrain_h'}: return 900.0
    if 'agl_altitude' in n: return 100.0
    if n in {'x','y','state_x','state_y','px','py'}: return 0.0
    if n in {'target_x','goal_x'}: return 1000.0
    if n in {'target_y','goal_y'}: return 200.0
    if n=='absolute_pressure' or n=='external_pressure' or n=='pressure': return 90000.0
    if n=='external_temperature' or n=='air_temperature' or n=='temperature': return 288.15
    if n=='external_density' or n=='air_density' or n=='rho': return 1.10
    if n=='relative_humidity': return 0.45

    # Air/ground velocity and wind (ENU for Wing, NED priors where named for MESA)
    if n=='airspeed' or n=='predicted_airspeed': return 13.0
    if n in {'air_velocity_x','vax'}: return 13.0
    if n in {'air_velocity_y','vay'}: return 0.5
    if n in {'air_velocity_h','vah'}: return 0.0
    if n=='ground_velocity_x': return 16.0
    if n=='ground_velocity_y': return 1.5
    if n=='ground_velocity_h': return 0.0
    if n in {'wind_x','wind_map_n'}: return 3.0
    if n in {'wind_y','wind_map_e'}: return 1.0
    if n in {'wind_h','wind_map_d'}: return 0.0
    if n.startswith('wind_time_derivative_') or n.startswith('wind_dt_'): return 0.0
    if n.startswith('wind_jacobian_') or n.startswith('wind_grad_'): return 1.0e-3 if n.endswith(('11','00')) else 0.0
    if n.startswith('wind_material_'): return 0.02
    if n=='wind_residual_variance': return 1.0

    # MESA flow / pitot / magnetic / pressure anchor inputs
    if n=='flow_bx': return -13.0
    if n=='flow_by': return 0.5
    if n=='flow_bz': return 0.0
    if n=='flow_filter_tau': return 10.0
    if n=='pitot_pressure': return 0.5*1.10*13.0*13.0
    if n=='airspeed_variance': return 1.0
    if n=='mag_bx' or n=='map_mag_n': return 20.0e-6
    if n=='mag_by' or n=='map_mag_e': return 2.0e-6
    if n=='mag_bz' or n=='map_mag_d': return 42.0e-6
    if n=='mag_variance': return (2.0e-6)**2
    if n=='log_pressure_prior': return math.log(90000.0)
    if n=='log_scale_height': return math.log(8400.0)
    if n in {'pressure_grad_n','pressure_grad_e'}: return 0.0
    if n=='pressure_variance': return 100.0
    if n=='pressure_time_scale': return 30.0
    if n.startswith('process_noise_diag_'): return 0.01
    if n.endswith('_anchor_residual') or n=='residual': return 0.1
    if n in {'measurement_variance','innovation_variance'}: return 1.0
    if n=='nis_gate': return 6.0
    if n=='student_nu': return 4.0
    if n=='jacobian_n': return 1.0
    if n=='jacobian_e': return 0.5
    if n=='min_information_eigenvalue': return 0.5
    if n=='min_condition_score': return 0.25
    if n=='parameter_index': return 1.0
    if n=='basis_value': return 1.0
    if n=='measurement': return 1.1
    if n=='forgetting_factor': return 0.99
    if n=='background_time_constant': return 10.0
    if n=='descriptor_scale': return 1.0
    if n=='acceptance_threshold': return 0.5
    if n=='store_descriptor': return 1.0
    if n.startswith('descriptor_'): return 0.02*index
    if n=='relative_displacement_n': return 5.0
    if n=='relative_displacement_e': return 2.0
    if n=='info_11': return 2.0
    if n=='info_12': return 0.1
    if n=='info_22': return 1.5
    if n=='candidate_count': return 3.0
    if n.startswith('candidate_') and n.endswith('info11'): return 2.0+0.02*index
    if n.startswith('candidate_') and n.endswith('info12'): return 0.1
    if n.startswith('candidate_') and n.endswith('info22'): return 1.5+0.015*index
    if n=='stall_speed': return 9.1
    if n=='load_factor': return 1.0
    if n=='max_load_factor': return 2.5
    if n=='sideslip': return 0.02
    if n=='max_sideslip': return math.radians(12.0)

    # WingMPET aerodynamic / mechanical
    if n=='mass': return 0.65
    if n=='wing_area' or n=='s': return 0.12
    if n=='b': return 0.64
    if n=='e': return 0.70
    if n=='cd0': return 0.045
    if n=='induced_drag_factor': return 0.134
    if n in {'cl_max','max_cl','clmax'}: return 1.05
    if n=='cl': return 0.6
    if n in {'bank_angle','bank_cmd'}: return 0.10
    if n=='flightpath_angle': return 0.0
    if n=='min_airspeed': return 12.0
    if n=='max_airspeed': return 18.0
    if n=='max_bank': return math.radians(45.0)
    if n=='min_flightpath': return -math.radians(20.0)
    if n=='max_flightpath': return math.radians(20.0)
    if n=='lift_command' or n=='lift_cmd' or n=='lift': return 0.70*9.80665
    if n=='thrust' or n=='thrust_cmd': return 4.0
    if n=='drag': return 1.0

    # Electric propulsion
    if n in {'propeller_diameter','prop_d'}: return 0.2032
    if n in {'rotation_hz','prop_n'}: return 100.0
    if n=='ct0': return 0.11
    if n=='ct1': return -0.08
    if n=='ct2': return -0.02
    if n=='cp0': return 0.05
    if n=='cp1': return -0.03
    if n=='cp2': return 0.01
    if n in {'motor_efficiency','motor_eff'}: return 0.80
    if n in {'inverter_efficiency','inverter_eff'}: return 0.95
    if n in {'propeller_efficiency','prop_eff'}: return 0.75
    if n in {'battery_ocv','battery_voc'}: return 11.1
    if n in {'battery_resistance','battery_r'}: return 0.08
    if n=='inverter_resistance': return 0.01
    if n in {'battery_capacity_ah','battery_q_ah'}: return 2.2
    if n=='soc': return 0.90
    if n=='auxiliary_power': return 8.0
    if n=='switch_frequency': return 20000.0
    if n=='switch_time': return 1e-7
    if n=='motor_resistance': return 0.05
    if n=='motor_torque_constant': return 0.08
    if n=='motor_thermal_capacity': return 500.0
    if n=='cooling_coefficient': return 5.0
    if n=='motor_temperature': return 300.0
    if n=='peukert_exponent': return 1.05

    # Combustion branch: verification-only representative values.
    if n in {'sea_level_power','engine_power_sl'}: return 1000.0
    if n in {'density_ratio','engine_alt_factor'}: return 0.90
    if n=='transmission_efficiency': return 0.90
    if n=='bsfc': return 2.5e-7
    if n=='fuel_mass': return 0.10
    if n=='dry_mass': return 0.65
    if n=='throttle': return 0.65
    if n=='power_lapse_exponent': return 1.0
    if n=='mode_electric': return 1.0

    # Safety / aggregation / optimization
    if n=='clearance' or n=='h_clear': return 50.0
    if 'obstacle_distance' in n: return 100.0
    if n in {'base_safe_radius','r_safe'}: return 20.0
    if n in {'position_sigma','wind_sigma'}: return 2.0
    if n=='inflation_factor': return 3.0
    if n=='groundspeed': return 16.0
    if n=='power': return 120.0
    if n=='fuel_rate': return 0.0
    if n=='path_length_prev': return 1000.0
    if n=='time_prev': return 60.0
    if n=='energy_prev': return 20000.0
    if n=='fuel_prev': return 0.0
    if n=='initial_goal_distance': return math.hypot(1000.0,200.0)
    if n=='goal_tolerance': return 10.0
    if n=='path_length': return 1000.0
    if n=='mission_time': return 75.0
    if n=='energy': return 20000.0
    if n=='safety_penalty': return 0.0
    if n.endswith('_reference'): return {'length_reference':1000.0,'time_reference':75.0,'energy_reference':20000.0,'safety_reference':1.0}.get(n,1.0)
    if n.startswith('weight_'): return 0.25
    if n=='minimum_length': return 950.0
    if n=='epsilon_length': return 0.10
    if n.startswith('control_') and n.endswith('_min'): return -1.0
    if n.startswith('control_') and n.endswith('_max'): return 1.0
    if n.startswith('control_'): return 0.0
    if n.startswith('constraint_residual_'): return 0.0
    if n=='sample_count': return 10.0
    if n.startswith('segment_margin_'): return 30.0
    if n=='replan_margin_threshold': return 10.0
    if n in {'wind_model_error','state_error'}: return 1.0
    if n in {'wind_error_threshold','state_error_threshold'}: return 5.0
    if n.startswith('guarantee_margin_'): return 1.0

    # Neutral fallback for true dimensionless placeholders only.
    return 0.1
