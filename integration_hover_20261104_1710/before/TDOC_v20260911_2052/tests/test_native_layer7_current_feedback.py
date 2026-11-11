"""Current wire-backed L6 segments admit transient L7 diagnostics only."""
from copy import deepcopy
from pathlib import Path

import pytest

from test_r6_03_first_move_segment import close
from layer6.command_correlation import digest
from layer6.feedback import build_execution_evidence
from layer6.telemetry_source import native_source_contract
from tddt_tdoc import TDOCConfig
from tddt_tdoc.adapters.layer7 import Layer7Adapter


def case(tmp_path, *, tube=False, canonical=False):
    root=Path(__file__).parents[1]
    cfg=TDOCConfig.from_yaml(root/'configs/tdoc.yaml')
    cfg.data['runtime']['root']=str(tmp_path/'runtime')
    adapter=Layer7Adapter(cfg)
    adapter._ensure()  # Cold import is independent of the measured segment age.
    _,ctx,_,proof=close(native_source_contract())
    boot='b'*32
    proof['snapshot']['status_quality']={'native_session_id':boot}
    proof['result_hash']=digest({k:v for k,v in proof.items() if k!='result_hash'})
    status=proof['snapshot'];snapshot={'snapshot_hash':'snapshot-current'}
    snapshot_hash='snapshot-current'
    if canonical:
        # Production Layer3 serialization contains no declared snapshot_hash;
        # Layer4 derives it from canonical fields rather than the whole wrapper.
        from layer4.snapshot import Layer3SnapshotAdapter
        snapshot={'policy_version':'v1','reference_time_s':1.,'std':{},'topology':{},
            'weights':{},'couplings':{},'center_of_consistency':{},'mismatch':0.,
            'uncertainty_td':{},'guidance':{},'constraints':{},'context':{},
            'stability':{},'slow_policy':{},'consistency_margin':.1,
            'provenance':{'snapshot_kind':'atomic_layer3_network_snapshot','fast_index':1},
            'registry':{'noncanonical_wrapper_metadata':True}}
        snapshot_hash=Layer3SnapshotAdapter(None).canonical_hash(snapshot)
    packet={'transaction_id':'tx','u_cmd':[0.,0.,0.,0.],
            'candidate_id':'C1','snapshot_hash':snapshot_hash}
    ack={'native_session_id':boot,'command_instance_id':ctx['instance_id']}
    ex=build_execution_evidence(packet,status,transaction_id='tx',intake_ack=ack)
    ex.update(source='Layer6/GUI/MAVLink',command=packet['u_cmd'],plant_time_s=status['plant_time_s'],
              rate_sync={'transaction_id':'tx'},segment_closure_proof=proof,
              execution_segment_policy=ctx['execution_segment'],
              native_execution_segment_s=None,native_execution_duration_bounds_s=proof['duration_bounds_s'],
              execution_start_plant_time_bounds_s=proof['first_event']['plant_time_bounds'],
              latch_cancel_ack={'cancelled':True,'transaction_id':'tx','command_instance_id':ctx['instance_id']})
    out={'next_vn':0.,'next_ve':0.,'next_vd':0.,'next_qw':1.,'next_qx':0.,'next_qy':0.,'next_qz':0.,
         'next_p_rate':0.,'next_q_rate':0.,'next_r_rate':0.,'airspeed':0.}
    timing={'execution_segment_s':.1,'executor_period_s':.1}
    l4={'verified_candidates':[{'candidate_id':'C1','verification':{
        'first_move_prediction':{'outputs':out,'timing_contract':timing}}}]}
    if tube:
        l4={'request':{'candidate_id':'C1','commit_segment_s':.1},
            'prediction':{'tube':[{'offset_s':.1,'center':[0.]*6+[1.,0.,0.,0.]+[0.]*7}]}}
    if canonical:l4['snapshot_hash']=snapshot_hash
    l5={'result':{'decision':{'candidate_id':'C1'},'gates':{'FINAL':True},'layer6_package':packet}}
    payload=Layer7Adapter.build_input(transaction_id='tx',version='v1',reference_time_s=1.,
        execution_evidence=ex,layer4_result=l4,layer5_result=l5,network_snapshot=snapshot,request={})
    arguments={'execution_evidence':ex,'layer4_result':l4,'layer5_result':l5,'network_snapshot':snapshot}
    return adapter,payload,arguments


