from pathlib import Path
import math
from layer5.config import Layer5Config
from layer5.phoenix import PhoenixSpecialization


def _ph():
    root=Path(__file__).resolve().parents[1]
    return PhoenixSpecialization(Layer5Config.from_yaml(root/'configs/configuration_phoenix.yaml'))


def _state(*, pd=0.0, vd=0.0, va=2.0, gamma=0.0, psi=0.0):
    x=[0.0]*19
    x[2]=pd; x[5]=vd; x[9]=va; x[10]=gamma; x[11]=psi
    return x


def _cand(phase, a, b, context=None):
    out={'tdoc_phase':phase,'trajectory':[a,b],'actions':[[0.0]*5]}
    if context is not None: out['phoenix_operating_context']=context
    return out


def _ctx():
    return {'air_density_kg_m3':0.958,'battery_soc':0.99,'environment_fingerprint':'solution6'}


def test_vertical_takeoff_uses_vertical_rate_not_airspeed_law():
    ph=_ph()
    c1=_cand('vertical_takeoff',_state(vd=-1.0,va=1.0),_state(vd=-1.5,va=2.0),_ctx())
    c2=_cand('vertical_takeoff',_state(vd=-1.0,va=12.0),_state(vd=-1.5,va=15.0),_ctx())
    u1,d1=ph._trajectory_nominal_controls(c1); u2,d2=ph._trajectory_nominal_controls(c2)
    assert d1['controller_regime']=='vertical_takeoff_force_vertical_rate'
    assert abs(u1[0][2]-u2[0][2]) < 1e-12
    seed=ph.offline_vtol_seed_controls(_ctx())
    pb=seed['physics_basis']; dt=1.0
    required=pb['mass_kg']*(pb['gravity_m_s2']-(-1.5-(-1.0))/dt)
    expected=math.sqrt(required/(pb['motor_count']*pb['motor_k_t_N_per_rad_s2']*pb['motor_omega_max_rad_s']**2))
    assert abs(u1[0][2]-expected)<1e-12
    assert d1['steps'][0]['vertical_collective_synthesis']['manual_gain_used'] is False


def test_vertical_takeoff_projects_subhover_trajectory_to_source_force_balance_floor():
    ph=_ph()
    candidate=_cand('vertical_takeoff',_state(vd=-1.0,va=2.0),_state(vd=-0.5,va=2.0),_ctx())
    controls,record=ph._trajectory_nominal_controls(candidate)
    assert controls
    step=record['steps'][0]
    projection=step['vertical_collective_synthesis']['airborne_takeoff_floor_projection']
    equilibrium=record['vertical_seed']['equilibrium_collective_norm']
    assert projection['used'] is True
    assert projection['manual_margin_used'] is False
    assert projection['requested_collective_norm'] < equilibrium
    assert abs(step['desired_motor_collective_norm']-equilibrium)<1e-12
    assert projection['full_fidelity_gate_remains_authoritative'] is True


def test_vertical_takeoff_floor_uses_same_transaction_quaternion_vertical_projection():
    ph=_ph(); pitch=math.radians(70.0)
    context={**_ctx(),'attitude_rate_state':{
        'qw':math.cos(pitch/2.0),'qx':0.0,'qy':math.sin(pitch/2.0),'qz':0.0,
        'p_rate':0.0,'q_rate':0.0,'r_rate':0.0,
        'source':'PT_LIB/FMI current simulation state'}}
    controls,record=ph._trajectory_nominal_controls(
        _cand('vertical_takeoff',_state(vd=-1.0,va=2.0),_state(vd=-0.5,va=2.0),context))
    assert controls
    diag=record['steps'][0]['takeoff_tilt_aware_floor']
    equilibrium=record['vertical_seed']['equilibrium_collective_norm']
    expected=equilibrium/math.sqrt(math.sin(pitch))
    assert diag['used'] is True
    assert abs(diag['vertical_thrust_fraction']-math.sin(pitch))<1e-12
    assert abs(diag['unclipped_tilt_collective_floor_norm']-expected)<1e-12
    assert record['steps'][0]['desired_motor_collective_norm']>=expected-1e-12
    assert diag['manual_margin_used'] is False
    assert diag['full_fidelity_gate_remains_authoritative'] is True


def test_online_vertical_takeoff_inverts_trajectory_derived_motor_target_to_pilot_request():
    ph=_ph(); seed=ph.offline_vtol_seed_controls(_ctx()); target=seed['equilibrium_collective_norm']
    request_op=0.756
    ph.runtime_phase_throttle_affine={'vertical_takeoff':{
        'request_bounds_norm':[request_op-0.04,request_op+0.04],
        'operating_request_norm':request_op,
        'target_motor_bounds_norm':[target-0.02,target+0.08],
        'operating_left_motor_norm':target,'operating_right_motor_norm':target,
        'left_gain_motor_norm_per_request_norm':1.25,'right_gain_motor_norm_per_request_norm':1.25}}
    controls,record=ph._trajectory_nominal_controls(
        _cand('vertical_takeoff',_state(vd=-1.0,va=1.0),_state(vd=-1.0,va=1.0),_ctx()))
    assert abs(record['steps'][0]['desired_motor_collective_norm']-target)<1e-12
    assert abs(controls[0][2]-request_op)<1e-12
    assert record['steps'][0]['throttle_coordinate_mapping']=='online_commissioned_ArduPilot_inverse'


