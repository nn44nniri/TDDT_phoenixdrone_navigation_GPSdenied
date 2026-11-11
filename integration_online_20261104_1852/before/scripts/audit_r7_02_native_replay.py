#!/usr/bin/env python3
"""Run R7.02 reference branches and the real, observation-only MAVProxy map.

This diagnostic never uses the mission executor and never arms. Results are
reference traces, not predictions, certificates, FINAL or flight qualification.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import uuid

from layer6.native_replay import NativeReferenceReplay, ports, reserve_ports, sha
from layer6.native_session import processes, write
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.model_compatibility import capture_model_dependencies
from tddt_tdoc.native_environment import prepare_binding


def screenshot(branch):
    title = 'Phoenix reference map ' + branch.session_id[:8]
    # Filter in memory: do not publish names or screenshots of unrelated apps.
    result = subprocess.run(['xwininfo', '-root', '-tree'], capture_output=True, text=True, timeout=5, check=True)
    matches = [line.strip() for line in result.stdout.splitlines() if title in line]
    if len(matches) != 1:
        raise RuntimeError('owned map window not uniquely visible')
    window = matches[0].split()[0]
    target = branch.output/'map/SCREENSHOT.png'
    subprocess.run(['import', '-window', window, str(target)], check=True, timeout=10)
    write(branch.output/'map/WINDOW.json', {'title': title, 'window_id': window,
          'matched_owned_window': matches[0], 'screenshot_sha256': sha(target)})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='configs/tdoc.yaml')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--instance', type=int, default=42)
    p.add_argument('--case-limit', type=int, default=4)
    a = p.parse_args()
    if not 1 <= a.case_limit <= 4:
        p.error('case limit must be 1..4')
    cfg = TDOCConfig.from_yaml(a.config)
    root = cfg.package_root.parent
    l6 = cfg.path_value('TDDT_LAYER6_ROOT', required=True)
    out = Path(a.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    old = cfg.package_root/'reports/online/R6_04_20261031_physical_reacceptance_v7'
    assert json.loads((old/'CLOSEOUT_RESULT.json').read_text())['status'] == 'PASS'
    model = capture_model_dependencies(cfg)
    approved = json.loads((old/'MODEL_DEPENDENCIES.json').read_text())
    for key in ('arduplane_binary', 'jsbsim_binary', 'native_aircraft', 'native_bridge',
                'native_bridge_header', 'native_body_rate_decoder', 'ardupilot_default_parameters'):
        if model['artifacts'][key] != approved['artifacts'][key]:
            raise ValueError('unaccepted native reference backend: ' + key)
    write(out/'MODEL_DEPENDENCIES.json', model)
    reference = old/'native_final/NATIVE_SESSION/mav.parm'
    binding = prepare_binding(cfg, output_path=out/'binding.json')
    registry = l6/'runtime/flightstack_state.json'
    registry_before = registry.read_bytes()
    mission_id = json.loads(registry_before)['session_id']
    mission_before = processes(mission_id)
    write(out/'MISSION_BEFORE.json', {'session_id': mission_id, 'processes': mission_before,
          'registry_sha256': sha(registry)})
    fs = l6/'FlightStack'
    ap = fs/'ardupilot'
    hashes = {'arduplane': sha(ap/'build/sitl/bin/arduplane'),
              'jsbsim': sha(fs/'jsbsim/build/src/JSBSim'),
              'aircraft': sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'),
              'reset': sha(ap/'Tools/autotest/aircraft/phoenix_v3/reset.xml'),
              'defaults': sha(ap/'Tools/autotest/default_params/phoenix_v3.parm'),
              'gui_config': sha(l6/'gui/config/f310_phoenix.yaml'), 'parameters': sha(reference)}
    cases = [
        {'candidate_id': 'FBWA-neutral', 'mode': 'FBWA', 'u_cmd': [0., 0., 0., 0.], 'duration_s': 1.},
        {'candidate_id': 'FBWA-axes', 'mode': 'FBWA', 'u_cmd': [.25, -.15, 0., .1], 'duration_s': 1.},
        {'candidate_id': 'QHOVER-axes', 'mode': 'QHOVER', 'u_cmd': [-.15, .1, 0., -.1], 'duration_s': 1.},
        {'candidate_id': 'QSTABILIZE-axes', 'mode': 'QSTABILIZE', 'u_cmd': [.15, -.1, 0., .1], 'duration_s': 1.},
    ][:a.case_limit]
    results = []
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', ports(a.instance)['fcu_tcp']))
        try:
            with reserve_ports(a.instance):
                raise AssertionError('occupied reference port accepted')
        except OSError:
            write(out/'OCCUPIED_PORT_REJECTION.json', {'status': 'PASS', 'occupied_port_rejected': True})
    for index, plan in enumerate(cases):
        branch = NativeReferenceReplay(layer6_root=l6, output=out/('branch_%02d' % index),
                    binding_file=out/'binding.json', parameter_file=reference, backend_hashes=hashes,
                    mission_session_id=mission_id, instance=a.instance)
        with reserve_ports(a.instance):
            sentinel = subprocess.Popen(['/bin/cat'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                env=dict(os.environ, TDDT_NATIVE_SESSION_ID=uuid.uuid4().hex))
            try:
                branch.start(show_map=True)
                rejected = []
                for change in ({'u_cmd': [0., 0., .1, 0.]}, {'mode': 'AUTO'}, {'ARM': True}):
                    sent = branch.link.master.mav.total_packets_sent
                    try:
                        branch.run_candidate({**plan, **change})
                    except ValueError as exc:
                        assert branch.link.master.mav.total_packets_sent == sent
                        rejected.append({'change': change, 'reason': str(exc), 'MAVLink_sent': False})
                    else:
                        raise AssertionError('unsafe reference candidate accepted')
                original = branch.expected['arduplane']
                branch.expected['arduplane'] = '0'*64
                try:
                    branch.verify_backend()
                except ValueError as exc:
                    rejected.append({'change': 'backend SHA256 mismatch', 'reason': str(exc), 'MAVLink_sent': False})
                else:
                    raise AssertionError('wrong backend accepted')
                finally:
                    branch.expected['arduplane'] = original
                result = branch.run_candidate(plan)
                sent = branch.link.master.mav.total_packets_sent
                try:
                    branch.run_candidate(plan)
                except RuntimeError as exc:
                    assert branch.link.master.mav.total_packets_sent == sent
                    rejected.append({'change': 'reuse of consumed cold branch', 'reason': str(exc), 'MAVLink_sent': False})
                else:
                    raise AssertionError('reference internal state reused')
                write(branch.output/'NEGATIVE_CASES.json', rejected)
                screenshot(branch)
                results.append(result)
                write(branch.output/'PROCESSES_DURING.json', processes(branch.session_id))
                print(json.dumps({'candidate': plan['candidate_id'], 'branch': branch.session_id,
                                  'status': 'TRACE_RECORDED', 'start': result['start_fcu_boot_s'],
                                  'stop': result['stop_fcu_boot_s']}, ensure_ascii=False), flush=True)
            except Exception as exc:
                write(out/'ERROR.json', {'case': index, 'error': repr(exc)})
                raise
            finally:
                try:
                    branch.stop()
                    assert sentinel.poll() is None, 'unrelated owned diagnostic process was stopped'
                    write(out/('UNRELATED_PROCESS_%02d.json' % index), {'status': 'PASS',
                          'sentinel_pid': sentinel.pid, 'different_session_survived_branch_stop': True})
                finally:
                    sentinel.terminate()
                    sentinel.wait(timeout=3)
        # Restart must bind the same private endpoints, with a new boot/session.
        if processes(branch.session_id):
            raise RuntimeError('reference cleanup incomplete')
    mission_after = processes(mission_id)
    if registry.read_bytes() != registry_before or mission_after != mission_before:
        raise RuntimeError('mission ownership/registry changed during isolated replay')
    if capture_model_dependencies(cfg) != model:
        raise RuntimeError('model changed during reference acceptance')
    write(out/'MISSION_AFTER.json', {'processes': mission_after, 'registry_sha256': sha(registry),
          'registry_and_ownership_unchanged': True, 'active_mission_present': bool(mission_before)})
    completed = []
    for index, result in enumerate(results):
        folder = out/('branch_%02d' % index)
        stop = json.loads((folder/'STOP_RESULT.json').read_text())
        obs = json.loads((folder/'map/observation.json').read_text())
        assert stop['stopped'] and stop['heartbeats'] and not any(h['armed'] for h in stop['heartbeats'])
        assert obs['position_matches_map'] and obs['wx_ready'] and obs['map_alive']
        assert stop['map_feed']['forwarded_map_control_packets'] == 0
        assert stop['map_feed']['discarded_map_packets'] >= 1
        counts = stop['message_counts']
        assert all(counts.get(k, 0) > 0 for k in ('ATTITUDE', 'SERVO_OUTPUT_RAW', 'GLOBAL_POSITION_INT', 'RC_CHANNELS'))
        completed.append({'candidate_id': result['candidate']['candidate_id'], 'branch_session_id': result['branch_session_id'],
                          'trace_sha256': stop['telemetry_sha256'], 'messages': counts,
                          'map_observation': obs, 'heartbeats': len(stop['heartbeats'])})
    write(out/'NATIVE_REPLAY_ACCEPTANCE.json', {'step': 'R7.02', 'status': 'PASS', 'cases': completed,
          'case_count': len(results), 'full_four_case_acceptance': len(results) == 4,
          'model_id': model['model_id'], 'binding_hash': binding['binding_hash'],
          'accepted_forcing_preserved': True, 'map_real_MAVProxy_wx': True,
          'cold_boot_reference_only': True, 'restored_active_controller_checkpoint': False,
          'provider_prediction_implemented': False, 'flight_authorized': False,
          'mission_registry_and_processes_unchanged': True, 'all_recorded_heartbeats_DISARMED': True})


if __name__ == '__main__':
    main()
