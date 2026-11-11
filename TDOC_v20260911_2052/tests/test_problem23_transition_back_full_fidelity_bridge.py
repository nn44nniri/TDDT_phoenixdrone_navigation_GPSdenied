from __future__ import annotations

from types import SimpleNamespace

from tddt_tdoc.adapters.layer5 import Layer5Adapter


def _adapter():
    a = Layer5Adapter.__new__(Layer5Adapter); a.config=SimpleNamespace(get=lambda key, default=None: 'OFFLINE_SE' if key == 'layers.layer6.online.transport' else default)
    a.engine = SimpleNamespace(phoenix=SimpleNamespace(elevon_max=0.53))
    a._ensure = lambda: None
    a._verification_model_certificate = {
        "schema": "Layer5-Phoenix-offline-model-certificate-v3",
        "ok": True,
        "simulation_only": True,
        "mixer": {
            "elevon_matrix_rad_per_norm": [[0.02, 0.02], [-0.02, 0.02]],
            "trim_rad": [-0.08, -0.08],
        },
        "trim": {"pitch_request_norm": 0.0, "throttle_request_norm": 0.30},
        "throttle_path": {
            "qualified_delta_from_operating_request_norm": [-0.10, 0.10],
            "trim_norm": 0.30,
            "gain_motor_norm_per_pilot_delta": 1.0,
        },
        "phase_throttle_paths": {
            "transition_back": {
                "model_qualified": True,
                "request_bounds_norm": [0.20, 0.42],
                "operating_request_norm": 0.30,
                "operating_motor_norm": 0.30,
                "gain_motor_norm_per_pilot_delta": 1.0,
                "qualification_scope": "command_mapping_only; physical transition-back feasibility deferred to FF replay",
            }
        },
    }
    return a


def test_transition_back_observed_request_is_phase_qualified():
    a = _adapter()
    # problem20_fixed_v1: generic delta was about +0.10319 from 0.30.
    t = 0.40319
    out = a._wingmpet_verification_mix([0.0, 0.0, t, 0.0], phase="transition_back")
    assert out["ok"] is True
    assert out["phase"] == "transition_back"
    assert out["phase_throttle_qualification"] == "transition_back_command_mapping_only"
    assert abs(out["physical"][2] - t) < 1e-12


def test_same_request_remains_outside_generic_forward_neighborhood():
    a = _adapter()
    out = a._wingmpet_verification_mix([0.0, 0.0, 0.40319, 0.0], phase="cruise")
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_throttle_outside_qualified_neighborhood"
    assert out["delta_from_operating_request_norm"] > 0.10


def test_transition_back_phase_envelope_is_fail_closed_outside_bounds():
    a = _adapter()
    out = a._wingmpet_verification_mix([0.0, 0.0, 0.421, 0.0], phase="transition_back")
    assert out["ok"] is False
    assert out["reason"] == "wingmpet_verification_transition_back_throttle_outside_qualified_neighborhood"
    assert out["qualified_request"] == [0.20, 0.42]


def test_transition_back_mapping_does_not_claim_physical_feasibility():
    a = _adapter()
    out = a._wingmpet_verification_mix([0.0, 0.0, 0.35, 0.0], phase="transition_back")
    assert out["ok"] is True
    assert out["scope"] == "L4_WINGMPET_FULL_FIDELITY_ONLY"
    assert out["simulation_only"] is True
    assert out["direct_pwm"] is False
