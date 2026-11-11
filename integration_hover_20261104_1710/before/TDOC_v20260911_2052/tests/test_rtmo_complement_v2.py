from pathlib import Path
from types import SimpleNamespace
import json

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.rtmo import PHASE_ORDER, MissionPhaseManager, build_capability_manifest
from tddt_tdoc.rtmo.execution_gate import execution_gate_trace
from tddt_tdoc.rate_sync import resolve_rate_sync

ROOT=Path(__file__).resolve().parents[1]

def test_rtmo_exact_11_phase_order():
    assert PHASE_ORDER == (
        'on_ground','vertical_takeoff','hover','transition_forward','climb','cruise',
        'turn','approach','transition_back','vertical_landing','touchdown')
    m=MissionPhaseManager.from_offline_report(ROOT/'reports/offline/mission/PHASE_HISTORY.csv')
    assert m.phase=='on_ground' and len(m.phase_order)==11

def test_rtmo_capability_manifest_is_evidence_based_and_fail_closed():
    m=build_capability_manifest(ROOT/'reports',layer6_forward_only=True)
    assert m.ready_11phase is False
    assert {'vertical_takeoff','hover','transition_back','vertical_landing','touchdown'} <= set(m.blocked_phases)
    phase_history=ROOT/'reports/offline/mission/PHASE_HISTORY.csv'
    if phase_history.is_file():
        assert m.phases['transition_forward']['l4_verified'] is True
        assert m.phases['transition_forward']['l5_admissible'] is True
    else:
        # A reset release contains no historical mission evidence; capability must remain fail-closed.
        assert m.phases['transition_forward']['l4_verified'] is False
        assert m.phases['transition_forward']['l5_admissible'] is False
    assert m.phases['vertical_takeoff']['capability'] is False

def test_rtmo_l6_failure_overrides_upstream_final():
    cycle={'downstream_layers':{'6':{'status':'FAILED','output':{'status':'FAILED','reason':'fmi_failed'}}}}
    out=execution_gate_trace(cycle,{'first_stop_gate':'FINAL','first_stop_reasons':[]})
    assert out['first_stop_gate']=='EXECUTION_FAILED'
    assert out['executed_final'] is False

def test_rtmo_rate_sync_uses_fast_tick_lease_without_hiding_slow_cycle():
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    r=resolve_rate_sync(cfg,SimpleNamespace(controller=None),{})
    assert r['schema']=='TDOC-PTLIB-rate-sync-v2'
    assert r['tdoc_decision_hz'] >= 10.0-1e-9
    assert abs(r['command_ttl_s']-0.3) < 1e-9

def test_rtmo_offline_scheduler_contract_is_not_rewritten():
    cfg=TDOCConfig.from_yaml(ROOT/'configs/tdoc.yaml')
    assert float(cfg.get('scheduler.fast_dt_s')) == 1.0
    assert float(cfg.get('tdoc.rtmo.realtime.max_control_period_s')) == 0.1

def test_rtmo_truth_injection_guard_present_in_tdoc_adapter():
    src=(ROOT/'src/tddt_tdoc/adapters/layer2.py').read_text()
    orch=(ROOT/'src/tddt_tdoc/orchestrator.py').read_text()
    assert 'rtmo_truth_injection_forbidden' in src
    assert 'online_execution_state' in orch
    assert 'truth_injection_to_mesa' in orch

def test_rtmo_layer4_ff_certificate_reuse_keeps_real_ff_origin():
    src=(ROOT/'src/tddt_tdoc/adapters/layer4.py').read_text()
    assert 'self.engine.mf.find' in src and 'self.engine.mf.build' in src
    assert 'FF_CERT_REUSE' in src and 'FF_CURRENT' in src
