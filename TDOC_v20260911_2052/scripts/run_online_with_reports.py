#!/usr/bin/env python3
"""Run the unchanged canonical Native command, then generate rich Online reports."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import traceback
import uuid
from zoneinfo import ZoneInfo

from tddt_tdoc.config import TDOCConfig
from online_reports.provenance import compare, snapshot, write_json


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', default=str(Path(__file__).resolve().parents[1] / 'configs/tdoc.yaml'))
    ap.add_argument('--reports-root', type=Path)
    ap.add_argument('--local-hover-seconds', type=float, help='Explicit commissioning test; omitted runs original full route')
    ap.add_argument('--local-hover-height-m', type=float, default=3.)
    args = ap.parse_args()
    cfg = TDOCConfig.from_yaml(Path(args.config).resolve())
    base = (args.reports_root or cfg.package_root / 'reports/online').resolve()
    offline = (cfg.package_root / 'reports/offline').resolve()
    if base == offline or base.is_relative_to(offline):
        ap.error('Online reports cannot be written inside Offline reports')
    run_id = datetime.now(ZoneInfo('America/Toronto')).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    run = base / 'runs' / run_id
    run.mkdir(parents=True, exist_ok=False)
    evidence = run / 'evidence'
    native = evidence / 'native'
    before = snapshot(cfg)
    write_json(evidence / 'preservation_before.json', before)
    if not before['handoff_gate']['ok']:
        write_json(run / 'RUN_STATUS.json', {'status': 'BLOCKED', 'reason': 'existing compatible Offline prior required'})
        return 2
    stop = evidence / 'observer.stop'
    command = [sys.executable, '-u', '-m', 'tddt_tdoc.cli', '--config', str(cfg.path),
        'nav-online', '--reuse-existing-offline-knowledge', '--online-episodes', '1',
        '--online-max-cycles-per-episode-auto', '--online-realtime-output', str(native)]
    if args.local_hover_seconds is not None:
        command += ['--online-local-hover-seconds', str(args.local_hover_seconds),
                    '--online-local-hover-height-m', str(args.local_hover_height_m)]
    write_json(evidence / 'invocation.json', {'command': command, 'cwd': str(cfg.package_root),
        'control_source_changed': False, 'reporting_stage': 'after Native command completion'})
    print('[Online report run]', run, flush=True)
    observer_log = (evidence / 'observer.log').open('w')
    observer = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('observe_online_session.py')),
        '--output', str(evidence / 'windows'), '--stop-file', str(stop)],
        stdout=observer_log, stderr=subprocess.STDOUT)
    child = None
    code = 2
    try:
        with (evidence / 'native.log').open('w', buffering=1) as log:
            child = subprocess.Popen(command, cwd=cfg.package_root, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in child.stdout:
                print(line, end='', flush=True)
                log.write(line)
            code = child.wait()
    except KeyboardInterrupt:
        if child is not None and child.poll() is None:
            import signal
            child.send_signal(signal.SIGINT)
            child.wait(timeout=30)
    finally:
        stop.write_text('Native command ended; stop read-only observer.\n')
        try:
            observer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            observer.terminate()
            observer.wait(timeout=5)
        observer_log.close()
    after = snapshot(cfg)
    write_json(evidence / 'preservation_after.json', after)
    preservation = compare(before, after)
    write_json(evidence / 'preservation.json', preservation)
    try:
        from online_reports.generate import generate
        summary = generate(native, run / 'mission', evidence=evidence, config=cfg)
        from online_reports.generate import publish_latest
        publish_latest(base, run, summary)
    except Exception as exc:
        write_json(run / 'REPORT_ERROR.json', {'status': 'FAIL', 'error': repr(exc), 'traceback': traceback.format_exc()})
        raise
    print('[Online reports]', run / 'mission/MISSION_MANAGEMENT_DASHBOARD.html', flush=True)
    return 0 if code == 0 and summary['qualification']['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
