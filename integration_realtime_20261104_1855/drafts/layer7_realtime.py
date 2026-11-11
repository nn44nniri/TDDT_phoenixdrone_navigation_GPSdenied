"""Time-matched, command-correlated sensor-domain feedback; never FCU truth aiding."""
import hashlib,json
import numpy as np


def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def feedback(nav,receipt,pending,model):
    if not receipt:return {'ok':False,'reason':'receipt_pending'}
    correlation=receipt.get('correlation') or {};record=correlation.get('record') or {}
    context=record.get('context') or {};tx=context.get('transaction_id')
    if correlation.get('available') is not True or correlation.get('record_hash')!=digest(record):
        return {'ok':False,'reason':'bounded_MAVLink_receipt_pending'}
    if tx not in pending:return {'ok':False,'reason':'unknown_or_already_consumed_command'}
    decision=pending[tx];requested=context.get('requested_gui_state') or {}
    if [requested.get(k) for k in ('roll','pitch','throttle','yaw')]!=decision['u_cmd']:
        return {'ok':False,'reason':'executed_command_identity_mismatch'}
    tube=decision['prediction']['prediction_tube'];t=float(nav['time_s'])
    if not tube[0]['time_s']<t<=tube[-1]['time_s']:
        return {'ok':False,'reason':'observation_outside_original_prediction_time'}
    bounds=record.get('plant_time_bounds') or {}
    if not bounds.get('verified') or t<float(bounds['upper_s']):
        return {'ok':False,'reason':'observation_not_after_verified_command_send'}
    times=np.array([row['time_s'] for row in tube]);states=np.array([row['state'] for row in tube])
    predicted=np.array([np.interp(t,times,states[:,j]) for j in range(6)])
    actual=np.array(nav['position_ned_m']+nav['velocity_ned_m_s']);residual=actual-predicted
    age=t-decision['source_time_s'];old=np.array(model['disturbance_ned_m_s2'])
    # Model adaptation from the approved-sensor reconstructed velocity only.
    # No FCU POSITION/VELOCITY/ATTITUDE residual is routed into MESA.
    correction=np.clip(residual[3:]/max(.1,age),-2.,2.)
    update={'disturbance_ned_m_s2':np.clip(old+.06*correction,-2.,2.).tolist(),
        'feedback_version':int(model['feedback_version'])+1,'feedback_source':tx,
        'inner_loop_tau_s':float(model['inner_loop_tau_s'])}
    evidence={'ok':True,'source_transaction_id':tx,'source_snapshot_hash':decision['snapshot_hash'],
        'candidate_id':decision['candidate_id'],'observation_time_s':t,
        'prediction_source_time_s':decision['source_time_s'],'prediction_retimed':False,
        'state_residual':residual.tolist(),'receipt_record_hash':correlation['record_hash'],
        'model_update':update,'destination':'Layer2/model-only','sensor_only':True,
        'FCU_pose_feedback':False,'GNSS_used':False,'actual_u_cmd':decision['u_cmd']}
    evidence['route_hash']=digest(evidence);pending.pop(tx)
    return evidence
