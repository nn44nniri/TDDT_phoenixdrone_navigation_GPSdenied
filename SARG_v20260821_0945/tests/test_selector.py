from pathlib import Path

import pandas as pd
import pytest

from sarg_builder.selector import PRIMARY_KEY, SARGIndexError, SARGSelector, build_lookup_index


def _records():
    rows=[]
    rid=1
    for regime in (1,2,3):
        for crp in (7,11):
            for phase in (1,2):
                rows.append({"record_id":rid,"scenario_id":regime,"regime_id":regime,"phase_id":phase,"crp_id":crp,"record_status":0})
                rid += 1
    return pd.DataFrame(rows)


def test_lookup_index_uses_regime_phase_crp_primary_key():
    records=_records()
    idx=build_lookup_index(records,[7,11])
    assert tuple(PRIMARY_KEY) == ("regime_id","phase_id","crp_id")
    assert len(idx)==len(records)
    assert not idx.duplicated(list(PRIMARY_KEY)).any()
    assert list(idx.columns)==["regime_id","crp_local_index","phase_id","crp_id","record_id","row_offset"]
    assert idx[idx.crp_id==7].crp_local_index.unique().tolist()==[0]
    assert idx[idx.crp_id==11].crp_local_index.unique().tolist()==[1]


def test_duplicate_canonical_key_is_rejected():
    records=_records()
    records=pd.concat([records,records.iloc[[0]]],ignore_index=True)
    with pytest.raises(SARGIndexError,match="Duplicate canonical SARG keys"):
        build_lookup_index(records,[7,11])


def test_runtime_selector_exact_and_candidate_mode(tmp_path: Path):
    records=_records().reset_index(drop=True)
    idx=build_lookup_index(records,[7,11])
    records.to_csv(tmp_path/"sarg_records.csv",index=False)
    idx.to_csv(tmp_path/"sarg_lookup_index.csv",index=False)
    selector=SARGSelector(tmp_path)

    exact=selector.select(2,1,7)
    assert exact["regime_id"]==2 and exact["phase_id"]==1 and exact["crp_id"]==7

    candidates=selector.select(None,1,7)
    assert [int(r["regime_id"]) for r in candidates]==[1,2,3]
    subset=selector.select_candidates(1,7,[1,3])
    assert [int(r["regime_id"]) for r in subset]==[1,3]


def test_selector_never_infers_regime(tmp_path: Path):
    records=_records().reset_index(drop=True)
    build_lookup_index(records,[7,11]).to_csv(tmp_path/"sarg_lookup_index.csv",index=False)
    records.to_csv(tmp_path/"sarg_records.csv",index=False)
    selector=SARGSelector(tmp_path)
    candidates=selector.select(None,2,11)
    assert len(candidates)==3
    assert {int(r["regime_id"]) for r in candidates}=={1,2,3}
