from pathlib import Path
import zipfile, shutil, json, html, re, math

R=Path(__file__).resolve().parents[1]
meta=json.loads((R/'metadata.json').read_text())
outputs_mesa={'ultrasonic_update_valid','ultrasonic_residual_norm','mode_lambda_w','mechanical_constraints_valid_vtol','layer2_quality_score','layer2_validity_score','layer2_reliability_score','wind_source_status_code','bu_n','bu_e','bu_d','rE','vN','innovation_norm','Ppos_N','vD','wN','rN','anchor_mag_valid','Ppos_E','revisit_score','qx','airspeed','robust_weight','flightpath','wD','Ppos_D','wE','qz','nis','anchor_pressure_valid','qw','rD','heading','qy','obs_score','agl','nees','vE','anchor_flow_valid','rls_residual'}
outputs_wing={'vtol_next_pn','vtol_next_pe','vtol_next_pd','vtol_next_vn','vtol_next_ve','vtol_next_vd','vtol_next_qw','vtol_next_qx','vtol_next_qy','vtol_next_qz','vtol_next_p_rate','vtol_next_q_rate','vtol_next_r_rate','vtol_next_motor_omega_left','vtol_next_motor_omega_right','vtol_next_soc','vtol_Va','vtol_alpha','vtol_beta','vtol_lambda_w','vtol_thrust_left_N','vtol_thrust_right_N','vtol_total_thrust_N','vtol_predicted_power_W','vtol_energy_increment_J','vtol_flight_mode_code','vtol_feasible','vtol_quality_score','vtol_q_dyn_Pa','vtol_CL','vtol_CD','vtol_lift_N','vtol_drag_N','wind_acc_h','battery_current','terrain_margin','wind_acc_y','groundspeed','feasible_wind','feasible_obstacle','terminal_voltage','feasible_terrain','load_factor','energy_J','feasible_energy','obstacle_margin','cost_length','wind_acc_x','cost_energy','cost_time','vgh'}

UNIT_DEFS = {
    '1': {},
    's': {'s':'1'},
    'm': {'m':'1'},
    'm2': {'m':'2'},
    'm/s': {'m':'1','s':'-1'},
    'm/s2': {'m':'1','s':'-2'},
    'rad': {'rad':'1'},
    'rad/s': {'rad':'1','s':'-1'},
    'rad/s2': {'rad':'1','s':'-2'},
    'deg': {'rad':'1','factor':repr(math.pi/180.0)},
    'K': {'K':'1'},
    'Pa': {'kg':'1','m':'-1','s':'-2'},
    'Pa/m': {'kg':'1','m':'-2','s':'-2'},
    'kg': {'kg':'1'},
    'kg/m3': {'kg':'1','m':'-3'},
    'kg/s': {'kg':'1','s':'-1'},
    'N': {'kg':'1','m':'1','s':'-2'},
    'N*m': {'kg':'1','m':'2','s':'-2'},
    'W': {'kg':'1','m':'2','s':'-3'},
    'J': {'kg':'1','m':'2','s':'-2'},
    'A': {'A':'1'},
    'V': {'kg':'1','m':'2','s':'-3','A':'-1'},
    'ohm': {'kg':'1','m':'2','s':'-3','A':'-2'},
    'Hz': {'s':'-1'},
    'T': {'kg':'1','s':'-2','A':'-1'},
    'T/m': {'kg':'1','m':'-1','s':'-2','A':'-1'},
    'Ah': {'A':'1','s':'1','factor':'3600'},
    'rpm': {'s':'-1','factor':repr(1.0/60.0)},
    '1/s': {'s':'-1'},
    'm^-1': {'m':'-1'},
}