def test_layer4_takeoff_envelope_is_derived_from_commissioning_and_motor_transient():
    ph=_ph(); seed=ph.offline_vtol_seed_controls(_ctx()); equilibrium=seed['equilibrium_collective_norm']
    phase_seed=seed['phases']['vertical_takeoff']['throttle_request_norm']
    ph.runtime_certificate={'accepted':True,'kind':'ONLINE_SITL','certificate':{'schema':'PTLIB-Phoenix-control-commission-v2'}}
    ph.nominal_tracker_authorized=True
    ph.runtime_phase_throttle_affine={'vertical_takeoff':{
        'target_motor_bounds_norm':[phase_seed-0.04,phase_seed+0.04]}}
    context={**_ctx(),'execution_segment_s':0.1,
             'motor_speed_state':{'omega_left_rad_s':240.0,'omega_right_rad_s':240.0}}
    contract=ph.layer4_vertical_action_envelope('vertical_takeoff',context,6)
    assert contract['ok'] is True and contract['applied'] is True
    assert contract['not_actuator_command'] is True and contract['actuation_authority'] is False
    assert contract['layer4_full_fidelity_required'] is True
    assert contract['layer5_final_required'] is True and contract['layer6_executor_gate_required'] is True
    assert contract['ardupilot_mixer_authoritative'] is True and contract['direct_jsbsim_actuation'] is False
    assert contract['source_tracker_motor_bounds_norm'][0]==phase_seed-0.04
    assert contract['per_step_delta_bounds'][0]['lower']==phase_seed-0.04-equilibrium
    assert contract['per_step_delta_bounds'][0]['lower'] < 0.0
    steady_upper=phase_seed+0.04-equilibrium
    assert abs(contract['per_step_delta_bounds'][2]['upper']-steady_upper)<1e-12
    assert contract['per_step_delta_bounds'][0]['upper'] < steady_upper
    assert contract['per_step_delta_bounds'][1]['upper'] == contract['per_step_delta_bounds'][0]['upper']
    assert contract['commit_transient']['method']=='source_motor_first_order_commit_inverse'


def test_layer4_hover_envelope_preserves_commissioned_sub_equilibrium_braking_range():
    ph=_ph(); seed=ph.offline_vtol_seed_controls(_ctx()); equilibrium=seed['equilibrium_collective_norm']
    target_lo=equilibrium-0.015
    target_hi=equilibrium+0.065
    ph.runtime_certificate={'accepted':True,'kind':'ONLINE_SITL','certificate':{'schema':'PTLIB-Phoenix-control-commission-v2'}}
    ph.nominal_tracker_authorized=True
    ph.runtime_phase_throttle_affine={'hover':{
        'target_motor_bounds_norm':[target_lo,target_hi]}}
    contract=ph.layer4_vertical_action_envelope('hover',_ctx(),6)
    assert contract['ok'] is True and contract['applied'] is True
    assert contract['source_tracker_motor_bounds_norm'][0]==target_lo
    assert contract['per_step_delta_bounds'][0]['lower']==target_lo-equilibrium
    assert contract['per_step_delta_bounds'][0]['lower'] < 0.0
    assert contract['not_actuator_command'] is True and contract['actuation_authority'] is False
    assert contract['layer4_full_fidelity_required'] is True
    assert contract['layer5_final_required'] is True and contract['layer6_executor_gate_required'] is True
    assert contract['ardupilot_mixer_authoritative'] is True and contract['direct_jsbsim_actuation'] is False


def test_bind_candidate_preserves_exact_tracker_rejection_reason():
    ph=_ph(); ph.nominal_tracker_authorized=True
    candidate=_cand('vertical_takeoff',_state(vd=0.0,va=1.0),_state(vd=-9.0,va=1.0),_ctx())
    seq,record=ph.bind_candidate(candidate)
    assert seq == []
    assert record['source']=='commissioned_layer4_trajectory_reference_tracker'
    assert record['reason']==record['tracker']['reason']
    assert record['reason']!='layer4_to_phoenix_nominal_control_binding_not_validated'


def test_takeoff_first_commit_segment_derives_motor_transient_reserve_from_source_dynamics():
    ph=_ph()
    context={**_ctx(),'execution_segment_s':0.1,
             'motor_speed_state':{
                 'omega_left_rad_s':240.0,'omega_right_rad_s':240.0,
                 'source':'PT_LIB/FMI current simulation state','simulation_only':True,
                 'actuation_authority':False}}
    states=[_state(vd=0.0,va=2.0),_state(vd=-0.01,va=2.0),_state(vd=-0.02,va=2.0)]
    candidate={'tdoc_phase':'vertical_takeoff','trajectory':states,
               'actions':[[0.0]*5,[0.0]*5],'phoenix_operating_context':context}
    controls,record=ph._trajectory_nominal_controls(candidate)
    assert controls
    transient=record['steps'][0]['vertical_collective_synthesis']['commit_segment_motor_transient']
    assert transient['used'] is True
    assert transient['manual_margin_used'] is False
    required=float(transient['required_exit_motor_speed_rad_s'])
    initial=float(transient['initial_min_motor_speed_rad_s'])
    decay=math.exp(-float(transient['execution_segment_s'])/float(transient['motor_tau_up_s']))
    expected=(required-initial*decay)/(1.0-decay)/ph.omega_max
    assert abs(float(transient['compensated_collective_norm'])-expected)<1e-12
    predicted=ph.omega_max*controls[0][2]+(initial-ph.omega_max*controls[0][2])*decay
    assert predicted>=required-1e-9
    assert transient['first_move_only'] is True
    assert transient['full_fidelity_gate_remains_authoritative'] is True


