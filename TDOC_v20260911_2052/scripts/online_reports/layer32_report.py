"""Describe sublayer 3.2 from same-run records, without invoking its dormant engine."""
from collections import Counter
import gzip
import html
import json
from pathlib import Path

import pandas as pd

from .data import digest
from .provenance import file_hash, write_json

CATALOG = [
    ('STD', 'STD', 'حالت مشترک هم‌تراز کامل؛ twins به‌تنهایی STD کامل نیست', 'std'),
    ('G', 'G', 'گراف رسمی با gate و metadata؛ ماتریس اعلان D جایگزین آن نیست', 'topology'),
    ('W', 'W', 'وزن معرفتی نرمال‌شدهٔ Twinها؛ quality_score وزن W نیست', 'weights'),
    ('K', 'K', 'شدت عددی couplingها؛ فهرست source/target ماتریس K نیست', 'couplings'),
    ('K_types', 'K_by_type', 'شدت شش نوع Data/Model/State/Error/Objective/Control', 'coupling_details'),
    ('Phi', 'Φ effects', 'آثار ثبت‌شدهٔ عملگر انتقال؛ نه بازاجرای مستقل عملگر', 'coupling_effects'),
    ('C_star', 'C*', 'مرکز سازگاری مشترک', 'center_of_consistency'),
    ('J_C', 'J_C', 'ناسازگاری شبکه؛ residual لایهٔ ۷ همان J_C نیست', 'mismatch'),
    ('Omega', 'Ω', 'ناحیهٔ سازگاری؛ به‌تنهایی گواه پایداری دینامیکی نیست', 'consistency_region'),
    ('stability', 'stability', 'ارزیابی پایداری با دامنه و شاهد خودش', 'stability'),
    ('X_E', 'X_E', 'حالت برآیندی؛ نبود آزمایش ablation با صفر جایگزین نمی‌شود', 'emergence'),
    ('LCI', 'LCI', 'شاخص hidden-loop؛ بی‌چرخه‌بودن D به معنی LCI=0 نیست', 'hidden_loop.lci'),
    ('margin', 'margin', 'حاشیهٔ سازگاری ارزیابی‌شده', 'consistency_margin'),
    ('policy', 'policy/commit', 'سیاست شبکه و وضعیت نسخه/commit ثبت‌شده', 'version_state'),
]
COUPLING_TYPES = ['Data', 'Model', 'State', 'Error', 'Objective', 'Control']
STATUS_FA = {'NOT_RECORDED': 'ثبت نشده', 'RECORDED': 'مقدار ثبت‌شده؛ بدون گواه صحت کامل',
    'RECORDED_NULL': 'فیلد ثبت‌شده با مقدار null', 'RECORDED_EMPTY': 'فیلد ثبت‌شده ولی خالی',
    'DECLARATION_LIST_ONLY': 'فقط فهرست روابط؛ K عددی نیست'}


def _lookup(obj, path):
    for key in path.split('.'):
        if not isinstance(obj, dict) or key not in obj:
            return False, None
        obj = obj[key]
    return True, obj


def _field(snapshot, key, path):
    present, value = _lookup(snapshot, path)
    if not present:
        status = 'NOT_RECORDED'
    elif key == 'K' and isinstance(value, list):
        return {'status': 'DECLARATION_LIST_ONLY', 'value': None}
    elif value is None:
        status = 'RECORDED_NULL'
    elif isinstance(value, (dict, list)) and not value:
        status = 'RECORDED_EMPTY'
    else:
        status = 'RECORDED'
    return {'status': status, 'value': value}


def owner(field):
    if field.startswith('Layer2.WingMPET'):
        return 'WingMPET'
    return field.split('.')[0]


def _cyclic(nodes, edges):
    if not nodes:
        return None
    graph = {n: [] for n in nodes}
    for e in edges:
        graph[e['source_node']].append(e['target_node'])
    seen, path = set(), set()
    def visit(n):
        if n in path:
            return True
        if n in seen:
            return False
        seen.add(n); path.add(n)
        if any(visit(t) for t in graph[n]):
            return True
        path.remove(n)
        return False
    return any(visit(n) for n in nodes)


