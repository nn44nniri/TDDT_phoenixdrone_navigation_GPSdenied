#pragma once
// Centralized compile-time Phoenix v3 parameters derived from config/phoenix_drone_v3.yaml.
// Runtime FMUs remain stand-alone native binaries; the YAML is also packaged as provenance.
namespace phoenix_v3 {
constexpr double gravity = 9.80665;
constexpr double mass_kg = 0.65;
constexpr double wing_area_m2 = 0.12;
constexpr double wing_span_m = 0.64;
constexpr double mean_chord_m = 0.19;
constexpr double Ixx = 0.0147563;
constexpr double Iyy = 0.00638929;
constexpr double Izz = 0.0177;
constexpr double cl_max = 1.05;
constexpr double cl_min = -1.05;
constexpr double cd0 = 0.045;
constexpr double induced_drag_factor = 0.134;
constexpr double stall_speed_m_s = 9.1;
constexpr double min_command_speed_m_s = 12.0;
constexpr double cruise_speed_m_s = 13.0;
constexpr double max_speed_m_s = 18.0;
constexpr double max_bank_rad = 0.7853981633974483;
constexpr double max_flightpath_rad = 0.3490658503988659;
constexpr double max_sideslip_rad = 0.20943951023931953;
constexpr double max_roll_rate_rad_s = 5.235987755982989;
constexpr double max_pitch_rate_rad_s = 3.490658503988659;
constexpr double max_yaw_rate_rad_s = 2.6179938779914944;
constexpr double max_load_factor = 2.5;
constexpr double navigation_nis_gate = 6.0;
constexpr double elevon_limit_rad = 0.53;
constexpr double motor_omega_max_rad_s = 800.0;
constexpr double motor_tau_up_s = 0.018;
constexpr double motor_tau_down_s = 0.018;
constexpr double motor_kT = 7.864e-06;
constexpr double motor_kQ_m = 0.023;
constexpr double max_thrust_each_N = 5.03296;
constexpr double max_total_thrust_N = 10.06592;
constexpr double prop_diameter_m = 0.2032;
constexpr double prop_efficiency = 0.75;
constexpr double motor_efficiency = 0.8;
constexpr double inverter_efficiency = 0.95;
constexpr double hover_power_W = 80.0;
constexpr double continuous_power_W = 240.0;
constexpr double peak_power_W = 350.0;
constexpr double battery_nominal_voltage_V = 11.1;
constexpr double battery_capacity_Ah = 2.2;
constexpr double battery_usable_energy_J = 20.5 * 3600.0;
constexpr double battery_internal_resistance_ohm = 0.08;
constexpr double battery_max_current_A = 44.0;
constexpr double motor_max_current_A_each = 18.0;
constexpr double esc_current_limit_A_each = 20.0;
constexpr double propwash_k_lift = 3.48e-06;
constexpr double propwash_k_drag = 1.75e-06;
constexpr double propwash_k_pitch = -3.44e-07;
// The following propwash CP coordinates are in the official Phoenix SDF source frame.
constexpr double propwash_left_cp_x = 0.0;
constexpr double propwash_left_cp_y = 0.195;
constexpr double propwash_left_cp_z = 0.036;
constexpr double propwash_right_cp_x = 0.0;
constexpr double propwash_right_cp_y = -0.195;
constexpr double propwash_right_cp_z = 0.036;
// Low-confidence forward-flight surrogates from phoenix_drone_v3.yaml.
constexpr double CL0 = 0.1;
constexpr double CL_alpha = 3.4;
constexpr double CL_q = 2.5;
constexpr double CL_delta = 0.45;
constexpr double Cm0 = 0.02;
constexpr double Cm_alpha = -0.55;
constexpr double Cm_q = -5.0;
constexpr double Cm_delta = -0.8;
constexpr double CY_beta = -0.35;
constexpr double Cl_beta = -0.08;
constexpr double Cl_p = -0.45;
constexpr double Cl_delta = 0.18;
constexpr double Cn_beta = 0.08;
constexpr double Cn_r = -0.18;
}