def test_takeoff_residual_is_first_move_only_and_cannot_erase_commit_dynamic_floor():
    ph=_ph()
    context={**_ctx(),'execution_segment_s':0.1,
             'motor_speed_state':{
                 'omega_left_rad_s':637.6,'omega_right_rad_s':637.6,
                 'source':'PT_LIB/FMI current simulation state','simulation_only':True,
                 'actuation_authority':False}}
    states=[_state(vd=0.0,va=2.0),_state(vd=-0.01,va=2.0),_state(vd=-0.02,va=2.0)]
    candidate={'tdoc_phase':'vertical_takeoff','trajectory':states,
               'actions':[[0.0]*5,[0.0]*5],'phoenix_operating_context':context,
               'twin_plant_residual_assimilation':{
                   'applied':True,'source_transaction_id':'same-run-previous-tx','route_hash':'route',
                   'attribution_confidence':1.0,'state_residual_evidence':{'velocity_d_m_s':-10.0}}}
    controls,record=ph._trajectory_nominal_controls(candidate)
    assert controls
    first=record['steps'][0]
    second=record['steps'][1]
    assert first['adaptive_first_move']['used'] is True
    dynamic_floor=float(first['adaptive_first_move']['commit_segment_dynamic_floor_norm'])
    assert float(first['desired_motor_collective_norm'])>=dynamic_floor-1e-12
    assert second['adaptive_first_move']['used'] is False
    assert second['adaptive_first_move']['reason']=='residual_correction_is_first_move_only'
    assert abs(float(second['adaptive_first_move'].get('applied_motor_delta_norm',0.0)))<1e-12


def test_online_vertical_takeoff_inverse_accepts_only_recorded_pwm_quantization_at_target_floor():
    ph=_ph(); context={**_ctx(),'vertical_takeoff_accel_request_m_s2':0.0}
    target=ph.offline_vtol_seed_controls(context)['phases']['vertical_takeoff']['throttle_request_norm']
    target_lo=ph.offline_vtol_seed_controls(context)['equilibrium_collective_norm']
    request_lo=0.73682; request_op=0.756876; gain=1.248657
    ph.runtime_phase_throttle_affine={'vertical_takeoff':{
        'request_bounds_norm':[request_lo,0.788876],
        'target_motor_bounds_norm':[target_lo,target+0.04],
        'operating_request_norm':request_op,
        'operating_left_motor_norm':0.821,'operating_right_motor_norm':0.821,
        'left_gain_motor_norm_per_request_norm':gain,'right_gain_motor_norm_per_request_norm':gain,
        'motor_feedback_quantization_norm':0.001,'max_target_motor_error_norm':0.00023}}
    candidate=_cand('vertical_takeoff',_state(vd=-1.0,va=2.0),_state(vd=-1.0,va=2.0),
                    {**context,'execution_segment_s':0.1})
    candidate['twin_plant_residual_assimilation']={
        'applied':True,'source_transaction_id':'tx','route_hash':'route','attribution_confidence':1.0,
        'state_residual_evidence':{'velocity_d_m_s':-10.0}}
    controls,record=ph._trajectory_nominal_controls(candidate)
    assert controls and abs(record['steps'][0]['desired_motor_collective_norm']-target_lo)<1e-12
    assert controls[0][2] == request_lo


def test_online_vertical_takeoff_inverse_rejects_error_beyond_recorded_quantization():
    ph=_ph(); target=ph.offline_vtol_seed_controls(_ctx())['phases']['vertical_takeoff']['throttle_request_norm']
    ph.runtime_phase_throttle_affine={'vertical_takeoff':{
        'request_bounds_norm':[0.75,0.76],
        'target_motor_bounds_norm':[target-0.04,target+0.04],
        'operating_request_norm':0.755,
        'operating_left_motor_norm':target,'operating_right_motor_norm':target,
        'left_gain_motor_norm_per_request_norm':1.25,'right_gain_motor_norm_per_request_norm':1.25,
        'motor_feedback_quantization_norm':0.001,'max_target_motor_error_norm':0.0002}}
    controls,record=ph._trajectory_nominal_controls(
        _cand('vertical_takeoff',_state(vd=0.0,va=1.0),_state(vd=-1.3,va=1.0),_ctx()))
    assert controls == []
    assert record['reason'] == 'phase_motor_to_pilot_inverse_unavailable'


def test_online_vertical_takeoff_projects_sub_bin_upper_request_to_commissioned_endpoint():
    ph=_ph(); context=_ctx(); seed=ph.offline_vtol_seed_controls(context)
    phase_seed=seed['phases']['vertical_takeoff']['throttle_request_norm']
    target_hi=phase_seed+0.04; requested=target_hi+0.0002
    request_lo=0.72; request_hi=0.788876; request_op=0.756876; gain=1.25
    motor_op=target_hi-gain*(request_hi-request_op)
    ph.runtime_phase_throttle_affine={'vertical_takeoff':{
        'request_bounds_norm':[request_lo,request_hi],
        'target_motor_bounds_norm':[seed['equilibrium_collective_norm'],target_hi],
        'operating_request_norm':request_op,
        'operating_left_motor_norm':motor_op,'operating_right_motor_norm':motor_op,
        'left_gain_motor_norm_per_request_norm':gain,'right_gain_motor_norm_per_request_norm':gain,
        'motor_feedback_quantization_norm':0.001,'max_target_motor_error_norm':0.00023,
        'max_affine_residual_norm':0.0}}
    pb=seed['physics_basis']; available=pb['motor_count']*pb['motor_k_t_N_per_rad_s2']*pb['motor_omega_max_rad_s']**2
    accel_down=pb['gravity_m_s2']-available*requested*requested/pb['mass_kg']
    s0=_state(vd=-1.0,va=2.0); s1=_state(vd=-1.0+accel_down,va=2.0)
    controls,record=ph._trajectory_nominal_controls(_cand('vertical_takeoff',s0,s1,context))
    assert controls
    assert abs(record['steps'][0]['desired_motor_collective_norm']-requested)<1e-12
    assert controls[0][2] == request_hi
    assert record['steps'][0]['throttle_coordinate_mapping'].endswith('measurement_tolerance_projection')