def unit_xml():
    items=[]
    for name,attrs in UNIT_DEFS.items():
        if attrs:
            base=' '.join(f'{k}="{v}"' for k,v in attrs.items())
            items.append(f'<Unit name="{html.escape(name)}"><BaseUnit {base}/></Unit>')
        else:
            items.append(f'<Unit name="{html.escape(name)}"><BaseUnit/></Unit>')
    return '<UnitDefinitions>'+''.join(items)+'</UnitDefinitions>'

def infer_unit(name:str, *, generic=False):
    n=name.lower()
    if generic or re.fullmatch(r'(input|output)_\d+', n): return '1'
    if n in {'x','y','h','state_x','state_y','state_h','target_x','target_y','target_h','next_x','next_y','next_h','computed_agl','terrain_height_echo','relative_displacement_n','relative_displacement_e','pseudo_displacement_n','pseudo_displacement_e'}: return 'm'
    if n in {'along_track_speed'}: return 'm/s'
    if n in {'rotation_hz','switching_frequency'}: return 'Hz'
    if n in {'flow_filter_tau','flow_dt','background_time_constant'}: return 's'
    if n in {'pitot_residual'}: return 'Pa'
    if n.startswith('flow_') and 'residual' in n: return 'm/s'
    if n.startswith('wind_') and 'residual' in n: return 'm/s'
    if n.startswith('mag_') and 'residual' in n: return 'T'
    if n.startswith('pressure_') and 'residual' in n: return 'Pa'
    if any(x in n for x in ['valid','feasible','accepted','mode_','flag','quality','operation_code','operation_count','dimension','count','version','revision','score','weight','nis','nees','soc','clmax','cl_min','clmax','cl','cd0','cd','ct0','ct1','ct2','cp0','cp1','cp2','motor_eff','inverter_eff','prop_eff','relative_humidity','beta','alpha','forgetting_factor','echo_','contract_value','problem_value']): return '1'
    if n in {'s','e','ar'}: return '1'
    if 'timestamp' in n or n in {'dt','event_age','late_max','future_tolerance','time_prev','time_s'} or n.endswith('_time') or n.endswith('_time_s'): return 's'
    if 'temperature' in n or n.startswith('t_air') or n.startswith('motor_temperature'): return 'K'
    if 'pressure_grad' in n: return 'Pa/m'
    if 'pressure' in n or n.endswith('_pa'): return 'Pa'
    if 'density' in n or n=='rho' or 'air_density' in n: return 'kg/m3'
    if 'battery_current' in n or n.endswith('_current') or 'current_' in n: return 'A'
    if 'voltage' in n or 'battery_voc' in n or n.startswith('u_'): return 'V'
    if 'resistance' in n or 'battery_r' in n or n.endswith('_ohm'): return 'ohm'
    if 'capacity_ah' in n or 'battery_q_ah' in n: return 'Ah'
    if 'power' in n or n.endswith('_w'): return 'W'
    if 'energy' in n or n.endswith('_j'): return 'J'
    if 'fuel' in n or n=='mass' or n.endswith('_mass') or n.endswith('_kg'): return 'kg'
    if 'thrust' in n or n=='lift' or n=='drag' or n.endswith('_force'): return 'N'
    if 'torque' in n or 'moment' in n: return 'N*m'
    if n.endswith('_rpm') or 'prop_n' in n or n=='rpm': return 'rpm'
    if 'turn_rate' in n or n.endswith('_rate_rad_s'): return 'rad/s'
    if any(x in n for x in ['heading','flightpath','gamma','psi','bank','roll','pitch','yaw','angle']): return 'rad'
    if 'wind_grad' in n: return '1/s'
    if 'mag' in n and ('grad' in n): return 'T/m'
    if 'mag_' in n or n.startswith('mag') or 'magmap' in n: return 'T'
    if 'acc' in n or n.startswith('imu_a') or 'acceleration' in n: return 'm/s2'
    if n.startswith('imu_g') or n.startswith('gyro'): return 'rad/s'
    if 'airspeed' in n or 'groundspeed' in n or 'velocity' in n or re.fullmatch(r'v[ang][xyzhed]', n) or n in {'van','vae','vad','vn','ve','vd','vgh'} or 'wind_' in n or 'windmap_' in n or n in {'wn','we','wd','flow_bx','flow_by','flow_bz'}: return 'm/s'
    if 'q_dyn' in n or 'dynamic_pressure' in n or n=='pitot_q': return 'Pa'
    if 'variance' in n:
        # variance unit may be signal-specific; leave dimensionless in generic schema unless explicit naming reveals dimension
        return '1'
    if 'area' in n or n=='s': return 'm2'
    if 'span' in n or 'chord' in n or 'diameter' in n or 'prop_d' in n or 'distance' in n or 'margin' in n or 'radius' in n or 'height' in n or 'altitude' in n or n in {'px','py','h','rn','re','rd','dem_h','agl','terrain_h','h_clear','r_safe','position_n','position_e','position_d'} or n.endswith('_m'): return 'm'
    if 'path_length' in n or 'cost_length' in n: return 'm'
    if 'load_factor' in n: return '1'
    if 'objective' in n or n.startswith('cost_') or 'residual' in n or 'innovation_norm' in n: return '1'
    return '1'

