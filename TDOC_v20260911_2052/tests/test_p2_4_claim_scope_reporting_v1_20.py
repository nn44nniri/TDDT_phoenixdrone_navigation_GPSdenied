from pathlib import Path
import importlib.util, json

ROOT=Path(__file__).resolve().parents[1]

def _workflow():
    p=ROOT/'scripts/run_training_workflows.py'
    spec=importlib.util.spec_from_file_location('workflow_p2_4',p)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def test_not_ready_report_persists_claim_scope_without_promoting_claims(tmp_path):
    mod=_workflow()
    readiness={
        'ok':True,'status':'READY','infrastructure_status':'INFRASTRUCTURE_READY',
        'claim_limits':{
            'vtol_autopilot_controller_validated':False,
            'aerodynamics_full_envelope_validated':False,
            'real_aircraft_flight_validated':False,
        },
        'claim_scope':{
            'status':'CLAIM_LIMITED_TO_SITL','scope_ceiling':'SITL_SIMULATION_ONLY',
            'beyond_sitl_claims_allowed':False,
            'unmet_beyond_sitl_claims':['vtol_autopilot_controller_validated','aerodynamics_full_envelope_validated','real_aircraft_flight_validated'],
        },
    }
    mod.write_online_not_ready(tmp_path,1,'synthetic_test',None,control_readiness=readiness)
    out=json.loads((tmp_path/'online/ONLINE_CLAIM_SCOPE.json').read_text())
    assert out['status']=='CLAIM_LIMITED_TO_SITL'
    assert out['scope_ceiling']=='SITL_SIMULATION_ONLY'
    assert out['current_sitl_infrastructure_ready'] is True
    assert out['claim_limits_block_current_sitl_by_themselves'] is False
    assert out['beyond_sitl_claims_allowed'] is False
    assert out['commissioning_may_upgrade_claim_limits'] is False
    assert out['claim_limits']['real_aircraft_flight_validated'] is False

def test_workflow_source_writes_dedicated_claim_scope_report():
    src=(ROOT/'scripts/run_training_workflows.py').read_text()
    assert "ONLINE_CLAIM_SCOPE.json" in src
    assert "commissioning_may_upgrade_claim_limits" in src
    assert "beyond_sitl_claims_allowed" in src
