"""Phase inspection and wind overlays for reporting only; no control authority."""
import html
import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .provenance import write_json

PHASE_LABELS = {'vertical_takeoff': 'برخاست عمودی', 'cruise_vtol': 'حرکت به مقصد',
    'vertical_landing': 'نزول عمودی', 'landing_flare': 'فرود نهایی', 'contact_settle': 'تثبیت تماس'}
PHASE_COLORS = dict(zip(PHASE_LABELS, ['#f9d65c', '#13cce4', '#fa91e7', '#ff8c4c', '#9efa91']))


def phase_segments(df):
    """Contiguous occurrences: repeated phases must not bridge another phase."""
    if df.empty:
        return []
    boundaries = [0] + [i for i in range(1, len(df)) if df.phase.iloc[i] != df.phase.iloc[i-1]] + [len(df)]
    return [(a, b, str(df.phase.iloc[a])) for a, b in zip(boundaries[:-1], boundaries[1:])]


def wind_samples(df):
    cols = ['cycle', 'flight_elapsed_s', 'phase', 'physical_east_m', 'physical_north_m',
        'physical_altitude_msl_m', 'wind_n_m_s', 'wind_e_m_s', 'wind_d_m_s',
        'climate_model_age_s', 'climate_forcing_sha256']
    w = df.reindex(columns=cols).copy()
    n, e, d = (pd.to_numeric(w[k], errors='coerce') for k in ('wind_n_m_s', 'wind_e_m_s', 'wind_d_m_s'))
    w['horizontal_speed_m_s'] = np.hypot(n, e)
    w['speed_3d_m_s'] = np.sqrt(n*n + e*e + d*d)
    moving = w.horizontal_speed_m_s > 1e-6
    # Bearing clockwise from true North. Meteorological FROM is opposite flow TO.
    w['flow_to_bearing_deg'] = ((np.degrees(np.arctan2(e, n)) + 360) % 360).where(moving)
    w['meteorological_from_bearing_deg'] = ((w.flow_to_bearing_deg + 180) % 360).where(moving)
    w['display_up_m_s'] = -d
    return w


def sampled_indices(df, per_segment=6):
    return sorted({int(i) for a, b, _ in phase_segments(df)
        for i in np.linspace(a, b-1, min(per_segment, b-a))})


def wind_traces(w, indices, *, anchor=(0., 0., 0.), span_m=2000.):
    """Arrow shafts use an explicit seconds-to-display-metres scale, not displacement."""
    span_m = float(span_m) if math.isfinite(span_m) else 1.
    q = w.iloc[indices].dropna(subset=['physical_east_m', 'physical_north_m', 'physical_altitude_msl_m',
        'wind_e_m_s', 'wind_n_m_s', 'wind_d_m_s']).copy()
    q = q[q.speed_3d_m_s > 1e-6]
    scale = max(span_m, 1.)*.06 / max(q.speed_3d_m_s.max() if not q.empty else 0., 1e-6)
    shafts = [[], [], []]; texts = []
    tx, ty, tz, ue, vn, wu, custom = [], [], [], [], [], [], []
    for r in q.itertuples():
        base = np.array([r.physical_east_m, r.physical_north_m, r.physical_altitude_msl_m])-anchor
        vector = np.array([r.wind_e_m_s, r.wind_n_m_s, -r.wind_d_m_s])
        tip = base + vector*scale
        fr = f'{r.meteorological_from_bearing_deg:.1f}°' if math.isfinite(r.meteorological_from_bearing_deg) else 'نامعین (باد افقی آرام)'
        text = f'باد مدل MicroMet؛ t={r.flight_elapsed_s:.2f} s؛ cycle={r.cycle}<br>سرعت افقی={r.horizontal_speed_m_s:.3f} m/s؛ از={fr}<br>N={r.wind_n_m_s:.3f}, E={r.wind_e_m_s:.3f}, Down={r.wind_d_m_s:.3f} m/s'
        for j in range(3):
            shafts[j].extend([float(base[j]), float(tip[j]), None])
        texts.extend([text, text, None])
        tx.append(tip[0]); ty.append(tip[1]); tz.append(tip[2])
        ue.append(vector[0]); vn.append(vector[1]); wu.append(vector[2]); custom.append(text)
    shaft = go.Scatter3d(x=shafts[0], y=shafts[1], z=shafts[2], mode='lines', line={'color': '#bd9aff', 'width': 4},
        name='پیکان حرکت باد MicroMet (مدل)', legendgroup='model-wind', text=texts,
        hovertemplate='%{text}<extra></extra>')
    head = go.Cone(x=tx, y=ty, z=tz, u=ue, v=vn, w=wu, anchor='tip',
        sizemode='absolute', sizeref=max(span_m, 1.)*.012, colorscale=[[0, '#bd9aff'], [1, '#bd9aff']],
        showscale=False, showlegend=False, legendgroup='model-wind', text=custom,
        hovertemplate='%{text}<extra></extra>', name='سر پیکان باد مدل')
    return [shaft, head], float(scale), q.cycle.astype(int).tolist()


