import json
from pathlib import Path
from tdoc_unit_converter.boundary_cli import main

H='a'*64

def sample():
    return {'schema':'TDDT-ISO-CrossLayerEnvelope-v1','semantic':'navigation_velocity',
        'data_role':'current_state','producer':'MESA_NAV','transaction_id':'txn-test',
        'snapshot_hash':H,'epoch_id':'example_sim_epoch','clock_domain':'PLANT_ELAPSED_S',
        'source_time_s':10.0,'unit':'m/s','frame':'NED','value':[1.,2.,-3.],
        'covariance':[[.01,0,0],[0,.04,0],[0,0,.09]]}

def args(path,output):
    return ['--input',str(path),'--output',str(output),'--target-unit','m/s',
            '--target-frame','ENU','--expected-semantic','navigation_velocity',
            '--expected-role','current_state','--expected-producer','MESA_NAV',
            '--expected-epoch-id','example_sim_epoch','--reference-time-s','10.01']

def test_success_cli_diagnostic(tmp_path):
    p=tmp_path/'input.json';p.write_text(json.dumps(sample()))
    dest=tmp_path/'out.json'
    assert main(args(p,dest))==0
    output=json.loads(dest.read_text())
    assert output['status']=='CONVERSION_DIAGNOSTIC_PASS_UNCERTIFIED'
    assert output['result']['value']==[2.,1.,3.]
    assert output['native_controller_certified'] is False
    assert output['flight_authorized'] is False
    assert main(args(p,dest))==3

def test_reject_semantic_swap_cli(tmp_path):
    d=sample();d['semantic']='pilot_throttle_reference'
    p=tmp_path/'input.json';p.write_text(json.dumps(d))
    dest=tmp_path/'blocked.json'
    assert main(args(p,dest))==2
    assert json.loads(dest.read_text())['status']=='BLOCKED_INVALID_BOUNDARY'
