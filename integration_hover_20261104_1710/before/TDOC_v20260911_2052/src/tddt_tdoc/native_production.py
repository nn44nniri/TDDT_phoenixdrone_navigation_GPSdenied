"""TDOC composition of current Native prediction; no request-installed provider.

Binding a trusted Python model is not qualification. The existing issuer,
Layer4 gates, Layer5 gates and independent consumer still authorize each cycle.
"""
from copy import deepcopy
import time

from layer4.native_operational import require, finite
from layer4.native_provider_contract import digest
from layer6.binding import context_guard
from layer6.status import validate_observation_status
from .native_operational_certificate import NativeOperationalAuthority, NativeOperationalService
from .native_execution import NativeExecutionRechecker


class NativeProductionLane:
    def __init__(self, config, adapters, model, binding_path, proposal):
        require(callable(proposal) and callable(getattr(model, 'verify_qualification', None))
                and callable(getattr(model, 'current_backend', None)) and callable(getattr(model, 'predict', None)),
                'trusted Native model/proposal protocol incomplete')
        require(config.get('layers.layer6.online.transport') == 'Layer6Client_GUI_MAVLink',
                'Native production binding requires Native transport')
        self.config = config
        self.adapters = adapters
        self.model = model
        self.proposal = proposal
        self.binding_path = binding_path
        self.guard = context_guard(binding_path)
        adapters[4]._ensure()
        self.authority = NativeOperationalAuthority(config, binding_path, bounded_model=model,
                                                    layer4_config=adapters[4].engine.config)
        self.checker = None

    def install(self):
        require(getattr(self.adapters[4], 'native_execution_source', None) is None,
                'Native model source already bound; replacement requires a new composition')
        self.adapters[4].bind_native_execution_source(self.source)
        self.adapters[3].native_snapshot_sealer = self.seal_snapshot
        self.adapters[2].native_observation_source = self.read_current_ingress
        return {'status': 'BOUND_PENDING_PER_TRANSACTION_QUALIFICATION',
                'FINAL_authorized': False, 'ARM_authorized': False, 'actuation_performed': False}

    def read_current_ingress(self):
        """Fresh IMU after slow climate work; FCU position remains evaluation-only."""
        self.guard.check()
        status = self.adapters[6]._status()
        check = validate_observation_status(status)
        require(check['ok'], 'current Native observation rejected: '+check['reason'])
        require(status['armed'] is False and status['mode'] in ('QHOVER', 'QSTABILIZE'),
                'Native observation outside DISARMED ground model scope')
        imu = deepcopy(status['imu'])
        return {'ok': True, 'source': 'Layer6/GUI/MAVLink', 'released': True,
                'gui_state': deepcopy(status), 'imu_samples': deepcopy(status.get('imu_samples', [])),
                'state': {**imu, 'runtime_ready': 1., 'imu_time_s': imu['plant_time_s'],
                          'imu_frame': imu['frame'], 'imu_source': imu['source']}}

    def seal_snapshot(self, snapshot, packets, request):
        """Publish current observation provenance without replacing MESA state."""
        if str(request.get('learning_mode', '')).upper() != 'ONLINE_JSBSIM':
            return snapshot
        binding = self.guard.check()
        status = self.adapters[6]._status()
        check = validate_observation_status(status)
        require(check['ok'], 'current Native observation rejected: '+check['reason'])
        ingress = (packets.get('MESA_NAV', {}).get('provenance', {}).get('online_imu_ingress') or {})
        require(ingress.get('navigation_authority') == 'MESA_STD'
                and ingress.get('measured_simulated_imu') is True
                and all(ingress.get(k) is False for k in ('truth_position_used', 'truth_velocity_used', 'gnss_used')),
                'actual MESA IMU-only propagation provenance required')
        require(finite(ingress.get('plant_time_s')) and finite(snapshot.get('reference_time_s'))
                and abs(ingress['plant_time_s']-snapshot['reference_time_s']) <= 1e-6,
                'MESA snapshot source time mismatch')
        original = (packets['MESA_NAV'].get('context', {}).get('online_state_ingress', {})
                    .get('ptlib_ingress', {}).get('gui_state') or {})
        observed = original.get('imu') or {}
        require(observed.get('plant_time_s') == ingress['plant_time_s']
                and original.get('session_id') == status['session_id']
                and original.get('monotonic_domain_id') == status['monotonic_domain_id']
                and original.get('mode') == status['mode'] and original.get('armed') is False,
                'MESA source observation/session/domain mismatch')
        received = observed.get('timestamp_monotonic')
        source_age = time.monotonic()-received if finite(received) else float('inf')
        maximum_age = float(self.adapters[4].engine.config.get('snapshot.max_age_s', 5.))
        # Snapshot age is not the 0.15s maximum *single IMU integration step*.
        # Use the existing Layer4 snapshot-age policy and preserve the measured
        # source delay for the predictor's per-request qualification check.
        require(0 <= source_age <= maximum_age
                and 0 <= status['imu']['plant_time_s']-ingress['plant_time_s'] <= maximum_age,
                'Native/MESA snapshot source age outside current policy')
        require(status['armed'] is False and status['mode'] in ('QHOVER', 'QSTABILIZE'),
                'Native production scope is DISARMED ground only')
        require(request.get('stage') == 'on_ground', 'Native production phase outside initial scope')
        # Derive age from the original observed IMU receipt, not the end of
        # computation. An old observation cannot become fresh by serialization.
        # Original FCU/FMI source clocks remain intact.
        result = deepcopy(snapshot)
        context = result.setdefault('context', {})
        context.update(snapshot_epoch_s=time.time()-source_age, phase=request['stage'],
                       coordinate_origin_sha256=digest(binding['mission_origin']),
                       rtmo_data_authority={'navigation': 'MESA_STD'}, truth_injection_to_mesa=False)
        context['snapshot_epoch_basis'] = 'derived wall receipt of actual MESA source IMU; original FCU clock retained'
        context['native_current_context'] = {
            'schema': 'TDOC-Native-current-context-v1', 'session_id': status['session_id'],
            'monotonic_domain_id': status['monotonic_domain_id'],
            'MESA_source_time_s': ingress['plant_time_s'], 'FCU_source_time_s': status['imu']['plant_time_s'],
            'source_receipt_age_s': source_age,
            'observation_source_delay_s': status['imu']['plant_time_s']-ingress['plant_time_s'],
            'predictor_must_cover_observation_delay': True,
            'mode': status['mode'], 'armed': status['armed'],
            'binding_hash': binding['binding_hash'], 'model_id': binding['model_id'],
            'forcing_sha256': binding['forcing_file_sha256'],
            'IMU_wire_sha256': status['imu']['wire_sha256'],
            'source_observation_role': 'IMU navigation; FCU position is evaluation only'}
        from layer4.snapshot import Layer3SnapshotAdapter
        result['snapshot_hash'] = Layer3SnapshotAdapter(self.adapters[4].engine.config).canonical_hash(result)
        result['provenance'].pop('snapshot_hash', None)
        self.guard.check()
        return result

    def source(self, snapshot, request):
        self.guard.check()
        current = snapshot.get('context', {}).get('native_current_context') or {}
        require(current.get('schema') == 'TDOC-Native-current-context-v1', 'current Native Layer3 context missing')
        require('native_safety_policy' in snapshot.get('constraints', {}),
                'mission-derived Native safety envelope unavailable; no fixture fallback')
        candidate = self.proposal(deepcopy(snapshot), deepcopy(request), self.model)
        require(candidate.get('snapshot') == snapshot, 'proposal changed current Layer3 snapshot')
        service = NativeOperationalService(self.authority)
        checker = NativeExecutionRechecker(self.config, self.binding_path, self.adapters[4], service)
        self.adapters[5]._ensure()
        self.adapters[5].engine.native_execution_checker = checker
        self.checker = checker
        return candidate, service
