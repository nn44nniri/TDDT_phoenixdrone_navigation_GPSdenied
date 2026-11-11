from collections import OrderedDict

from layer4.budget import InvocationMemoryProbe, evaluate_budget
from layer4.scenario import ScenarioManager


class Cfg:
    def __init__(self):
        self.data={
            'edge':{'cpu_budget_ms':2000.0,'memory_budget_mb':192.0,'energy_budget_j':50.0,'estimated_cpu_power_w':8.0},
        }
    def get(self,key,default=None):
        cur=self.data
        for part in key.split('.'):
            if not isinstance(cur,dict) or part not in cur:
                return default
            cur=cur[part]
        return cur


def test_budget_charges_invocation_increment_not_lifetime_process_peak():
    cfg=Cfg()
    memory={
        'baseline_process_rss_mb':600.0,
        'peak_process_rss_mb':650.0,
        'end_process_rss_mb':610.0,
        'invocation_peak_increment_mb':50.0,
        'invocation_retained_delta_mb':10.0,
        'sampling_interval_ms':10.0,
        'measurement_scope':'test',
    }
    b=evaluate_budget(cfg,0.1,memory)
    assert b['peak_rss_mb']==50.0
    assert b['peak_process_rss_mb']==650.0
    assert b['within_memory_budget'] is True
    assert b['all_within_budget'] is True


def test_budget_still_fails_when_layer4_increment_exceeds_budget():
    cfg=Cfg()
    memory={
        'baseline_process_rss_mb':100.0,'peak_process_rss_mb':310.0,'end_process_rss_mb':120.0,
        'invocation_peak_increment_mb':210.0,'invocation_retained_delta_mb':20.0,
        'sampling_interval_ms':10.0,'measurement_scope':'test'}
    b=evaluate_budget(cfg,0.1,memory)
    assert b['within_memory_budget'] is False
    assert b['all_within_budget'] is False


def test_memory_probe_reports_nonnegative_increment():
    p=InvocationMemoryProbe(interval_ms=1.0)
    x=bytearray(2*1024*1024)
    s=p.stop()
    assert x[0]==0
    assert s['invocation_peak_increment_mb']>=0.0
    assert s['peak_process_rss_mb']>=s['baseline_process_rss_mb']


def test_scenario_reduction_cache_is_explicitly_bounded_in_source_contract():
    # Regression guard: the cache type and configured maximum are part of the
    # bounded-memory realization, independent of particular scenario fixtures.
    import inspect, layer4.scenario as mod
    source=inspect.getsource(mod.ScenarioManager)
    assert 'OrderedDict' in source
    assert 'reduction_cache_entries' in source
    assert 'popitem(last=False)' in source
