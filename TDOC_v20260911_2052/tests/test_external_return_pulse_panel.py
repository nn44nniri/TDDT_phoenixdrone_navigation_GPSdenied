import gzip, json, importlib.util
from pathlib import Path
import pandas as pd


def _load_workflow_module():
    p=Path('scripts/run_training_workflows.py').resolve()
    spec=importlib.util.spec_from_file_location('tdoc_workflow_test',p)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_return_pulses_map_layer_source_cycle_and_behavior(tmp_path):
    mod=_load_workflow_module(); runtime=tmp_path/'runtime'; chunks=runtime/'events/invocations/chunks'; chunks.mkdir(parents=True)
    events=[]
    for i,layer in enumerate((2,3,4,5),1):
        events.append({'transaction_id':'tx1','source':f'Layer{layer}','destination':'TDOC','event_type':'Route','status':'OK','reason':'output_ready','event_time':f'2026-09-03T00:00:0{i}+00:00','details':{'elapsed_s':0.01*layer,'deadline_ok':True}})
    with gzip.open(chunks/'chunk_000000.jsonl.gz','wt',encoding='utf-8') as f:
        for e in events:f.write(json.dumps(e)+'\n')
    df=pd.DataFrame([{'cycle':7,'transaction_id':'tx1','flight_elapsed_s':6.0,'phase':'cruise'}])
    details=[{'cycle':7,'transaction_id':'tx1','sensors':{'MicroMetTwin':{'validity':{'valid':True},'provenance':{'tdoc_external_warning_filter':{'count':1}}}},'layer3_return':{'stability':True,'tdoc_health':{'ok':True}},'layer4_return':{'candidate_count':3,'feasible_count':2,'verified_count':0},'layer5_decision':{'status':'OK','decision':{'approval':{'final_gate':False}},'fallback':{'mode':'HOLD'},'operational_mode':'HOLD'}}]
    pulses=mod._external_return_pulses(runtime,df,details)
    assert [p['layer'] for p in pulses]==[2,3,4,5]
    assert all(p['cycle']==7 and p['destination']=='TDOC' for p in pulses)
    assert pulses[0]['behavior']=='WARNING_FILTERED'
    assert pulses[2]['behavior']=='FAIL_CLOSED_UNVERIFIED'
    assert pulses[3]['behavior']=='HOLD_FAIL_CLOSED'
    assert all(p['sequence_in_cycle']>=1 for p in pulses)