def describe_layer32(payload):
    s = payload['snapshot']; twins = s.get('twins') or {}; nodes = list(twins)
    formal = {key: _field(s, key, field) for key, _, _, field in CATALOG}
    internal, boundary = [], []
    for e in payload['edges']:
        src, dst = owner(e['source']), owner(e['target'])
        entry = {**e, 'source_node': src, 'target_node': dst}
        if src not in nodes or dst not in nodes:
            boundary.append(entry)
            continue
        source, destination = twins[src], twins[dst]
        initial = 'initial_wind_prior' in e['target']
        context = {}
        if src == 'MESA_NAV':
            context = {k: source.get(k) for k in ('position_ned_m', 'velocity_ned_m_s', 'q_wxyz', 'source_wire_sha256')}
        elif src == 'MicroMetTwin':
            context = {k: source.get(k) for k in ('wind_ned_m_s', 'air_density_kg_m3', 'forcing_sha256')}
        entry.update(initial_prior_only=initial, source_time_s=source.get('time_s'), target_time_s=destination.get('time_s'),
            available_source_packet_value=context,
            source_age_at_snapshot_s=s['time_s']-source['time_s'] if s.get('time_s') is not None and source.get('time_s') is not None else None,
            target_binding={k: destination.get(k) for k in ('FMU_operation', 'climate_input_time_s', 'climate_input_forcing_sha256') if k in destination},
            numeric_gate_a=None, numeric_strength_K=None, six_type_strengths={c: None for c in COUPLING_TYPES},
            transfer_delay_tau_s=None, transferred_value=None,
            transfer_evidence='source packet and provenance only; exact numeric transfer not logged',
            initial_prior_value_is_current_packet=False if initial else None)
        # Preserve explicit edge metadata only; never infer a gate or gain from MATCH.
        declared = (s.get('couplings') or [])[e['edge_index']]
        for target, field in [('numeric_gate_a', 'a'), ('numeric_strength_K', 'K'), ('transfer_delay_tau_s', 'tau_s')]:
            if field in declared:
                entry[target] = declared[field]
        if isinstance(declared.get('K_by_type'), dict):
            entry['six_type_strengths'] = {c: declared['K_by_type'].get(c) for c in COUPLING_TYPES}
        internal.append(entry)
    adjacency = [[int(any(e['source_node'] == src and e['target_node'] == dst for e in internal)) for dst in nodes] for src in nodes]
    current = [[int(any(e['source_node'] == src and e['target_node'] == dst and not e['initial_prior_only'] for e in internal)) for dst in nodes] for src in nodes]
    return {'schema': 'TDOC-online-layer32-cycle-detail-v1', 'cycle': payload['cycle'],
        'snapshot_hash': s.get('snapshot_hash'), 'node_order': nodes,
        'declaration_adjacency_D': adjacency, 'noninitial_declaration_adjacency_D': current,
        'matrix_semantics': 'rows=source; columns=target; 1=declared, 0=not declared; neither a runtime gate nor numeric K',
        'formal_outputs': formal, 'internal_edges': internal, 'boundary_edges': boundary,
        'declared_graph_has_directed_cycle': _cyclic(nodes, internal),
        'LCI_inferred_from_graph': False, 'controller_or_engine_executed_by_report': False,
        'formal_assessment': 'NOT_ASSESSED_BY_REPORT',
        'initial_prior_warning': 'current MicroMet value is not evidence of the initial prior value actually used',
        'feedback_delay_warning': 'prior accepted-cycle gap is not per-edge transport tau',
        'coverage_note': 'inspect exact recorded formal fields; missing fields are not recomputed from Offline or a separate engine'}