def infer_frame(name:str, model:str):
    n=name.lower()
    # Scalars derived from vectors must not inherit a vector frame merely due to their name.
    if any(x in n for x in ['density','pressure','temperature','humidity','airspeed_norm','dynamic_pressure','groundspeed','magnitude','norm','score','valid','variance','energy','power','current','voltage','mass','soc']): return 'SCALAR'
    if any(x in n for x in ['_bx','_by','_bz','imu_','bgx','bgy','bgz','bfx','bfy','bfz','bmx','bmy','bmz','bax','bay','baz']): return 'BODY'
    if model=='MESA_NAV' and (n.endswith('_n') or n.endswith('_e') or n.endswith('_d') or n in {'rn','re','rd','vn','ve','vd','wn','we','wd'}): return 'NED'
    if model=='WingMPET' and n.startswith('vtol_') and any(k in n for k in ['_pn','_pe','_pd','_vn','_ve','_vd','wind_n','wind_e','wind_d']): return 'NED'
    if model=='WingMPET' and n.startswith('vtol_') and any(k in n for k in ['p_rate','q_rate','r_rate']): return 'BODY_FRD'
    if model=='WingMPET' and n.startswith('vtol_q'): return 'BODY_TO_NED'
    if model=='WingMPET' and (n.endswith('_x') or n.endswith('_y') or n.endswith('_h') or n in {'x','y','h','px','py','vax','vay','vah','state_x','state_y','state_h','target_x','target_y','target_h','next_x','next_y','next_h'}): return 'ENU'
    if 'air_unit_' in n or 'lift_unit_' in n or n.startswith('va') or 'air_velocity_' in n: return 'AIR_RELATIVE_ENU'
    if 'magmap' in n or 'windmap' in n or 'dem_' in n or 'terrain' in n: return 'MAP'
    return 'SCALAR'

def desc_for(name, unit, frame, generic=False):
    if generic:
        return 'Context-dependent generic operation bus scalar; semantic unit/frame are defined by the selected operation alias in resources/signal_schema.json.'
    return f'Semantic unit={unit}; frame={frame}. Extended provenance/quality schema: resources/signal_schema.json.'

def expose_canonical(model,name):
    # Keep value references stable but do not expose superseded canonical variables in FMI XML/schema.
    if re.fullmatch(r'(input|output)_\d+',name) or name in {'dt','operation_code'}:
        return True
    if model=='MESA_NAV':
        hidden={'flow_bx','flow_by','flow_bz','act_aileron','act_elevator','act_rudder','act_throttle','bax','bay','baz'}
        return name not in hidden
    if model=='WingMPET':
        # New formalism uses the explicit VTOL canonical block; legacy point-mass/fuel state stays internal ABI only.
        return name.startswith('vtol_') or name in {'left_motor_cmd','right_motor_cmd','left_elevon_rad','right_elevon_rad'}
    return True

