from __future__ import annotations
import importlib.util, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from tddt_tdoc.storage import write_json_gz

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('tdoc_time_handoff_v2052',ROOT/'scripts/run_training_workflows.py')
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def _window():
    return datetime(2026,9,11,16,0,0), datetime(2026,9,12,15,0,0)


def test_online_reuses_same_offline_effective_epoch_when_static_layer3_epoch_is_outside(monkeypatch):
    start,end=_window()
    monkeypatch.setattr(mod,'_layer3_mission_epoch_utc',lambda:datetime(2026,8,24,11,0,0,tzinfo=timezone.utc))
    contract={'ok':True,'effective_climate_epoch':start.isoformat(),'forcing_start':start.isoformat(),'forcing_end':end.isoformat(),'source':'handoff_reuse_contract'}
    req={'climate':{},'context':{}}
    t=mod._apply_online_climate_time(req,(start,end),90.0,contract)
    assert t==datetime(2026,9,11,16,1,30)
    assert req['context']['climate_time_alignment_basis']=='offline_handoff_effective_climate_epoch + ONLINE simulator elapsed_s'
    assert req['context']['online_climate_configured_layer3_epoch'].startswith('2026-08-24T11:00:00')
    assert req['context']['online_climate_effective_epoch']=='2026-09-11T16:00:00'


def test_online_static_epoch_outside_forcing_without_offline_contract_stays_fail_closed(monkeypatch):
    start,end=_window()
    monkeypatch.setattr(mod,'_layer3_mission_epoch_utc',lambda:datetime(2026,8,24,11,0,0,tzinfo=timezone.utc))
    with pytest.raises(RuntimeError,match='same finalized Offline handoff'):
        mod._apply_online_climate_time({'climate':{},'context':{}},(start,end),0.0,{})


def test_legacy_handoff_recovers_alignment_only_from_its_source_cycle(tmp_path,monkeypatch):
    start,end=_window(); runtime=tmp_path/'runtime'; (runtime/'knowledge/handoff').mkdir(parents=True); (runtime/'cycles').mkdir(parents=True)
    hm={'schema':'TDOC-offline-online-knowledge-handoff-v1','source_transaction_id':'mission-offline-0230','created_at':'2026-09-11T20:00:00Z','reuse_contract':{}}
    write_json_gz(runtime/'knowledge/handoff/offline_latest.json.gz',hm,indent=2)
    alignment={'basis':'active_offline_forcing_window_start','configured_layer3_epoch':'2026-08-24T11:00:00','effective_climate_epoch':start.isoformat(),'forcing_start':start.isoformat(),'forcing_end':end.isoformat(),'epoch_rebased_to_forcing_start':True}
    cycle={'layer3_result':{'snapshot':{'context':{'offline_climate_time_alignment':alignment}}}}
    write_json_gz(runtime/'cycles/mission-offline-0230.json.gz',cycle,indent=2)
    forcing_file=tmp_path/'openmeteo_24h.csv'; forcing_file.write_text('time\n2026-09-11T16:00:00\n')
    monkeypatch.setattr(mod,'resolve_climate_forcing',lambda root,require_exists=True:forcing_file)
    rec=mod._load_online_climate_alignment(runtime,runtime/'knowledge/handoff/offline_latest.json.gz',(start,end),tmp_path)
    assert rec['ok'] is True
    assert rec['source']=='legacy_source_cycle_recovery'
    assert rec['effective_climate_epoch']=='2026-09-11T16:00:00'
    assert rec['legacy_window_only_identity'] is True


def test_alignment_contract_rejects_different_current_forcing_window(tmp_path,monkeypatch):
    start,end=_window(); runtime=tmp_path/'runtime'; (runtime/'knowledge/handoff').mkdir(parents=True)
    hm={'schema':'TDOC-offline-online-knowledge-handoff-v1','source_transaction_id':'mission-offline-0230','reuse_contract':{'climate_time_alignment':{'effective_climate_epoch':start.isoformat(),'forcing_start':start.isoformat(),'forcing_end':end.isoformat()}}}
    write_json_gz(runtime/'knowledge/handoff/offline_latest.json.gz',hm,indent=2)
    forcing_file=tmp_path/'openmeteo_24h.csv'; forcing_file.write_text('x')
    monkeypatch.setattr(mod,'resolve_climate_forcing',lambda root,require_exists=True:forcing_file)
    rec=mod._load_online_climate_alignment(runtime,runtime/'knowledge/handoff/offline_latest.json.gz',(start+timedelta(hours=1),end+timedelta(hours=1)),tmp_path)
    assert rec['ok'] is False
    assert rec['reason']=='offline_online_forcing_window_mismatch'
