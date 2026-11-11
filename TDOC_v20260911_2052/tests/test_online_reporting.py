"""Reporting regressions: provenance, causality of reads, and honest missing data."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from online_reports.data import telemetry_alignment
from online_reports.generate import generate, PAGES
from online_reports.provenance import file_hash
from online_reports.validation import qualify


@pytest.fixture
def cycle():
    return json.loads((Path(__file__).parent / 'fixtures/online_native_report_cycle.json').read_text())


def test_asof_telemetry_never_uses_future_servo_values(tmp_path):
    packets = [
        {'type': 'SERVO_OUTPUT_RAW', 'data': {'servo3_raw': 1200}, 'received_monotonic': 1.},
        {'type': 'HIGHRES_IMU', 'data': {}, 'received_monotonic': 1.1, 'wire_sha256': 'source'},
        {'type': 'SERVO_OUTPUT_RAW', 'data': {'servo3_raw': 1900}, 'received_monotonic': 1.2},
    ]
    p = tmp_path / 'wire.jsonl'
    p.write_text(''.join(json.dumps(x) + '\n' for x in packets))
    samples, _, _, _ = telemetry_alignment([{'L2': {'navigation': {'source_wire_sha256': 'source'}}}], p)
    assert samples['source']['SERVO_OUTPUT_RAW']['data']['servo3_raw'] == 1200


def test_raw_packet_tampering_is_detected(tmp_path):
    p = tmp_path / 'wire.jsonl'
    p.write_text(json.dumps({'type': 'HIGHRES_IMU', 'wire_hex': '0102', 'wire_sha256': 'tampered', 'data': {}}) + '\n')
    assert telemetry_alignment([], p)[3] is False


def test_qualification_recomputes_goal_from_pose_and_rejects_late_driver_error(tmp_path, cycle):
    r = copy.deepcopy(cycle)
    sid = r['health']['session_id']
    imu = {'native_session_id': sid}
    samples = {r['L2']['navigation']['source_wire_sha256']: {'HIGHRES_IMU': imu}}
    sim = r['external_validation_only']['SIM_STATE']
    source_goal = [sim['data']['lon_int'] * 1e-7, sim['data']['lat_int'] * 1e-7 + .1]
    result = {'status': 'PASS', 'error': 'late binding failure', 'native_session_id': sid,
              'goal_lonlat': source_goal, 'actual_goal_error_m': 0.}
    q = qualify([r], result, samples, {'SIM_STATE': sim}, True, tmp_path, {'status': 'PASS'})
    assert q['status'] == 'FAIL'
    assert q['checks']['driver_completed_without_error'] is False
    assert q['checks']['actual_goal_within_25m'] is False
    assert q['checks']['driver_goal_error_agrees_with_raw'] is False
    assert q['actual_goal_error_m_independent'] > 10000.


def test_report_generation_preserves_source_and_does_not_forge_unavailable_evidence(tmp_path, cycle):
    source = tmp_path / 'native'; source.mkdir()
    (source / 'cycles.jsonl').write_text(json.dumps(cycle) + '\n')
    (source / 'telemetry_wire.jsonl').write_text('')
    (source / 'MISSION_RESULT.json').write_text(json.dumps({'status': 'FAIL', 'native_session_id': cycle['health']['session_id']}))
    before = {p.name: file_hash(p) for p in source.iterdir()}
    out = tmp_path / 'mission'
    summary = generate(source, out, evidence=tmp_path)
    assert summary['qualification']['status'] != 'PASS'
    assert {p.name: file_hash(p) for p in source.iterdir()} == before
    assert pd.read_csv(out / 'LAYER4_CANDIDATES.csv')['verified'].isna().all()
    assert pd.read_csv(out / 'ENERGY_HISTORY.csv')['battery_voltage_v'].isna().all()
    availability = json.loads((out / 'DATA_AVAILABILITY.json').read_text())
    assert availability['wind_counterfactual']['available'] is False
    assert json.loads((out / 'WIND_CAUSALITY_METRICS.json').read_text())['max_counterfactual_displacement_m'] is None
    index = (out / 'assets/explorer_data.js').read_text()
    assert 'TDDT_CYCLE_INDEX' in index and 'details' not in index.lower().replace('detail_chunks', '')
    assert (out / 'assets/details_0000.js').exists()
    assert len(list(out.glob('*.html'))) == len(PAGES)
    # Regeneration must not hash the previous manifest as if it were an output.
    generate(source, out, evidence=tmp_path)
    manifest = json.loads((out / 'REPORT_MANIFEST.json').read_text())
    assert 'REPORT_MANIFEST.json' not in [x['path'] for x in manifest['files']]
    assert all(file_hash(out / x['path']) == x['sha256'] for x in manifest['files'])


def test_missing_control_data_cannot_inherit_pass_from_a_summary(tmp_path):
    source = tmp_path / 'native'; source.mkdir()
    (source / 'MISSION_RESULT.json').write_text('{"status":"PASS"}')
    out = tmp_path / 'mission'
    q = generate(source, out, evidence=tmp_path)['qualification']
    assert q['status'] != 'PASS'
    assert pd.read_csv(out / 'MISSION_TIMELINE.csv').empty
    assert pd.read_csv(out / 'MISSION_DETAIL_TIMESERIES.csv').empty
    with gzip.open(out / 'DECISION_POINT_DETAILS.json.gz', 'rt') as f:
        assert json.load(f) == []


def test_report_output_cannot_overwrite_source_or_offline_reports(tmp_path):
    source = tmp_path / 'native'; source.mkdir()
    with pytest.raises(ValueError, match='overwrite'):
        generate(source, source)
    cfg = SimpleNamespace(package_root=tmp_path / 'TDOC')
    with pytest.raises(ValueError, match='Offline'):
        generate(source, cfg.package_root / 'reports/offline/mission', config=cfg)


def test_energy_time_phase_totals_and_landing_use_correct_sources(cycle):
    from online_reports.data import normalize
    from online_reports.performance import performance
    records = [copy.deepcopy(cycle) for _ in range(3)]
    for i, row in enumerate(records):
        row['L2']['navigation']['time_s'] = 10. + .2*i
        row['phase'] = 'vertical_takeoff' if i == 0 else 'cruise_vtol'
    samples = {cycle['L2']['navigation']['source_wire_sha256']: {
        'BATTERY_STATUS': {'data': {'voltages': [12000, 65535], 'current_battery': 200}}}}
    timeline = normalize(records, {}, samples)[0]
    energy = 24*.4/3600
    assert timeline[-1]['telemetry_energy_integral_covered_wh'] == pytest.approx(energy)
    goal = [-105.3, 40.01]
    summary = {'flight_duration_s': .4, 'goal_tolerance_m': 25., 'source_start_s': 10.,
        'energy_telemetry_coverage_s': .4, 'energy_telemetry_covered_wh': energy,
        'energy_used_wh': energy, 'energy_source': 'SITL'}
    final = {'SIM_STATE': {'data': {'lat_int': 400101000, 'lon_int': -1053000000}}}
    q = {'checks': {}, 'actual_goal_error_m_independent': 11.1}
    perf = performance(timeline, {'goal_lonlat': goal}, q, final, summary)
    assert perf['landing_accuracy']['target_to_actual_north_m'] > 11
    assert abs(perf['landing_accuracy']['target_to_actual_east_m']) < 1e-5
    assert perf['landing_accuracy']['vertical_landing_error_m'] is None
    assert perf['energy']['consumed_j'] == pytest.approx(9.6)
    assert sum(p['duration_s'] for p in perf['phases']) == pytest.approx(.4)
    assert sum(p['energy_used_wh'] for p in perf['phases']) == pytest.approx(energy)
    assert perf['time']['startup_calibration_and_shutdown_included'] is False
    summary['energy_used_wh'] = None
    summary['energy_telemetry_coverage_s'] = 0.
    perf = performance(timeline, {}, q, {}, summary)
    assert perf['energy']['consumed_j'] is None
    assert perf['landing_accuracy']['actual_final_lonlat'] is None


def test_terrain_is_georeferenced_unshifted_and_rejects_changed_bound_dem(tmp_path, cycle):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from pyproj import Transformer
    from online_reports.data import normalize
    from online_reports.terrain import build_terrain, _bilinear
    from online_reports.generate import _terrain_figure

    origin = cycle['L3']['origin']
    x, y = Transformer.from_crs('EPSG:4326', 'EPSG:32613', always_xy=True).transform(origin['longitude_deg'], origin['latitude_deg'])
    dem = tmp_path / 'dem.tif'
    rr, cc = np.indices((10, 10))
    with rasterio.open(dem, 'w', driver='GTiff', width=10, height=10, count=1,
            dtype='float32', crs='EPSG:32613', transform=from_origin(x-500, y+500, 100, 100), nodata=-9999) as ds:
        ds.write((1700+rr*2+cc).astype('float32'), 1)
    with rasterio.open(dem) as ds:
        assert _bilinear(ds, [origin['longitude_deg']], [origin['latitude_deg']])[0] == pytest.approx(1713.5)
        assert np.isnan(_bilinear(ds, [0.], [0.])[0])
    evidence = tmp_path / 'evidence'; (evidence / 'native_session').mkdir(parents=True)
    binding = evidence / 'native_session/binding.json'
    binding.write_text(json.dumps({'origin': {'terrain_file': {'path': str(dem), 'file_sha256': file_hash(dem)}}}))
    out = tmp_path / 'mission'; out.mkdir()
    timeline = normalize([cycle], {}, {})[0]
    before = file_hash(dem)
    meta, grid, frame = build_terrain(out, evidence, timeline, [cycle], {}, None)
    assert meta['available'] and meta['binding_sha256_verified']
    assert file_hash(dem) == before
    assert grid['height_offset_m'] == 0 and grid['height_exaggeration'] == 1
    assert frame.physical_CG_minus_DEM_m.iloc[0] > 80  # no draping to terrain
    assert frame.navigation_altitude_msl_m.iloc[0] == pytest.approx(origin['altitude_msl_m']-timeline[0]['navigation_down_m'])
    fig = _terrain_figure(pd.DataFrame(timeline), meta, grid, frame, {})
    assert fig.data[0].type == 'surface'
    assert list(fig.data[1].z) == [timeline[0]['physical_altitude_msl_m']]
    binding.write_text(json.dumps({'origin': {'terrain_file': {'path': str(dem), 'file_sha256': 'incorrect'}}}))
    meta, grid, frame = build_terrain(out, evidence, timeline, [cycle], {}, None)
    assert meta['available'] is False and grid is None
    assert 'hash differs' in meta['reason']


def test_wind_bearings_and_arrows_preserve_ned_direction_and_calm_nulls():
    from online_reports.route_details import wind_samples, wind_traces
    df = pd.DataFrame({'cycle': [1, 2, 3], 'flight_elapsed_s': [0., 1., 2.],
        'phase': ['a']*3, 'physical_east_m': [10.]*3, 'physical_north_m': [20.]*3,
        'physical_altitude_msl_m': [1700.]*3,
        'wind_n_m_s': [1., 0., 0.], 'wind_e_m_s': [0., 1., 0.], 'wind_d_m_s': [2., -3., 5.]})
    w = wind_samples(df)
    assert list(w.flow_to_bearing_deg[:2]) == [0., 90.]
    assert list(w.meteorological_from_bearing_deg[:2]) == [180., 270.]
    assert pd.isna(w.flow_to_bearing_deg.iloc[2])
    assert pd.isna(w.meteorological_from_bearing_deg.iloc[2])
    traces, scale, cycles = wind_traces(w, [0], anchor=(10., 20., 1700.), span_m=100.)
    shaft, cone = traces
    assert cycles == [1] and scale > 0
    assert list(shaft.x[:2]) == [0., 0.]
    assert shaft.y[1] > 0 and shaft.z[1] < 0
    assert list(cone.u) == [0.] and list(cone.v) == [1.] and list(cone.w) == [-2.]


def test_phase_detail_keeps_every_cycle_and_separates_repeated_occurrences(cycle):
    from online_reports.data import normalize
    from online_reports.route_details import phase_segments, phase_viewer, wind_samples, sampled_indices
    records = [copy.deepcopy(cycle) for _ in range(6)]
    for i, row in enumerate(records):
        row['L2']['navigation']['time_s'] += i*.1
        row['phase'] = ['vertical_takeoff', 'vertical_takeoff', 'cruise_vtol', 'vertical_takeoff', 'landing_flare', 'contact_settle'][i]
    timeline = normalize(records, {}, {})[0]
    df = pd.DataFrame(timeline)
    assert [(a, b) for a, b, _ in phase_segments(df)] == [(0, 2), (2, 3), (3, 4), (4, 5), (5, 6)]
    assert sampled_indices(df) == list(range(6))
    fig = phase_viewer(df, wind_samples(df), {'origin_altitude_msl_m': cycle['L3']['origin']['altitude_msl_m']})
    assert len(fig.frames) == 5
    assert [len(f.data[0].x) for f in fig.frames] == [2, 1, 1, 1, 1]
    assert [int(item[0]) for f in fig.frames for item in f.data[0].customdata] == [1, 2, 3, 4, 5, 6]
    assert fig.frames[0].data[0].z[0] == 0.
    assert len(fig.layout.updatemenus[0].buttons) == 5
    assert 'برخاست' in fig.layout.title.text


def test_layer3_report_verifies_snapshot_content_and_distinguishes_initial_prior(cycle):
    from online_reports.layer3_report import inspect_snapshots
    before = copy.deepcopy(cycle)
    timeline, edges, payload, summary = inspect_snapshots([cycle])
    assert cycle == before
    assert summary['report_integrity'] == 'PASS'
    assert timeline[0]['snapshot_hash_valid'] is True
    assert timeline[0]['L2_packets_match_snapshot'] is True
    assert summary['topology_count'] == 1 and summary['topology_changes'] == 0
    assert len(edges) == 5 and edges[0]['evidence_status'] == 'INITIAL_PRIOR_ONLY'
    assert edges[-1]['evidence_status'] == 'NO_FEEDBACK_YET'
    assert payload[0]['snapshot'] == cycle['L3']
    altered = copy.deepcopy(cycle)
    altered['L3']['twins']['MicroMetTwin']['forcing_sha256'] = 'changed'
    timeline, _, _, summary = inspect_snapshots([altered])
    assert summary['report_integrity'] == 'FAIL'
    assert timeline[0]['snapshot_hash_valid'] is False
    assert timeline[0]['climate_binding_matches'] is False
    altered = copy.deepcopy(cycle)
    altered['L2']['climate']['forcing_sha256'] = 'not-the-snapshot-packet'
    _, edges, _, summary = inspect_snapshots([altered])
    assert summary['report_integrity'] == 'FAIL'
    assert edges[1]['evidence_status'] == 'MISMATCH'
    assert inspect_snapshots([{}])[3]['report_integrity'] == 'BLOCKED'
    assert inspect_snapshots([])[3]['report_integrity'] == 'BLOCKED'


def test_layer3_feedback_must_be_from_prior_cycle_and_is_not_recounted_when_held(cycle):
    from online_reports.layer3_report import inspect_snapshots
    from online_reports.data import digest
    records = [copy.deepcopy(cycle) for _ in range(3)]
    update = {'feedback_version': 1, 'feedback_source': 'prior-decision', 'disturbance_ned_m_s2': [.1, .2, .3], 'inner_loop_tau_s': 1.}
    f = {'ok': True, 'model_update': update, 'destination': 'Layer2/model-only', 'sensor_only': True,
        'source_transaction_id': 'prior-decision'}
    f['route_hash'] = digest(f)
    records[0]['L7'] = f
    for i, r in enumerate(records[1:], 1):
        r['L7'] = {'ok': False}
        for n in (r['L2']['navigation'], r['L3']['twins']['MESA_NAV']):
            n['feedback_consumed'] = f['route_hash']
        for w in (r['L2']['wing'], r['L3']['twins']['WingMPET']):
            w['reduced_inner_loop'].update(update)
        r['L3']['feedback_route_hash'] = f['route_hash']
        r['L3']['snapshot_hash'] = digest({k: v for k, v in r['L3'].items() if k != 'snapshot_hash'})
        r['L4']['snapshot_hash'] = r['L5']['snapshot_hash'] = r['L3']['snapshot_hash']
        for c in r['L4']['candidates']:
            c['source_snapshot_hash'] = r['L3']['snapshot_hash']
        r['L5']['prediction']['source_snapshot_hash'] = r['L3']['snapshot_hash']
    timeline, _, payload, summary = inspect_snapshots(records)
    assert summary['report_integrity'] == 'PASS'
    assert summary['unique_feedback_routes_applied'] == 1
    assert timeline[1]['feedback_newly_applied'] is True
    assert timeline[1]['feedback_apply_delay_cycles'] == 1
    assert timeline[2]['feedback_newly_applied'] is False
    assert timeline[2]['feedback_apply_delay_cycles'] is None
    assert payload[1]['consumed_L7_prior'] == f
    assert payload[1]['current_L7_return']['ok'] is False
    lookahead = copy.deepcopy(records[1]); lookahead['L7'] = f
    assert inspect_snapshots([lookahead])[0][0]['feedback_model_binding'] is False


def test_layer32_separates_twin_graph_from_boundary_and_does_not_invent_formal_values(cycle):
    from online_reports.layer3_report import inspect_snapshots
    from online_reports.layer32_report import describe_layer32
    before = copy.deepcopy(cycle)
    p = inspect_snapshots([cycle])[2][0]
    d = describe_layer32(p)
    assert cycle == before
    assert len(d['internal_edges']) == 3 and len(d['boundary_edges']) == 2
    assert sum(map(sum, d['declaration_adjacency_D'])) == 3
    assert sum(map(sum, d['noninitial_declaration_adjacency_D'])) == 2
    assert d['declared_graph_has_directed_cycle'] is False
    assert d['formal_outputs']['K'] == {'status': 'DECLARATION_LIST_ONLY', 'value': None}
    for key in ('W', 'C_star', 'J_C', 'Omega', 'X_E', 'LCI'):
        assert d['formal_outputs'][key]['status'] == 'NOT_RECORDED'
        assert d['formal_outputs'][key]['value'] is None
    assert all(e['numeric_gate_a'] is None and e['numeric_strength_K'] is None and e['transfer_delay_tau_s'] is None for e in d['internal_edges'])
    assert all(all(v is None for v in e['six_type_strengths'].values()) for e in d['internal_edges'])
    assert d['internal_edges'][0]['transferred_value'] is None
    assert d['internal_edges'][0]['initial_prior_value_is_current_packet'] is False
    assert d['controller_or_engine_executed_by_report'] is False


def test_layer32_keeps_explicit_zero_false_empty_and_does_not_replace_lci_with_cycle_count(cycle):
    from online_reports.layer3_report import inspect_snapshots
    from online_reports.layer32_report import describe_layer32
    r = copy.deepcopy(cycle)
    r['L3'].update(weights={'MESA_NAV': 0.}, mismatch=0., center_of_consistency={},
        consistency_region={'inside': False}, hidden_loop={'lci': 0.})
    r['L3']['couplings'][1].update(a=0, K=0., tau_s=0., K_by_type={'Data': 0.})
    d = describe_layer32(inspect_snapshots([r])[2][0])
    assert d['formal_outputs']['J_C'] == {'status': 'RECORDED', 'value': 0.}
    assert d['formal_outputs']['LCI'] == {'status': 'RECORDED', 'value': 0.}
    assert d['formal_outputs']['C_star']['status'] == 'RECORDED_EMPTY'
    assert d['formal_outputs']['Omega']['value']['inside'] is False
    assert d['internal_edges'][1]['numeric_gate_a'] == 0
    assert d['internal_edges'][1]['numeric_strength_K'] == 0.
    assert d['internal_edges'][1]['six_type_strengths']['Data'] == 0.
    assert d['internal_edges'][1]['six_type_strengths']['Model'] is None
    r = copy.deepcopy(cycle)
    r['L3']['couplings'].append({'source': 'MESA_NAV.location', 'target': 'MicroMetTwin.query'})
    d = describe_layer32(inspect_snapshots([r])[2][0])
    assert d['declared_graph_has_directed_cycle'] is True
    assert d['formal_outputs']['LCI']['value'] is None
