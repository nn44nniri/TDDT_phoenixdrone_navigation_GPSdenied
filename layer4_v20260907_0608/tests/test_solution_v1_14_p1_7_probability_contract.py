import math
from types import SimpleNamespace

from layer4.models import Candidate
from layer4.utils import probability01
from layer4.feasibility import FeasibilityEngine


def _candidate(vp, p=1.0):
    return Candidate(
        candidate_id="C", scenario_id="S", snapshot_version="v", snapshot_hash="h",
        probability=p, trajectory=[], uncertainty=[], network_trajectory=[], actions=[],
        objective_vector={}, violation_probability=vp,
        provenance={"optimizer_chance_probability": vp},
    )


def test_probability01_clamps_roundoff_above_one_and_below_zero():
    assert probability01(1.0000000000000002) == 1.0
    assert probability01(-2.220446049250313e-16) == 0.0


def test_probability01_nonfinite_fails_closed():
    assert probability01(float("nan")) == 1.0
    assert probability01(float("inf")) == 1.0


def test_candidate_enforces_closed_probability_domain():
    assert _candidate(1.0000000000000002).violation_probability == 1.0
    assert _candidate(-1e-15).violation_probability == 0.0


def test_explicit_chance_aggregation_is_closed_domain():
    # Build without invoking VTOL config because chance_probability only reads candidates.
    engine = object.__new__(FeasibilityEngine)
    c1 = _candidate(1.0000000000000002, 0.4)
    c2 = _candidate(1.0000000000000002, 0.6)
    value = engine.chance_probability([c1, c2])
    assert value == 1.0
    assert 0.0 <= value <= 1.0
