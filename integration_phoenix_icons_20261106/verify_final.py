"""Verify installation, source preservation, and the actual Native test evidence."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
A = Path(__file__).resolve().parent
TDOC = ROOT / 'TDOC_v20260911_2052'
RUN = TDOC / 'reports/online/runs/20261106_092359_103f0383'
sys.path.insert(0, str(TDOC / 'scripts'))
from online_reports.provenance import snapshot, compare, write_json
from tddt_tdoc.config import TDOCConfig


def read(path):
    return json.loads(path.read_text())


cfg = TDOCConfig.from_yaml(TDOC / 'configs/tdoc.yaml')
after = snapshot(cfg)
write_json(A / 'preservation_after_online.json', after)
preservation = compare(read(A / 'preservation_before.json'), after)
write_json(A / 'preservation_final.json', preservation)
result = read(RUN / 'evidence/native/MISSION_RESULT.json')
qualification = read(RUN / 'mission/NATIVE_QUALIFICATION.json')
installation = read(A / 'install_manifest_first.json')
live = read(A / 'live_native_process.json')
manifest_checks = []
for file in sorted((RUN / 'evidence/windows').glob('*/manifest.json')):
    m = read(file)
    images = []
    for role, row in m['windows'].items():
        p = Path(row['image'])
        images.append(row['ok'] and p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == row['sha256'])
    manifest_checks.append({'path': str(file), 'all_six': m['all_six_captured'] and len(images) == 6 and all(images)})
opens = [json.loads(line) for line in (A / 'live_resource_opens.jsonl').read_text().splitlines()]
events = [r for r in opens if r['kind'] == 'resource_open']
process_time = live['mavproxy_process.json']['started_monotonic']
assert live['native_panels.json']['session_id'] == result['native_session_id']
assert live['native_panels.json']['heartbeat']['armed'] is True
assert live['actual_process_cmdline'][0] == installation['native_python']
assert any(r['filename'] == 'greenplane.png' for r in events)
assert len(installation['decoded_pixel_matches']) == 25
assert preservation['status'] == 'PASS'
assert manifest_checks and all(r['all_six'] for r in manifest_checks)
assert read(A / 'PATCH_APPLY_VERIFICATION.json')['status'] == 'PASS'
assert read(A / 'presentation_isolation.json')['status'] == 'PASS'
for row in installation['installed_files']:
    assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() == row['sha256']
summary = {'schema': 'TDDT-Phoenix-map-patch-qualification-v1',
    'icon_patch_status': 'PASS', 'native_mission_status': result['status'],
    'independent_qualification_status': qualification['status'],
    'overall_status': 'PASS' if result['status'] == qualification['status'] == 'PASS' else 'FAIL',
    'session_id': result['native_session_id'], 'run': str(RUN),
    'model_id': after['model_dependencies']['model_id'], 'preservation': preservation,
    'cycles': result.get('cycles'), 'actual_goal_error_m': result.get('actual_goal_error_m'),
    'accepted_feedback': result.get('accepted_feedback'), 'consumed_feedback': result.get('consumed_feedback'),
    'touchdown_confirmed': result.get('touchdown_confirmed'), 'disarm_confirmed': result.get('disarm_confirmed'),
    'screenshot_manifest_count': len(manifest_checks), 'screenshot_manifests': manifest_checks,
    'actual_native_python_verified': True, 'aircraft_aliases_pixel_verified': 25,
    'live_resource_events': events, 'inotify_PID_attribution_claimed': False,
    'physical_map_screenshots_visually_reviewed': True,
    'no_new_reset_or_build': True, 'no_flight_control_source_change': True}
write_json(A / 'FINAL_VERIFICATION.json', summary)
print(json.dumps({k: v for k, v in summary.items() if k not in ('screenshot_manifests', 'preservation')}, indent=2))
