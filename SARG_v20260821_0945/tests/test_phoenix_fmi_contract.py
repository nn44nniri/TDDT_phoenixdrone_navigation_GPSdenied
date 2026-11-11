from sarg_builder.fmi_execution import StrictFMIEngine


class FakeConfig:
    def get(self, key, default=None):
        return default


class FakeFMU:
    def __init__(self, model, ops):
        self.model = model
        self._ops = set(ops)
        self.calls = []

    def list_operations(self):
        return sorted(self._ops)

    def evaluate_operation(self, operation, inputs=None, use_config=True, require_all_inputs=False):
        self.calls.append(operation)
        if operation == 1:
            return {"formalism_version": 3.0 if self.model == "wing" else 4.0}
        if self.model == "wing" and operation == 34:
            return {"formalism_complete_flag": 1.0, "vtol_full_envelope_flag": 1.0}
        if self.model == "mesa" and operation == 27:
            return {"formalism_complete_flag": 1.0, "mode_aware_constraints_flag": 1.0}
        raise AssertionError(f"Unexpected constructor-time call: {self.model} op {operation}")


class FakeManager:
    def __init__(self):
        # Deliberately omit retired Wing 10/26 and MESA 8/20.
        self.wingmpet = FakeFMU("wing", {1, 2, 5, 6, 7, 9, 13, 14, 15, 16, 30, 34})
        self.mesa = FakeFMU("mesa", {1, 7, 10, 11, 14, 15, 19, 21, 24, 27})


def test_phoenix_contract_accepts_new_operation_set_without_retired_ops():
    manager = FakeManager()
    engine = StrictFMIEngine(manager, FakeConfig())
    assert engine.contract_profile == "PHOENIX_FA_20260820"
    assert 10 not in manager.wingmpet.calls
    assert 26 not in manager.wingmpet.calls
    assert 8 not in manager.mesa.calls
    assert 20 not in manager.mesa.calls
    assert manager.wingmpet.calls == [1, 34]
    assert manager.mesa.calls == [1, 27]
    assert engine.contract_details["replacement_operations"]["WingMPET.flight_envelope"] == 30
    assert engine.contract_details["replacement_operations"]["MESA_NAV.wind_context_anchor"] == 11
    assert engine.contract_details["replacement_operations"]["MESA_NAV.mechanical_constraints"] == 24


def test_crp_wind_never_synthesizes_ultrasonic_measurement():
    manager = FakeManager()
    engine = StrictFMIEngine(manager, FakeConfig())

    def mesa_eval(operation, inputs=None, use_config=True, require_all_inputs=False):
        manager.mesa.calls.append(operation)
        assert operation == 11
        return {
            "terrain_flow_update_residual": 0.0,
            "terrain_flow_update_valid": 1.0,
            "terrain_flow_update_weight": 1.0,
        }

    manager.mesa.evaluate_operation = mesa_eval
    result = engine._wind_context(
        wind={"x": 4.0, "y": -1.0, "h": 0.0, "variance": 1.0, "sigma": 1.0},
        dt=0.1,
    )
    assert result["physical_ultrasonic_used"] == 0.0
    assert 23 not in manager.mesa.calls
    assert manager.mesa.calls[-1] == 11
