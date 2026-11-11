from tddt_tdoc.participation import cycle_decision_gate_trace


def test_native_execution_rejection_survives_summary_trace():
    cycle={'downstream_layers':{'5':{'output':{'result':{
        'gates':{'FINAL':False},
        'native_intent_rejection':'native_pilot_controller_prediction_unavailable'}}}}}
    trace=cycle_decision_gate_trace(cycle)
    assert trace['first_stop_gate']=='L5_FINAL'
    assert trace['first_stop_reasons']==['native_pilot_controller_prediction_unavailable']