def test_online_vertical_takeoff_still_rejects_upper_request_beyond_certificate_tolerance():
    ph=_ph(); context=_ctx(); seed=ph.offline_vtol_seed_controls(context)
    phase_seed=seed['phases']['vertical_takeoff']['throttle_request_norm']
    target_hi=phase_seed+0.04; requested=target_hi+0.0011
    ph.runtime_phase_throttle_affine={'vertical_takeoff':{
        'request_bounds_norm':[0.72,0.80],
        'target_motor_bounds_norm':[seed['equilibrium_collective_norm'],target_hi],
        'operating_request_norm':0.76,
        'operating_left_motor_norm':phase_seed,'operating_right_motor_norm':phase_seed,
        'left_gain_motor_norm_per_request_norm':1.25,'right_gain_motor_norm_per_request_norm':1.25,
        'motor_feedback_quantization_norm':0.001,'max_target_motor_error_norm':0.00023,
        'max_affine_residual_norm':0.0}}
    pb=seed['physics_basis']; available=pb['motor_count']*pb['motor_k_t_N_per_rad_s2']*pb['motor_omega_max_rad_s']**2
    accel_down=pb['gravity_m_s2']-available*requested*requested/pb['mass_kg']
    controls,record=ph._trajectory_nominal_controls(
        _cand('vertical_takeoff',_state(vd=-1.0,va=2.0),_state(vd=-1.0+accel_down,va=2.0),context))
    assert controls == []
    assert record['reason']=='vertical_takeoff_trajectory_collective_outside_source_braking_envelope'


def test_vertical_takeoff_residual_first_move_preserves_source_derived_phase_reserve():
    ph=_ph(); context={**_ctx(),'execution_segment_s':0.1}
    candidate=_cand('vertical_takeoff',_state(vd=-1.0,va=2.0),_state(vd=-1.5,va=2.5),context)
    candidate['twin_plant_residual_assimilation']={
        'applied':True,
        'source_transaction_id':'tx-previous',
        'route_hash':'route-previous',
        'attribution_confidence':1.0,
        'state_residual_evidence':{'velocity_d_m_s':-10.0},
    }
    controls,record=ph._trajectory_nominal_controls(candidate)
    diag=record['steps'][0]['adaptive_first_move']
    limit=record['limits']['vertical_takeoff_max_feedback_correction_norm']
    assert diag['used'] is True
    assert diag['trust_region_saturated'] is True
    assert diag['raw_motor_delta_norm'] < -limit
    assert abs(diag['applied_motor_delta_norm']) <= limit+1e-12
    seed=ph.offline_vtol_seed_controls(context)['phases']['vertical_takeoff']['throttle_request_norm']
    equilibrium=ph.offline_vtol_seed_controls(context)['equilibrium_collective_norm']
    assert abs(controls[0][2]-seed) <= limit+1e-12
    assert abs(diag['nominal_tracker_delta_norm']+diag['bounded_motor_delta_norm']) <= limit+1e-12
    assert controls[0][2] >= diag['source_braking_collective_floor_norm']-1e-12
    assert diag['source_braking_collective_floor_norm'] >= equilibrium-1e-12
    assert abs(diag['hover_equilibrium_collective_norm']-equilibrium) < 1e-12
    assert diag['source_braking_envelope_preserved'] is True
    assert 0.0 <= controls[0][2] <= 1.0


def test_vertical_takeoff_commissioning_includes_source_bound_braking_below_equilibrium():
    ph=_ph(); seed=ph.offline_vtol_seed_controls(_ctx())
    request=ph.online_phase_commissioning_requests(_ctx())['requests']['vertical_takeoff']
    lo,hi=request['target_motor_bounds_norm']
    assert lo < seed['equilibrium_collective_norm'] < hi
    assert request['basis']=='source_force_balance_takeoff_plus_source_bound_braking_feedback'


def test_vertical_pitch_gains_are_derived_from_source_dynamics_and_l4_timing_contract():
    ph=_ph()
    ph.runtime_wingmpet_surface_matrix_rad=[[0.3,-0.3],[-0.3,-0.3]]
    pitch=math.radians(75.0)
    context={**_ctx(),'airspeed_m_s':6.0,'execution_segment_s':0.1,
             'forecast_step_s':1.0,'horizon_steps':6,
             'attitude_rate_state':{
                 'qw':math.cos(pitch/2.0),'qx':0.0,'qy':math.sin(pitch/2.0),'qz':0.0,
                 'p_rate':0.0,'q_rate':0.5,'r_rate':0.0,
                 'source':'PT_LIB/FMI current simulation state'}}
    trajectory=[_state(vd=-1.0,va=6.0) for _ in range(7)]
    candidate={'tdoc_phase':'vertical_takeoff','trajectory':trajectory,
               'actions':[[0.0]*5 for _ in range(6)],'phoenix_operating_context':context}
    controls,record=ph._trajectory_nominal_controls(candidate)
    step=record['steps'][0]
    effectiveness=abs(record['vertical_seed']['physics_basis']['takeoff_propwash_pitch_slope_Nm_per_rad']/
                      record['vertical_seed']['physics_basis']['takeoff_propwash_terms']['Iyy_kg_m2'])
    omega=4.0/1.0
    dt=0.1; pole=math.exp(-omega*dt); gap=1.0-pole
    assert controls
    assert step['gain_schedule']=='SOURCE_DYNAMICS_DISCRETE_CRITICAL_DAMPING'
    assert abs(step['attitude_gain_rad_per_rad']-(gap*gap)/(dt*dt*effectiveness))<1e-12
    assert abs(step['rate_gain_rad_per_rad_s']-(2.0*gap-0.5*gap*gap)/(dt*effectiveness))<1e-12
    assert abs(step['prediction_pole']-pole)<1e-12
    assert step['settling_horizon_s']==1.0


