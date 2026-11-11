import importlib.util
from pathlib import Path
from types import SimpleNamespace

HERE=Path(__file__).resolve()
SCRIPT=HERE.parents[1]/'scripts'/'run_training_workflows.py'
spec=importlib.util.spec_from_file_location('tddt_solution10_workflow',SCRIPT)
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def test_wait_capability_reason_is_preserved_not_relabelled_max_cycles():
    terminal={'reached':False,'reason':'WAIT_CAPABILITY','position':None,'goal_distance_m':None}
    records=[{'status':'HOLD','layer6_runtime':{'status':'NOT_INVOKED','source':'RTMO_WAIT_CAPABILITY'}}]
    assert mod._resolve_online_termination_reason(terminal,records,366)=='WAIT_CAPABILITY'


def test_max_cycles_only_when_budget_really_exhausted():
    records=[{'status':'OK'} for _ in range(3)]
    assert mod._resolve_online_termination_reason({},records,3)=='max_cycles_exhausted'
    assert mod._resolve_online_termination_reason({},records[:1],3)=='stopped_before_cycle_budget_without_terminal_reason'


def test_not_invoked_placeholder_does_not_claim_layer6_invocation():
    assert mod._layer6_was_invoked({'status':'NOT_INVOKED','source':'RTMO_WAIT_CAPABILITY'}) is False
    assert mod._layer6_was_invoked({'status':'NOT_INVOKED_PRE_RELEASE_VERIFY','source':'PRE_RELEASE_VERIFY'}) is False
    assert mod._layer6_was_invoked({'status':'EXECUTED','source':'Layer6/PT_LIB'}) is True


def test_online_diagnostics_match_not_invoked_status(tmp_path, monkeypatch):
    # Avoid visualization/terrain dependencies; this test targets report semantics only.
    monkeypatch.setattr(mod,'TDOCReportGenerator',lambda *a,**k: None)
    records=[{
        'status':'HOLD','episode_index':1,'cycle_in_episode':1,'transaction_id':None,
        'offline_knowledge_handoff':{'ok':True,'setd_package_id':'SETD-test'},
        'knowledge':{},'layer7_status':'NOT_INVOKED',
        'layer6_runtime':{'status':'NOT_INVOKED','source':'RTMO_WAIT_CAPABILITY','live_runtime_evidence':False},
        'terminal':{'reached':False,'reason':'WAIT_CAPABILITY'},
    }]
    episodes=[{'episode_index':1,'terminal_reached':False,'termination_reason':'WAIT_CAPABILITY','cycles_completed':1,'max_cycles':366}]
    args=SimpleNamespace(online_training=True,online_observation_only=False,online_episodes=1,online_control_readiness=None,terrain_grid_n=8)
    class Cfg:
        def get(self,*a,**k): return 1.0
    out=mod._write_online_reports(None,Cfg(),records,1,args,tmp_path,episodes=episodes)
    diag=out['orchestration_diagnostics']
    assert diag['layer6_invoked'] is False
    assert diag['layer6_invocation_count']==0
    assert diag['layer6_statuses']==['NOT_INVOKED']
    assert diag['termination_reasons']==['WAIT_CAPABILITY']
    assert diag['last_termination_reason']=='WAIT_CAPABILITY'
    assert out['episodes'][0]['termination_reason']=='WAIT_CAPABILITY'
