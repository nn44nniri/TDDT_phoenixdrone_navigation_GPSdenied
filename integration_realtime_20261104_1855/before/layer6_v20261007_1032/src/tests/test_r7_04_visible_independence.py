"""Guards separating live observation from the conditional forecast path."""
import pytest
from layer6.independent_validation import IndependentObservationRunner, VisibleNativeRunner


def test_independent_runner_cannot_act_as_forecast_provider():
    runner=object.__new__(IndependentObservationRunner)
    with pytest.raises(RuntimeError,match='cannot call forecast'):
        runner.forecast({})


def test_v9_visible_runner_cannot_silently_disable_map():
    runner=object.__new__(VisibleNativeRunner)
    with pytest.raises(ValueError,match='GUI and map together'):
        runner.start(show_map=False)
