from copy import deepcopy
from types import SimpleNamespace
import pytest
from tddt_tdoc.adapters.layer5 import Layer5Adapter
from tddt_tdoc.adapters.layer7 import Layer7Adapter


@pytest.mark.parametrize('projected',[False,True])
def test_mpc_engine_receives_current_navigation_and_original_artifact_is_unchanged(projected):
    original={'snapshot_hash':'verified-source','candidates':[{'candidate_id':'C1',
        'phoenix_operating_context':{'forecast_step_s':1.,'mesa_local_guidance':{'position_ned_m':[-99.]*3}}}]}
    original['verified_candidates']=[{'candidate_id':'C1'}]
    if projected:
        from tddt_tdoc.rtmo.artifact_gate import RTMOL4ArtifactGate
        original=RTMOL4ArtifactGate._execution_projection(original)
    before=deepcopy(original);seen=[]
    class Captured(Exception):pass
    def run(data,**kwargs):seen.append(data);raise Captured()
    a=Layer5Adapter.__new__(Layer5Adapter);a._ensure=lambda:None
    a.config=SimpleNamespace(get=lambda key,default=None:default)
    a.engine=SimpleNamespace(run=run)
    snapshot={'std':{'representations':{'MESA_NAV':{'canonical_state':{
        'position_ned_m':[10.,20.,-30.],'ground_velocity_ned_m_s':[1.,2.,3.],
        'attitude_quaternion_wxyz':[1.,0.,0.,0.]}}},'context':{'stage':'hover',
            'online_phase_reference_targets':{'hover':[20.,10.,30.]}}}}
    with pytest.raises(Captured):a.invoke({'layer4_result':original,'network_snapshot':snapshot,'learning_mode':'ONLINE_JSBSIM'})
    ctx=seen[0]['candidates'][0]['phoenix_operating_context']
    assert ctx['mesa_local_guidance']['position_ned_m']==[10.,20.,-30.]
    assert ctx['forecast_step_s']==1.
    assert original==before
    assert seen[0]['snapshot_hash']=='verified-source'
    assert seen[0]['verified_candidates']==[{'candidate_id':'C1'}]
    assert seen[0]['provenance']['current_decision_snapshot_hash']!='verified-source'
    if projected:
        from tddt_tdoc.native_mission import validate_execution_projection
        assert validate_execution_projection(original)==seen[0]['provenance']['source_execution_projection_sha256']
        assert validate_execution_projection(seen[0])!=validate_execution_projection(original)
        original['provenance']['rtmo_execution_projection']['sha256']='forged'
        with pytest.raises(ValueError,match='projection integrity'):
            a.invoke({'layer4_result':original,'network_snapshot':snapshot,'learning_mode':'ONLINE_JSBSIM'})


def test_l7_does_not_use_current_prediction_for_another_command():
    l4={'candidates':[{'candidate_id':'C1'}],'committed_first_move':{
        'candidate_id':'C1','transaction_id':'tx','u_cmd':[0.,0.,.4,0.],'source_snapshot_hash':'current'}}
    l5={'decision':{'candidate_id':'C1'},'layer6_package':{'transaction_id':'tx','u_cmd':[0.,0.,.8,0.]}}
    state,candidate,meta=Layer7Adapter._matched_prediction_state(l4,l5)
    assert state=={} and candidate is None and meta['reason']=='current_first_move_command_identity_mismatch'
