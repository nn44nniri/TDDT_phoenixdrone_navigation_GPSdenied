from tddt_tdoc.adapters.layer7 import Layer7Adapter


def _fixture(tx="tx-1", executor=.1):
    q=(1.0,0.0,0.0,0.0)
    outputs={"next_vn":1.0,"next_ve":2.0,"next_vd":3.0,
             "next_qw":q[0],"next_qx":q[1],"next_qy":q[2],"next_qz":q[3],
             "next_p_rate":.1,"next_q_rate":.2,"next_r_rate":.3,"airspeed":4.0}
    timing={"execution_segment_s":.1,"executor_period_s":executor}
    layer4={"verified_candidates":[{"candidate_id":"C1","verification":{
        "timing_contract":timing,"steps":[{"physical_replay":{"outputs":outputs,
        "timing_contract":timing}}]}}]}
    layer5={"result":{"decision":{"candidate_id":"C1"}}}
    state={"timestamp":12.3,"velocity":[1.1,2.1,3.1],"attitude":[.01,.02,.03],
           "angular_rates":[.11,.21,.31]}
    execution={"command":[0,0,.5,0],"write_result":{"rate_sync":{"transaction_id":tx},
        "latch_result":{"release_confirmation":{"bridge":{"current_state":state}}}}}
    return layer4,layer5,execution


def test_selected_prediction_and_realized_state_share_transaction():
    l4,l5,ex=_fixture()
    payload=Layer7Adapter.build_input(transaction_id="tx-1",version="v1",reference_time_s=1.0,
        execution_evidence=ex,layer4_result=l4,layer5_result=l5,network_snapshot={},request={})
    assert payload["ExecutionEvidence"]["x_obs"]["velocity_n_m_s"] == 1.1
    assert payload["PredictionEvidence"]["x_hat_td"]["velocity_n_m_s"] == 1.0
    assert payload["ExecutionEvidence"]["timestamp"] == 12.3
    assert payload["PredictionEvidence"]["timestamp"] == 12.3
    assert payload["PredictionEvidence"]["provenance"]["candidate"] == "C1"


def test_state_residual_pair_fails_closed_on_transaction_or_clock_mismatch():
    for tx,executor in (("wrong",.1),("tx-1",.2)):
        l4,l5,ex=_fixture(tx=tx,executor=executor)
        payload=Layer7Adapter.build_input(transaction_id="tx-1",version="v1",reference_time_s=1.0,
            execution_evidence=ex,layer4_result=l4,layer5_result=l5,network_snapshot={},request={})
        assert payload["ExecutionEvidence"]["x_obs"] == {}
        assert payload["PredictionEvidence"]["x_hat_td"] == {}