def test_forecast_tail_reserves_latter_half_for_terminal_invariance():
    ph=_ph()
    ph.runtime_wingmpet_surface_matrix_rad=[[0.3,-0.3],[-0.3,-0.3]]
    pitch=math.radians(75.0)
    base={**_ctx(),'airspeed_m_s':6.0,'execution_segment_s':1.0,
          'forecast_step_s':1.0,'horizon_steps':6,
          'attitude_rate_state':{'qw':math.cos(pitch/2.0),'qx':0.0,
              'qy':math.sin(pitch/2.0),'qz':0.0,'p_rate':0.0,
              'q_rate':0.5,'r_rate':0.0,'source':'WingMPET forecast state'}}
    trajectory=[_state(vd=-1.0,va=6.0) for _ in range(7)]
    def record(ctx):
        _,r=ph._trajectory_nominal_controls({'tdoc_phase':'vertical_takeoff',
            'trajectory':trajectory,'actions':[[0.0]*5 for _ in range(6)],
            'phoenix_operating_context':ctx})
        return r['steps'][0]
    commit=record(dict(base))
    horizon=record({**base,'full_fidelity_lane':'FORECAST_HORIZON'})
    assert commit['settling_horizon_s']==1.0
    assert horizon['settling_horizon_s']==3.0
    assert horizon['transient_steps']==3
    assert horizon['terminal_reserve_steps']==3
    assert horizon['attitude_gain_rad_per_rad'] < commit['attitude_gain_rad_per_rad']
    assert horizon['rate_gain_rad_per_rad_s'] < commit['rate_gain_rad_per_rad_s']


def test_vertical_pitch_uses_mission_quaternion_error_across_euler_singularity():
    ph=_ph()
    ph.runtime_wingmpet_surface_matrix_rad=[[0.3,-0.3],[-0.3,-0.3]]
    heading=math.radians(45.0); vertical=math.pi/2.0; over_vertical=math.radians(125.0)
    def attitude_quaternion(pitch):
        cy,sy=math.cos(heading/2.0),math.sin(heading/2.0)
        cp,sp=math.cos(pitch/2.0),math.sin(pitch/2.0)
        return [cy*cp,-sy*sp,cy*sp,sy*cp]
    target=attitude_quaternion(vertical); current=attitude_quaternion(over_vertical)
    context={**_ctx(),'airspeed_m_s':5.0,'execution_segment_s':0.1,
             'forecast_step_s':1.0,'horizon_steps':6,
             'vertical_attitude_reference':{
                 'quaternion_wxyz':target,'initialization_reference_only':True,
                 'actuation_authority':False},
             'attitude_rate_state':{
                 'qw':current[0],'qx':current[1],'qy':current[2],'qz':current[3],
                 'p_rate':0.0,'q_rate':0.5,'r_rate':0.0,
                 'source':'PT_LIB/FMI current simulation state'}}
    trajectory=[_state(vd=-1.0,va=5.0) for _ in range(7)]
    candidate={'tdoc_phase':'vertical_takeoff','trajectory':trajectory,
               'actions':[[0.0]*5 for _ in range(6)],'phoenix_operating_context':context}
    controls,record=ph._trajectory_nominal_controls(candidate)
    step=record['steps'][0]
    assert controls
    assert step['vertical_attitude_error_source']=='mission_reference_quaternion_pitch_twist'
    assert abs(step['vertical_attitude_error_rad']-math.radians(35.0))<1e-12
    # At 125 degrees the Euler asin representation is 55 degrees and would have
    # the wrong error sign.  The quaternion controller must request a surface that
    # produces negative q acceleration under the source Cm_delta < 0 model.
    assert step['surface_request_rad'] > 0.0
    assert step['predicted_pitch_rate_rad_s'] < 0.5


def test_vertical_takeoff_stabilizes_roll_with_source_propwash_and_commissioned_mixer():
    ph=_ph()
    # WingMPET effective surface coordinates: aileron produces dL<0,dR>0,
    # while elevator produces equal negative symmetric deflection.
    ph.runtime_wingmpet_surface_matrix_rad=[[-0.33125,-0.33125],[0.33125,-0.33125]]
    target=[math.sqrt(0.5),0.0,math.sqrt(0.5),0.0]
    roll_error=math.radians(12.0)
    # q_current = q_target * q_roll_error, so conjugate(target)*current is
    # exactly the positive roll error used by the controller.
    rw,rx=math.cos(roll_error/2.0),math.sin(roll_error/2.0)
    tw,tx,ty,tz=target
    current=[tw*rw-tx*rx, tw*rx+tx*rw, ty*rw+tz*rx, -ty*rx+tz*rw]
    context={**_ctx(),'airspeed_m_s':4.0,'execution_segment_s':0.1,
             'forecast_step_s':1.0,'horizon_steps':6,
             'vertical_attitude_reference':{'quaternion_wxyz':target,
                 'initialization_reference_only':True,'actuation_authority':False},
             'attitude_rate_state':{
                 'qw':current[0],'qx':current[1],'qy':current[2],'qz':current[3],
                 'p_rate':0.4,'q_rate':0.0,'r_rate':0.0,
                 'source':'PT_LIB/FMI current simulation state'}}
    trajectory=[_state(vd=-1.0,va=4.0) for _ in range(7)]
    controls,record=ph._trajectory_nominal_controls({'tdoc_phase':'vertical_takeoff',
        'trajectory':trajectory,'actions':[[0.0]*5 for _ in range(6)],
        'phoenix_operating_context':context})
    step=record['steps'][0]
    assert controls and step['roll_stabilization_active'] is True
    assert abs(step['vertical_roll_attitude_error_rad']-roll_error)<1e-12
    assert step['differential_surface_request_rad'] < 0.0
    assert controls[0][0] < 0.0
    assert step['predicted_roll_rate_rad_s'] < 0.4
    assert record['vertical_seed']['physics_basis']['takeoff_propwash_roll_slope_Nm_per_rad'] > 0.0