def reference_context():
    root = Path(__file__).resolve().parents[3]
    paths = [
        'doc_v2/TDDT_Dynamic_NTwin_v6/TDDT_Dynamic_NTwin_Layer3_details_FA_v4.md',
        'doc_v2/TDDT_Dynamic_NTwin_v6/TDDT_Dynamic_NTwin_TDOC_details_FA_v5.md',
        'layer3_v20260911_1131/src/layer3/realtime.py',
        'layer3_v20260911_1131/src/layer3/engine.py',
        'layer3_v20260911_1131/src/layer3/models.py',
        'TDOC_v20260911_2052/src/tddt_tdoc/realtime.py',
    ]
    references = [{'path': name, 'sha256': file_hash(root/name) if (root/name).is_file() else None} for name in paths]
    return {'references': references,
        'formal_contract': {'inputs': ['STD', 'Z', 'W', 'Policy_G', 'Policy_K'],
            'outputs': ['G', 'K', 'C*', 'J_C', 'Omega', 'X_E', 'LCI']},
        'source_scope': 'realtime.py emits the reduced Native snapshot; engine.py contains the separate full plan_network/step path',
        'full_engine_invoked_to_fill_report': False}


def attach_layer32(out, payloads, mission_summary):
    counts = {key: Counter() for key, _, _, _ in CATALOG}
    timeline, detailed = [], []
    for p in payloads:
        detail = describe_layer32(p); p['sublayer32'] = detail; detailed.append(detail)
        for key, value in detail['formal_outputs'].items():
            counts[key][value['status']] += 1
        timeline.append({'cycle': p['cycle'], 'snapshot_hash': p['snapshot'].get('snapshot_hash'),
            'node_count': len(detail['node_order']), 'internal_declared_edges': len(detail['internal_edges']),
            'initial_prior_edges': sum(e['initial_prior_only'] for e in detail['internal_edges']),
            'boundary_edges': len(detail['boundary_edges']),
            'declaration_topology_hash': digest({'nodes': detail['node_order'], 'D': detail['declaration_adjacency_D']}),
            'declared_graph_has_directed_cycle': detail['declared_graph_has_directed_cycle'],
            **{key+'_status': value['status'] for key, value in detail['formal_outputs'].items()}})
    coverage = [{'key': key, 'symbol': symbol, 'meaning_fa': meaning, 'snapshot_path': path,
        'status_counts': dict(counts[key])} for key, symbol, meaning, path in CATALOG]
    summary = {'schema': 'TDOC-online-layer32-coverage-v1', 'snapshot_count': len(payloads),
        **{k: mission_summary.get(k) for k in ('native_session_id', 'model_id', 'source_directory')},
        'coverage': coverage, 'coupling_types': COUPLING_TYPES,
        'internal_edge_record_count': sum(t['internal_declared_edges'] for t in timeline),
        'boundary_edge_record_count': sum(t['boundary_edges'] for t in timeline),
        'internal_declaration_topologies': len({t['declaration_topology_hash'] for t in timeline}),
        'formal_assessment': 'NOT_ASSESSED_BY_REPORT', 'reporting_only': True,
        'missing_values_recomputed': False, 'runtime_graph_is_fully_observed': False,
        'reference_context': reference_context()}
    write_json(out/'LAYER32_COVERAGE.json', summary)
    pd.DataFrame(timeline, columns=list(timeline[0]) if timeline else ['cycle', 'snapshot_hash', 'node_count']).to_csv(out/'LAYER32_TIMELINE.csv', index=False)
    pd.DataFrame([{'key': c['key'], 'symbol': c['symbol'], 'meaning_fa': c['meaning_fa'], 'snapshot_path': c['snapshot_path'],
        **{status: c['status_counts'].get(status, 0) for status in STATUS_FA}} for c in coverage]).to_csv(out/'LAYER32_FIELD_COVERAGE.csv', index=False)
    with gzip.open(out/'LAYER32_DETAILS.jsonl.gz', 'wt', encoding='utf-8') as f:
        for detail in detailed:
            f.write(json.dumps(detail, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')
    (out/'assets').mkdir(exist_ok=True)
    (out/'assets/layer32_catalog.js').write_text('window.TDDT_L32_CATALOG='+json.dumps(coverage, ensure_ascii=False).replace('<', '\\u003c')+
        ';window.TDDT_L32_STATUS='+json.dumps(STATUS_FA, ensure_ascii=False)+';', encoding='utf-8')
    return summary


PANEL = r'''
<script src="assets/layer32_catalog.js"></script>
<section id="layer32-detail"><h2>زیرلایهٔ ۳.۲ — شبکهٔ coupling به‌تفکیک</h2>
<p class="badge">این بخش، پوشش واقعی دادهٔ ۳.۲ را نشان می‌دهد. نبود خروجی عددی با اجرای مجدد موتور دیگر، دادهٔ آفلاین یا عدد فرضی پر نمی‌شود.</p>
<p>ورودی رسمی: STD، فضای مشترک Z، وزن W و Policy_G/Policy_K. خروجی رسمی: G، K، C*، J_C، Ω، X_E و LCI.
وجود snapshot سه Twin به معنی اجرای کامل این قرارداد نیست. مسیر کامل <code>Layer3Engine.plan_network/step</code> در باندل وجود دارد؛ وجود سورس آن شاهد اجرای آن در همین نشست نیست.</p>
<label>چرخهٔ ۳.۲ <select id="l32-cycle" aria-label="چرخهٔ زیرلایهٔ ۳.۲"></select></label>
<button id="l32-prev">چرخهٔ قبل</button><button id="l32-next">چرخهٔ بعد</button>
<p id="l32-state" role="status">در حال خواندن داده…</p>
<h3>گراف اعلان سه Twin؛ روابط بیرونی جدا هستند</h3>
<svg id="l32-graph" viewBox="0 0 900 500" style="width:100%;max-height:500px;background:#10202e" role="img" aria-label="گراف اعلان درونی سه Twin"></svg>
<div style="display:flex;flex-wrap:wrap;gap:24px"><div><h3>ماتریس اعلان D</h3><table id="l32-D"></table></div><div><h3>اعلان‌های غیر از prior اولیه</h3><table id="l32-current"></table></div></div>
<p>سطر = مبدأ، ستون = مقصد؛ ۱ یعنی در couplings اعلام شده و ۰ یعنی اعلام نشده است. این اعداد gate واقعی a، ضریب K یا نتیجهٔ اجرای یک انتقال عددی نیستند.
یال prior اولیه در ماتریس دوم کنار گذاشته شده؛ کنارگذاشتن آن به معنی فرمان غیرفعال‌سازی نیست. گراف از فهرست اعلام‌شده استخراج می‌شود و کامل‌بودن تمام فراخوانی‌های اجرایی را اثبات نمی‌کند.</p>
<h3>خروجی‌های رسمی ۳.۲ در همین چرخه</h3><div style="overflow-x:auto"><table id="l32-formal"></table></div>
<h3>جزئیات یال‌های بین Twinها</h3><div style="overflow-x:auto"><table id="l32-edges"></table></div>
<p>سبز بودن شاهد یال، K=1 یا a=1 نیست. سن بسته، تأخیر انتقال τ نیست. مقدار فعلی MicroMet برای یال prior، مقدار دقیق prior ابتدای پرواز فرض نمی‌شود.
quality_score و covariance موجود نیز وزن معرفتی W نیستند. residual شش‌مولفه‌ای L7 به جای J_C یا مرکز سازگاری قرار نمی‌گیرد. بی‌چرخه بودن گراف اعلان، LCI=0 را نتیجه نمی‌دهد.</p>
<h3>شدت شش نوع coupling ثبت‌شده به‌ازای هر یال</h3><div style="overflow-x:auto"><table id="l32-types"></table></div>
<h3>روابط مرزی؛ خارج از ماتریس سه Twin</h3><table id="l32-boundary"></table>
<label>جزئیات یال <select id="l32-edge-select"></select></label><pre id="l32-edge-packet" style="max-height:450px;overflow:auto;white-space:pre-wrap;text-align:left"></pre>
<details><summary>بستهٔ کامل تشریح ۳.۲ در این چرخه، تولیدشده توسط گزارش‌ساز</summary><pre id="l32-json" style="max-height:550px;overflow:auto;white-space:pre-wrap;text-align:left"></pre></details>
<p><a href="LAYER32_COVERAGE.json">پوشش و مرجع مستندات/سورس با hash</a> · <a href="LAYER32_TIMELINE.csv">روند ۳.۲ در تمام چرخه‌ها</a> ·
<a href="LAYER32_FIELD_COVERAGE.csv">پوشش هر کمیت رسمی</a> · <a href="LAYER32_DETAILS.jsonl.gz">جزئیات کامل ۳.۲ برای تمام چرخه‌ها</a></p>
</section>
<script>
document.addEventListener('DOMContentLoaded',()=>{const s=document.getElementById('l32-cycle');(window.TDDT_L3_INDEX||[]).forEach((v,i)=>{const o=document.createElement('option');o.value=i;o.textContent=v.cycle+' — '+v.phase;s.appendChild(o)});function go(i){window.TDDT_L3_SELECT?.(Math.max(0,Math.min(s.options.length-1,i)))}s.addEventListener('change',()=>go(Number(s.value)));document.getElementById('l32-prev').addEventListener('click',()=>go(Number(s.value)-1));document.getElementById('l32-next').addEventListener('click',()=>go(Number(s.value)+1))});
window.TDDT_L32_RENDER=p=>{const d=p.sublayer32;if(!d)return;const fmt=v=>v===null||v===undefined?'ناموجود':typeof v==='object'?JSON.stringify(v):String(v);function table(id,head,rows){const t=document.getElementById(id);t.replaceChildren();for(const [cells,tag] of [[head,'th'],...rows.map(r=>[r,'td'])]){const tr=document.createElement('tr');for(const v of cells){const e=document.createElement(tag);e.textContent=fmt(v);e.style.wordBreak='break-word';tr.appendChild(e)}t.appendChild(tr)}}
document.getElementById('l32-cycle').value=p.cycle-1;const recorded=Object.values(d.formal_outputs).filter(v=>v.status==='RECORDED').length;document.getElementById('l32-state').textContent='چرخه '+p.cycle+'؛ '+d.internal_edges.length+' یال درونی و '+d.boundary_edges.length+' رابطهٔ مرزی؛ '+recorded+' از '+Object.keys(d.formal_outputs).length+' قلم رسمی دارای مقدار ثبت‌شده است؛ این شمارش گواهی صحت فرمالیسم نیست.';
for(const [id,matrix] of [['l32-D',d.declaration_adjacency_D],['l32-current',d.noninitial_declaration_adjacency_D]])table(id,['مبدأ → مقصد',...d.node_order],matrix.map((r,i)=>[d.node_order[i],...r]));
table('l32-formal',['نماد','معنا','مسیر واقعی در snapshot','وضعیت','مقدار'],window.TDDT_L32_CATALOG.map(c=>[c.symbol,c.meaning_fa,c.snapshot_path,window.TDDT_L32_STATUS[d.formal_outputs[c.key].status],d.formal_outputs[c.key].value]));
table('l32-edges',['مبدأ','مقصد','نقش','زمان مبدأ [s]','سن بسته [s]','وضعیت شاهد','a','K','τ [s]'],d.internal_edges.map(e=>[e.source_node,e.target_node,e.initial_prior_only?'prior اولیه؛ نه مشاهدهٔ جاری':'وابستگی جاری اعلام‌شده',e.source_time_s,e.source_age_at_snapshot_s,e.evidence_status,e.numeric_gate_a,e.numeric_strength_K,e.transfer_delay_tau_s]));
table('l32-types',['یال',...['Data','Model','State','Error','Objective','Control']],d.internal_edges.map(e=>[e.source_node+' → '+e.target_node,...Object.values(e.six_type_strengths)]));
table('l32-boundary',['مبدأ','مقصد','شاهد','علت تفکیک'],d.boundary_edges.map(e=>[e.source,e.target,e.evidence_status,'هر دو انتهای رابطه عضو مجموعهٔ Twinهای snapshot نیستند']));
const select=document.getElementById('l32-edge-select');select.replaceChildren();d.internal_edges.forEach((e,i)=>{const o=document.createElement('option');o.value=i;o.textContent=e.source_node+' → '+e.target_node;select.appendChild(o)});function showEdge(){document.getElementById('l32-edge-packet').textContent=JSON.stringify(d.internal_edges[Number(select.value)]??null,null,2)}select.onchange=showEdge;showEdge();document.getElementById('l32-json').textContent=JSON.stringify(d,null,2);
const ns='http://www.w3.org/2000/svg',g=document.getElementById('l32-graph');g.replaceChildren();function svg(tag,a,text){const e=document.createElementNS(ns,tag);Object.entries(a).forEach(([k,v])=>e.setAttribute(k,v));if(text!==undefined)e.textContent=text;return e}const defs=svg('defs',{});for(const [id,color] of [['prior','#a4afbe'],['ok','#56c8a1'],['bad','#ff7180'],['unknown','#e8bf68']]){const m=svg('marker',{id:'l32-arrow-'+id,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto'});m.appendChild(svg('path',{d:'M0 0L10 5L0 10z',fill:color}));defs.appendChild(m)}g.appendChild(defs);const pos={MESA_NAV:[200,110],MicroMetTwin:[200,390],WingMPET:[665,250]};d.node_order.forEach((n,i)=>{if(!pos[n])pos[n]=[150+230*i,450]});
d.internal_edges.forEach((e,i)=>{const a=pos[e.source_node],b=pos[e.target_node],dx=b[0]-a[0],dy=b[1]-a[1],k=Math.min(dx?120/Math.abs(dx):Infinity,dy?45/Math.abs(dy):Infinity);const kind=e.initial_prior_only?'prior':e.evidence_status==='MATCH'?'ok':e.evidence_status==='MISMATCH'?'bad':'unknown';const color={prior:'#a4afbe',ok:'#56c8a1',bad:'#ff7180',unknown:'#e8bf68'}[kind];const line=svg('path',{d:`M${a[0]+dx*k},${a[1]+dy*k}L${b[0]-dx*k},${b[1]-dy*k}`,fill:'none',stroke:color,'stroke-width':3,'stroke-dasharray':e.initial_prior_only?'7 5':'none','marker-end':'url(#l32-arrow-'+kind+')'});line.style.cursor='pointer';line.addEventListener('click',()=>{select.value=i;showEdge()});line.appendChild(svg('title',{},e.source+' → '+e.target));g.appendChild(line)});
for(const name of d.node_order){const [x,y]=pos[name];g.appendChild(svg('rect',{x:x-120,y:y-45,width:240,height:90,rx:13,fill:'#264b66',stroke:'#b8defe'}));g.appendChild(svg('text',{x,y:y-3,'text-anchor':'middle',fill:'#fff','font-size':19},name));g.appendChild(svg('text',{x,y:y+25,'text-anchor':'middle',fill:'#d9ecff','font-size':13},'t='+fmt(p.snapshot.twins[name].time_s)))}
window.TDDT_L32_RENDER_STATE={cycle:p.cycle,internal_edges:d.internal_edges.length,boundary_edges:d.boundary_edges.length,recorded_formal_fields:recorded,formal_K:d.formal_outputs.K.value,formal_LCI:d.formal_outputs.LCI.value};
};
</script>
'''


def panel(summary):
    refs = summary['reference_context']['references']
    sources = '<details><summary>مرجع تعریف ۳.۲ و پیاده‌سازی؛ مسیر و hash</summary><ul>'+''.join(
        '<li><code>'+html.escape(r['path'])+'</code><br><code>'+html.escape(r['sha256'] or 'ناموجود')+'</code></li>' for r in refs)+'</ul></details>'
    return PANEL+sources
