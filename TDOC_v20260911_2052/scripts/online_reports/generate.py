"""Rich Offline-equivalent reports sourced exclusively from a completed Online run."""
from collections import Counter
from datetime import datetime, timezone
import gzip
import html
import json
import math
import os
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

from .data import GEOD, load_json, norm, normalize, physical, rows_from, telemetry_alignment
from .provenance import file_hash, write_json
from .validation import qualify
from .terrain import build_terrain
from .performance import performance
from .route_details import (PHASE_LABELS, PHASE_COLORS, phase_segments, wind_samples,
    sampled_indices, wind_traces, export_route_details, phase_table, phase_viewer, wind_figure)
from .layer3_report import build_network_report


def clean(v):
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, np.ndarray)):
        return [clean(x) for x in v]
    if isinstance(v, np.generic):
        return clean(v.item())
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def stats(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    return dict(zip(('min', 'median', 'p95', 'p99', 'max'), map(float, np.percentile(values, [0, 50, 95, 99, 100])))) if values else None


PAGES = {
    'MISSION_MANAGEMENT_DASHBOARD.html': 'داشبورد جامع مأموریت Online',
    'MISSION_ROUTE_3D.html': 'مسیر فیزیکی، تخمین و پیش‌بینی سه‌بعدی',
    'LANDING_ENERGY_TIME_REPORT.html': 'دقت فرود، انرژی مصرفی و زمان مأموریت',
    'LAYER3_DYNAMIC_NETWORK_REPORT.html': 'شبکهٔ داینامیکی و snapshotهای لایهٔ ۳',
    'LAYER4_OPTIMIZATION_2D.html': 'گزینه‌های لایهٔ ۴ ـ دوبعدی',
    'LAYER4_OPTIMIZATION_3D.html': 'گزینه‌های لایهٔ ۴ ـ سه‌بعدی',
    'LAYER4_OPTIMIZATION_TIMELINE.html': 'روند بهینه‌سازی و feasibility',
    'DECISION_ACCURACY_EFFICIENCY_REPORT.html': 'دقت تخمین و کارایی تصمیم',
    'VTOL_PHASE_DASHBOARD.html': 'فازهای VTOL و شواهد پایان',
    'WIND_CAUSALITY_REPORT.html': 'اقلیم و حدود استنباط اثر باد',
    'SARG_CRP_TREEMAP.html': 'زمینهٔ prior آفلاین SARG/CRP',
    'SENSORS_ACTUATORS_REPORT.html': 'حسگرها، فرمان و خروجی واقعی سروو',
    'LAYER7_FEEDBACK_REPORT.html': 'شاهد اجرا و بازخورد لایهٔ ۷',
    'EXECUTION_EVIDENCE_REPORT.html': 'اعتبارسنجی Native و تصاویر واقعی',
}


def _page(path, title, figures, summary, extra='', *, figure_titles=None):
    links = ' · '.join('<a href="' + n + '">' + html.escape(t) + '</a>' for n, t in PAGES.items())
    qualification = summary['qualification']
    cards = {label: summary.get(key) for label, key in (
        ('شناسهٔ اجرا', 'run_id'), ('نشست Native', 'native_session_id'), ('نتیجهٔ مأموریت', 'mission_status'),
        ('تعداد چرخه', 'cycle_count'), ('فاصلهٔ مأموریت [متر]', 'route_distance_m'),
        ('زمان حلقهٔ کنترل [ثانیه]', 'flight_duration_s'), ('خطای افقی محل فرود [متر]', 'actual_goal_error_m'),
        ('فاصلهٔ تخمینی نهایی تا مقصد [متر]', 'estimated_goal_error_m'),
        ('انرژی مصرفی مدل باتری SITL [Wh]', 'energy_used_wh'),
        ('پوشش زمانی دادهٔ انرژی [ثانیه]', 'energy_telemetry_coverage_s'))}
    rows = ''.join('<tr><td>' + html.escape(str(k)) + '</td><td dir="ltr">' + html.escape('ناموجود' if v is None else f'{v:.3f}' if isinstance(v, float) else str(v)) + '</td></tr>' for k, v in cards.items())
    figures_html = ''.join(('<h2 id="route-section-' + str(i) + '">' + html.escape(figure_titles[i]) + '</h2>' if figure_titles else '') +
        pio.to_html(f, include_plotlyjs=False, full_html=False, auto_play=False,
        config={'responsive': True, 'displaylogo': False}) for i, f in enumerate(figures))
    style = '''body{font:15px/1.9 Tahoma,sans-serif;margin:24px;background:#101923;color:#e2ebf5}a{color:#7acbff}nav{padding:14px;background:#1d2a39}table{border-collapse:collapse;max-width:100%;font-size:13px}td,th{border:1px solid #41516a;padding:7px;vertical-align:top}code,pre{direction:ltr;unicode-bidi:embed}select,input{padding:7px;margin:8px}section{padding:16px;background:#182536;margin:20px 0}.badge{border:2px solid #d5b256;padding:12px}#detail-table td{word-break:break-word}summary{cursor:pointer}img{max-width:100%}'''
    explorer = '''<section><h2>کاوشگر جزئیات چرخهٔ تصمیم</h2><p>دادهٔ کامل ثبت‌شده در <a href="RAW_CYCLE_RECORDS.jsonl.gz">آرشیو raw چرخه‌ها</a> و <a href="DECISION_POINT_DETAILS.json.gz">جزئیات ساخت‌یافته</a> موجود است. تصویر فیزیکی فقط برای ارزیابی است و وارد کنترل نشده است.</p><select id="cycle"></select><input id="query" placeholder="جست‌وجوی نام فیلد یا مقدار"><label><input id="nulls" type="checkbox">نمایش مقادیر ناموجود</label><p id="count"></p><table id="detail-table"><thead><tr><th>فیلد</th><th>مقدار</th></tr></thead><tbody></tbody></table></section>
<script>
const d=window.TDDT_CYCLE_INDEX||[],s=document.getElementById('cycle'),q=document.getElementById('query'),n=document.getElementById('nulls'),b=document.querySelector('#detail-table tbody'),loads={};
d.forEach((x,i)=>{const o=document.createElement('option');o.value=i;o.textContent=x.cycle+' — '+x.transaction_id;s.appendChild(o)});
function flat(v,p,a){if(v!==null&&typeof v==='object'){Object.entries(v).forEach(([k,x])=>flat(x,p?p+'.'+k:k,a))}else a.push([p,v])}
function loadChunk(i){if(window.TDDT_DETAIL_CHUNKS[i])return Promise.resolve();if(!loads[i])loads[i]=new Promise((ok,fail)=>{const tag=document.createElement('script');tag.src='assets/details_'+String(i).padStart(4,'0')+'.js';tag.onload=ok;tag.onerror=fail;document.head.appendChild(tag)});return loads[i]}
async function show(){const selected=Number(s.value)||0,meta=d[selected],a=[];if(!meta){b.replaceChildren();return}document.getElementById('count').textContent='در حال خواندن جزئیات همین چرخه…';try{await loadChunk(meta.chunk)}catch(e){document.getElementById('count').textContent='فایل جزئیات باز نشد؛ پوشه assets کنار گزارش لازم است.';return}if((Number(s.value)||0)!==selected)return;flat(window.TDDT_DETAIL_CHUNKS[meta.chunk][meta.offset],'',a);b.replaceChildren();let count=0;for(const [k,v] of a){if(v===null&&!n.checked)continue;if(!(k+' '+String(v)).toLowerCase().includes(q.value.toLowerCase()))continue;const tr=document.createElement('tr');for(const x of [k,v===null?'ناموجود':String(v)]){const td=document.createElement('td');td.textContent=x;tr.appendChild(td)}b.appendChild(tr);count++}document.getElementById('count').textContent=count+' فیلد';}
s.addEventListener('change',show);q.addEventListener('input',show);n.addEventListener('change',show);show();
</script>'''
    body = f'''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>{html.escape(title)}</title><style>{style}</style>
<script src="assets/plotly.min.js"></script><script src="assets/explorer_data.js"></script>
<h1>{html.escape(title)}</h1><nav>{links}</nav><p class="badge">نتیجهٔ مأموریت: {html.escape(str(summary['mission_status']))}؛ ارزیابی پس از پرواز: <strong>{qualification['status']}</strong>. تولید گزارش به معنی قبولی پرواز نیست.</p>
<table>{rows}</table><p>منبع: Native همان نشست؛ کنترل: MESA و مدل جاری. پروفایل محدود VTOL؛ prior جزئی؛ drift مصنوعی زمانی خاموش. مقادیر ناموجود صفر یا موفقیت فرض نشده‌اند. نمودارها و کاوشگر بدون اینترنت باز می‌شوند.</p>{extra}{figures_html}{explorer}</html>'''
    path.write_text(body, encoding='utf-8')


def _lines(df, fields, title, *, x='flight_elapsed_s', unit=''):
    f = go.Figure()
    for col in fields:
        if col in df:
            f.add_trace(go.Scatter(x=df[x].tolist(), y=df[col].tolist(), name=col, mode='lines', connectgaps=False))
    f.update_layout(title=title, xaxis_title='زمان از آغاز کنترل [s]', yaxis_title=unit,
                    template='plotly_dark', height=520, legend={'orientation': 'h'})
    return f


def _table_figure(obj, title):
    f = go.Figure(go.Table(header={'values': ['شاخص', 'مقدار']},
        cells={'values': [list(obj), [json.dumps(clean(v), ensure_ascii=False) for v in obj.values()]]}))
    f.update_layout(title=title, template='plotly_dark', height=max(450, min(1800, len(obj) * 36)))
    return f


def _coverage(out, offline_dir):
    names = [p.name for p in Path(offline_dir).glob('*') if p.is_file()] if offline_dir else []
    coverage = []
    limited = {'WIND_CAUSALITY_METRICS.json': 'No same-command no-wind experiment was performed in Native; causal effect is unavailable.',
               'WIND_CAUSALITY_REPORT.html': 'Shows model climate and observed errors; no causal/counterfactual claim.',
               'SARG_CRP_TREEMAP.html': 'Offline prior context only; no per-cycle CRP classification asserted.',
               'SARG_CRP_RELATION.csv': 'Offline prior identity, not Online scenario selection.',
               'ENERGY_HISTORY.csv': 'SITL battery telemetry and model power are separate; no measured hardware energy claim.'}
    for name in names:
        coverage.append({'offline_artifact': name, 'online_artifact': name,
            'created': (out / name).is_file(),
            'semantics': limited.get(name, 'Online same-session data; source semantics documented in DATA_AVAILABILITY.json')})
    return {'schema': 'TDOC-offline-online-report-equivalence-v1', 'artifacts': coverage,
            'all_offline_artifact_names_covered': all(x['created'] for x in coverage),
            'same_file_names_do_not_imply_same_control_or_validation_contract': True}


def _archive_native_session(evidence, sid):
    """Copy the completed, matching session so later resets cannot erase its evidence."""
    target = evidence / 'native_session'
    if target.exists():
        return
    for observation in rows_from(evidence / 'windows/native_observations.jsonl'):
        health = observation.get('health') or {}
        if health.get('session_id') != sid:
            continue
        session = Path(health.get('session_dir', ''))
        if session.is_dir() and session.name == sid:
            shutil.copytree(session, target)
            write_json(evidence / 'native_session_archive.json', {'native_session_id': sid,
                'copied_from': str(session), 'scope': 'post-flight copy, original files untouched',
                'files': {p.relative_to(target).as_posix(): file_hash(p) for p in target.rglob('*') if p.is_file()}})
        return


def generate(source, out, *, evidence=None, config=None):
    source, out = Path(source).resolve(), Path(out).resolve()
    if out == source or out.is_relative_to(source):
        raise ValueError('Reports must not overwrite or be nested inside Native source evidence')
    if config is not None:
        offline = (config.package_root / 'reports/offline').resolve()
        if out == offline or out.is_relative_to(offline):
            raise ValueError('Online reporting must never overwrite Offline reports')
    out.mkdir(parents=True, exist_ok=True)
    evidence = Path(evidence or source.parent).resolve()
    result = load_json(source / 'MISSION_RESULT.json')
    if result.get('native_session_id'):
        _archive_native_session(evidence, result['native_session_id'])
    rows = list(rows_from(source / 'cycles.jsonl'))
    samples, counts, final_wire, raw_verified = telemetry_alignment(rows, source / 'telemetry_wire.jsonl')
    timeline, candidates, details, pulses = normalize(rows, result, samples)
    df = pd.DataFrame(timeline) if timeline else pd.DataFrame(columns=['cycle', 'transaction_id', 'flight_elapsed_s'])
    cdf = pd.DataFrame(candidates) if candidates else pd.DataFrame(columns=['cycle', 'candidate_id', 'feasible'])
    preservation = load_json(evidence / 'preservation.json')
    expected_goal = None
    if config is not None and result.get('requested_hover_seconds') is None:
        region = load_json(config.package_root / 'region/region_v1.geojson')
        expected_goal = next(f['geometry']['coordinates'] for f in region['features'] if f['properties'].get('feature_role') == 'destination')
    qualification = qualify(rows, result, samples, final_wire, raw_verified, evidence, preservation, expected_goal)
    initial_t = timeline[0]['source_time_s'] if timeline else None
    duration = timeline[-1]['flight_elapsed_s'] if timeline else 0.
    result_goal = qualification.get('actual_goal_error_m_independent')
    route_length = 0.
    previous = None
    for x in timeline:
        if x['physical_lon_deg'] is not None:
            if previous is not None:
                route_length += GEOD.inv(previous['physical_lon_deg'], previous['physical_lat_deg'], x['physical_lon_deg'], x['physical_lat_deg'])[2]
            previous = x
    energy_covered = timeline[-1]['energy_covered_duration_s'] if timeline else 0.
    summary = {'schema': 'TDOC-native-online-mission-summary-v1', 'run_id': out.parent.name,
        'generated_utc': datetime.now(timezone.utc).isoformat(), 'mode': 'ONLINE_JSBSIM',
        'source_directory': str(source), 'native_session_id': result.get('native_session_id'),
        'model_id': result.get('model_id'), 'mission_status': result.get('status', 'BLOCKED'),
        'driver_error': result.get('error'), 'qualification': qualification, 'cycle_count': len(rows),
        'cycles_completed': len(rows), 'flight_duration_s': duration, 'source_start_s': initial_t,
        'route_distance_m': result.get('route_distance_m'), 'realized_route_distance_m': route_length,
        'goal_lonlat': result.get('goal_lonlat'), 'actual_goal_error_m': result_goal,
        'estimated_goal_error_m': result.get('estimated_goal_error_m'), 'goal_tolerance_m': 25.,
        'mission_complete': qualification['status'] == 'PASS',
        'touchdown_reached': qualification['checks'].get('physical_contact_and_stationary'),
        'disarm_confirmed': qualification['checks'].get('FCU_disarmed'),
        'accepted_feedback': qualification['accepted_feedbacks'], 'consumed_feedback': qualification['consumed_feedbacks'],
        'raw_message_counts': dict(counts), 'preservation': preservation,
        'profile_scope': 'unchanged Native sensor VTOL prototype; partial Offline prior; no vision/odometry/GNSS navigation',
        'reporting_only': True, 'current_control_state_changed_by_reporting': False,
        'energy_telemetry_coverage_s': energy_covered,
        'energy_telemetry_covered_wh': timeline[-1]['telemetry_energy_integral_covered_wh'] if timeline else None,
        'energy_used_wh': timeline[-1]['telemetry_energy_integral_covered_wh'] if timeline and duration > 0 and energy_covered / duration >= .95 else None,
        'energy_source': 'SITL AP_BattMonitor telemetry; independent of Wing SOC=1 assumption',
        'full_formalism_verified': False}
    availability = {'schema': 'TDOC-online-data-availability-v1',
        'navigation': {'available': bool(rows), 'source': 'MESA approved sensors'},
        'physical_pose': {'available': any(x['physical_lon_deg'] is not None for x in timeline), 'scope': 'validation/display only'},
        'candidate_FF_verified': {'available': False, 'reason': 'Realtime reduced candidates have no old FF verification certificate'},
        'seven_objective_costs': {'available': False, 'reason': 'Current MPC exposes scalar cost, not the legacy seven objectives'},
        'wind_counterfactual': {'available': False, 'reason': 'No same-command no-wind Native experiment was run'},
        'Online_CRP_SARG_selection': {'available': False, 'reason': 'Only compatible Offline prior context is loaded'},
        'terrain_constraint_execution': {'available': False, 'reason': 'This realtime control profile does not certify terrain/obstacle constraints'},
        'energy': {'available': energy_covered > 0, 'coverage_s': energy_covered, 'source': summary['energy_source']},
        'temperature_humidity_pressure_in_snapshot': {'available': False, 'reason': 'Not published by current realtime climate packet'},
        'physical_weather_application': {'scope': 'Native adapter horizontal wind parameter readbacks only; no full atmosphere equivalence claim'}}
    energy_quality = {'schema': 'TDOC-native-online-energy-data-quality-v1',
        'telemetry_source': summary['energy_source'],
        'hardware_energy_measurement': False, 'energy_budget_certified': False,
        'WingMPET_SOC_input': 1.0, 'battery_telemetry_used_by_MPC': False,
        'telemetry_integral_wh': summary['energy_used_wh'],
        'Wing_model_power_integral_wh': timeline[-1]['model_power_integral_wh'] if timeline else None,
        'battery_remaining_final_pct': timeline[-1]['battery_remaining_pct'] if timeline else None,
        'zero_remaining_reported_during_run': any(x['battery_remaining_pct'] == 0 for x in timeline),
        'interpretation': 'AP_BattMonitor SITL and WingMPET use different energy models; do not interpret either as calibrated hardware energy or MPC energy feasibility.'}
    l4 = {'schema': 'TDOC-native-online-layer4-v1', 'candidate_records': len(candidates),
        'decision_points': len(rows), 'feasible_ratio': float(cdf.feasible.mean()) if not cdf.empty else None,
        'verified_ratio': None, 'pareto_rank_1_ratio': None, 'FF_certificate_available': False,
        'scalar_cost_statistics': stats([c['scalar_cost'] for c in candidates]),
        'FMU_feasible_false_cycles': sum(x.get('FMU_vtol_feasible') == 0 for x in timeline),
        'semantics': 'limited reduced-model feasibility; differs from physical FMU vtol_feasible'}
    metrics = {'schema': 'TDOC-native-online-accuracy-efficiency-v1', 'actual_destination_error_m': result_goal,
        'navigation_horizontal_error_m': stats([x['navigation_position_error_horizontal_m'] for x in timeline]),
        'navigation_velocity_error_m_s': stats([x['navigation_velocity_error_m_s'] for x in timeline]),
        'L7_sensor_prediction_position_residual_m': stats([x['l7_position_residual_m'] for x in timeline]),
        'L7_sensor_prediction_velocity_residual_m_s': stats([x['l7_velocity_residual_m_s'] for x in timeline]),
        'cycle_wall_s': stats([x['cycle_wall_s'] for x in timeline]),
        'source_interval_s': stats([x['source_interval_s'] for x in timeline]),
        'cycles_over_100ms': sum(x['cycle_wall_s'] > .1 for x in timeline),
        'realized_route_distance_m': route_length, 'duration_s': duration,
        'mean_route_speed_m_s': route_length / duration if duration else None,
        'energy_used_wh': summary['energy_used_wh'],
        'wh_per_km': summary['energy_used_wh'] / (route_length / 1000.) if summary['energy_used_wh'] is not None and route_length else None,
        'physical_truth_used_for_control': False,
        'prediction_residual_is_sensor_domain_not_truth': True}
    perf = performance(timeline, result, qualification, final_wire, summary)
    export_route_details(out, df, perf)
    terrain, terrain_grid, terrain_route = build_terrain(out, evidence, timeline, rows, result, config)
    availability['terrain_visualization'] = {'available': terrain['available'],
        'source': terrain.get('source_raster'), 'reason': terrain.get('reason'), 'used_for_control': False}
    summary['landing_energy_time'] = perf
    summary['terrain_visualization_available'] = terrain['available']
    phases = list(dict.fromkeys(x['phase'] for x in timeline))
    phase_metrics = {'schema': 'TDOC-native-online-vtol-phase-v1', 'phases_seen': phases,
        'phase_counts': dict(Counter(x['phase'] for x in timeline)),
        'takeoff_physical': qualification['checks'].get('actual_takeoff'),
        'contact_physical': qualification['checks'].get('physical_contact_and_stationary'),
        'upright_physical': qualification['checks'].get('upright_after_landing'),
        'disarmed': qualification['checks'].get('FCU_disarmed'),
        'all_11_original_control_phases_certified': False,
        'startup_and_DISARM_are_lifecycle_events_not_invented_control_cycles': True}
    mpc = {'schema': 'TDOC-native-online-MPC-participation-v1', 'cycle_count': len(rows),
        'layer4_invoked': len(rows), 'layer5_invoked': len(rows),
        'cycles_with_multiple_candidates': sum(x['l4_candidate_count'] > 1 for x in timeline),
        'cycles_with_one_candidate': sum(x['l4_candidate_count'] == 1 for x in timeline),
        'FINAL_guard_passed': sum(x['l5_final_gate'] is True for x in timeline),
        'L6_intake_accepted': sum(x['l6_intake_ok'] is True for x in timeline),
        'L7_accepted': qualification['accepted_feedbacks'], 'L7_consumed_unique': qualification['consumed_feedbacks'],
        'semantics': 'realtime finite-candidate MPC/guards, not full legacy qLPV/PSF certification'}
    wind = {'schema': 'TDOC-native-online-wind-context-v1', 'samples': len(rows),
        'counterfactual_available': False, 'wind_causal_effect_verified': False,
        'max_counterfactual_displacement_m': None, 'final_counterfactual_displacement_m': None,
        'model_wind_speed_m_s': stats([x['wind_speed_m_s'] for x in timeline]),
        'interpretation': 'Recorded climate model and tracking errors; no causal no-wind comparison is fabricated'}
    offline_root = config.package_root / 'reports/offline/mission' if config else None
    prior_summary = load_json(offline_root / 'MISSION_SUMMARY.json') if offline_root else {}
    prior = prior_summary.get('reference_route') or {}
    prior_context = {'schema': 'TDOC-native-online-prior-context-v1',
        'scenario_id': prior.get('scenario_id'), 'crp_id': prior.get('crp_id'),
        'offline_cycles': prior_summary.get('cycles_completed'),
        'source': 'existing Offline prior metadata, not Online scenario selection',
        'Online_context_classification_available': False}
    analytics = {'schema': 'TDOC-native-online-mission-analytics-v1', 'summary': summary,
        'layer4': l4, 'accuracy_efficiency': metrics, 'mpc_participation': mpc,
        'vtol': phase_metrics, 'wind': wind, 'sarg_crp': prior_context,
        'landing_energy_time': perf, 'terrain': terrain}
    for name, value in {'MISSION_SUMMARY.json': summary, 'MISSION_ANALYTICS.json': analytics,
        'LAYER4_OPTIMIZATION_SUMMARY.json': l4, 'DECISION_ACCURACY_EFFICIENCY.json': metrics,
        'MPC_PARTICIPATION.json': mpc, 'VTOL_PHASE_METRICS.json': phase_metrics,
        'WIND_CAUSALITY_METRICS.json': wind, 'DATA_AVAILABILITY.json': availability,
        'ENERGY_DATA_QUALITY.json': energy_quality,
        'LANDING_ENERGY_TIME_METRICS.json': perf,
        'NATIVE_QUALIFICATION.json': qualification, 'OFFLINE_PRIOR_CONTEXT.json': prior_context}.items():
        write_json(out / name, clean(value))
    pd.DataFrame(perf['phases'], columns=['phase', 'start_s', 'end_s', 'duration_s', 'samples',
        'telemetry_energy_covered_wh', 'energy_coverage_s', 'energy_coverage_fraction', 'energy_used_wh',
        'Wing_model_energy_wh']).to_csv(out / 'PHASE_PERFORMANCE.csv', index=False)
    df.to_csv(out / 'MISSION_TIMELINE.csv', index=False)
    cdf.to_csv(out / 'LAYER4_CANDIDATES.csv', index=False)
    if timeline:
        groups = {'PHASE_HISTORY.csv': ['cycle', 'flight_elapsed_s', 'phase', 'phase_id', 'l5_mode', 'l6_mode_observed', 'l6_armed_observed'],
            'ROUTE_HISTORY.csv': [k for k in df if any(v in k for v in ('north', 'east', 'height', 'lon_deg', 'lat_deg', 'goal_distance', 'position_error', 'altitude'))],
            'CLIMATE_HISTORY.csv': [k for k in df if any(v in k for v in ('wind_', 'density', 'climate_', 'temperature', 'humidity', 'pressure_pa'))],
            'ENERGY_HISTORY.csv': [k for k in df if any(v in k for v in ('power', 'energy', 'battery', 'SOC'))],
            'ACTUATOR_HISTORY.csv': [k for k in df if any(v in k for v in ('servo', 'pilot_cmd', 'motor_command', 'l5_mode', 'l6_mode', 'l6_armed', 'rate_rad', 'vtol_'))]}
        for name, columns in groups.items():
            columns = list(dict.fromkeys(['cycle', 'flight_elapsed_s'] + columns))
            df[columns].to_csv(out / name, index=False)
    else:
        for name in ('PHASE_HISTORY.csv', 'ROUTE_HISTORY.csv', 'CLIMATE_HISTORY.csv', 'ENERGY_HISTORY.csv', 'ACTUATOR_HISTORY.csv'):
            pd.DataFrame(columns=['cycle', 'flight_elapsed_s']).to_csv(out / name, index=False)
    pd.DataFrame(pulses, columns=['cycle', 'flight_elapsed_s', 'layer', 'transaction_id', 'status', 'semantics']).to_csv(out / 'EXTERNAL_LAYER_RETURN_PULSES.csv', index=False)
    pd.DataFrame([prior_context]).to_csv(out / 'SARG_CRP_RELATION.csv', index=False)
    # Per-cycle scalar details, with every channel retaining its own name/frame.
    detail_frame = pd.json_normalize(details, sep='.') if details else pd.DataFrame(columns=['cycle', 'transaction_id'])
    detail_frame.drop(columns=[k for k in detail_frame.columns
        if k.endswith('candidate_summaries') or k.endswith('selected_original_prediction_tube')], errors='ignore').to_csv(out / 'MISSION_DETAIL_TIMESERIES.csv', index=False)
    for name, value in [('DECISION_POINT_DETAILS.json.gz', details), ('EXTERNAL_LAYER_RETURN_PULSES.json.gz', pulses)]:
        with gzip.open(out / name, 'wt', encoding='utf-8') as stream:
            json.dump(clean(value), stream, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if (source / 'cycles.jsonl').exists():
        with (source / 'cycles.jsonl').open('rb') as src, gzip.open(out / 'RAW_CYCLE_RECORDS.jsonl.gz', 'wb') as dst:
            shutil.copyfileobj(src, dst)
    else:
        with gzip.open(out / 'RAW_CYCLE_RECORDS.jsonl.gz', 'wb'):
            pass
    assets = out / 'assets'; assets.mkdir(exist_ok=True)
    (assets / 'plotly.min.js').write_text(get_plotlyjs(), encoding='utf-8')
    index = []
    for start in range(0, len(details), 64):
        chunk = start // 64
        group = details[start:start + 64]
        payload = json.dumps(clean(group), ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', chr(92) + 'u003c')
        (assets / ('details_%04d.js' % chunk)).write_text('window.TDDT_DETAIL_CHUNKS[' + str(chunk) + ']=' + payload + ';', encoding='utf-8')
        index.extend({'cycle': d['cycle'], 'transaction_id': d['transaction_id'], 'chunk': chunk, 'offset': i}
                     for i, d in enumerate(group))
    (assets / 'explorer_data.js').write_text('window.TDDT_DETAIL_CHUNKS={};window.TDDT_CYCLE_INDEX=' + json.dumps(index, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False) + ';', encoding='utf-8')
    _render(out, df, cdf, summary, metrics, l4, phase_metrics, wind, prior_context, energy_quality,
            perf, terrain, terrain_grid, terrain_route)
    build_network_report(out, rows, summary, _page, PAGES['LAYER3_DYNAMIC_NETWORK_REPORT.html'])
    equivalence = _coverage(out, offline_root)
    write_json(out / 'REPORT_EQUIVALENCE.json', equivalence)
    write_json(out / 'CSV_SCHEMA.json', {'columns': list(df.columns),
        'physical_fields': 'independent validation only', 'navigation_fields': 'MESA current sensor state',
        'nulls': 'unavailable, never auto-zero-filled', 'power': summary['energy_source']})
    artifacts = [{'path': p.relative_to(out).as_posix(), 'bytes': p.stat().st_size, 'sha256': file_hash(p)}
                 for p in sorted(out.rglob('*')) if p.is_file() and p != out / 'REPORT_MANIFEST.json']
    write_json(out / 'REPORT_MANIFEST.json', {'schema': 'TDOC-native-online-report-manifest-v1',
        'native_session_id': result.get('native_session_id'), 'model_id': result.get('model_id'),
        'source_files': {p.name: {'path': str(p), 'sha256': file_hash(p)} for p in
            (source / 'cycles.jsonl', source / 'telemetry_wire.jsonl', source / 'MISSION_RESULT.json') if p.exists()},
        'report_generator_sources': {p.name: file_hash(p) for p in Path(__file__).parent.glob('*.py')},
        'terrain_source': {k: terrain.get(k) for k in ('source_raster', 'source_sha256', 'binding_sha256_verified')},
        'files': artifacts, 'qualification_status': qualification['status'],
        'all_offline_artifact_names_covered': equivalence['all_offline_artifact_names_covered']})
    return summary


def _terrain_figure(df, terrain, grid, terrain_route, summary):
    """Plot original MSL values; never drape or raise a route to hide DEM mismatch."""
    fig = go.Figure()
    if grid:
        fig.add_trace(go.Surface(x=grid['east_m'], y=grid['north_m'], z=grid['elevation_msl_m'],
            name='ارتفاعات زمین DEM', colorscale='Earth', opacity=.87, connectgaps=False,
            showlegend=True, colorbar={'title': 'DEM [m MSL]', 'x': 1.02, 'len': .65},
            contours={'z': {'show': True, 'color': '#6d7761', 'width': 1}},
            hovertemplate='E=%{x:.1f} m<br>N=%{y:.1f} m<br>DEM=%{z:.2f} m MSL<extra>DEM</extra>'))
    seen = set()
    for a, b, phase in phase_segments(df):
        # Keep repeated occurrences separate; include the next boundary point.
        q = df.iloc[a:min(b+1, len(df))]
        fig.add_trace(go.Scatter3d(x=q.physical_east_m, y=q.physical_north_m,
            z=q.physical_altitude_msl_m, mode='lines', line={'color': PHASE_COLORS.get(phase, '#13cce4'), 'width': 6},
            name=PHASE_LABELS.get(phase, phase), showlegend=phase not in seen, legendgroup='phase-'+phase,
            customdata=q[['flight_elapsed_s', 'cycle']],
            hovertemplate='E=%{x:.2f} m<br>N=%{y:.2f} m<br>CG=%{z:.2f} m MSL<br>t=%{customdata[0]:.2f} s<br>cycle=%{customdata[1]}<extra>%{fullData.name}</extra>'))
        seen.add(phase)
    origin_msl = terrain.get('origin_altitude_msl_m')
    if origin_msl is not None:
        fig.add_trace(go.Scatter3d(x=df.navigation_east_m, y=df.navigation_north_m,
            z=origin_msl-df.navigation_down_m, mode='lines', line={'color': '#ffffff', 'width': 3, 'dash': 'dash'},
            visible='legendonly', name='تخمین MESA در مرجع MSL مبدأ'))
    points = df[['physical_east_m', 'physical_north_m', 'physical_altitude_msl_m']].dropna()
    if not points.empty:
        for point, label, color in [(points.iloc[0], 'آغاز ثبت مسیر', '#ffffff'), (points.iloc[-1], 'آخرین چرخه پس از فرود', '#ff4f64')]:
            fig.add_trace(go.Scatter3d(x=[point.iloc[0]], y=[point.iloc[1]], z=[point.iloc[2]],
                mode='markers', marker={'size': 6, 'color': color}, name=label))
    if terrain.get('origin_lonlat') and summary.get('goal_lonlat') and terrain.get('goal_terrain_elevation_msl_m') is not None:
        bearing, _, distance = GEOD.inv(*terrain['origin_lonlat'], *summary['goal_lonlat'])
        fig.add_trace(go.Scatter3d(x=[distance*math.sin(math.radians(bearing))], y=[distance*math.cos(math.radians(bearing))],
            z=[terrain['goal_terrain_elevation_msl_m']], mode='markers', marker={'size': 7, 'color': '#ee35f1', 'symbol': 'diamond'},
            name='مختصات مقصد روی DEM؛ مرجع افقی'))
    span = max(df.physical_east_m.max()-df.physical_east_m.min(), df.physical_north_m.max()-df.physical_north_m.min(), 1.)
    arrows, _, _ = wind_traces(wind_samples(df), sampled_indices(df), span_m=span)
    for trace in arrows:
        fig.add_trace(trace)
    fig.update_layout(title='نقشهٔ زمین و مسیر مراحل پرواز؛ ارتفاع‌های اصلی، بدون جابه‌جایی یا اغراق',
        template='plotly_dark', height=1050, margin={'l': 15, 'r': 150, 't': 90, 'b': 180},
        legend={'orientation': 'h', 'entrywidth': 350, 'entrywidthmode': 'pixels',
            'x': 0, 'y': -.03, 'yanchor': 'top'}, scene={'xaxis_title': 'East [m]', 'yaxis_title': 'North [m]',
            'zaxis_title': 'Declared MSL [m]', 'aspectmode': 'data', 'camera': {'eye': {'x': 1.3, 'y': -1.6, 'z': 1.1}}})
    return fig


def _render(out, df, cdf, summary, metrics, l4, phases, wind, prior, energy_quality,
            perf, terrain, terrain_grid, terrain_route):
    if df.empty:
        for name, title in PAGES.items():
            _page(out / name, title, [_table_figure(summary, 'اجرای فاقد چرخهٔ کنترل')], summary,
                  '<p>هیچ دادهٔ پروازی ساخته یا با اجرای قبلی جایگزین نشده است.</p>')
        return
    route = go.Figure()
    route.add_trace(go.Scatter(x=df.physical_east_m, y=df.physical_north_m, name='مسیر فیزیکی ـ فقط ارزیابی', mode='lines'))
    route.add_trace(go.Scatter(x=df.navigation_east_m, y=df.navigation_north_m, name='تخمین MESA ـ ورودی کنترل', mode='lines'))
    route.update_layout(title='مقایسهٔ مسیر فیزیکی و ناوبری', xaxis_title='East [m]', yaxis_title='North [m]', template='plotly_dark')
    graphs = [route,
        _lines(df, ['navigation_height_above_launch_m', 'physical_height_above_launch_CG_m'], 'ارتفاع نسبت به آغاز؛ ارتفاع CG و بارومتر یک مشاهده نیستند', unit='m'),
        _lines(df, ['actual_goal_distance_m', 'estimated_goal_distance_m', 'navigation_position_error_horizontal_m'], 'فاصلهٔ مقصد و خطای ناوبری', unit='m'),
        _lines(df, ['wind_n_m_s', 'wind_e_m_s', 'wind_d_m_s'], 'MicroMet جاری ـ مدل اقلیم', unit='m/s'),
        _lines(df, ['l5_pilot_cmd_0', 'l5_pilot_cmd_1', 'l5_pilot_cmd_2', 'l5_pilot_cmd_3'], 'فرمان pilot-domain لایهٔ ۵'),
        _lines(df, ['cycle_wall_s', 'source_interval_s', 'climate_model_age_s'], 'زمان‌بندی و سن مدل', unit='s')]
    _page(out / 'MISSION_MANAGEMENT_DASHBOARD.html', PAGES['MISSION_MANAGEMENT_DASHBOARD.html'], graphs, summary,
          '<p><a href="REPORT_EQUIVALENCE.json">تطبیق با تمام گزارش‌های آفلاین</a> · <a href="DATA_AVAILABILITY.json">داده‌های موجود و ناموجود</a></p>')
    f3 = _terrain_figure(df, terrain, terrain_grid, terrain_route, summary)
    if not cdf.empty:
        q = cdf[cdf.selected].iloc[::max(1, len(df) // 120)]
        # Use the same surveyed CG altitude frame, never height-above-launch on MSL terrain.
        origin_msl = terrain.get('origin_altitude_msl_m')
        if origin_msl is not None:
            f3.add_trace(go.Scatter3d(x=q.endpoint_e_m, y=q.endpoint_n_m, z=origin_msl-q.endpoint_down_m,
                mode='markers', marker={'size': 2}, visible='legendonly', name='endpoint پیش‌بینی منتخب؛ نمونه‌برداری نمایشی'))
    route_wind = wind_samples(df)
    phase_details = pd.read_csv(out / 'ROUTE_PHASE_DETAILS.csv').to_dict('records')
    _page(out / 'MISSION_ROUTE_3D.html', PAGES['MISSION_ROUTE_3D.html'],
        [f3, phase_viewer(df, route_wind, terrain), wind_figure(route_wind), route], summary,
        '<p class="badge">' + html.escape(terrain['warning_fa']) + '</p><p>محورهای افقی: متر نسبت به مبدأ؛ محور عمودی: MSL اعلام‌شده. '
        'سطح زمین و مسیر فیزیکی با مقادیر اصلی رسم شده‌اند؛ سطح زمین حسگر تازه یا ورودی کنترل نیست. '
        'با کلیک راهنما، زمین، هر فاز و تخمین MESA را روشن/خاموش کنید. نمودار سه‌بعدی به WebGL مرورگر نیاز دارد.</p>'
        '<p><a href="TERRAIN_CONTEXT.json">منبع و hash زمین</a> · <a href="TERRAIN_SURFACE.json">شبکهٔ ارتفاعات</a> · '
        '<a href="TERRAIN_ROUTE_HISTORY.csv">ارتفاع مسیر و DEM در هر چرخه</a> · '
        '<a href="ROUTE_PHASE_DETAILS.csv">جزئیات مراحل</a> · <a href="ROUTE_WIND_HISTORY.csv">باد تمام چرخه‌ها</a> · '
        '<a href="ROUTE_DETAIL_CONTEXT.json">تعریف جهت باد و دامنهٔ گزارش</a></p>'
        '<p><a href="#route-section-0">نقشهٔ کل مسیر</a> · <a href="#route-section-1">بزرگ‌نمایی و نمودارهای هر مرحله</a> · '
        '<a href="#route-section-2">جهت و سرعت باد در زمان</a></p>'
        '<p>پیکان‌های بنفش، جهت حرکت هوا (TO) را نشان می‌دهند؛ جهت هواشناسی FROM در hover و نمودار زمانی جدا آمده است. '
        'زاویه‌ها ساعت‌گرد از شمال حقیقی‌اند: ۰ شمال، ۹۰ شرق. مؤلفهٔ Down برای نمایش Up معکوس می‌شود. '
        'طول پیکان مقیاس نمایشی دارد و جابه‌جایی پهپاد نیست. باد، خروجی مدل MicroMet همان چرخه است؛ اندازه‌گیری حسگر باد ادعا نمی‌شود.</p>'
        '<p>نمای جزئیات از برخاست شروع می‌شود؛ از فهرست بالای آن نزول، فرود نهایی و تثبیت تماس را انتخاب کنید. '
        'مبدأ محلی هر نما، موقعیت فیزیکی شروع همان مرحله است. همهٔ چرخه‌های آن بخش و چهار نمودار هم‌زمان نمایش داده می‌شوند. '
        'این بزرگ‌نمایی موقعیت یا ارتفاع خام را تغییر نمی‌دهد.</p>' + phase_table(phase_details) +
        ('' if terrain['available'] else '<p>سطح زمین ناموجود: ' + html.escape(terrain.get('reason', 'unknown')) + '</p>'),
        figure_titles=['نقشهٔ کامل سه‌بعدی و پیکان‌های باد', 'جزئیات سه‌بعدی و تله‌متری هر مرحله',
            'جهت و سرعت باد ثبت‌شده', 'مقایسهٔ مسیر دوبعدی'])
    phase_df = pd.DataFrame(perf['phases'])
    phase_fig = make_subplots(rows=1, cols=2, subplot_titles=['زمان هر مرحله [s]', 'انرژی تله‌متری روی بازهٔ دارای داده [Wh]'])
    phase_fig.add_trace(go.Bar(x=phase_df.phase, y=phase_df.duration_s, name='زمان مرحله [s]'), row=1, col=1)
    phase_fig.add_trace(go.Bar(x=phase_df.phase, y=phase_df.energy_used_wh, name='انرژی مرحله [Wh]'), row=1, col=2)
    phase_fig.update_layout(template='plotly_dark', height=530)
    landing = perf['landing_accuracy']
    readable = {'خطای افقی فرود نسبت به مقصد [m]': landing['actual_horizontal_error_m'],
        'تلرانس مقصد [m]': landing['tolerance_m'], 'اختلاف شمالی محل فرود با مقصد [m]': landing['target_to_actual_north_m'],
        'اختلاف شرقی محل فرود با مقصد [m]': landing['target_to_actual_east_m'],
        'زمان ثبت‌شدهٔ حلقهٔ کنترل [s]': perf['time']['control_duration_s'],
        'زمان ثبت‌شدهٔ حلقهٔ کنترل [min]': perf['time']['control_duration_min'],
        'انرژی مصرفی SITL روی بازهٔ کنترل [Wh]': perf['energy']['consumed_wh'],
        'انرژی مصرفی SITL روی بازهٔ کنترل [J]': perf['energy']['consumed_j'],
        'پوشش زمانی دادهٔ انرژی': perf['energy']['coverage_fraction'],
        'خاموش‌بودن GPS تأییدشده': landing['GNSS_disabled_verified'],
        'ورودی‌های ناوبری مجاز تأییدشده': landing['approved_navigation_inputs_verified']}
    _page(out / 'LANDING_ENERGY_TIME_REPORT.html', PAGES['LANDING_ENERGY_TIME_REPORT.html'],
        [_table_figure(readable, 'خلاصهٔ دقت فرود، زمان و انرژی'), phase_fig,
         _lines(df, ['actual_goal_distance_m', 'estimated_goal_distance_m'], 'فاصلهٔ فیزیکی و تخمینی تا مقصد', unit='m'),
         _lines(df, ['telemetry_energy_integral_covered_wh', 'model_power_integral_wh'], 'انرژی تجمعی؛ تله‌متری SITL و مدل Wing جدا هستند', unit='Wh')], summary,
        '<p>دقت فرود، فاصلهٔ افقی مقصد تا آخرین SIM_STATE پس از فرود است؛ این داده فقط مرجع ارزیابی است. '
        'خطای عمودی فرود به علت نبود مرجع مستقل معتبر گزارش نمی‌شود. زمان از اولین تا آخرین چرخه در ساعت FCU است؛ '
        'راه‌اندازی، کالیبراسیون و خاموش‌سازی را شامل نمی‌شود. انرژی، انتگرال ولتاژ × جریان باتری مدل SITL در همین بازه است؛ '
        'اندازه‌گیری انرژی سخت‌افزار نیست. مجموع کامل انرژی تنها با پوشش حداقل ۹۵٪ ارائه می‌شود.</p>'
        '<p><a href="LANDING_ENERGY_TIME_METRICS.json">شاخص‌ها و تعریف دقیق بازه‌ها</a> · '
        '<a href="PHASE_PERFORMANCE.csv">زمان و انرژی تفکیکی مراحل</a> · <a href="ENERGY_DATA_QUALITY.json">کیفیت مدل باتری</a></p>')
    f2 = go.Figure(); fcost = go.Figure()
    if not cdf.empty:
        shown = cdf.iloc[::max(1, math.ceil(len(cdf) / 12000))]
        f2.add_trace(go.Scatter(x=shown.acceleration_e_m_s2, y=shown.acceleration_n_m_s2,
            mode='markers', marker={'size': 3, 'color': shown.scalar_cost, 'colorscale': 'Viridis', 'showscale': True},
            text=shown.candidate_id, name='نمایش نمونه‌برداری‌شده؛ همهٔ candidateها در CSV'))
        q = cdf[cdf.selected]
        fcost.add_trace(go.Scatter3d(x=q.acceleration_e_m_s2, y=q.acceleration_n_m_s2, z=q.scalar_cost,
                                   mode='markers', marker={'size': 2}, name='انتخاب‌شده‌ها'))
    f2.update_layout(title='فضای شتاب و هزینهٔ scalar', template='plotly_dark', xaxis_title='a_E [m/s²]', yaxis_title='a_N [m/s²]')
    fcost.update_layout(title='شتاب candidate منتخب و cost؛ Pareto محاسبه نشده است', template='plotly_dark', height=800)
    _page(out / 'LAYER4_OPTIMIZATION_2D.html', PAGES['LAYER4_OPTIMIZATION_2D.html'], [f2], summary)
    _page(out / 'LAYER4_OPTIMIZATION_3D.html', PAGES['LAYER4_OPTIMIZATION_3D.html'], [fcost], summary)
    _page(out / 'LAYER4_OPTIMIZATION_TIMELINE.html', PAGES['LAYER4_OPTIMIZATION_TIMELINE.html'],
          [_lines(df, ['l4_scalar_cost'], 'cost گزینهٔ منتخب'),
           _lines(df, ['l4_candidate_count', 'l4_feasible_count', 'FMU_vtol_feasible', 'FMU_quality_score'], 'تعریف feasibility مدل کاهش‌یافته و FMU جداست')], summary)
    _page(out / 'DECISION_ACCURACY_EFFICIENCY_REPORT.html', PAGES['DECISION_ACCURACY_EFFICIENCY_REPORT.html'],
          [_table_figure(metrics, 'شاخص‌های مستقل و حسگرمحور'),
           _lines(df, ['navigation_position_error_horizontal_m', 'navigation_sigma_n_m', 'navigation_sigma_e_m'], 'خطا و covariance گزارش‌شده؛ مصرف آن در MPC ادعا نمی‌شود', unit='m'),
           _lines(df, ['telemetry_power_w', 'predicted_power_w'], 'توان SITL و توان مدل؛ دو منبع متفاوت', unit='W'),
           _lines(df, ['telemetry_energy_integral_covered_wh', 'model_power_integral_wh'], 'انتگرال توان روی بازهٔ دارای داده', unit='Wh'),
           _table_figure(energy_quality, 'دامنهٔ اعتبار دادهٔ انرژی؛ بودجهٔ انرژی تأیید نشده است')], summary)
    _page(out / 'VTOL_PHASE_DASHBOARD.html', PAGES['VTOL_PHASE_DASHBOARD.html'],
          [_table_figure(phases, 'پوشش فاز واقعی؛ همهٔ ۱۱ فاز ادعا نشده‌اند'),
           _lines(df, ['phase_id'], 'فازها'),
           _lines(df, ['physical_height_above_launch_CG_m', 'navigation_height_above_launch_m'], 'صعود و نزول', unit='m'),
           _lines(df, ['physical_vtol_roll_deg', 'physical_vtol_pitch_deg'], 'وضعیت فیزیکی بدنه ـ فقط ارزیابی', unit='deg')], summary)
    _page(out / 'WIND_CAUSALITY_REPORT.html', PAGES['WIND_CAUSALITY_REPORT.html'],
          [_table_figure(wind, 'محدودیت استنباط'), _lines(df, ['wind_n_m_s', 'wind_e_m_s', 'wind_d_m_s'], 'باد مدل', unit='m/s'),
           _lines(df, ['navigation_position_error_horizontal_m', 'l7_position_residual_m'], 'خطا/باقیمانده؛ همبستگی اثبات علیت نیست', unit='m')], summary,
          '<p class="badge">آزمایش خلاف‌واقع بدون باد اجرا نشده؛ هیچ منحنی no-wind یا اثر علّیِ ساختگی وجود ندارد.</p>')
    if prior['scenario_id'] is not None and prior['crp_id'] is not None:
        labels = ['prior آفلاین', 'scenario ' + str(prior['scenario_id']), 'CRP ' + str(prior['crp_id'])]
        tree = go.Figure(go.Treemap(labels=labels, parents=['', labels[0], labels[1]], values=[1, 1, 1], branchvalues='total'))
    else:
        tree = go.Figure()
        tree.add_annotation(text='شناسهٔ SARG/CRP در metadata ورودی موجود نیست.', showarrow=False)
    tree.update_layout(title='صرفاً شناسهٔ prior؛ مساحت‌ها تعداد بازدید Online نیستند', template='plotly_dark')
    _page(out / 'SARG_CRP_TREEMAP.html', PAGES['SARG_CRP_TREEMAP.html'], [tree, _table_figure(prior, 'دامنهٔ context')], summary)
    sensor_figs = [
        _lines(df, ['sensor_xgyro', 'sensor_ygyro', 'sensor_zgyro'], 'gyro حسگر', unit='rad/s'),
        _lines(df, ['sensor_xacc', 'sensor_yacc', 'sensor_zacc'], 'شتاب ویژه', unit='m/s²'),
        _lines(df, ['sensor_xmag', 'sensor_ymag', 'sensor_zmag'], 'مغناطیس‌سنج', unit='gauss'),
        _lines(df, ['sensor_pressure_alt'], 'ارتفاع بارومتری', unit='m MSL'),
        _lines(df, ['airspeed_m_s'], 'pitot اسکالر', unit='m/s'),
        _lines(df, ['servo1_pwm_us', 'servo2_pwm_us', 'servo3_pwm_us', 'servo4_pwm_us'], 'PWM واقعی خروجی FCU', unit='µs'),
        _lines(df, ['battery_voltage_v', 'battery_current_a', 'battery_remaining_pct'], 'تله‌متری جانبی باتری SITL؛ واحد هر سری در نام')]
    _page(out / 'SENSORS_ACTUATORS_REPORT.html', PAGES['SENSORS_ACTUATORS_REPORT.html'], sensor_figs, summary)
    _page(out / 'LAYER7_FEEDBACK_REPORT.html', PAGES['LAYER7_FEEDBACK_REPORT.html'],
          [_lines(df, ['feedback_version'], 'نسخهٔ مدل پس از مصرف بازخورد'),
           _lines(df, ['l7_position_residual_m', 'l7_velocity_residual_m_s'], 'residual در زمان prediction اصلی؛ واحد هر سری متفاوت است'),
           _lines(df, ['disturbance_n_m_s2', 'disturbance_e_m_s2', 'disturbance_d_m_s2'], 'پارامترهای disturbance مدل', unit='m/s²')], summary)
    images = ''
    for manifest in summary['qualification']['real_window_manifests']:
        m = load_json(manifest)
        images += '<details><summary>' + html.escape(Path(manifest).parent.name) + '</summary>'
        for name, value in m['windows'].items():
            rel = os.path.relpath(value['image'], out)
            images += '<p>' + html.escape(name) + '</p><a href="' + html.escape(rel, quote=True) + '"><img loading="lazy" src="' + html.escape(rel, quote=True) + '"></a>'
        images += '</details>'
    _page(out / 'EXECUTION_EVIDENCE_REPORT.html', PAGES['EXECUTION_EVIDENCE_REPORT.html'],
          [_table_figure(summary['qualification']['checks'], 'چک‌های واقعی پس از پرواز'),
           _table_figure(summary['preservation'], 'حفظ مدل، prior، forcing و گزارش آفلاین')], summary, images)


def publish_latest(base, run, summary):
    base, run = Path(base).resolve(), Path(run).resolve()
    for name, target in (('latest', run), ('mission', run / 'mission'), ('evidence', run / 'evidence')):
        dest = base / name
        if dest.exists() and not dest.is_symlink():
            raise ValueError('Refusing to overwrite an existing non-symlink report path: ' + str(dest))
        temp = base / ('.' + name + '_' + uuid.uuid4().hex)
        temp.symlink_to(os.path.relpath(target, base), target_is_directory=True)
        os.replace(temp, dest)
    write_json(base / 'ONLINE_JSBSIM_SUMMARY.json', summary)
    write_json(base / 'ONLINE_VALIDATION.json', summary['qualification'])
    write_json(base / 'ONLINE_FLIGHT_PERFORMANCE.json', load_json(run / 'mission/DECISION_ACCURACY_EFFICIENCY.json'))
    alias = base / 'ONLINE_FLIGHT_PERFORMANCE.html'
    if alias.exists() and not alias.is_symlink():
        raise ValueError('Refusing to overwrite existing performance report: ' + str(alias))
    temp = base / ('.performance_' + uuid.uuid4().hex)
    temp.symlink_to('mission/DECISION_ACCURACY_EFFICIENCY_REPORT.html')
    os.replace(temp, alias)
    links = ''.join('<li><a href="mission/' + n + '">' + html.escape(t) + '</a></li>' for n, t in PAGES.items())
    (base / 'INDEX.html').write_text('<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>گزارش‌های Online TDDT</title>'
        '<h1>گزارش‌های هدایت آنلاین Native</h1><p>نتیجهٔ آخرین اجرا: ' + summary['qualification']['status'] + '</p><ul>' + links +
        '</ul><p>اجراهای قبلی در runs حفظ می‌شوند. دادهٔ هر گزارش از همان نشست است.</p></html>', encoding='utf-8')