@pytest.mark.parametrize('tube',[False,True])
def test_wire_backed_current_pair_computes_physical_residual_without_learning(tmp_path,tube):
    adapter,payload,args=case(tmp_path,tube=tube)
    before=list(adapter.kstore_root.rglob('*'))
    try:
        assert args['execution_evidence']['native_execution_segment_s'] is None
        assert payload['PredictionEvidence']['provenance']['target_plant_timestamp'] is None
        assert payload['ExecutionEvidence']['x_obs']
        assert adapter.prepare_native_diagnostic(payload,**args)['ok']
        result=adapter.invoke(payload)
        assert result['ok'] and result['result']['evidence_ok']
        assert result['result']['provenance']['evidence_scope']=='MODEL_PREDICTION_NATIVE_EXECUTION_DIAGNOSTIC'
        assert result['result']['feedback_layer2']['StateResidualEvidence']
        assert result['result']['commit']['status']=='TRANSIENT_ONLY'
        assert not result['result']['provenance']['persistent_update']
        assert before==list(adapter.kstore_root.rglob('*'))
        # Process-local admission is consumed once, even for byte-identical JSON.
        assert adapter.invoke(payload)['ok'] is False
    finally:adapter.close()


@pytest.mark.parametrize('fault',['tx','session','proof','duration','prediction','snapshot'])
def test_native_current_diagnostic_rejects_changed_identity_or_evidence(tmp_path,fault):
    adapter,payload,args=case(tmp_path)
    try:
        if fault=='tx':payload['transaction_id']='other'
        elif fault=='session':args['execution_evidence']['provenance']['native_session_id']='c'*32
        elif fault=='proof':args['execution_evidence']['segment_closure_proof']['close']['nonce']+=1
        elif fault=='duration':payload['ExecutionEvidence']['provenance']['execution_duration_bounds_s']['upper_s']=.5
        elif fault=='prediction':payload['PredictionEvidence']['x_hat_td']['roll_rad']=.7
        elif fault=='snapshot':payload['NetworkSnapshot']['content_hash']='other'
        assert adapter.prepare_native_diagnostic(payload,**args)['ok'] is False
        out=adapter.invoke(payload)
        assert not out['ok'] and out['status']=='REJECTED' and not out['no_change']
        assert out['result']['evidence_ok'] is False
        assert not out['result']['feedback_layer2']
    finally:adapter.close()


def test_native_packet_flags_and_missing_prediction_never_replace_physical_evidence(tmp_path):
    adapter,payload,args=case(tmp_path)
    try:
        payload['ExecutionEvidence']['provenance']['native_diagnostic_validated']=True
        assert not adapter.invoke(payload)['ok']
        args['layer5_result']['result']['decision']['candidate_id']='missing'
        unmatched=Layer7Adapter.build_input(transaction_id='tx',version='v1',reference_time_s=1.,
            execution_evidence=args['execution_evidence'],layer4_result=args['layer4_result'],
            layer5_result=args['layer5_result'],network_snapshot=args['network_snapshot'],request={})
        assert unmatched['ExecutionEvidence']['x_obs']=={}
        assert unmatched['ExecutionEvidence']['y_real']=={}
        assert 'pilot_control_effort' not in unmatched['PredictionEvidence']['y_hat_td']
        assert not adapter.prepare_native_diagnostic(unmatched,**args)['ok']
    finally:adapter.close()