def export_route_details(out, df, perf):
    w = wind_samples(df)
    w.to_csv(out / 'ROUTE_WIND_HISTORY.csv', index=False)
    segments = []
    for index, (a, b, phase) in enumerate(phase_segments(df)):
        q = df.iloc[a:b]; p = perf['phases'][index]
        segments.append({'segment_id': index, 'phase': phase, 'label_fa': PHASE_LABELS.get(phase, phase),
            'start_cycle': int(q.cycle.iloc[0]), 'end_cycle': int(q.cycle.iloc[-1]), 'sample_count': len(q),
            'start_s': p['start_s'], 'end_s': p['end_s'], 'duration_s': p['duration_s'],
            'physical_height_min_m': q.physical_height_above_launch_CG_m.min(),
            'physical_height_max_m': q.physical_height_above_launch_CG_m.max(),
            'physical_speed_max_m_s': q.physical_speed_m_s.max(),
            'physical_roll_abs_max_deg': q.physical_vtol_roll_deg.abs().max(),
            'physical_pitch_abs_max_deg': q.physical_vtol_pitch_deg.abs().max(),
            'actual_goal_distance_end_m': q.actual_goal_distance_m.iloc[-1],
            'estimated_goal_distance_end_m': q.estimated_goal_distance_m.iloc[-1],
            'armed_confirmed_receipts': int(q.l6_armed_observed.eq(True).sum()),
            'energy_used_wh': p['energy_used_wh'],
            'MPC_modes': ', '.join(str(v) for v in q.l5_mode.unique()),
            'accepted_feedback_count': int(q.l7_feedback_accepted.sum()),
            'wind_horizontal_speed_min_m_s': w.horizontal_speed_m_s.iloc[a:b].min(),
            'wind_horizontal_speed_max_m_s': w.horizontal_speed_m_s.iloc[a:b].max()})
    pd.DataFrame(segments, columns=['segment_id', 'phase', 'label_fa', 'start_cycle', 'end_cycle', 'sample_count',
        'start_s', 'end_s', 'duration_s', 'physical_height_min_m', 'physical_height_max_m', 'physical_speed_max_m_s',
        'physical_roll_abs_max_deg', 'physical_pitch_abs_max_deg', 'actual_goal_distance_end_m',
        'estimated_goal_distance_end_m', 'armed_confirmed_receipts', 'energy_used_wh', 'MPC_modes',
        'accepted_feedback_count', 'wind_horizontal_speed_min_m_s', 'wind_horizontal_speed_max_m_s']).to_csv(out / 'ROUTE_PHASE_DETAILS.csv', index=False)
    indices = sampled_indices(df)
    meta = {'schema': 'TDOC-online-route-detail-v1', 'source': 'same-session cycle records',
        'wind_source': 'Layer2 MicroMet model; not an ultrasonic wind sensor measurement',
        'vector_frame': 'recorded NED; displayed East,North,Up = E,N,-D',
        'bearing_reference': 'degrees clockwise from true North; FROM=(TO+180)%360',
        'calm_horizontal_direction': 'null when horizontal speed <= 1e-6 m/s',
        'arrow_direction': 'flow TO; arrows point in the direction air moves',
        'arrow_length': 'symbolic display scale, not actual drift or prediction',
        'arrow_sample_cycles': w.cycle.iloc[indices].astype(int).tolist(),
        'wind_rows': len(w), 'phase_segments': len(segments), 'phase_cycle_counts': [s['sample_count'] for s in segments],
        'phase_view': 'all recorded points; local coordinates relative to physical phase entry; no invented phases',
        'reporting_only': True, 'control_state_changed': False}
    write_json(out / 'ROUTE_DETAIL_CONTEXT.json', meta)
    return w, segments


