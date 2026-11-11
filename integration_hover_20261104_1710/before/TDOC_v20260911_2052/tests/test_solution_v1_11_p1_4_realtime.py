from __future__ import annotations
import json, time
from pathlib import Path

from tddt_tdoc.rtmo.realtime_metrics import RTMOControlLatencyTracker
from tddt_tdoc.rtmo.persistence_lane import OnlinePersistenceLane
from tddt_tdoc.rtmo.artifact_gate import RTMOL4ArtifactGate

class SlowKnowledge:
    def __init__(self):
        self.calls=0
    def record(self, cycle):
        self.calls += 1
        time.sleep(0.05)
        return {'ok':True,'transaction_id':cycle.get('transaction_id')}
class SlowArchive:
    def record_cycle(self, cycle):
        time.sleep(0.05)
        return {'2':{'ok':True}}

def _snap():
    return {'policy_version':'P1','std':{'representations':{'MESA_NAV':{'canonical_state':{'position_ned_m':[0,0,0],'ground_velocity_ned_m_s':[10,0,0]}}}}}
def _req():
    return {'stage':'cruise','context':{'offline_knowledge_handoff':{'knowledge_version':'K1'},'online_climate_time_alignment':{'forcing_fingerprint':'F1'}}}
def _verified():
    c={'candidate_id':'C1','verified':True,'candidate_verified':True,'predicted_trajectory':[[0,0,0,10,0,0],[1,0,0,10,0,0]],'verification':{'executed':True,'verified':True}}
    return {'verified_candidates':[c],'candidates':[c],'conformance':{'C_FF_runtime_clean':True},'provenance':{}}

def test_latency_tracker_requires_real_sample_volume_before_claim():
    t=RTMOControlLatencyTracker(deadline_s=.1,min_samples_for_claim=1000)
    for i in range(10): t.record(transaction_id=f't{i}',latency_s=.01,phase='cruise',execution_status='EXECUTED',actuation_executed=True)
    s=t.summary()
    assert s['p99_s'] <= .1
    assert s['claim_status']=='INSUFFICIENT_EXECUTED_SAMPLES'
    assert s['experimentally_verified_p99_le_deadline'] is False

def test_online_persistence_lane_is_nonblocking(tmp_path):
    knowledge=SlowKnowledge()
    lane=OnlinePersistenceLane(knowledge=knowledge,layer_archive=SlowArchive(),runtime_root=tmp_path,max_queue=8)
    cycle={'transaction_id':'tx1','provenance':{'learning_mode':'ONLINE_JSBSIM'},'downstream_layers':{}}
    t0=time.perf_counter(); st=lane.submit(cycle); elapsed=time.perf_counter()-t0
    assert st['accepted'] is True and elapsed < .03
    assert lane.flush(2.0) is True
    assert (tmp_path/'cycles/tx1.json.gz').is_file()
    assert knowledge.calls == 0
    assert lane.status()['persistent_learning_authority'] == 'Layer7'
    assert lane.status()['general_knowledge_writes'] == 0
    lane.close(1.0)

def test_warm_artifact_gate_orchestration_overhead_p99_below_100ms():
    gate=RTMOL4ArtifactGate(max_age_s=30,position_tolerance_m=20,velocity_tolerance_m_s=4)
    pub=gate.publish_bootstrap(result=_verified(),snapshot=_snap(),request=_req(),orchestration_version='V1',reference_time_s=0,l5_final=True,bootstrap_qualified=True,transaction_id='boot')
    assert pub.ok
    vals=[]
    for i in range(2000):
        t0=time.perf_counter(); got=gate.acquire(snapshot=_snap(),request=_req(),orchestration_version='V1',reference_time_s=.1); vals.append(time.perf_counter()-t0); assert got.ok
    vals.sort(); p99=vals[int(.99*(len(vals)-1))]
    assert p99 < .1

def test_source_contract_moves_heavy_persistence_out_of_online_hot_path():
    root=Path(__file__).resolve().parents[1]
    src=(root/'src/tddt_tdoc/orchestrator.py').read_text()
    wf=(root/'scripts/run_training_workflows.py').read_text()
    assert 'self.provenance.health().get("ok",False)' in src
    assert 'BOUNDED_ASYNC_POST_ACK' in src
    assert 'LAYER3_ATOMIC_OPERATIONAL_SNAPSHOT_READY' in src
    assert 'LAYER6_NATIVE_EXECUTION_ACK_RETURNED_TO_TDOC' in src
    assert "tdoc.rtmo.realtime.max_control_period_s" in wf
    assert 'ONLINE_REALTIME_CONTROL_METRICS.json' in wf
    assert 'realtime_control_metrics.json.gz' in src
