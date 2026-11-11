"""A command prediction keeps its source clock, even after delayed delivery."""
from copy import deepcopy

import pytest

from test_native_layer7_current_feedback import case
from tddt_tdoc.adapters.layer7 import Layer7Adapter


@pytest.mark.parametrize('delivery_delay_s', [0., 1.])
def test_committed_prediction_cannot_be_retimed_to_the_execution(delivery_delay_s, tmp_path):
    adapter, _, args = case(tmp_path, canonical=True)
    try:
        l4 = args['layer4_result']
        prediction = deepcopy(l4['verified_candidates'][0]['verification']['first_move_prediction'])
        package = args['layer5_result']['result']['layer6_package']
        source = args['execution_evidence']['plant_time_s'] - .1 - delivery_delay_s
        prediction.update(candidate_id='C1', transaction_id='tx', u_cmd=package['u_cmd'],
                          source_snapshot_hash=l4['snapshot_hash'], source_plant_time_s=source)
        l4['committed_first_move'] = prediction
        payload = Layer7Adapter.build_input(transaction_id='tx', version='v1', reference_time_s=1.,
                                           request={}, **args)
        meta = payload['PredictionEvidence']['provenance']
        assert meta['prediction_target_plant_time_bounds_s'] == {
            'lower_s': source + .1, 'upper_s': source + .1}
        assert meta['state_transaction_match'] is (delivery_delay_s == 0.)
        admitted = adapter.prepare_native_diagnostic(payload, **args)
        assert admitted['ok'] is (delivery_delay_s == 0.)
        if delivery_delay_s == 0.:
            assert adapter.invoke(payload)['ok']
        else:
            assert payload['ExecutionEvidence']['x_obs'] == {}
    finally:
        adapter.close()
