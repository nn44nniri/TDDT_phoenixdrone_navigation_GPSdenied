from copy import deepcopy
import pytest
from test_native_complete_readiness import complete_status
from tddt_tdoc.online_mission import validate_native_origin


def case():
    status=complete_status(1.)
    status.update(landed_state='ON_GROUND',throttle=0.)
    status['status_quality']['native_session_id']='a'*32
    return status,{'session_id':'a'*32,'session_ready':True}


def test_launch_validation_does_not_consume_or_modify_navigation():
    status,session=case();before=deepcopy(status)
    result=validate_native_origin(status,session,[-105.,40.],50.)
    assert result['verified'] and result['distance_m']==0.
    assert result['navigation_input'] is False and status==before


def test_decision_readiness_cannot_start_transport():
    from tddt_tdoc.adapters.layer6 import Layer6OnlineAdapter
    adapter=Layer6OnlineAdapter.__new__(Layer6OnlineAdapter)
    adapter._session_open=False
    adapter._status=lambda:pytest.fail('readiness started or queried an unprepared stack')
    with pytest.raises(RuntimeError,match='native_transport_not_prepared'):
        adapter.current_online_status()
    adapter._session_open=True
    adapter._status=lambda:{'live':True}
    assert adapter.current_online_status()=={'live':True}


@pytest.mark.parametrize('fault',['position','session','missing_component','stale','armed','airborne','nan'])
def test_launch_reference_never_passes_invalid_physical_evidence(fault):
    status,session=case()
    if fault=='position':status['latitude_deg']=41.
    elif fault=='session':session['session_id']='b'*32
    elif fault=='missing_component':session['session_ready']=False
    elif fault=='stale':status['telemetry_metadata']['GLOBAL_POSITION_INT']['received_monotonic']=0.
    elif fault=='armed':status['armed']=True
    elif fault=='airborne':status['landed_state']='IN_AIR'
    else:status['longitude_deg']=float('nan')
    assert not validate_native_origin(status,session,[-105.,40.],50.)['verified']