def test_payload_changed_after_process_local_registration_is_rejected(tmp_path):
    adapter,payload,args=case(tmp_path)
    try:
        assert adapter.prepare_native_diagnostic(payload,**args)['ok']
        changed=deepcopy(payload)
        changed['PredictionEvidence']['x_hat_td']['yaw_rad']+=.2
        assert not adapter.invoke(changed)['ok']
    finally:adapter.close()


def test_production_snapshot_without_declared_hash_uses_layer4_canonical_fields(tmp_path):
    adapter,payload,args=case(tmp_path,canonical=True)
    try:
        assert 'snapshot_hash' not in args['network_snapshot']
        assert payload['NetworkSnapshot']['content_hash']==args['layer4_result']['snapshot_hash']
        assert payload['NetworkSnapshot']['content_hash']!=digest(args['network_snapshot'])
        assert adapter.prepare_native_diagnostic(payload,**args)['ok']
        assert adapter.invoke(payload)['ok']
    finally:adapter.close()


@pytest.mark.parametrize('fault',['canonical_field','claimed_hash','spoofed_snapshot_metadata'])
def test_current_canonical_snapshot_mismatch_rejects_before_diagnostic(tmp_path,fault):
    adapter,_,args=case(tmp_path,canonical=True)
    try:
        if fault=='canonical_field':args['network_snapshot']['reference_time_s']+=1.
        elif fault=='claimed_hash':args['layer4_result']['snapshot_hash']='forged'
        else:args['network_snapshot']['snapshot_hash']='forged'
        with pytest.raises(ValueError,match='canonical snapshot hash mismatch'):
            Layer7Adapter.build_input(transaction_id='tx',version='v1',reference_time_s=1.,
                execution_evidence=args['execution_evidence'],layer4_result=args['layer4_result'],
                layer5_result=args['layer5_result'],network_snapshot=args['network_snapshot'],request={})
    finally:adapter.close()


def test_reused_prediction_keeps_source_identity_and_current_network_identity(tmp_path):
    adapter,_,args=case(tmp_path,canonical=True)
    try:
        source=deepcopy(args['network_snapshot'])
        args['prediction_snapshot']=source
        args['network_snapshot']['reference_time_s']+=.2
        payload=Layer7Adapter.build_input(transaction_id='tx',version='v1',reference_time_s=1.2,
            request={},**args)
        from layer4.snapshot import Layer3SnapshotAdapter
        current=Layer3SnapshotAdapter(None).canonical_hash(args['network_snapshot'])
        assert current!=args['layer4_result']['snapshot_hash']
        assert payload['NetworkSnapshot']['content_hash']==current
        assert payload['PredictionEvidence']['provenance']['prediction_source_snapshot_hash']==args['layer4_result']['snapshot_hash']
        assert adapter.prepare_native_diagnostic(payload,**args)['ok']
        assert adapter.invoke(payload)['ok']
        # A saved reuse flag cannot substitute for the actual producer snapshot.
        args['prediction_snapshot']['reference_time_s']+=1.
        args['layer4_result'].setdefault('provenance',{})['rtmo_l4_artifact_reuse']={'reuse':True}
        with pytest.raises(ValueError,match='canonical snapshot hash mismatch'):
            Layer7Adapter.build_input(transaction_id='tx',version='v1',reference_time_s=1.2,
                request={},**args)
    finally:adapter.close()


def test_completed_segment_is_historical_evidence_not_current_observation(tmp_path,monkeypatch):
    adapter,payload,args=case(tmp_path)
    try:
        from layer6.status import validate_observation_status
        import layer6.status as status_module
        snapshot=args['execution_evidence']['segment_closure_proof']['snapshot']
        recorded=snapshot['timestamp_monotonic']
        monkeypatch.setattr(status_module.time,'monotonic',lambda:recorded+60.)
        assert not validate_observation_status(snapshot)['ok']
        assert adapter.prepare_native_diagnostic(payload,**args)['ok']
        result=adapter.invoke(payload)
        assert result['ok'] and not result['result']['provenance']['persistent_update']
        assert not result['result']['provenance']['flight_authorized']
    finally:adapter.close()
