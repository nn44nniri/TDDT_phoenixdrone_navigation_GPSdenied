"""Inspect recorded atomic snapshots and dependencies; never run or modify Layer3."""
from collections import Counter
import gzip
import json
import math

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .data import digest
from .provenance import write_json
from .layer32_report import attach_layer32, panel as layer32_panel

CHECKS = {
    'snapshot_hash_valid': 'بازمحاسبهٔ hash snapshot',
    'snapshot_atomic_flag': 'پرچم اتمیک اعلام‌شده',
    'three_twins_present': 'حضور سه Twin',
    'couplings_declared': 'وجود وابستگی‌های اعلام‌شدهٔ شبکه',
    'L2_packets_match_snapshot': 'برابری بسته‌های L2 با snapshot',
    'navigation_wing_clock_aligned': 'هم‌زمانی منبع MESA/Wing',
    'climate_binding_matches': 'اتصال forcing و زمان اقلیم به Wing',
    'climate_age_within_profile': 'سن اقلیم در محدودهٔ پروفایل Native',
    'transactions_match': 'همسانی transaction لایه‌های ۳ و ۵',
    'snapshot_consumed_by_L4_L5': 'مصرف همین snapshot در L4/L5',
    'candidate_snapshot_binding': 'اتصال candidateها به snapshot',
    'approved_navigation_flags': 'پرچم‌های ناوبری مجاز',
    'feedback_route_matches_navigation': 'برابری مسیر بازخورد L3/MESA',
    'feedback_model_binding': 'تطبیق بازخورد قبلی با پارامترهای مدل',
}


def _same(*values):
    return None if any(v is None for v in values) else all(v == values[0] for v in values[1:])


def _all_checks(*values):
    if any(v is False for v in values):
        return False
    return None if any(v is None for v in values) else True


def _difference(a, b):
    return float(a)-float(b) if a is not None and b is not None else None


def _status(checks):
    if any(v is False for v in checks.values()):
        return 'FAIL'
    return 'BLOCKED' if any(v is None for v in checks.values()) else 'PASS'