def make_schema(model, variables, catalog, outputs):
    signals={}
    # canonical variables
    for n in variables:
        if not expose_canonical(model,n): continue
        generic=bool(re.fullmatch(r'(input|output)_\d+', n))
        unit=infer_unit(n,generic=generic); frame=infer_frame(n,model)
        signals[n]={
            'value': None, 'unit': unit, 'frame': 'CONTEXT_DEPENDENT' if generic else frame,
            'timestamp': {'policy':'fmu_time','unit':'s'},
            'source': 'external' if n not in outputs and not n.startswith('output_') else f'{model}.fmu',
            'quality': None, 'uncertainty': None,
            'validity': {'default':'unknown','rule':'finite_numeric_after_get' if (n in outputs or n.startswith('output_')) else 'caller_supplied'},
        }
    for op,desc in catalog['operations'].items():
        prefix=f'op{int(op):02d}'
        for direction,key in [('in','inputs'),('out','outputs')]:
            for n in desc[key]:
                alias=f'{prefix}__{direction}__{n}'
                unit=infer_unit(n); frame=infer_frame(n,model)
                signals[alias]={
                    'value': None, 'unit': unit, 'frame': frame,
                    'timestamp': {'policy':'fmu_time','unit':'s'},
                    'source': 'external' if direction=='in' else f'{model}.fmu:operation_{int(op)}',
                    'quality': None, 'uncertainty': None,
                    'validity': {'default':'unknown','rule':'caller_supplied' if direction=='in' else 'finite_numeric_after_get'},
                    'operation_code': int(op), 'semantic_name': n,
                }
    signals['formula_operation_code']={
        'value':None,'unit':'1','frame':'SCALAR','timestamp':{'policy':'fmu_time','unit':'s'},
        'source':'library','quality':None,'uncertainty':None,'validity':{'default':'valid','rule':'operation_catalog_membership'}
    }
    return {'schema_version':'1.0','model':model,'fmi_version':'2.0','fields':['value','unit','frame','timestamp','source','quality','uncertainty','validity'], 'signals':signals}

