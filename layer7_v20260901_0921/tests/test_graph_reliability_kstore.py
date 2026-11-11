from pathlib import Path
from tddt_layer7.graph import TarjanSCC, TopologyManager, bounded_cycle_lci
from tddt_layer7.reliability import BayesianReliabilityStore
from tddt_layer7.config import KnowledgeConfig
from tddt_layer7.models import EvidenceType
from tddt_layer7.kstore import BoundedKStore, SETDKStore


def test_tarjan_and_cycle_budget():
    e=[('A','B'),('B','C'),('C','A'),('C','D')]
    s=TarjanSCC.compute(['A','B','C','D'],e)
    assert any(set(x)=={'A','B','C'} for x in s)
    assert len(TarjanSCC.selected_cycles([],e,budget=1))<=1
    l=bounded_cycle_lci(e,{'A->B->C->A':2,'bad->cycle':9},1)
    assert list(l)==['A->B->C->A']


def test_topology_chattering_and_dwell_lock():
    tm=TopologyManager(dwell_ticks=2)
    assert tm.chattering_rate([('A','B')],[('A','C')])==2.0
    tm.lock_for_dwell()
    cur=[('A','B')]
    assert tm.candidate(cur,{'A->B':0,'A->C':2})==cur
    assert tm.candidate(cur,{'A->B':0,'A->C':2})==cur


def test_sim_prior_is_capped_and_effective_but_field_posterior_separate():
    st=BayesianReliabilityStore(KnowledgeConfig(simulation_prior_cap=0.4,simulation_power=0.2))
    for _ in range(10): st.observe('T','C',True,EvidenceType.SIMULATION)
    c=st.cell('T','C'); assert c.simulation_mass<=0.4000001
    assert abs(c.mean-0.5)<1e-12
    assert c.effective_mean()>0.5 and c.tempered_prior_mean()>0.5
    before=c.mean; st.observe('T','C',False,EvidenceType.FIELD); assert st.cell('T','C').mean<before


def test_kstore_bounded_ram_and_disk(tmp_path):
    ss=BoundedKStore(3,tmp_path/'ss',segment_records=1,max_segments=2)
    for i in range(5): ss.append({'residuals':{'x':i}})
    assert len(ss.snapshot())==3
    assert len(list((tmp_path/'ss').glob('segment_*.jsonl')))<=2
    setd=SETDKStore(tmp_path/'setd',max_packages=2)
    for i in range(4): setd.aggregate(ss.snapshot(),str(i))
    assert len([p for p in (tmp_path/'setd').glob('*.json') if not p.name.startswith('.')])<=2
