#!/usr/bin/env python3
"""R7.03 real Native first-move/horizon forecasts; never a mission executor."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from layer4.native_provider_contract import DOMAIN, validate_prediction
from layer4.native_reference_provider import NativeReferenceProvider, candidate_digest, make_reference_request
from layer6.native_forecast import NativeForecastRunner
from layer6.native_replay import reserve_ports, sha
from layer6.native_session import processes, write
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.model_compatibility import capture_model_dependencies
from tddt_tdoc.native_environment import prepare_binding


class ClockStalledRunner(NativeForecastRunner):
    def forecast(self, request):
        self.inject_stall = True
        self.stall_applied = False
        return super().forecast(request)

    def _tick(self):
        if getattr(self, 'inject_stall', False) and not self.stall_applied and self.source_time > self._anchor+.03:
            self._freeze()
            self.stall_applied = True
        return super()._tick()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='configs/tdoc.yaml')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--case-limit', type=int, default=3)
    p.add_argument('--no-map', action='store_true')
    p.add_argument('--include-clock-fault', action='store_true')
    a = p.parse_args()
    if not 1 <= a.case_limit <= 3:
        p.error('case limit 1..3')
    cfg = TDOCConfig.from_yaml(a.config)
    root = cfg.package_root.parent
    l6 = cfg.path_value('TDDT_LAYER6_ROOT', required=True)
    out = Path(a.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    previous = cfg.package_root/'reports/online/R7_02_20261031_isolated_native_replay_v8'
    accepted = json.loads((previous/'CLOSEOUT_RESULT.json').read_text())
    assert accepted['status'] == 'PASS'
    current = capture_model_dependencies(cfg)
    prior = json.loads((previous/'MODEL_DEPENDENCIES.json').read_text())
    for name in ('arduplane_binary', 'jsbsim_binary', 'native_aircraft', 'native_bridge',
                 'native_body_rate_decoder', 'native_bridge_header', 'ardupilot_default_parameters',
                 'layer6/native_replay.py'):
        assert current['artifacts'][name] == prior['artifacts'][name], name
    write(out/'MODEL_DEPENDENCIES.json', current)
    binding = prepare_binding(cfg, output_path=out/'binding.json')
    registry = l6/'runtime/flightstack_state.json'
    before = registry.read_bytes()
    mission = json.loads(before)['session_id']
    mission_processes = processes(mission)
    write(out/'MISSION_BEFORE.json', {'registry_sha256': sha(registry), 'processes': mission_processes})
    ap, fs = l6/'FlightStack/ardupilot', l6/'FlightStack'
    reference_parameters = cfg.package_root/'reports/online/R6_04_20261031_physical_reacceptance_v7/native_final/NATIVE_SESSION/mav.parm'
    hashes = {'arduplane': sha(ap/'build/sitl/bin/arduplane'), 'jsbsim': sha(fs/'jsbsim/build/src/JSBSim'),
              'aircraft': sha(ap/'Tools/autotest/aircraft/phoenix_v3/phoenix_v3.xml'),
              'reset': sha(ap/'Tools/autotest/aircraft/phoenix_v3/reset.xml'),
              'defaults': sha(ap/'Tools/autotest/default_params/phoenix_v3.parm'),
              'gui_config': sha(l6/'gui/config/f310_phoenix.yaml'), 'parameters': sha(reference_parameters)}
    cases = [('FBWA', False), ('QHOVER', False), ('QSTABILIZE', False)][:a.case_limit]
    if a.include_clock_fault:
        cases.append(('FBWA', True))
    results = []
    for index, (mode, fault) in enumerate(cases):
        cls = ClockStalledRunner if fault else NativeForecastRunner
        runner = cls(layer6_root=l6, output=out/('branch_%02d' % index),
                     binding_file=out/'binding.json', parameter_file=reference_parameters,
                     backend_hashes=hashes, mission_session_id=mission, instance=43)
        with reserve_ports(43):
            try:
                runner.start(show_map=not a.no_map)
                reference = runner.prepare_reference(mode)
                plan = [{'domain': DOMAIN, 'u_cmd': [x, y, 0., z], 'mode': mode, 'discrete': []}
                        for x, y, z in ((.2, -.1, .1), (-.15, .1, -.1), (.1, .05, .05))]
                request = make_reference_request(reference, candidate_id=mode+'-varying-horizon', pilot_plan=plan)
                provider = NativeReferenceProvider(runner)
                negatives = []
                for change in ('deadline', 'candidate', 'state', 'ARM', 'throttle', 'mode', 'horizon'):
                    bad = deepcopy(request)
                    if change == 'deadline': bad['timing']['admission_deadline_unix_s'] = 1.
                    if change == 'candidate': bad['context']['candidate_hash'] = '0'*64
                    if change == 'state': bad['initial_state']['spool']['uncertainty']['reason'] = 'foreign checkpoint'
                    if change == 'ARM': bad['pilot_plan'][0]['discrete'] = ['ARM']
                    if change == 'throttle': bad['pilot_plan'][0]['u_cmd'][2] = .1
                    if change == 'mode': bad['pilot_plan'][1]['mode'] = 'QLOITER'
                    if change == 'horizon': bad['pilot_plan'] *= 10
                    if change in ('ARM', 'throttle', 'mode', 'horizon'):
                        bad['context']['candidate_hash'] = candidate_digest(bad['pilot_plan'], .2, .1)
                    sent = runner.link.master.mav.total_packets_sent
                    rejection = provider.predict(bad)
                    assert rejection['status'] == 'UNAVAILABLE' and not rejection['samples']
                    assert validate_prediction(bad, rejection)['contract_valid']
                    assert runner.link.master.mav.total_packets_sent == sent and runner._frozen
                    negatives.append({'case': change, 'reason': rejection['reason'], 'no_MAVLink_sent': True})
                write(runner.output/'NEGATIVE_CASES.json', negatives)
                write(runner.output/'REQUEST.json', request)
                prediction = provider.predict(request)
                write(runner.output/'PREDICTION.json', prediction)
                write(runner.output/'FORECAST_EVIDENCE.json', provider.last_evidence)
                valid = validate_prediction(request, prediction)
                write(runner.output/'CONTRACT_VALIDATION.json', valid)
                assert valid['contract_valid'], valid
                if fault:
                    assert runner.stall_applied and prediction['status'] == 'UNAVAILABLE'
                    assert not prediction['samples'] and provider.last_evidence is None
                else:
                    assert prediction['status'] == 'PREDICTED', prediction['reason']
                    assert len(prediction['samples']) == 4
                    assert provider.last_evidence['first_move']['offset_s'] == .1
                    assert len(provider.last_evidence['plan_application']['changes']) == 3
                    assert prediction['uncertainty']['kind'] == 'unbounded'
                results.append({'mode': mode, 'clock_fault': fault, 'status': 'PASS',
                                'prediction_status': prediction['status'], 'session_id': runner.session_id,
                                'request_sha256': prediction['request_sha256']})
                print(json.dumps(results[-1]), flush=True)
            except Exception as exc:
                write(out/'ERROR.json', {'case': index, 'reason': repr(exc)})
                raise
            finally:
                runner.stop()
        assert not processes(runner.session_id)
    assert registry.read_bytes() == before and processes(mission) == mission_processes
    assert capture_model_dependencies(cfg) == current
    write(out/'MISSION_AFTER.json', {'registry_sha256': sha(registry), 'processes': processes(mission),
          'registry_and_processes_unchanged': True, 'active_mission_present': bool(mission_processes)})
    write(out/'NATIVE_PREDICTION_ACCEPTANCE.json', {'step': 'R7.03', 'status': 'PASS', 'cases': results,
          'model_id': current['model_id'], 'binding_hash': binding['binding_hash'],
          'cold_boot_reference_only': True, 'first_move_and_horizon_generated': True,
          'independent_R704_tolerance_validation_done': False, 'certificate_issued': False,
          'mission_FINAL_or_flight_authorized': False})


if __name__ == '__main__':
    main()