def inspect_snapshots(rows):
    """Audit time, content and causal model feedback independently of stored PASS."""
    timeline, edges, payloads = [], [], []
    known_feedback, applied = {}, set()
    previous_route, previous_version, previous_topology = None, 0, None
    topology_changes = 0
    start = (rows[0].get('L3') or {}).get('time_s') if rows else None
    for cycle, r in enumerate(rows, 1):
        s = r.get('L3') or {}; twins = s.get('twins') or {}
        nav = twins.get('MESA_NAV') or {}; wing = twins.get('WingMPET') or {}; climate = twins.get('MicroMetTwin') or {}
        l2, l4, l5 = r.get('L2') or {}, r.get('L4') or {}, r.get('L5') or {}
        model = wing.get('reduced_inner_loop') or {}
        couplings = s.get('couplings') if isinstance(s.get('couplings'), list) else []
        topology = digest(sorted([(e.get('source'), e.get('target')) for e in couplings])) if 'couplings' in s else None
        if previous_topology is not None and topology != previous_topology:
            topology_changes += 1
        previous_topology = topology
        climate_age = _difference(s.get('time_s'), climate.get('time_s'))
        wing_delta = _difference(wing.get('time_s'), nav.get('time_s'))
        checks = {
            'snapshot_hash_valid': digest({k: v for k, v in s.items() if k != 'snapshot_hash'}) == s['snapshot_hash'] if s.get('snapshot_hash') else None,
            'snapshot_atomic_flag': s.get('snapshot_atomic') is True if 'snapshot_atomic' in s else None,
            'three_twins_present': set(twins) == {'MESA_NAV', 'WingMPET', 'MicroMetTwin'} if 'twins' in s else None,
            'couplings_declared': bool(couplings) if 'couplings' in s else None,
            'L2_packets_match_snapshot': all(twins.get(name) == l2[key] for name, key in
                [('MESA_NAV', 'navigation'), ('WingMPET', 'wing'), ('MicroMetTwin', 'climate')]) if all(k in l2 for k in ('navigation', 'wing', 'climate')) and twins else None,
            'navigation_wing_clock_aligned': abs(wing_delta) <= 1e-9 and _same(nav.get('time_s'), s.get('time_s')) if wing_delta is not None else None,
            'climate_binding_matches': (_same(wing.get('climate_input_time_s'), climate.get('time_s')) and
                _same(wing.get('climate_input_forcing_sha256'), climate.get('forcing_sha256'))) if all(wing.get(k) is not None for k in ('climate_input_time_s', 'climate_input_forcing_sha256')) and climate.get('forcing_sha256') else None,
            'climate_age_within_profile': 0 <= climate_age <= 3. if climate_age is not None else None,
            'transactions_match': _same(r.get('transaction_id'), s.get('transaction_id'), l5.get('transaction_id')),
            'snapshot_consumed_by_L4_L5': (_same(s.get('snapshot_hash'), l4.get('snapshot_hash'), l5.get('snapshot_hash')) and
                _same(s.get('time_s'), l4.get('source_time_s'))),
            'candidate_snapshot_binding': all(c.get('source_snapshot_hash') == s.get('snapshot_hash') for c in l4['candidates']) and
                l5.get('prediction') in l4['candidates'] if l4.get('candidates') and s.get('snapshot_hash') else None,
            'approved_navigation_flags': all(nav[k] is False for k in ('truth_used', 'GNSS_used', 'odometry_used', 'vision_used')) if all(k in nav for k in ('truth_used', 'GNSS_used', 'odometry_used', 'vision_used')) else None,
            'feedback_route_matches_navigation': s['feedback_route_hash'] == nav['feedback_consumed'] if 'feedback_route_hash' in s and 'feedback_consumed' in nav else None,
        }
        route, version = s.get('feedback_route_hash'), model.get('feedback_version')
        source = known_feedback.get(route)
        new = bool(route and route != previous_route)
        if route:
            model_ok = bool(source and source['cycle'] < cycle and source['hash_verified'] and source['feedback'].get('model_update') and
                source['feedback'].get('destination') == 'Layer2/model-only' and source['feedback'].get('sensor_only') is True and
                all(model.get(k) == v for k, v in source['feedback'].get('model_update', {}).items()))
            model_ok &= (previous_version is not None and version == previous_version+1 and route not in applied) if new else version == previous_version
        else:
            model_ok = version == 0 if version is not None else None
        checks['feedback_model_binding'] = model_ok
        if new:
            applied.add(route)
        previous_route, previous_version = route, version
        row = {'cycle': cycle, 'transaction_id': r.get('transaction_id'), 'phase': s.get('phase'),
            'flight_elapsed_s': _difference(s.get('time_s'), start), 'snapshot_time_s': s.get('time_s'),
            'snapshot_hash': s.get('snapshot_hash'), 'frame': s.get('frame'), 'schema': s.get('schema'),
            'navigation_authority': s.get('navigation_authority'), 'twin_count': len(twins),
            'declared_coupling_count': len(couplings), 'topology_hash': topology,
            'MESA_time_s': nav.get('time_s'), 'Wing_time_s': wing.get('time_s'), 'MicroMet_time_s': climate.get('time_s'),
            'Wing_minus_MESA_time_s': wing_delta, 'climate_age_s': climate_age,
            'forcing_sha256': climate.get('forcing_sha256'), 'Wing_forcing_sha256': wing.get('climate_input_forcing_sha256'),
            'snapshot_atomic_declared': s.get('snapshot_atomic'), 'network_integrity': _status(checks),
            'feedback_route_hash': route, 'feedback_version': version, 'feedback_newly_applied': new,
            'feedback_accepted_cycle': source['cycle'] if source else None,
            'feedback_apply_delay_cycles': cycle-source['cycle'] if new and source else None,
            'L4_candidate_count': len(l4.get('candidates') or []), 'L5_candidate_id': l5.get('candidate_id'), **checks}
        timeline.append(row)
        current_edges = []
        for number, edge in enumerate(couplings):
            src, target = edge.get('source', ''), edge.get('target', '')
            predicate, scope = None, 'declared dependency; numerical transfer not independently logged'
            if 'initial_wind_prior' in target:
                state = 'INITIAL_PRIOR_ONLY'; scope = 'initialization role, not a current navigation observation'
            else:
                if 'WingMPET.current_physical_model_inputs' in target:
                    predicate = _all_checks(checks['climate_binding_matches'], checks['L2_packets_match_snapshot']); scope = 'source time/forcing binding and L2 packet identity; FMU numeric inputs are not re-executed'
                elif 'WingMPET.current_state' in target:
                    predicate = _all_checks(checks['L2_packets_match_snapshot'], checks['navigation_wing_clock_aligned']); scope = 'recorded packet identity and clock binding; not an independent FMU input trace'
                elif target == 'Layer4.reachable_predictions':
                    predicate = _all_checks(checks['snapshot_consumed_by_L4_L5'], checks['candidate_snapshot_binding']); scope = 'downstream snapshot/candidate provenance; no assertion of all model equations'
                elif src.startswith('Layer7.'):
                    predicate = model_ok if route else None; scope = 'previous accepted route hash, model update and version'
                state = ('MATCH' if predicate is True else 'MISMATCH' if predicate is False else 'UNAVAILABLE')
                if src.startswith('Layer7.') and not route:
                    state = 'NO_FEEDBACK_YET'
            e = {'cycle': cycle, 'transaction_id': r.get('transaction_id'), 'edge_index': number,
                'source': src, 'target': target, 'evidence_status': state, 'evidence_scope': scope}
            edges.append(e); current_edges.append(e)
        feedback = r.get('L7') or {}
        payloads.append({'cycle': cycle, 'transaction_id': r.get('transaction_id'), 'snapshot': s,
            'checks': checks, 'integrity': row['network_integrity'], 'edges': current_edges,
            'trace': {'L4_snapshot_hash': l4.get('snapshot_hash'), 'L5_snapshot_hash': l5.get('snapshot_hash'),
                'candidate_count': row['L4_candidate_count'], 'selected_candidate': l5.get('candidate_id'),
                'feedback_newly_applied': new, 'previous_accepted_cycle': row['feedback_accepted_cycle'],
                'feedback_apply_delay_cycles': row['feedback_apply_delay_cycles'],
                'feedback_source_transaction': source['feedback'].get('source_transaction_id') if source else None},
            'current_L7_return': feedback, 'consumed_L7_prior': source['feedback'] if source else None})
        # Register only after auditing this snapshot: a current-cycle residual cannot be its prior.
        if feedback.get('ok') and feedback.get('route_hash'):
            known_feedback[feedback['route_hash']] = {'cycle': cycle, 'feedback': feedback,
                'hash_verified': digest({k: v for k, v in feedback.items() if k != 'route_hash'}) == feedback['route_hash']}
    tallies = {key: {'matched': sum(x.get(key) is True for x in timeline),
        'mismatched': sum(x.get(key) is False for x in timeline),
        'unavailable': sum(x.get(key) is None for x in timeline)} for key in CHECKS}
    statuses = Counter(x['network_integrity'] for x in timeline)
    summary = {'schema': 'TDOC-online-layer3-network-report-v1', 'snapshot_count': len(timeline),
        'report_integrity': 'FAIL' if statuses['FAIL'] else 'BLOCKED' if not timeline or statuses['BLOCKED'] else 'PASS',
        'snapshot_status_counts': dict(statuses), 'check_counts': tallies,
        'topology_count': len({x['topology_hash'] for x in timeline if x['topology_hash']}),
        'topology_changes': topology_changes, 'declared_edge_records': len(edges),
        'distinct_snapshots': len({x['snapshot_hash'] for x in timeline if x['snapshot_hash']}),
        'accepted_feedback_routes': len(known_feedback), 'unique_feedback_routes_applied': len(applied),
        'feedback_applies_without_prior': sum(x['feedback_newly_applied'] and x['feedback_accepted_cycle'] is None for x in timeline),
        'climate_age_profile_limit_s': 3., 'reporting_only': True,
        'not_recorded': ['adaptive edge weights', 'automatic topology rewiring', 'full legacy network/formalism certificate',
            'per-edge network transport latency', 'separate snapshot publication wall timestamp'],
        'scope': 'recorded realtime atomic three-twin graph and cross-layer provenance; no control changes',
        'climate_time_policy': 'MESA/Wing source time aligns; asynchronous MicroMet has its own time and bounded age',
        'constraints_policy': 'snapshot constraints are declarations, not proof of their complete downstream enforcement'}
    return timeline, edges, payloads, summary