def test_vertical_takeoff_consumes_mesa_local_guidance_as_source_bounded_thrust_attitude():
    ph=_ph()
    ph.runtime_wingmpet_surface_matrix_rad=[[-0.33125,-0.33125],[0.33125,-0.33125]]
    target=[math.sqrt(0.5),0.0,math.sqrt(0.5),0.0]
    context={**_ctx(),'airspeed_m_s':4.0,'execution_segment_s':0.1,
             'forecast_step_s':1.0,'horizon_steps':6,
             'vertical_attitude_reference':{'quaternion_wxyz':target,
                 'initialization_reference_only':True,'actuation_authority':False},
             'attitude_rate_state':{
                 'qw':target[0],'qx':target[1],'qy':target[2],'qz':target[3],
                 'p_rate':0.0,'q_rate':0.0,'r_rate':0.0,
                 'source':'PT_LIB/FMI current simulation state'},
             'mesa_local_guidance':{
                 'schema':'TDOC-MESA-local-guidance-v1','phase':'vertical_takeoff',
                 'frame':'PROJECTED_NED','position_ned_m':[-3.0,-2.0,-1800.0],
                 'ground_velocity_ned_m_s':[-2.0,-1.0,-0.7],
                 'target_position_ned_m':[0.0,0.0,-1804.0],
                 'horizontal_velocity_target_ned_m_s':[0.0,0.0],
                 'navigation_authority':'MESA_STD','truth_injection_to_mesa':False,
                 'actuation_authority':False}}
    s0=_state(pd=-1800.0,vd=-0.7,va=4.0); s1=_state(pd=-1801.5,vd=-2.2,va=4.0)
    s0[0],s0[1],s0[3],s0[4]=-3.0,-2.0,-2.0,-1.0
    s1[0],s1[1],s1[3],s1[4]=-4.0,-2.5,-1.8,-0.9
    controls,record=ph._trajectory_nominal_controls({'tdoc_phase':'vertical_takeoff',
        'trajectory':[s0,s1]+[s1[:] for _ in range(5)],
        'actions':[[0.0]*5 for _ in range(6)],'phoenix_operating_context':context})
    assert controls
    guidance=record['steps'][0]['horizontal_local_guidance']
    assert guidance['applied'] is True
    assert guidance['navigation_authority']=='MESA_STD'
    assert guidance['manual_gain_used'] is False
    assert guidance['desired_horizontal_acceleration_ned_m_s2'][0] > 0.0
    assert guidance['desired_horizontal_acceleration_ned_m_s2'][1] > 0.0
    assert guidance['target_thrust_axis_ned'][0] > 0.0
    assert guidance['target_thrust_axis_ned'][1] > 0.0
    assert guidance['direct_pwm'] is False and guidance['direct_jsbsim_actuation'] is False
    compensation=record['steps'][0]['vertical_collective_synthesis']['horizontal_hold_thrust_compensation']
    assert compensation['used'] is True and compensation['manual_margin_used'] is False
    assert compensation['compensated_collective_norm'] >= compensation['uncorrected_collective_norm']
    assert compensation['full_fidelity_gate_remains_authoritative'] is True


def test_mesa_thrust_axis_respects_collective_ceiling_over_the_full_prediction_horizon():
    ph=_ph()
    ph.runtime_wingmpet_surface_matrix_rad=[[-0.33125,-0.33125],[0.33125,-0.33125]]
    target=[math.sqrt(0.5),0.0,math.sqrt(0.5),0.0]
    context={**_ctx(),'airspeed_m_s':4.0,'execution_segment_s':0.1,
             'forecast_step_s':1.0,'horizon_steps':6,
             'vertical_attitude_reference':{'quaternion_wxyz':target,
                 'initialization_reference_only':True,'actuation_authority':False},
             'attitude_rate_state':{
                 'qw':target[0],'qx':target[1],'qy':target[2],'qz':target[3],
                 'p_rate':0.0,'q_rate':0.0,'r_rate':0.0,
                 'source':'PT_LIB/FMI current simulation state'},
             'mesa_local_guidance':{
                 'schema':'TDOC-MESA-local-guidance-v1','phase':'vertical_takeoff',
                 'frame':'PROJECTED_NED','position_ned_m':[-3.0,-2.0,-1800.0],
                 'ground_velocity_ned_m_s':[-2.0,-1.0,-0.4],
                 'target_position_ned_m':[0.0,0.0,-1804.0],
                 'horizontal_velocity_target_ned_m_s':[0.0,0.0],
                 'navigation_authority':'MESA_STD','truth_injection_to_mesa':False,
                 'actuation_authority':False}}
    velocities=[-0.4,-0.7,-1.7,-1.7,-1.7,-1.7,-1.7]
    states=[]
    for i,vd in enumerate(velocities):
        s=_state(pd=-1800.0-float(i),vd=vd,va=4.0)
        s[0],s[1],s[3],s[4]=-3.0,-2.0,-2.0,-1.0
        states.append(s)
    controls,record=ph._trajectory_nominal_controls({'tdoc_phase':'vertical_takeoff',
        'trajectory':states,'actions':[[0.0]*5 for _ in range(6)],
        'phoenix_operating_context':context})
    assert controls
    ceiling=record['vertical_seed']['phases']['vertical_takeoff']['throttle_request_norm'] \
        + record['limits']['vertical_takeoff_max_feedback_correction_norm']
    desired=[step['desired_motor_collective_norm'] for step in record['steps']]
    assert max(desired)<=ceiling+1e-12
    guidance=record['steps'][0]['horizontal_local_guidance']
    assert guidance['force_cap_horizon_edges']==6
    assert guidance['vertical_force_horizon_max_N']==max(guidance['vertical_force_profile_N'])
    assert guidance['vertical_force_profile_N'][1]>guidance['vertical_force_profile_N'][0]
    assert guidance['full_fidelity_gate_remains_authoritative'] is True


