"""Normalize real Native records without granting any navigation authority."""
from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from pyproj import Geod
from scipy.spatial.transform import Rotation

GEOD = Geod(ellps='WGS84')


def rows_from(path):
    path = Path(path)
    if not path.exists():
        return
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    with (gzip.open(path, 'rt') if path.suffix == '.gz' else path.open()) as stream:
        return json.load(stream)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def norm(v):
    return math.sqrt(sum(float(x) ** 2 for x in v))


def physical(row):
    packet = (row.get('external_validation_only') or {}).get('SIM_STATE') or {}
    s = packet.get('data') or {}
    # Require the precise MAVLink2 extensions, including legitimate zero lat/lon.
    if not s or 'lat_int' not in s or 'lon_int' not in s:
        return None
    lat, lon = s['lat_int'] * 1e-7, s['lon_int'] * 1e-7
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return {'latitude_deg': lat, 'longitude_deg': lon, 'altitude_msl_m': s['alt'],
            'velocity_ned_m_s': [s[k] for k in ('vn', 've', 'vd')],
            'q_wxyz': [s[k] for k in ('q1', 'q2', 'q3', 'q4')],
            'received_monotonic': packet.get('received_monotonic'),
            'source': 'SIM_STATE validation only; never fed into control'}


def local_xy(origin, lon, lat):
    bearing, _, distance = GEOD.inv(origin['longitude_deg'], origin['latitude_deg'], lon, lat)
    return distance * math.cos(math.radians(bearing)), distance * math.sin(math.radians(bearing))


def lonlat(origin, n, e):
    lon, lat, _ = GEOD.fwd(origin['longitude_deg'], origin['latitude_deg'],
                          math.degrees(math.atan2(e, n)), math.hypot(n, e))
    return lon, lat


def vtol_angles(q):
    r = Rotation.from_quat([q[1], q[2], q[3], q[0]]) * Rotation.from_euler('y', -90, degrees=True)
    return r.as_euler('xyz', degrees=True).tolist()


def telemetry_alignment(rows, path):
    wanted = {r['L2']['navigation']['source_wire_sha256'] for r in rows}
    samples, latest = {}, {}
    counts = Counter()
    final = {}
    keep = {'HIGHRES_IMU', 'VFR_HUD', 'SERVO_OUTPUT_RAW', 'BATTERY_STATUS', 'SYS_STATUS',
            'HEARTBEAT', 'EXTENDED_SYS_STATE', 'SCALED_PRESSURE'}
    raw_verified = True
    for packet in rows_from(path):
        name = packet['type']
        counts[name] += 1
        if packet.get('wire_hex'):
            raw_verified &= hashlib.sha256(bytes.fromhex(packet['wire_hex'])).hexdigest() == packet.get('wire_sha256')
        if name in keep:
            latest[name] = packet
        if name in ('HEARTBEAT', 'SIM_STATE', 'EXTENDED_SYS_STATE'):
            final[name] = packet
        if name == 'HIGHRES_IMU' and packet.get('wire_sha256') in wanted:
            # An as-of acquisition view; never select future telemetry for a cycle.
            samples[packet['wire_sha256']] = dict(latest)
    return samples, counts, final, raw_verified


def _battery(aux):
    b = (aux.get('BATTERY_STATUS') or {}).get('data') or {}
    volts = [float(v) / 1000. for v in b.get('voltages', []) if 0 < v < 65535]
    voltage = sum(volts) if volts else None
    current = b.get('current_battery')
    current = float(current) / 100. if current is not None and current >= 0 else None
    remaining = b.get('battery_remaining')
    remaining = remaining if remaining is not None and 0 <= remaining <= 100 else None
    return voltage, current, remaining