(R/'fmus').mkdir(exist_ok=True)
for name,vars in [('MESA_NAV',meta['mesa_variables']),('WingMPET',meta['wingmpet_variables'])]:
    d=R/'build'/'stage'/name
    shutil.rmtree(d,ignore_errors=True)
    (d/'binaries'/'linux64').mkdir(parents=True)
    (d/'resources').mkdir(parents=True)
    shutil.copy(R/'build'/(name+'.so'),d/'binaries'/'linux64'/(name+'.so'))
    shutil.copytree(R/'src',d/'sources'/'src')
    catalog=json.loads((R/'resources'/f'{name}_formula_catalog.json').read_text())
    shutil.copy(R/'resources'/f'{name}_formula_catalog.json',d/'resources'/'formula_catalog.json')
    outs=outputs_mesa if name=='MESA_NAV' else outputs_wing
    schema=make_schema(name,vars,catalog,outs)
    (R/'resources'/f'{name}_signal_schema.json').write_text(json.dumps(schema,indent=2,ensure_ascii=False))
    (d/'resources'/'signal_schema.json').write_text(json.dumps(schema,indent=2,ensure_ascii=False))
    cfg=R/'config'/'phoenix_drone_v3.yaml'
    if cfg.exists(): shutil.copy(cfg,d/'resources'/'phoenix_drone_v3.yaml')
    formalism = R/'docs'/('MESA_NAV_Multifield_Excited_Self_Anchoring_Navigation__FA_v20260820_1141.md' if name=='MESA_NAV' else 'WingMPET_Unified_fixed_wing_mechanical_path_time_energy_formalism_FA_v20260820_1141.md')
    if formalism.exists(): shutil.copy(formalism,d/'resources'/'formalism.md')
    tddt=R/'resources'/'tddt_layer2'/name
    if tddt.exists(): shutil.copytree(tddt,d/'resources'/'tddt_layer2',dirs_exist_ok=True)
    rows=[]; outidx=[]
    # Canonical variables
    for k,n in enumerate(vars):
        if not expose_canonical(name,n): continue
        c='output' if (n in outs or n.startswith('output_')) else 'input'
        init='calculated' if c=='output' else 'exact'
        generic=bool(re.fullmatch(r'(input|output)_\d+',n))
        unit=infer_unit(n,generic=generic); frame=infer_frame(n,name)
        description=desc_for(n,unit,'CONTEXT_DEPENDENT' if generic else frame,generic)
        real = f'<Real unit="{html.escape(unit)}"/>' if c=='output' else f'<Real start="0" unit="{html.escape(unit)}"/>'
        rows.append(f'<ScalarVariable name="{html.escape(n)}" valueReference="{k}" causality="{c}" variability="continuous" initial="{init}" description="{html.escape(description)}">{real}</ScalarVariable>')
        if c=='output': outidx.append(len(rows))
    op_vr=vars.index('operation_code'); in0=vars.index('input_001'); out0=vars.index('output_001')
    # Explicit named aliases
    for op,desc in catalog['operations'].items():
        prefix=f'op{int(op):02d}'
        for j,n in enumerate(desc['inputs']):
            alias=f'{prefix}__in__{n}'; unit=infer_unit(n); frame=infer_frame(n,name)
            description=desc_for(alias,unit,frame)
            rows.append(f'<ScalarVariable name="{html.escape(alias)}" valueReference="{in0+j}" causality="input" variability="continuous" initial="exact" description="{html.escape(description)}"><Real start="0" unit="{html.escape(unit)}"/></ScalarVariable>')
        for j,n in enumerate(desc['outputs']):
            alias=f'{prefix}__out__{n}'; unit=infer_unit(n); frame=infer_frame(n,name)
            description=desc_for(alias,unit,frame)
            rows.append(f'<ScalarVariable name="{html.escape(alias)}" valueReference="{out0+j}" causality="output" variability="continuous" initial="calculated" description="{html.escape(description)}"><Real unit="{html.escape(unit)}"/></ScalarVariable>')
            outidx.append(len(rows))
    rows.append(f'<ScalarVariable name="formula_operation_code" valueReference="{op_vr}" causality="input" variability="continuous" initial="exact" description="Operation selector; dimensionless."><Real start="0" unit="1"/></ScalarVariable>')
    xml=('<?xml version="1.0" encoding="UTF-8"?>'
         f'<fmiModelDescription fmiVersion="2.0" modelName="{name}" guid="{meta["guids"][name]}" generationTool="twins_layer2 native C++ explicit-formula-FMI with semantic signal schema" variableNamingConvention="flat" numberOfEventIndicators="0">'
         f'<CoSimulation modelIdentifier="{name}" needsExecutionTool="false" canHandleVariableCommunicationStepSize="true" canInterpolateInputs="false" maxOutputDerivativeOrder="0" canRunAsynchronuously="false" canBeInstantiatedOnlyOncePerProcess="false" canNotUseMemoryManagementFunctions="true"/>'
         +unit_xml()+
         '<ModelVariables>'+''.join(rows)+'</ModelVariables><ModelStructure><Outputs>'+''.join(f'<Unknown index="{x}"/>' for x in outidx)+'</Outputs></ModelStructure></fmiModelDescription>')
    (d/'modelDescription.xml').write_text(xml)
    with zipfile.ZipFile(R/'fmus'/(name+'.fmu'),'w',zipfile.ZIP_DEFLATED) as z:
        for q in d.rglob('*'):
            if q.is_file(): z.write(q,q.relative_to(d))
