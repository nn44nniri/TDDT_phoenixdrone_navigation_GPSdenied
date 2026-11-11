"""Post-flight physical and cross-layer qualification; no command authority."""
import math
from pathlib import Path

from .data import GEOD, digest, norm, physical, vtol_angles
from .provenance import file_hash
from .data import load_json


def qualify(rows, result, samples, final_wire, raw_verified, evidence, preservation, expected_goal=None):
    checks = {
        'driver_completed_without_error': result.get('status') == 'PASS' and not result.get('error'),
        'records_available': bool(rows), 'raw_wire_hashes': raw_verified,
        'preserved_model_prior_forcing_offline': preservation.get('status') == 'PASS',
    }
    sid = result.get('native_session_id')
    known = {}; consumed = set(); previous_route = None; previous_version = 0
    atomic = correlated = approved = climate = healthy = route_valid = True
    receipts_valid = True
    by_tx = {}
    for r in rows:
        n = r['L2']['navigation']; w = r['L2']['wing']; c = r['L2']['climate']; s = r['L3']
        source = samples.get(n['source_wire_sha256'], {}).get('HIGHRES_IMU')
        approved &= all(n.get(k) is False for k in ('truth_used', 'GNSS_used', 'odometry_used', 'vision_used'))
        approved &= source is not None and source.get('native_session_id') == sid
        body = {k: v for k, v in s.items() if k != 'snapshot_hash'}
        atomic &= digest(body) == s['snapshot_hash'] == r['L4']['snapshot_hash'] == r['L5']['snapshot_hash']
        correlated &= s['transaction_id'] == r['transaction_id'] == r['L5']['transaction_id']
        correlated &= r['L5']['prediction'] in r['L4']['candidates']
        climate &= (w['climate_input_time_s'] == c['time_s']
                    and w['climate_input_forcing_sha256'] == c['forcing_sha256']
                    and 0 <= n['time_s'] - c['time_s'] <= 3.)
        healthy &= (r['health']['ok'] and r['health']['session_id'] == sid
                    and bool(r['health']['panels_ready']) and r['health']['physical_map']['ready'])
        route = n.get('feedback_consumed')
        if route:
            f = known.get(route)
            version = w['reduced_inner_loop']['feedback_version']
            route_valid &= f is not None
            if route != previous_route:
                route_valid &= route not in consumed and version == previous_version + 1
                consumed.add(route)
            else:
                route_valid &= version == previous_version
            if f:
                route_valid &= all(w['reduced_inner_loop'].get(k) == v for k, v in f['model_update'].items())
            previous_route, previous_version = route, version
        by_tx[r['transaction_id']] = r
        f = r['L7']
        if f.get('ok'):
            receipts_valid &= digest({k: v for k, v in f.items() if k != 'route_hash'}) == f['route_hash']
            src = by_tx.get(f['source_transaction_id'])
            receipt = r['L6'].get('execution_receipt') or {}
            record = (receipt.get('correlation') or {}).get('record') or {}
            receipts_valid &= digest(record) == f['receipt_record_hash']
            if src:
                decision = src['L5']; tube = decision['prediction']['prediction_tube']
                receipts_valid &= (f['source_snapshot_hash'] == src['L3']['snapshot_hash']
                    and f['candidate_id'] == decision['candidate_id']
                    and tube[0]['time_s'] < f['observation_time_s'] <= tube[-1]['time_s']
                    and f['prediction_retimed'] is False)
                context = record.get('context') or {}
                requested = context.get('requested_gui_state') or {}
                receipts_valid &= context.get('instance_id') == src['L6']['intake'].get('command_instance_id')
                receipts_valid &= [requested.get(k) for k in ('roll', 'pitch', 'throttle', 'yaw')] == decision['u_cmd']
                try:
                    from layer6.command_correlation import verified_send_bounds
                    receipts_valid &= verified_send_bounds(receipt.get('execution_witness') or {}, f['source_transaction_id']) is not None
                    receipts_valid &= f['observation_time_s'] >= record['plant_time_bounds']['upper_s']
                except (KeyError, ValueError, TypeError):
                    receipts_valid = False
            else:
                receipts_valid = False
            known[f['route_hash']] = f
    checks.update(approved_navigation_inputs=bool(approved and rows), atomic_snapshot_hashes=bool(atomic and rows),
        candidate_and_transaction_identity=bool(correlated and rows), current_climate_coupling=bool(climate and rows),
        all_recorded_cycles_panel_health=bool(healthy and rows), unique_next_cycle_model_feedback=bool(route_valid and rows),
        raw_receipt_and_original_prediction_time=bool(receipts_valid and known),
        at_least_two_feedbacks_consumed=len(consumed & set(known)) >= 2)
    if rows:
        first = next((physical(r) for r in rows if physical(r)), None)
        final = final_wire.get('SIM_STATE', {}).get('data') or {}
        checks['actual_takeoff'] = first is not None and max(
            (physical(r)['altitude_msl_m'] - first['altitude_msl_m'] for r in rows if physical(r)), default=0.) > 1.
        if final and first and 'lon_int' in final and 'lat_int' in final:
            actual_goal = result.get('goal_lonlat')
            lon, lat = final['lon_int'] * 1e-7, final['lat_int'] * 1e-7
            error = GEOD.inv(lon, lat, *actual_goal)[2] if actual_goal else None
            speed = norm([final[k] for k in ('vn', 've', 'vd')])
            lean = max(abs(v) for v in vtol_angles([final[k] for k in ('q1', 'q2', 'q3', 'q4')])[:2])
            checks['physical_contact_and_stationary'] = abs(final['alt'] - first['altitude_msl_m']) < .5 and speed < .4
            checks['upright_after_landing'] = lean <= 25.
            checks['actual_goal_within_25m'] = error is not None and error <= 25.
            checks['driver_goal_error_agrees_with_raw'] = error is not None and abs(error - result.get('actual_goal_error_m', math.inf)) < 1.
            checks['expected_mission_goal'] = expected_goal is None or list(actual_goal) == list(expected_goal)
        else:
            error = lean = None
            for key in ('physical_contact_and_stationary', 'upright_after_landing', 'actual_goal_within_25m',
                        'driver_goal_error_agrees_with_raw', 'expected_mission_goal'):
                checks[key] = None
    else:
        error = lean = None
        checks['actual_takeoff'] = None
    hb = final_wire.get('HEARTBEAT', {}).get('data') or {}
    land = final_wire.get('EXTENDED_SYS_STATE', {}).get('data') or {}
    checks['FCU_disarmed'] = not bool(hb['base_mode'] & 128) if hb else None
    checks['FCU_on_ground'] = land.get('landed_state') == 1 if land else None
    names = ('GPS1_TYPE', 'GPS2_TYPE', 'SIM_GPS1_ENABLE', 'SIM_GPS2_ENABLE', 'SIM_GPS3_ENABLE', 'SIM_GPS4_ENABLE')
    readbacks = result.get('parameter_readbacks') or {}
    checks['GPS_disabled_readbacks'] = all(readbacks.get(k, {}).get('value') == 0 for k in names)
    images = []
    for p in Path(evidence).rglob('manifest.json'):
        m = load_json(p)
        if m.get('native_session_id') != sid or not m.get('all_six_captured'):
            continue
        valid = set(m.get('windows', {})) == {'ardupilot', 'jsbsim', 'environment', 'gui', 'map', 'console'}
        for w in m.get('windows', {}).values():
            image = Path(w.get('image', ''))
            valid &= w.get('ok') is True and image.is_file()
            if image.is_file():
                valid &= file_hash(image) == w.get('sha256')
        if valid:
            images.append(str(p.resolve()))
    checks['six_actual_window_images_verified'] = bool(images)
    status = 'FAIL' if any(v is False for v in checks.values()) else ('BLOCKED' if any(v is None for v in checks.values()) else 'PASS')
    return {'schema': 'TDOC-native-online-report-qualification-v1', 'status': status,
            'native_session_id': sid, 'checks': checks, 'actual_goal_error_m_independent': error,
            'final_physical_VTOL_lean_deg': lean, 'accepted_feedbacks': len(known),
            'consumed_feedbacks': len(consumed & set(known)), 'real_window_manifests': images,
            'scope': 'Native VTOL prototype; no hardware/full-formalism certification',
            'verification_scope': 'independent raw pose endpoint, wire/cross-layer trace and evidence hashes'}