def normalize(rows, result, samples):
    timeline, candidates, details, pulses = [], [], [], []
    if not rows:
        return timeline, candidates, details, pulses
    initial_t = rows[0]['L2']['navigation']['time_s']
    origin = rows[0]['L3']['origin']
    goal = result.get('goal_lonlat')
    initial_physical = next((physical(r) for r in rows if physical(r) is not None), None)
    altitude0 = initial_physical['altitude_msl_m'] if initial_physical else None
    phase_ids = {}
    prior_model_energy = 0.
    measured_energy = 0.
    covered_energy_s = 0.
    previous = None
    for i, r in enumerate(rows, 1):
        n = r['L2']['navigation']; w = r['L2']['wing']; c = r['L2']['climate']
        t = n['time_s']; p = n['position_ned_m']; v = n['velocity_ned_m_s']
        aux = samples.get(n['source_wire_sha256'], {})
        h = aux.get('HIGHRES_IMU') or {}; imu = h.get('data') or {}
        servo = (aux.get('SERVO_OUTPUT_RAW') or {}).get('data') or {}
        bvoltage, bcurrent, bsoc = _battery(aux)
        power = bvoltage * bcurrent if bvoltage is not None and bcurrent is not None else None
        phy = physical(r)
        angles = vtol_angles(n['q_wxyz'])
        actual_angles = vtol_angles(phy['q_wxyz']) if phy else [None] * 3
        navlon, navlat = lonlat(origin, p[0], p[1])
        actual_n, actual_e = local_xy(origin, phy['longitude_deg'], phy['latitude_deg']) if phy else (None, None)
        ng = GEOD.inv(navlon, navlat, *goal)[2] if goal else None
        ag = GEOD.inv(phy['longitude_deg'], phy['latitude_deg'], *goal)[2] if phy and goal else None
        phase_ids.setdefault(r['phase'], len(phase_ids))
        selected = r['L5']['prediction']
        pred_next = selected['prediction_tube'][1]['state'] if len(selected['prediction_tube']) > 1 else None
        wind = c['wind_ned_m_s']
        modelpower = w['physical_outputs'].get('predicted_power')
        dt = t - previous['source_time_s'] if previous else 0.
        if previous and 0 < dt <= .5:
            if modelpower is not None and previous.get('predicted_power_w') is not None:
                prior_model_energy += .5 * (modelpower + previous['predicted_power_w']) * dt / 3600.
            if power is not None and previous.get('telemetry_power_w') is not None:
                measured_energy += .5 * (power + previous['telemetry_power_w']) * dt / 3600.
                covered_energy_s += dt
        lf = r['L7']; rec = r['L6'].get('execution_receipt') or {}
        source_age = rec.get('recorded_monotonic', h.get('received_monotonic', 0)) - h.get('received_monotonic', 0) if h else None
        row = {
            'cycle': i, 'transaction_id': r['transaction_id'], 'source_time_s': t,
            'flight_elapsed_s': t - initial_t, 'phase': r['phase'], 'phase_id': phase_ids[r['phase']],
            'state_source': 'MESA approved sensors', 'physical_state_source': 'SIM_STATE validation only',
            'navigation_north_m': p[0], 'navigation_east_m': p[1], 'navigation_down_m': p[2],
            'navigation_height_above_launch_m': -p[2], 'navigation_lon_deg': navlon, 'navigation_lat_deg': navlat,
            'navigation_vn_m_s': v[0], 'navigation_ve_m_s': v[1], 'navigation_vd_m_s': v[2],
            'navigation_speed_m_s': norm(v), 'estimated_goal_distance_m': ng,
            'physical_north_m': actual_n, 'physical_east_m': actual_e,
            'physical_altitude_msl_m': phy['altitude_msl_m'] if phy else None,
            'physical_height_above_launch_CG_m': phy['altitude_msl_m'] - altitude0 if phy else None,
            'physical_lon_deg': phy['longitude_deg'] if phy else None,
            'physical_lat_deg': phy['latitude_deg'] if phy else None,
            'physical_speed_m_s': norm(phy['velocity_ned_m_s']) if phy else None,
            'actual_goal_distance_m': ag,
            'navigation_position_error_horizontal_m': math.hypot(p[0] - actual_n, p[1] - actual_e) if phy else None,
            'navigation_velocity_error_m_s': norm([v[j] - phy['velocity_ned_m_s'][j] for j in range(3)]) if phy else None,
            'navigation_sigma_n_m': math.sqrt(max(0., n['covariance'][0][0])),
            'navigation_sigma_e_m': math.sqrt(max(0., n['covariance'][1][1])),
            'navigation_vtol_roll_deg': angles[0], 'navigation_vtol_pitch_deg': angles[1],
            'navigation_vtol_yaw_deg': angles[2], 'physical_vtol_roll_deg': actual_angles[0],
            'physical_vtol_pitch_deg': actual_angles[1], 'physical_vtol_yaw_deg': actual_angles[2],
            'airspeed_m_s': n.get('airspeed_m_s'),
            'l4_candidate_count': len(r['L4']['candidates']),
            'l4_feasible_count': sum(q['feasible'] for q in r['L4']['candidates']),
            'l4_verified_count': None, 'l4_best_candidate_id': r['L5']['candidate_id'],
            'l4_scalar_cost': selected['cost'], 'l4_ff_verification_available': False,
            'l5_mode': r['L5']['mode'], 'l5_final_gate': r['L5']['gates']['FINAL'],
            'l5_pilot_cmd_0': r['L5']['u_cmd'][0], 'l5_pilot_cmd_1': r['L5']['u_cmd'][1],
            'l5_pilot_cmd_2': r['L5']['u_cmd'][2], 'l5_pilot_cmd_3': r['L5']['u_cmd'][3],
            'l6_intake_ok': r['L6']['intake'].get('ok'), 'l6_command_instance_id': r['L6']['intake'].get('command_instance_id'),
            'l6_receipt_available': bool(rec), 'l6_mode_observed': rec.get('mode'), 'l6_armed_observed': rec.get('armed'),
            'l7_feedback_accepted': lf.get('ok'), 'l7_reason': lf.get('reason', 'ACCEPTED'),
            'l7_position_residual_m': norm(lf['state_residual'][:3]) if lf.get('ok') else None,
            'l7_velocity_residual_m_s': norm(lf['state_residual'][3:]) if lf.get('ok') else None,
            'feedback_consumed': n.get('feedback_consumed'),
            'feedback_version': w['reduced_inner_loop']['feedback_version'],
            'disturbance_n_m_s2': w['reduced_inner_loop']['disturbance_ned_m_s2'][0],
            'disturbance_e_m_s2': w['reduced_inner_loop']['disturbance_ned_m_s2'][1],
            'disturbance_d_m_s2': w['reduced_inner_loop']['disturbance_ned_m_s2'][2],
            'wind_n_m_s': wind[0], 'wind_e_m_s': wind[1], 'wind_d_m_s': wind[2], 'wind_speed_m_s': norm(wind),
            'air_density_kg_m3': c['air_density_kg_m3'], 'climate_model_age_s': t - c['time_s'],
            'climate_forcing_sha256': c['forcing_sha256'],
            'temperature_c': None, 'humidity_pct': None, 'pressure_pa': None,
            'wind_counterfactual_displacement_m': None,
            'FMU_vtol_feasible': w['physical_outputs'].get('vtol_feasible'),
            'FMU_quality_score': w['physical_outputs'].get('quality_score'),
            'predicted_power_w': modelpower, 'model_power_integral_wh': prior_model_energy,
            'battery_voltage_v': bvoltage, 'battery_current_a': bcurrent, 'battery_remaining_pct': bsoc,
            'telemetry_power_w': power, 'telemetry_energy_integral_covered_wh': measured_energy,
            'energy_covered_duration_s': covered_energy_s,
            'wing_SOC_input': 1., 'energy_cost_in_MPC': None,
            'snapshot_hash': r['L3']['snapshot_hash'], 'panels_healthy': r['health']['ok'],
            'physical_map_ready': r['health']['physical_map']['ready'],
            'cycle_wall_s': r['cycle_wall_s'], 'source_interval_s': dt if previous else None,
            'source_to_receipt_wall_s': source_age,
            'terrain_clearance_m': None, 'terrain_clearance_used_in_control': False,
            'sarg_used_as_next_state': False, 'teacher_forced': False,
        }
        for j, name in enumerate(('roll_rate_rad_s', 'pitch_rate_rad_s', 'yaw_rate_rad_s')):
            row[name] = n['body_rates_rad_s'][j]
        for j in (1, 2, 3, 4):
            row['servo%d_pwm_us' % j] = servo.get('servo%d_raw' % j)
        row['left_motor_command_norm'] = max(0., min(1., (servo['servo3_raw'] - 1000.) / 1000.)) if 'servo3_raw' in servo else None
        row['right_motor_command_norm'] = max(0., min(1., (servo['servo4_raw'] - 1000.) / 1000.)) if 'servo4_raw' in servo else None
        for field, value in imu.items():
            if field != 'mavpackettype':
                row['sensor_' + field] = value
        for cand in r['L4']['candidates']:
            end = cand['prediction_tube'][-1]['state']
            candidates.append({'cycle': i, 'source_time_s': t, 'flight_elapsed_s': t - initial_t,
                'candidate_id': cand['candidate_id'], 'selected': cand['candidate_id'] == r['L5']['candidate_id'],
                'feasible': cand['feasible'], 'verified': None, 'pareto_rank': None, 'scalar_cost': cand['cost'],
                'acceleration_n_m_s2': cand['acceleration_ne_m_s2'][0],
                'acceleration_e_m_s2': cand['acceleration_ne_m_s2'][1], 'climb_rate_up_m_s': cand['climb_rate_up_m_s'],
                'endpoint_n_m': end[0], 'endpoint_e_m': end[1], 'endpoint_down_m': end[2],
                'model': cand['model'], 'source_snapshot_hash': cand['source_snapshot_hash'],
                'prediction_start_s': cand['prediction_tube'][0]['time_s'],
                'prediction_end_s': cand['prediction_tube'][-1]['time_s'],
                'FMU_operation': cand['physical_FMU_operation']})
        receipt_record = ((rec.get('correlation') or {}).get('record') or {})
        detail = {'cycle': i, 'transaction_id': r['transaction_id'],
            'time_location': {k: row[k] for k in row if any(s in k for s in ('time', 'elapsed', 'latitude', 'longitude', 'north', 'east', 'height', 'distance'))},
            'mission': {'phase': r['phase'], 'reference': r['L3']['reference'], 'goal_lonlat': goal},
            'uav_navigation': n, 'flight_state': phy,
            'sensors': {'IMU': imu, 'airspeed_m_s': n.get('airspeed_m_s'),
                'ULTRASONIC_WIND': 'MISSING', 'IMU_wire_sha256': n['source_wire_sha256']},
            'actuators': {'requested_pilot_command': r['L5']['u_cmd'], 'mode': r['L5']['mode'], 'actual_servo_PWM': servo,
                'battery': (aux.get('BATTERY_STATUS') or {}).get('data')},
            'climate': c, 'layer3_return': {k: v for k, v in r['L3'].items() if k != 'twins'},
            'layer4_return': {'candidate_summaries': candidates[-len(r['L4']['candidates']):],
                'selected_original_prediction_tube': selected['prediction_tube']},
            'layer5_decision': {k: v for k, v in r['L5'].items() if k != 'prediction'},
            'layer6_execution': {'intake': r['L6']['intake'], 'mode': rec.get('mode'), 'armed': rec.get('armed'),
                'correlation_available': (rec.get('correlation') or {}).get('available'),
                'plant_time_bounds': receipt_record.get('plant_time_bounds'),
                'record_hash': (rec.get('correlation') or {}).get('record_hash')},
            'layer7_feedback': lf, 'tdoc_layer_status': r['health']}
        details.append(detail)
        for layer, status, kind in ((2, 'OK', 'current_sensor_state'), (3, 'OK', 'atomic_snapshot'),
                                  (4, 'OK' if row['l4_feasible_count'] else 'NO_CANDIDATE', 'prediction'),
                                  (5, 'OK' if row['l5_final_gate'] else 'REJECTED', 'limited_MPC_gate'),
                                  (6, 'OK' if row['l6_intake_ok'] else 'REJECTED', 'intake_only'),
                                  (7, 'ACCEPTED' if lf.get('ok') else 'PENDING_OR_REJECTED', row['l7_reason'])):
            pulses.append({'cycle': i, 'flight_elapsed_s': t - initial_t, 'layer': layer,
                           'transaction_id': r['transaction_id'], 'status': status, 'semantics': kind})
        timeline.append(row)
        previous = row
    return timeline, candidates, details, pulses
