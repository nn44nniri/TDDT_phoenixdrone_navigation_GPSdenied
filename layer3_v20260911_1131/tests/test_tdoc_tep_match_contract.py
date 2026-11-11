from types import SimpleNamespace

from layer3.runtime import Layer3FMURuntime


def test_unmatched_tep_distance_is_json_null_not_infinity():
    record = Layer3FMURuntime._tep_match_record(
        SimpleNamespace(matched=False, distance=float("inf"), novel=True)
    )
    assert record == {"matched": False, "distance": None, "novel": True}


def test_finite_tep_distance_is_preserved():
    record = Layer3FMURuntime._tep_match_record(
        SimpleNamespace(matched=True, distance=1.25, novel=False)
    )
    assert record["distance"] == 1.25