def phase_table(segments):
    columns = [('label_fa', 'مرحله'), ('sample_count', 'چرخه'), ('start_s', 'شروع [s]'), ('end_s', 'پایان بازه [s]'),
        ('duration_s', 'زمان [s]'), ('physical_height_min_m', 'کمینه ارتفاع از آغاز مأموریت [m]'),
        ('physical_height_max_m', 'بیشینه ارتفاع [m]'), ('physical_speed_max_m_s', 'بیشینه سرعت [m/s]'),
        ('physical_roll_abs_max_deg', 'بیشینه |roll| [°]'), ('physical_pitch_abs_max_deg', 'بیشینه |pitch| [°]'),
        ('actual_goal_distance_end_m', 'فاصلهٔ واقعی تا مقصد در پایان مرحله [m]'),
        ('estimated_goal_distance_end_m', 'فاصلهٔ تخمینی تا مقصد در پایان مرحله [m]'),
        ('armed_confirmed_receipts', 'رسید دارای ARM مشاهده‌شده'),
        ('energy_used_wh', 'انرژی SITL [Wh]'), ('accepted_feedback_count', 'بازخورد پذیرفته'), ('MPC_modes', 'مد کنترل')]
    def fmt(v):
        if v is None or isinstance(v, float) and not math.isfinite(v):
            return 'ناموجود'
        return f'{v:.3f}' if isinstance(v, float) else str(v)
    return ('<section><h2>جزئیات تمام مراحل ثبت‌شده</h2><p>نمونه‌های برخاست، نزول، فرود نهایی و تثبیت تماس حذف نشده‌اند. '
        'در نمای جزئیات، هر بخش را از فهرست بالای نمودار انتخاب کنید. فازهای اجرا‌نشده ساخته نمی‌شوند.</p>'
        '<div style="overflow-x:auto"><table><thead><tr>' + ''.join('<th>'+t+'</th>' for _, t in columns) + '</tr></thead><tbody>' + \
        ''.join('<tr>'+''.join('<td>'+html.escape(fmt(s.get(k)))+'</td>' for k, _ in columns)+'</tr>' for s in segments) + '</tbody></table></div></section>')


def phase_viewer(df, wind, terrain):
    """One WebGL detail scene switches phase; four synchronized SVG telemetry plots."""
    frames = []
    initial = None
    for index, (a, b, phase) in enumerate(phase_segments(df)):
        q = df.iloc[a:b]
        fig = make_subplots(rows=3, cols=2, specs=[[{'type': 'scene', 'colspan': 2}, None], [{}, {}], [{}, {}]],
            row_heights=[.55, .225, .225], vertical_spacing=.09,
            subplot_titles=['نمای محلی مرحله؛ هر محور متر نسبت به آغاز همان مرحله',
                'ارتفاع نسبت به آغاز مأموریت [m]', 'سرعت [m/s]', 'roll و pitch جسم [°]', 'فرمان pilot-domain لایهٔ ۵'])
        positions = q[['physical_east_m', 'physical_north_m', 'physical_altitude_msl_m']].dropna()
        anchor = positions.iloc[0].to_numpy(dtype=float) if not positions.empty else np.full(3, np.nan)
        xyz = q[['physical_east_m', 'physical_north_m', 'physical_altitude_msl_m']].to_numpy(dtype=float) - anchor
        fig.add_trace(go.Scatter3d(x=xyz[:, 0], y=xyz[:, 1], z=xyz[:, 2], mode='lines+markers',
            line={'color': PHASE_COLORS.get(phase, '#13cce4'), 'width': 5}, marker={'size': 2.5},
            name='جسم؛ تمام چرخه‌های مرحله', customdata=q[['cycle', 'flight_elapsed_s', 'physical_altitude_msl_m', 'physical_speed_m_s']],
            hovertemplate='cycle=%{customdata[0]}<br>t=%{customdata[1]:.3f} s<br>ΔE=%{x:.3f}, ΔN=%{y:.3f}, ΔH=%{z:.3f} m<br>MSL=%{customdata[2]:.3f} m<br>speed=%{customdata[3]:.3f} m/s<extra></extra>'), row=1, col=1)
        msl = terrain.get('origin_altitude_msl_m')
        nav_z = msl-q.navigation_down_m-anchor[2] if msl is not None else np.full(len(q), np.nan)
        fig.add_trace(go.Scatter3d(x=q.navigation_east_m-anchor[0], y=q.navigation_north_m-anchor[1], z=nav_z,
            mode='lines', line={'color': '#ffffff', 'dash': 'dash'}, name='تخمین MESA', visible='legendonly'), row=1, col=1)
        span = max(float(np.nanmax(np.ptp(xyz, axis=0))) if np.isfinite(xyz).all() else 1., 1.)
        arrows, _, _ = wind_traces(wind, [a+int(i) for i in np.linspace(0, b-a-1, min(8, b-a))], anchor=anchor, span_m=span)
        for trace in arrows:
            fig.add_trace(trace, row=1, col=1)
        for row, col, fields in [(2, 1, ['physical_height_above_launch_CG_m', 'navigation_height_above_launch_m']),
                (2, 2, ['physical_speed_m_s', 'navigation_speed_m_s']),
                (3, 1, ['physical_vtol_roll_deg', 'physical_vtol_pitch_deg']),
                (3, 2, ['l5_pilot_cmd_0', 'l5_pilot_cmd_1', 'l5_pilot_cmd_2', 'l5_pilot_cmd_3'])]:
            for name in fields:
                fig.add_trace(go.Scatter(x=q.flight_elapsed_s.tolist(), y=q[name].tolist(), mode='lines+markers',
                    marker={'size': 3}, connectgaps=False, name=name), row=row, col=col)
        scene = {'xaxis_title': 'ΔEast [m]', 'yaxis_title': 'ΔNorth [m]', 'zaxis_title': 'ΔCG height [m]',
            'aspectmode': 'data', 'camera': {'eye': {'x': 1.4, 'y': -1.7, 'z': 1.2}}}
        for j, axis in enumerate(('xaxis', 'yaxis', 'zaxis')):
            valid = xyz[:, j][np.isfinite(xyz[:, j])]
            arrow_tips = np.asarray(getattr(arrows[1], ('x', 'y', 'z')[j]), dtype=float)
            valid = np.concatenate([valid, arrow_tips[np.isfinite(arrow_tips)]])
            lo, hi = (valid.min(), valid.max()) if valid.size else (-1., 1.)
            pad = max((hi-lo)*.2, span*.05, .25)
            scene[axis] = {'range': [float(lo-pad), float(hi+pad)], 'title': scene[axis+'_title']}
        title = f'{PHASE_LABELS.get(phase, phase)} — چرخه {int(q.cycle.iloc[0])} تا {int(q.cycle.iloc[-1])}؛ {len(q)} نمونه'
        layout = {'scene': scene, 'title': {'text': title, 'x': .98, 'xanchor': 'right'},
            **{axis: {'range': [float(q.flight_elapsed_s.iloc[0])-.05, float(q.flight_elapsed_s.iloc[-1])+.05], 'autorange': False}
               for axis in ('xaxis', 'xaxis2', 'xaxis3', 'xaxis4')},
            **{axis: {'autorange': True} for axis in ('yaxis', 'yaxis2', 'yaxis3', 'yaxis4')}}
        frames.append(go.Frame(name=str(index), data=fig.data, layout=layout))
        if initial is None:
            initial = fig
    if initial is None:
        return go.Figure()
    initial.update_layout(frames[0].layout)
    initial.frames = frames
    initial.update_layout(template='plotly_dark', height=1400, margin={'l': 70, 'r': 40, 't': 145, 'b': 220},
        legend={'orientation': 'h', 'entrywidth': 300, 'y': -.09, 'yanchor': 'top'},
        updatemenus=[{'type': 'dropdown', 'x': 0, 'y': 1.07, 'xanchor': 'left', 'yanchor': 'bottom',
            'buttons': [{'label': f'{i+1}. {PHASE_LABELS.get(phase, phase)}', 'method': 'animate',
                'args': [[str(i)], {'mode': 'immediate', 'frame': {'duration': 0, 'redraw': True}, 'transition': {'duration': 0}}]}
                for i, (_, _, phase) in enumerate(phase_segments(df))]}])
    return initial


