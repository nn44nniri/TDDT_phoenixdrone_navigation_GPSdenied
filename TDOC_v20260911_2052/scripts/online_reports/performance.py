"""Explicit post-flight landing, elapsed-time and energy measurements."""
import math

from .data import GEOD


def performance(timeline, result, qualification, final_wire, summary):
    sim_packet = final_wire.get('SIM_STATE') or {}
    sim = sim_packet.get('data') or {}
    goal = result.get('goal_lonlat')
    final_lonlat = [sim['lon_int']*1e-7, sim['lat_int']*1e-7] if 'lon_int' in sim and 'lat_int' in sim else None
    north = east = None
    if goal and final_lonlat:
        bearing, _, distance = GEOD.inv(*goal, *final_lonlat)
        north, east = distance*math.cos(math.radians(bearing)), distance*math.sin(math.radians(bearing))
    elapsed = summary['flight_duration_s']
    phases = []
    # Attribute each left-closed interval and its trapezoidal energy to its start phase.
    for i, row in enumerate(timeline):
        nxt = timeline[i+1] if i+1 < len(timeline) else row
        if not phases or phases[-1]['phase'] != row['phase']:
            phases.append({'phase': row['phase'], 'start_s': row['flight_elapsed_s'], 'end_s': row['flight_elapsed_s'],
                'duration_s': 0., 'telemetry_energy_covered_wh': 0., 'energy_coverage_s': 0.,
                'Wing_model_energy_wh': 0., 'samples': 0})
        p = phases[-1]
        p['samples'] += 1
        p['end_s'] = nxt['flight_elapsed_s']
        p['duration_s'] += max(0., nxt['flight_elapsed_s'] - row['flight_elapsed_s'])
        p['telemetry_energy_covered_wh'] += nxt['telemetry_energy_integral_covered_wh'] - row['telemetry_energy_integral_covered_wh']
        p['energy_coverage_s'] += nxt['energy_covered_duration_s'] - row['energy_covered_duration_s']
        p['Wing_model_energy_wh'] += nxt['model_power_integral_wh'] - row['model_power_integral_wh']
    for p in phases:
        ratio = p['energy_coverage_s'] / p['duration_s'] if p['duration_s'] else None
        p['energy_coverage_fraction'] = ratio
        p['energy_used_wh'] = p['telemetry_energy_covered_wh'] if ratio is not None and ratio >= .95 else None
    error = qualification.get('actual_goal_error_m_independent')
    coverage = summary['energy_telemetry_coverage_s']
    landing = {'actual_horizontal_error_m': error, 'tolerance_m': summary['goal_tolerance_m'],
        'within_tolerance': error <= summary['goal_tolerance_m'] if error is not None else None,
        'target_lonlat': goal, 'actual_final_lonlat': final_lonlat,
        'target_to_actual_north_m': north, 'target_to_actual_east_m': east,
        'estimated_goal_error_m': result.get('estimated_goal_error_m'),
        'physical_contact_confirmed': qualification['checks'].get('physical_contact_and_stationary'),
        'FCU_on_ground': qualification['checks'].get('FCU_on_ground'),
        'FCU_disarmed': qualification['checks'].get('FCU_disarmed'),
        'position_sample': 'final raw SIM_STATE after landing, not inferred touchdown instant',
        'position_received_monotonic_s': sim_packet.get('received_monotonic'),
        'physical_truth_used_for_navigation': False,
        'GNSS_disabled_verified': qualification['checks'].get('GPS_disabled_readbacks'),
        'approved_navigation_inputs_verified': qualification['checks'].get('approved_navigation_inputs'),
        'vertical_landing_error_m': None,
        'vertical_error_unavailable_reason': 'No independently certified destination touchdown altitude/DEM datum and physical contact-surface equivalence'}
    return {'schema': 'TDOC-online-landing-energy-time-v1', 'native_session_id': result.get('native_session_id'),
        'landing_accuracy': landing,
        'time': {'control_duration_s': elapsed, 'control_duration_min': elapsed / 60.,
            'source_start_s': summary['source_start_s'],
            'source_end_s': timeline[-1]['source_time_s'] if timeline else None,
            'clock': 'HIGHRES_IMU source time (FCU); first to last recorded control cycle',
            'startup_calibration_and_shutdown_included': False,
            'phase_interval_convention': '[current cycle, next cycle); last sample has zero duration'},
        'energy': {'consumed_wh': summary['energy_used_wh'],
            'consumed_j': summary['energy_used_wh']*3600 if summary['energy_used_wh'] is not None else None,
            'covered_interval_wh': summary['energy_telemetry_covered_wh'],
            'coverage_s': coverage, 'coverage_fraction': coverage / elapsed if elapsed else None,
            'minimum_coverage_for_total': .95,
            'average_covered_power_w': summary['energy_telemetry_covered_wh']*3600/coverage if coverage else None,
            'Wing_model_integral_wh': timeline[-1]['model_power_integral_wh'] if timeline else None,
            'source': summary['energy_source'], 'integration': 'trapezoidal V*I on source-time intervals 0 < dt <= 0.5 s',
            'scope': 'recorded control interval, not entire powered session; SITL model, not calibrated hardware',
            'no_data_is_not_zero_consumption': True},
        'phases': phases, 'reporting_only': True}
