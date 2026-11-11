from pathlib import Path
import json

ROOT=Path(__file__).parents[1]

def test_prepare_all_bootstraps_native_sources_before_datasets_and_tep_stage08_is_official():
    src=(ROOT/'scripts/prepare_all.py').read_text(encoding='utf-8')
    body=src[src.index('def main():'):]
    anchors=[
        'prepare_tdoc_entrypoint(report,no_install=bool(a.no_install))',
        'check_roots(roots,report)',
        "run(report,'Build/validate Navigation + Flight Simulator Twins'",
        "source_package_ready(report,'Prepare ISO unit/frame/time adapter package'",
        "source_package_ready(report,'Prepare Boulder map/CRP package'",
        "source_package_ready(report,'Prepare SARG package'",
        'build_layer2_tep(report,twins,validation_only=validation_only)',
        "source_package_ready(report,'Prepare Layer 3 package'",
        "source_package_ready(report,'Prepare Layer 4 package'",
        "source_package_ready(report,'Prepare Layer 5 package'",
        "source_package_ready(report,'Prepare Layer6 client package'",
        "source_package_ready(report,'Prepare Layer 7 package'",
        'validate_region(region,report)',
        "run(report,'Build canonical CRP dataset'",
        "run(report,'Build SARG dataset from Twins + Map + CRP'",
        "run(report,'Download current Climate Twin forcing data to central internet dataset cache'",
        "run(report,'Inspect route/region and generate CLI route reports'",
    ]
    indices=[body.index(s) for s in anchors]
    assert indices==sorted(indices)
    assert "TEP_FILES=('tep.csv','manifest.json','heldout_validation.json','quality_report.json','VERSION')" in src
    assert 'python/build_tddt_layer2_tep.py' in src
    assert 'total_stages=26 if a.build_fmus_refresh else 25' in body
    assert "--build-fmus" in body and "--build-fmus-refresh" in body
    assert 'Layer 3 strict external runtime/dataset preflight' in body
    assert 'Prepare PT_LIB package' not in body
    assert 'build_execution_fmus' not in body



def test_prepare_all_preserves_validation_only_compatibility():
    t=(ROOT/'scripts/prepare_all.py').read_text(encoding='utf-8')
    assert "validation_only=bool(a.no_build)" in t
    assert "reuse_downloaded=bool(a.reuse_downloaded_datasets or a.no_download)" in t
    assert "no_download=bool(reuse_downloaded or validation_only)" in t
    assert '--no-install' in t

def test_offline_workflow_defaults_to_stateful_mission_and_reports_management_history():
    t=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    assert "choices=['mission','legacy'],default='mission'" in t
    assert 'L4_ADVISORY_WINGMPET_SIMULATION' in t
    assert 'sarg_used_as_next_state' in t
    assert 'OfflineWingMPETPlant' in t
    assert 'L5_CLOSED_LOOP' in t
    assert 'MISSION_MANAGEMENT_DASHBOARD.html' in t
    assert 'LAYER4_CANDIDATES.csv' in t
    assert 'ACTUATOR_HISTORY.csv' in t
    assert 'CLIMATE_HISTORY.csv' in t
    assert 'ENERGY_HISTORY.csv' in t
    assert 'PHASE_HISTORY.csv' in t
    assert 'ROUTE_HISTORY.csv' in t

def test_offline_driver_never_labels_layer4_abstract_action_as_physical_actuator():
    t=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    assert 'first_abstract_action' in t
    assert 'physical/pilot command only if Layer5 FINAL=true; otherwise no actuator command' in t
    assert 'unverified actions are never treated as physical actuation' in t

def test_online_training_cli_requires_offline_setd_and_reports_ptlib_layer7_contract():
    t=(ROOT/'scripts/run_training_workflows.py').read_text(encoding='utf-8')
    assert '--online-training' in t
    assert '--offline-knowledge-manifest' in t
    assert 'finalize_offline_knowledge' in t
    assert 'offline_knowledge_loaded_all' in t
    assert 'ptlib_layer6_participated_any' in t
    assert 'layer7_participated_any' in t
    assert 'MPC_PARTICIPATION.json' in t


def test_primary_cli_exposes_explicit_train_online_command():
    t=(ROOT/'src/tddt_tdoc/cli.py').read_text(encoding='utf-8')
    assert 'train-online' in t
    assert '--reuse-existing-offline-knowledge' in t
    assert '--online-training' in t


def test_prepare_runner_exposes_heartbeat_for_long_external_commands():
    t=(ROOT/'scripts/prepare_all.py').read_text(encoding='utf-8')
    assert 'still running (' in t
    assert 'selectors.DefaultSelector' in t

def test_reset_cli_exposes_refresh_timeout_and_live_progress_path():
    t=(ROOT/'src/tddt_tdoc/cli.py').read_text(encoding='utf-8')
    r=(ROOT/'src/tddt_tdoc/reset.py').read_text(encoding='utf-8')
    assert '--refresh-timeout-s' in t
    assert 'progress=(lambda msg: print(msg,flush=True))' in t
    assert '_run_streaming' in r and "progress('[refresh] external purge complete" in r


def test_prepare_all_has_fail_closed_source_entrypoint_mode_without_build_backend():
    text=(ROOT/'scripts/prepare_all.py').read_text(encoding='utf-8')
    assert 'AUTHORITATIVE_SOURCE_TREE_ENTRYPOINT' in text
    assert 'origin==expected' in text
    assert 'os.access(launcher,os.X_OK)' in text
    assert 'no dependency or validation bypass' in text


def test_offline_reuse_prohibits_network_and_retired_execution_fmu_build():
    src=(ROOT/'scripts/prepare_all.py').read_text(encoding='utf-8')
    assert 'reuse_downloaded=bool(a.reuse_downloaded_datasets or a.no_download)' in src
    assert 'no_download=bool(reuse_downloaded or validation_only)' in src
    assert "if no_download: cmd.append('--offline')" in src
    assert "reason='offline reuse of complete canonical regional map'" in src
    assert "network_access=False" in src
    assert 'build_execution_fmus' not in src
    assert 'TDDT_FETCH_DISCONNECTED' not in src



def test_online_not_ready_always_writes_online_reports(tmp_path):
    import importlib.util
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('tdoc_training_workflow_for_test',root/'scripts/run_training_workflows.py')
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    out=mod.write_online_not_ready(tmp_path,100,'SETD unavailable',{'ok':False})
    assert out['status']=='NOT_READY'
    assert out['cycle_count_completed']==0
    assert out['online_training_contract']['contract_ok'] is False
    assert (tmp_path/'online/ONLINE_JSBSIM_SUMMARY.json').is_file()
    assert (tmp_path/'online/ONLINE_ORCHESTRATION_DIAGNOSTICS.json').is_file()
    diag=json.loads((tmp_path/'online/ONLINE_ORCHESTRATION_DIAGNOSTICS.json').read_text())
    assert diag['ptlib_invoked'] is False and diag['layer7_invoked'] is False


def test_train_online_cli_has_auto_reuse_and_force_fresh_modes():
    root=Path(__file__).parents[1]
    t=(root/'src/tddt_tdoc/cli.py').read_text()
    assert '--run-offline-first' in t
    assert 'AUTO_REUSE_EXISTING' in t
    assert 'offline_latest.json.gz' in t