def wind_figure(w):
    f = make_subplots(rows=3, cols=1, shared_xaxes=True, subplot_titles=[
        'سرعت باد افقی و سه‌بعدی مدل MicroMet [m/s]', 'جهت باد؛ درجه ساعت‌گرد از شمال حقیقی', 'مؤلفه‌های N/E/Down [m/s]'])
    for name in ('horizontal_speed_m_s', 'speed_3d_m_s'):
        f.add_trace(go.Scatter(x=w.flight_elapsed_s.tolist(), y=w[name].tolist(), name=name, mode='lines'), row=1, col=1)
    for name, label in [('meteorological_from_bearing_deg', 'از کدام سمت می‌وزد (FROM)'), ('flow_to_bearing_deg', 'به کدام سمت حرکت می‌کند (TO)')]:
        f.add_trace(go.Scatter(x=w.flight_elapsed_s.tolist(), y=w[name].tolist(), name=label, mode='markers', marker={'size': 2}), row=2, col=1)
    for name in ('wind_n_m_s', 'wind_e_m_s', 'wind_d_m_s'):
        f.add_trace(go.Scatter(x=w.flight_elapsed_s.tolist(), y=w[name].tolist(), name=name, mode='lines'), row=3, col=1)
    f.update_yaxes(range=[0, 360], tickvals=[0, 90, 180, 270, 360], ticktext=['N', 'E', 'S', 'W', 'N'], row=2, col=1)
    f.update_xaxes(title_text='زمان از اولین چرخه [s]', row=3, col=1)
    f.update_layout(template='plotly_dark', height=1000, legend={'orientation': 'h', 'y': -.1, 'yanchor': 'top'}, margin={'b': 170})
    return f