def test_mesa_thrust_axis_uses_only_qualified_roll_pitch_allocation_when_yaw_is_unqualified():
    ph=_ph()
    ph.runtime_wingmpet_surface_matrix_rad=[[-0.33125,-0.33125],[0.33125,-0.33125]]
    # Reproduce the geometry at the first incomplete Online cycle: the desired
    # horizontal correction has rotated away from the current pitch plane, while
    # differential-thrust yaw remains intentionally unqualified.
    current=[0.5851100655100467,-0.21079934824820606,
             0.6564669055680448,0.42692042339395764]
    target=[math.sqrt(0.5),0.0,math.sqrt(0.5),0.0]
    context={**_ctx(),'airspeed_m_s':3.2,'execution_segment_s':0.1,
             'forecast_step_s':1.0,'horizon_steps':6,
             'vertical_attitude_reference':{'quaternion_wxyz':target,
                 'initialization_reference_only':True,'actuation_authority':False},
             'attitude_rate_state':{
                 'qw':current[0],'qx':current[1],'qy':current[2],'qz':current[3],
                 'p_rate':-0.14915104271900975,'q_rate':0.5393524196587638,
                 'r_rate':0.6138254083879796,
                 'source':'PT_LIB/FMI current simulation state'},
             'mesa_local_guidance':{
                 'schema':'TDOC-MESA-local-guidance-v1','phase':'vertical_takeoff',
                 'frame':'PROJECTED_NED','position_ned_m':[-1.2141260504722595,-0.31838889577193186,-1799.0],
                 'ground_velocity_ned_m_s':[-1.4075501455603168,0.9060320595782957,-0.3348041014137714],
                 'target_position_ned_m':[0.0,0.0,-1804.0],
                 'horizontal_velocity_target_ned_m_s':[0.0,0.0],
                 'navigation_authority':'MESA_STD','truth_injection_to_mesa':False,
                 'actuation_authority':False}}
    states=[]
    for i in range(7):
        s=_state(pd=-1799.0-i*0.2,vd=-0.3348041014137714,va=3.2)
        s[0],s[1]=-1.2141260504722595,-0.31838889577193186
        s[3],s[4]=-1.4075501455603168,0.9060320595782957
        states.append(s)
    controls,record=ph._trajectory_nominal_controls({'tdoc_phase':'vertical_takeoff',
        'trajectory':states,'actions':[[0.0]*5 for _ in range(6)],
        'phoenix_operating_context':context})
    assert controls
    step=record['steps'][0]; allocation=step['horizontal_local_guidance']
    assert allocation['attitude_allocation']=='underactuated_body_x_roll_then_body_y_pitch'
    assert allocation['qualified_attitude_axes']==['body_x_roll','body_y_pitch']
    assert allocation['unqualified_attitude_axis']=='body_z_yaw'
    assert allocation['differential_thrust_used'] is False
    assert step['vertical_attitude_error_source']=='mesa_reachable_thrust_axis_allocation'
    assert abs(allocation['roll_alignment_increment_rad'])>math.radians(45.0)
    # The command must rotate toward the reachable pitch plane.  The former
    # generic quaternion twist selected the opposite aileron sign here.
    assert controls[0][0]*allocation['roll_alignment_increment_rad']>0.0
    assert controls[0][3]==0.0
    assert allocation['full_fidelity_gate_remains_authoritative'] is True


def test_near_antipodal_pitch_axis_uses_equivalent_signed_pitch_not_pi_roll():
    ph=_ph()
    separation=0.00046262349345943635
    roll,pitch,evidence=ph._shortest_roll_pitch_axis_allocation(
        -1.0,-6.575e-5,separation)
    assert abs(evidence['raw_roll_alignment_increment_rad'])>math.radians(179.0)
    assert evidence['canonical_axis_symmetry_applied'] is True
    assert evidence['pitch_axis_sign']==-1.0
    assert abs(roll)<math.radians(1.0)
    assert pitch<0.0
    assert abs(abs(pitch)-separation)<1e-9


def test_already_aligned_thrust_axis_keeps_zero_roll_pitch_with_complete_evidence():
    ph=_ph()
    roll,pitch,evidence=ph._shortest_roll_pitch_axis_allocation(1.0,0.0,0.0)
    assert roll==0.0 and pitch==0.0
    assert evidence['canonical_axis_symmetry_applied'] is False
    assert evidence['pitch_alignment_factor']==1.0