def export_network(out, rows, *, mission_summary=None):
    timeline, edges, payloads, summary = inspect_snapshots(rows)
    summary.update({key: (mission_summary or {}).get(key) for key in ('native_session_id', 'model_id', 'source_directory')})
    summary['sublayer32'] = attach_layer32(out, payloads, mission_summary or {})
    pd.DataFrame(timeline, columns=list(timeline[0]) if timeline else ['cycle', 'snapshot_hash', 'network_integrity']).to_csv(out / 'LAYER3_NETWORK_TIMELINE.csv', index=False)
    pd.DataFrame(edges, columns=['cycle', 'transaction_id', 'edge_index', 'source', 'target', 'evidence_status', 'evidence_scope']).to_csv(out / 'LAYER3_NETWORK_EDGES.csv', index=False)
    write_json(out / 'LAYER3_NETWORK_SUMMARY.json', summary)
    with gzip.open(out / 'LAYER3_SNAPSHOTS.jsonl.gz', 'wt', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r.get('L3'), ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')
    assets = out / 'assets'; assets.mkdir(exist_ok=True)
    index = []
    for start in range(0, len(payloads), 64):
        bank = start//64
        chunk = payloads[start:start+64]
        data = json.dumps(chunk, ensure_ascii=False, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c')
        (assets / f'layer3_{bank:04d}.js').write_text(f'window.TDDT_L3_CHUNKS[{bank}]='+data+';', encoding='utf-8')
        index.extend({'cycle': p['cycle'], 'transaction_id': p['transaction_id'], 'phase': p['snapshot'].get('phase'),
            'time_s': p['snapshot'].get('time_s'), 'bank': bank, 'offset': i,
            'feedback_new': p['trace']['feedback_newly_applied']} for i, p in enumerate(chunk))
    (assets / 'layer3_index.js').write_text('window.TDDT_L3_CHECK_LABELS='+json.dumps(CHECKS, ensure_ascii=False)+';window.TDDT_L3_CHUNKS={};window.TDDT_L3_INDEX='+
        json.dumps(index, ensure_ascii=False, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c')+';', encoding='utf-8')
    return pd.DataFrame(timeline), payloads, summary


def network_figures(df):
    if df.empty:
        return []
    heat = go.Figure(go.Heatmap(x=df.flight_elapsed_s.tolist(), y=list(CHECKS.values()),
        z=[[1 if v is True else 0 if v is False else None for v in df[k]] for k in CHECKS],
        zmin=0, zmax=1, colorscale=[[0, '#ed6674'], [.49, '#ed6674'], [.5, '#56c8a1'], [1, '#56c8a1']],
        hovertemplate='t=%{x:.3f} s<br>%{y}<br>match=%{z}<extra></extra>', showscale=False))
    heat.update_layout(template='plotly_dark', title='کنترل پیوستگی دادهٔ شبکه؛ سبز تطبیق، قرمز ناسازگاری، خالی ناموجود',
        xaxis_title='زمان از اولین snapshot [s]', height=650, margin={'l': 310})
    clocks = go.Figure()
    for name in ('climate_age_s', 'Wing_minus_MESA_time_s'):
        clocks.add_trace(go.Scatter(x=df.flight_elapsed_s.tolist(), y=df[name].tolist(), name=name, mode='lines'))
    clocks.add_hline(y=3., line_dash='dash', annotation_text='حد سن اقلیم در پروفایل Native')
    clocks.update_layout(template='plotly_dark', title='اختلاف ساعت Twinها و سن واقعی اقلیم', xaxis_title='زمان [s]', yaxis_title='s', height=480)
    feedback = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=['نسخهٔ مدل و اعمال بازخورد تازه', 'تعداد candidateهای مصرف‌کنندهٔ snapshot'])
    feedback.add_trace(go.Scatter(x=df.flight_elapsed_s.tolist(), y=df.feedback_version.tolist(), name='نسخهٔ مدل Wing', mode='lines'), row=1, col=1)
    new = df[df.feedback_newly_applied]
    feedback.add_trace(go.Scatter(x=new.flight_elapsed_s.tolist(), y=new.feedback_version.tolist(), name='اعمال مسیر جدید', mode='markers', marker={'size': 3}), row=1, col=1)
    feedback.add_trace(go.Scatter(x=df.flight_elapsed_s.tolist(), y=df.L4_candidate_count.tolist(), name='candidate count', mode='lines'), row=2, col=1)
    feedback.update_layout(template='plotly_dark', title='پویایی پارامترهای مشترک و مصرف شبکه', height=650)
    return [heat, clocks, feedback]


NETWORK_PANEL = r'''
<script src="assets/layer3_index.js"></script>
<section id="l3-inspector"><h2>کاوشگر شبکهٔ لایهٔ ۳ در هر چرخه</h2>
<p>چرخه یا مرحله را انتخاب کنید؛ روی هر Twin کلیک کنید تا بستهٔ کامل همان snapshot نمایش داده شود.</p>
<label>چرخه <select id="l3-cycle" aria-label="چرخهٔ شبکه"></select></label>
<button id="l3-prev">چرخهٔ قبل</button><button id="l3-next">چرخهٔ بعد</button>
<button id="l3-feedback-next">اعمال بازخورد تازهٔ بعدی</button>
<label>رفتن به مرحله <select id="l3-phase"></select></label>
<input id="l3-slider" type="range" min="0" value="0" style="width:95%" aria-label="زمان شبکه">
<p id="l3-state" role="status">در حال بارگذاری دادهٔ همان اجرا…</p>
<svg id="l3-graph" viewBox="0 0 1220 650" style="width:100%;max-height:650px;background:#10202e" role="img" aria-label="شبکهٔ Twinها و وابستگی‌های اعلام‌شده"></svg>
<p>گره‌های داخل کادر: سه Twin اشتراک‌گذاشته‌شده در snapshot؛ گره‌های بیرون: وابستگی به L4 و L7. خط خاکستری نقطه‌چین = prior اولیه؛ سبز = تطبیق شواهد ثبت‌شده؛ قرمز = ناسازگاری؛ زرد = شاهد ناموجود یا هنوز بازخوردی نرسیده است. این نمودار وزن یا تأخیر انتقال ثبت‌نشده تولید نمی‌کند.</p>
<div style="overflow-x:auto"><table id="l3-meta"></table></div>
<h3>اعتبارسنجی محتوای snapshot</h3><table id="l3-checks"></table>
<h3>وابستگی‌های اعلام‌شده و حدود شواهد</h3><div style="overflow-x:auto"><table id="l3-edges"></table></div>
<label>بستهٔ انتخابی <select id="l3-role"><option value="MESA_NAV">MESA_NAV</option><option value="WingMPET">WingMPET</option><option value="MicroMetTwin">MicroMetTwin</option><option value="snapshot">snapshot کامل</option><option value="Layer3.2">جزئیات تفکیکی ۳.۲ از گزارش‌ساز</option><option value="Layer4">ارجاع‌های L4/L5</option><option value="Layer7">ورودی قبلی و خروجی جاری L7</option></select></label>
<pre id="l3-packet" style="max-height:640px;overflow:auto;white-space:pre-wrap;word-break:break-word;text-align:left"></pre></section>
<script>
document.addEventListener('DOMContentLoaded',()=>{
const index=window.TDDT_L3_INDEX||[],select=document.getElementById('l3-cycle'),slider=document.getElementById('l3-slider'),phase=document.getElementById('l3-phase'),role=document.getElementById('l3-role'),state=document.getElementById('l3-state'),packet=document.getElementById('l3-packet'),loads={};let active=null,seq=0;
const fmt=x=>x===null||x===undefined?'ناموجود':typeof x==='object'?JSON.stringify(x):String(x);
const node=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=fmt(text);return e};
function table(id,headers,rows){const t=document.getElementById(id);t.replaceChildren();if(headers){const tr=node('tr');headers.forEach(h=>tr.appendChild(node('th',h)));t.appendChild(tr)}for(const row of rows){const tr=node('tr');row.forEach(v=>{const td=node('td',v);td.style.wordBreak='break-word';tr.appendChild(td)});t.appendChild(tr)}}
index.forEach((r,i)=>{const o=node('option',r.cycle+' — '+r.phase+' — t='+r.time_s);o.value=i;select.appendChild(o)});
for(const p of [...new Set(index.map(r=>r.phase))]){const o=node('option',p);o.value=p;phase.appendChild(o)}
slider.max=Math.max(0,index.length-1);
function load(bank){if(window.TDDT_L3_CHUNKS[bank])return Promise.resolve();if(!loads[bank])loads[bank]=new Promise((ok,fail)=>{const s=node('script');s.src='assets/layer3_'+String(bank).padStart(4,'0')+'.js';s.onload=ok;s.onerror=fail;document.head.appendChild(s)});return loads[bank]}
function showPacket(){if(!active)return;const key=role.value;packet.textContent=JSON.stringify(key==='snapshot'?active.snapshot:key==='Layer3.2'?active.sublayer32:key==='Layer4'?active.trace:key==='Layer7'?{prior_consumed_by_snapshot:active.consumed_L7_prior,current_L7_return:active.current_L7_return}:active.snapshot.twins?.[key]??null,null,2)}
const ns='http://www.w3.org/2000/svg';function svg(tag,attrs,text){const e=document.createElementNS(ns,tag);Object.entries(attrs||{}).forEach(([k,v])=>e.setAttribute(k,v));if(text!==undefined)e.textContent=text;return e}
function owner(field){if(field.startsWith('Layer2.WingMPET'))return 'WingMPET';return field.split('.')[0]}
function draw(p){const g=document.getElementById('l3-graph');g.replaceChildren();const defs=svg('defs');for(const [name,color] of [['good','#56c8a1'],['bad','#ff7180'],['none','#e8bf68'],['prior','#a4afbe']]){const m=svg('marker',{id:'l3-arrow-'+name,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});m.appendChild(svg('path',{d:'M0 0L10 5L0 10z',fill:color}));defs.appendChild(m)}g.appendChild(defs);
g.appendChild(svg('rect',{x:25,y:70,width:740,height:530,rx:20,fill:'#172f41',stroke:'#6e9fca','stroke-dasharray':'8 5'}));g.appendChild(svg('text',{x:390,y:105,'text-anchor':'middle',fill:'#d9ecff','font-size':18},'سه Twin در snapshot اتمیک لایهٔ ۳'));
const ids=new Set(Object.keys(p.snapshot.twins||{}));p.edges.forEach(e=>{ids.add(owner(e.source));ids.add(owner(e.target))});
const positions={MESA_NAV:[185,190],MicroMetTwin:[185,455],WingMPET:[590,320],Layer4:[1030,190],Layer7:[1030,455]};let extra=0;for(const id of ids)if(!positions[id])positions[id]=[180+extra++*270,620];
for(const e of p.edges){const a=positions[owner(e.source)],b=positions[owner(e.target)];if(!a||!b)continue;const dx=b[0]-a[0],dy=b[1]-a[1],k=Math.min(dx?120/Math.abs(dx):Infinity,dy?58/Math.abs(dy):Infinity);const kind=e.evidence_status==='MATCH'?'good':e.evidence_status==='MISMATCH'?'bad':e.evidence_status==='INITIAL_PRIOR_ONLY'?'prior':'none';const color={good:'#56c8a1',bad:'#ff7180',none:'#e8bf68',prior:'#a4afbe'}[kind];const path=svg('path',{d:`M${a[0]+dx*k},${a[1]+dy*k} L${b[0]-dx*k},${b[1]-dy*k}`,stroke:color,'stroke-width':3,fill:'none','marker-end':'url(#l3-arrow-'+kind+')',...(kind==='prior'?{'stroke-dasharray':'7 5'}:{})});path.appendChild(svg('title',{},e.source+' → '+e.target+' | '+e.evidence_status));path.style.cursor='pointer';path.addEventListener('click',()=>packet.textContent=JSON.stringify(e,null,2));g.appendChild(path);g.appendChild(svg('text',{x:(a[0]+b[0])/2+12,y:(a[1]+b[1])/2-8,fill:color,'font-size':18},'#'+(e.edge_index+1)))}
const s=p.snapshot,t=s.twins||{},short=x=>Array.isArray(x)?x.map(v=>typeof v==='number'?v.toFixed(2):fmt(v)).join(', '):fmt(x);
for(const id of ids){const [x,y]=positions[id],group=svg('g',{tabindex:0,role:'button','aria-label':id});group.style.cursor='pointer';group.appendChild(svg('rect',{x:x-120,y:y-58,width:240,height:116,rx:14,fill:'#264b66',stroke:'#b8defe'}));const lines=[id];if(id==='MESA_NAV')lines.push('NED: '+short(t[id]?.position_ned_m),'t='+fmt(t[id]?.time_s));else if(id==='MicroMetTwin')lines.push('wind NED: '+short(t[id]?.wind_ned_m_s),'t='+fmt(t[id]?.time_s));else if(id==='WingMPET')lines.push('model v='+fmt(t[id]?.reduced_inner_loop?.feedback_version),'t='+fmt(t[id]?.time_s));else if(id==='Layer4')lines.push('candidates='+fmt(p.trace.candidate_count),'snapshot consumer');else if(id==='Layer7')lines.push('current ok='+fmt(p.current_L7_return?.ok),'prior accepted cycle='+fmt(p.trace.previous_accepted_cycle));lines.forEach((line,i)=>group.appendChild(svg('text',{x,y:y-25+i*27,'text-anchor':'middle',fill:'#f1f7ff','font-size':i?13:19},line)));function choose(){role.value=id;showPacket()}group.addEventListener('click',choose);group.addEventListener('keydown',e=>{if(e.key==='Enter')choose()});g.appendChild(group)}
}
async function render(i,sync=true){if(!index.length){state.textContent='BLOCKED: دادهٔ شبکه موجود نیست.';return}i=Math.max(0,Math.min(index.length-1,i));select.value=i;slider.value=i;phase.value=index[i].phase;const token=++seq;state.textContent='در حال خواندن snapshot…';try{await load(index[i].bank)}catch(e){state.textContent='BLOCKED: فایل جزئیات این چرخه باز نشد؛ پوشه assets باید کنار گزارش باشد.';return}if(token!==seq)return;active=window.TDDT_L3_CHUNKS[index[i].bank][index[i].offset];const p=active,s=p.snapshot;
state.textContent='چرخه '+p.cycle+'؛ '+s.phase+'؛ ارزیابی شبکه: '+p.integrity;state.style.color=p.integrity==='PASS'?'#56c8a1':p.integrity==='FAIL'?'#ff7180':'#e8bf68';
table('l3-meta',null,[['زمان snapshot [FCU s]',s.time_s],['زمان‌های MESA / Wing / MicroMet [s]',[s.twins?.MESA_NAV?.time_s,s.twins?.WingMPET?.time_s,s.twins?.MicroMetTwin?.time_s]],['سن مدل اقلیم [s]',s.time_s!==undefined&&s.twins?.MicroMetTwin?.time_s!==undefined?s.time_s-s.twins.MicroMetTwin.time_s:null],['transaction',s.transaction_id],['snapshot hash',s.snapshot_hash],['frame / navigation authority',s.frame+' / '+s.navigation_authority],['reference',s.reference],['constraints اعلام‌شده؛ بدون ادعای اجرای کامل',s.constraints],['prior role',s.prior_role],['feedback route hash',s.feedback_route_hash],['بازخورد تازه / چرخهٔ پذیرش قبلی / فاصلهٔ اعمال',p.trace]]);
const statusLabels={MATCH:'تطبیق شواهد',MISMATCH:'ناسازگار',UNAVAILABLE:'شاهد ناموجود',INITIAL_PRIOR_ONLY:'فقط prior اولیه',NO_FEEDBACK_YET:'هنوز بازخورد قبلی نیست'};
table('l3-checks',['کنترل','نتیجه'],Object.entries(p.checks).map(([k,v])=>[window.TDDT_L3_CHECK_LABELS?.[k]||k,v===true?'تطبیق':v===false?'ناسازگار':'ناموجود']));table('l3-edges',['#','source','target','وضعیت شاهد','دامنهٔ شاهد'],p.edges.map(e=>[e.edge_index+1,e.source,e.target,statusLabels[e.evidence_status]||e.evidence_status,e.evidence_scope]));draw(p);showPacket();window.TDDT_L3_RENDER_STATE={cycle:p.cycle,integrity:p.integrity,edge_count:p.edges.length,model_version:s.twins?.WingMPET?.reduced_inner_loop?.feedback_version};
window.TDDT_L32_RENDER?.(p);
if(sync){const generic=document.getElementById('cycle');if(generic&&generic.value!==String(i)){generic.value=i;generic.dispatchEvent(new Event('change'))}}}
select.addEventListener('change',()=>render(Number(select.value)));slider.addEventListener('input',()=>render(Number(slider.value)));role.addEventListener('change',showPacket);phase.addEventListener('change',()=>render(index.findIndex(r=>r.phase===phase.value)));document.getElementById('l3-prev').addEventListener('click',()=>render(Number(select.value)-1));document.getElementById('l3-next').addEventListener('click',()=>render(Number(select.value)+1));document.getElementById('l3-feedback-next').addEventListener('click',()=>{const j=index.findIndex((r,i)=>i>Number(select.value)&&r.feedback_new);if(j>=0)render(j)});document.getElementById('cycle')?.addEventListener('change',e=>{if(Number(e.target.value)!==Number(select.value))render(Number(e.target.value),false)});window.TDDT_L3_SELECT=i=>render(i);render(0);
});
</script>
'''


def build_network_report(out, rows, mission_summary, page_writer, title):
    df, payloads, summary = export_network(out, rows, mission_summary=mission_summary)
    labels = {'تعداد snapshot': summary['snapshot_count'], 'snapshot با hash متمایز': summary['distinct_snapshots'],
        'تعداد توپولوژی مشاهده‌شده': summary['topology_count'], 'تغییر توپولوژی': summary['topology_changes'],
        'مسیر بازخورد پذیرفته': summary['accepted_feedback_routes'], 'مسیر بازخورد یکتای اعمال‌شده': summary['unique_feedback_routes_applied']}
    import html
    stats = '<table>'+''.join('<tr><td>'+html.escape(k)+'</td><td>'+str(v)+'</td></tr>' for k, v in labels.items())+'</table>'
    extra = ('<p class="badge">ارزیابی مستقل دادهٔ شبکه: <strong>'+summary['report_integrity']+'</strong>. این ارزیابی، گواهی کامل فرمالیسم شبکه یا کنترل نیست.</p>'+stats+
        '<p>مالک orchestration، TDOC است؛ لایهٔ ۳ در این پروفایل وضعیت مشترک و snapshot را می‌سازد. پویایی ثبت‌شده شامل تغییر state، زمان، اقلیم، reference و پارامترهای مدل است. تعداد تغییر توپولوژی از خود couplings محاسبه شده است؛ '
        'وزن تطبیقی یال‌ها، بازآرایی خودکار و تأخیر جداگانهٔ هر یال در این داده ثبت نشده‌اند. snapshot_atomic یک فیلد اعلام‌شده است؛ hash و تطبیق محتوا جدا بازبینی می‌شوند.</p>'
        '<p>MESA و Wing زمان منبع مشترک دارند؛ MicroMet ناهم‌زمان با سن محدود مصرف می‌شود. max_navigation_age_s در constraints، همان حد سن اقلیم نیست. '
        'یال MicroMet به MESA صرفاً prior اولیه است؛ باد مدل مشاهدهٔ ناوبری نیست. قیدهای ثبت‌شده به معنی اثبات اعمال کامل آن‌ها نیستند.</p>'
        '<p><a href="LAYER3_NETWORK_SUMMARY.json">خلاصه و حدود اعتبار</a> · <a href="LAYER3_NETWORK_TIMELINE.csv">روند همهٔ چرخه‌ها</a> · '
        '<a href="LAYER3_NETWORK_EDGES.csv">یال‌های هر چرخه</a> · <a href="LAYER3_SNAPSHOTS.jsonl.gz">snapshotهای کامل اصلی</a></p>'
        '<p><a href="#layer32-detail">بخش اختصاصی ۳.۲: گراف، ماتریس‌ها و پوشش فرمالیسم</a> · '
        '<a href="#l3-inspector">کاوشگر عمومی snapshotها و روابط بین لایه‌ها</a></p>'+
        layer32_panel(summary['sublayer32'])+NETWORK_PANEL)
    page_writer(out / 'LAYER3_DYNAMIC_NETWORK_REPORT.html', title, network_figures(df), mission_summary, extra)
    return summary