def test_hover_uses_force_altitude_vertical_rate_not_airspeed():
    ph=_ph()
    c1=_cand('hover',_state(pd=10,vd=0.5,va=1.0),_state(pd=11,vd=0.8,va=2.0),_ctx())
    c2=_cand('hover',_state(pd=10,vd=0.5,va=10.0),_state(pd=11,vd=0.8,va=15.0),_ctx())
    u1,d1=ph._trajectory_nominal_controls(c1); u2,d2=ph._trajectory_nominal_controls(c2)
    assert d1['controller_regime']=='hover_force_altitude_vertical_rate'
    assert abs(u1[0][2]-u2[0][2]) < 1e-12


def test_hover_retains_source_bound_vertical_attitude_tracker():
    ph=_ph()
    ph.runtime_wingmpet_surface_matrix_rad=[[-0.33125,-0.33125],[0.33125,-0.33125]]
    target_pitch=math.pi/2.0; current_pitch=math.radians(70.0)
    target=[math.cos(target_pitch/2.0),0.0,math.sin(target_pitch/2.0),0.0]
    current=[math.cos(current_pitch/2.0),0.0,math.sin(current_pitch/2.0),0.0]
    context={**_ctx(),'airspeed_m_s':5.0,'execution_segment_s':0.1,
             'forecast_step_s':1.0,'horizon_steps':2,
             'vertical_attitude_reference':{'quaternion_wxyz':target,
                 'initialization_reference_only':True,'actuation_authority':False},
             'attitude_rate_state':{
                 'qw':current[0],'qx':current[1],'qy':current[2],'qz':current[3],
                 'p_rate':0.0,'q_rate':0.0,'r_rate':0.0,
                 'source':'PT_LIB/FMI current simulation state'}}
    trajectory=[_state(pd=-10.0,vd=0.0,va=5.0) for _ in range(3)]
    controls,record=ph._trajectory_nominal_controls({'tdoc_phase':'hover',
        'trajectory':trajectory,'actions':[[0.0]*5 for _ in range(2)],
        'phoenix_operating_context':context})
    step=record['steps'][0]
    assert controls
    assert step['controller_regime']=='hover_force_altitude_vertical_rate'
    assert step['pitch_stabilization_active'] is True
    assert step['vertical_attitude_error_source']=='mission_reference_quaternion_pitch_twist'
    assert abs(step['vertical_attitude_error_rad']-math.radians(-20.0))<1e-12
    assert step['surface_request_rad'] < 0.0
    assert controls[0][1] > 0.0
    assert step['predicted_pitch_rate_rad_s'] > 0.0
    assert step['state_source']=='PT_LIB/FMI current simulation state'


def test_transition_forward_blends_vertical_and_wing_borne_by_airspeed_authority():
    ph=_ph()
    low=_cand('transition_forward',_state(va=2.0),_state(va=3.0),_ctx())
    high=_cand('transition_forward',_state(va=10.0),_state(va=12.0),_ctx())
    ul,dl=ph._trajectory_nominal_controls(low); uh,dh=ph._trajectory_nominal_controls(high)
    assert dl['controller_regime']=='transition_blended_qLPV_reference'
    assert dl['steps'][0]['lambda_wing_borne'] == 0.0
    assert dh['steps'][0]['lambda_wing_borne'] == 1.0
    assert ul[0][2] != uh[0][2]


def test_transition_back_uses_same_blend_with_reverse_phase_semantics():
    ph=_ph()
    mid=_cand('transition_back',_state(va=8.0),_state(va=6.0),_ctx())
    u,d=ph._trajectory_nominal_controls(mid)
    assert d['controller_regime']=='transition_blended_qLPV_reference'
    assert d['steps'][0]['transition_direction']=='transition_back'
    assert 0.0 < d['steps'][0]['lambda_wing_borne'] < 1.0


def test_vertical_landing_tracks_descent_rate_and_delegates_terminal_corridor_to_layer4():
    ph=_ph(); seed=ph.offline_vtol_seed_controls(_ctx())['phases']['hover']['throttle_request_norm']
    c=_cand('vertical_landing',_state(vd=1.5,va=3.0),_state(vd=1.0,va=10.0),_ctx())
    u,d=ph._trajectory_nominal_controls(c)
    assert d['controller_regime']=='vertical_landing_descent_rate'
    assert u[0][2] <= seed + d['limits']['vertical_landing_max_braking_boost_norm'] + 1e-12
    assert d['steps'][0]['terminal_agl_corridor_authority']=='Layer4 verified landing corridor'


def test_cruise_retains_legacy_airspeed_tracker():
    ph=_ph()
    c1=_cand('cruise',_state(va=10.0),_state(va=10.0))
    c2=_cand('cruise',_state(va=16.0),_state(va=16.0))
    u1,d1=ph._trajectory_nominal_controls(c1); u2,d2=ph._trajectory_nominal_controls(c2)
    assert d1['controller_regime']=='wing_borne_airspeed'
    assert u2[0][2] > u1[0][2]


def test_flightpath_feedback_uses_phoenix_elevon_moment_sign():
    ph=_ph()
    descending=_cand('approach',_state(va=13.0,gamma=-0.2),_state(va=13.0,gamma=-0.2))
    climbing=_cand('climb',_state(va=13.0,gamma=0.2),_state(va=13.0,gamma=0.2))
    u_down,d_down=ph._trajectory_nominal_controls(descending)
    u_up,d_up=ph._trajectory_nominal_controls(climbing)
    assert d_down['steps'][0]['flightpath_angle_rad'] < 0.0
    assert u_down[0][1] > 0.0  # positive symmetric elevon -> Cm_delta<0 -> nose down
    assert d_up['steps'][0]['flightpath_angle_rad'] > 0.0
    assert u_up[0][1] < 0.0    # negative symmetric elevon -> nose up
