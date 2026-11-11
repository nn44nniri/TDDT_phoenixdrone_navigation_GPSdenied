from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import numpy as np

PRIMARY_KEY = ("regime_id", "phase_id", "crp_id")
INDEX_ORDER = ("regime_id", "crp_local_index", "phase_id")


class SARGSelectionError(RuntimeError):
    """Base error for canonical SARG lookup failures."""


class SARGIndexError(SARGSelectionError):
    """Raised when the SARG lookup index is not one-to-one."""


def build_lookup_index(records: pd.DataFrame, crp_ids: Iterable[int]) -> pd.DataFrame:
    """Build the compact Regime × CRP-local × Phase edge lookup index.

    `regime_id, phase_id, crp_id` is the canonical record key.  The local CRP
    index is only an offset helper and never replaces the raw `crp_id` stored
    in the canonical record.
    """
    missing = set(PRIMARY_KEY + ("record_id",)) - set(records.columns)
    if missing:
        raise SARGIndexError(f"Missing canonical lookup columns: {sorted(missing)}")

    dup = records.duplicated(list(PRIMARY_KEY), keep=False)
    if bool(dup.any()):
        sample = records.loc[dup, list(PRIMARY_KEY) + ["record_id"]].head(10).to_dict("records")
        raise SARGIndexError(f"Duplicate canonical SARG keys detected: {sample}")

    ordered_crp = [int(v) for v in crp_ids]
    if len(ordered_crp) != len(set(ordered_crp)):
        raise SARGIndexError("CRP local-order list contains duplicate crp_id values")
    local = {cid: i for i, cid in enumerate(ordered_crp)}
    unknown = sorted(set(int(v) for v in records["crp_id"].unique()) - set(local))
    if unknown:
        raise SARGIndexError(f"Records contain crp_id values missing from CRP local order: {unknown}")

    indexed = records.reset_index(drop=True).reset_index(names="row_offset")
    indexed["crp_local_index"] = indexed["crp_id"].astype(int).map(local).astype(int)
    out = indexed[["regime_id", "crp_local_index", "phase_id", "crp_id", "record_id", "row_offset"]].copy()
    out = out.sort_values(list(INDEX_ORDER), kind="stable").reset_index(drop=True)
    return out


@dataclass
class SARGSelector:
    """Runtime selector for canonical SARG references.

    Canonical exact selector:
        select(regime_id, phase_id, crp_id)

    If the current regime has not yet been selected, callers must request the
    candidate set instead of allowing SARG to choose a regime implicitly:
        select_candidates(phase_id, crp_id, regime_ids=None)

    For convenience `select(None, phase_id, crp_id)` delegates to candidate
    selection and therefore never chooses a regime arbitrarily.
    """

    dataset_dir: str | Path

    def __post_init__(self) -> None:
        self.dataset_dir = Path(self.dataset_dir)
        records_path = self.dataset_dir / "sarg_records.csv"
        index_path = self.dataset_dir / "sarg_lookup_index.csv"
        if not records_path.is_file():
            raise FileNotFoundError(records_path)
        if not index_path.is_file():
            raise FileNotFoundError(index_path)
        self.records = pd.read_csv(records_path)
        self.index = pd.read_csv(index_path)
        self._validate_loaded_contract()
        self._by_key = {
            (int(r.regime_id), int(r.phase_id), int(r.crp_id)): int(r.row_offset)
            for r in self.index.itertuples(index=False)
        }

    def _validate_loaded_contract(self) -> None:
        missing_records = set(PRIMARY_KEY) - set(self.records.columns)
        missing_index = set(PRIMARY_KEY + ("row_offset", "crp_local_index", "record_id")) - set(self.index.columns)
        if missing_records or missing_index:
            raise SARGIndexError(
                f"Invalid lookup contract: records_missing={sorted(missing_records)}, index_missing={sorted(missing_index)}"
            )
        if bool(self.records.duplicated(list(PRIMARY_KEY), keep=False).any()):
            raise SARGIndexError("sarg_records.csv contains duplicate (regime_id, phase_id, crp_id) keys")
        if bool(self.index.duplicated(list(PRIMARY_KEY), keep=False).any()):
            raise SARGIndexError("sarg_lookup_index.csv contains duplicate canonical keys")
        if len(self.index) != len(self.records):
            raise SARGIndexError("Lookup index cardinality does not match sarg_records.csv")
        if self.index.empty:
            raise SARGIndexError("Lookup index is empty")
        # Verify actual offsets, IDs and local-cluster mapping; distinct offsets
        # alone do NOT establish that the index points at the right records.
        for label, frame, fields in (
            ("record", self.records, (*PRIMARY_KEY, "record_id")),
            ("index", self.index, (*PRIMARY_KEY, "record_id", "row_offset", "crp_local_index")),
        ):
            for key in fields:
                if key not in frame.columns:
                    raise SARGIndexError(f"{label}:{key}:missing")
                num = pd.to_numeric(frame[key], errors="coerce")
                if (num.isna().any() or not np.isfinite(num.to_numpy(dtype=float)).all()
                        or (num.to_numpy(dtype=float) != np.floor(num.to_numpy(dtype=float))).any()):
                    raise SARGIndexError(f"{label}:{key}:invalid_integer")
        offs = self.index["row_offset"].to_numpy(dtype=int)
        if len(set(offs)) != len(offs) or min(offs) < 0 or max(offs) >= len(self.records):
            raise SARGIndexError("Lookup index row_offset values are invalid")
        targets = self.records.iloc[offs].reset_index(drop=True)
        for field in (*PRIMARY_KEY, "record_id"):
            actual = targets[field].to_numpy(dtype=np.int64)
            declared = self.index[field].to_numpy(dtype=np.int64)
            if not np.array_equal(actual, declared):
                raise SARGIndexError(f"Lookup index {field} does not match record at row_offset")
        crp_local = self.index[["crp_id", "crp_local_index"]].drop_duplicates()
        if crp_local["crp_id"].duplicated().any() or crp_local["crp_local_index"].duplicated().any():
            raise SARGIndexError("Lookup index CRP local mapping is not one-to-one")
        indexes = sorted(crp_local["crp_local_index"].astype(int).tolist())
        if indexes != list(range(len(indexes))):
            raise SARGIndexError("Lookup index CRP local indices are not contiguous")

    def select(self, regime_id: int | None, phase_id: int, crp_id: int):
        """Return one exact record, or all candidates when regime_id is None."""
        if regime_id is None:
            return self.select_candidates(phase_id=phase_id, crp_id=crp_id)
        key = (int(regime_id), int(phase_id), int(crp_id))
        if key not in self._by_key:
            raise KeyError(f"No canonical SARG record for key {key}")
        return self.records.iloc[self._by_key[key]].to_dict()

    def select_candidates(self, phase_id: int, crp_id: int, regime_ids: Iterable[int] | None = None) -> list[dict]:
        """Return all valid regime candidates for a Phase/Stage × CRP context."""
        frame = self.records[
            (self.records["phase_id"].astype(int) == int(phase_id))
            & (self.records["crp_id"].astype(int) == int(crp_id))
        ]
        if regime_ids is not None:
            wanted = {int(v) for v in regime_ids}
            frame = frame[frame["regime_id"].astype(int).isin(wanted)]
        frame = frame.sort_values("regime_id", kind="stable")
        return frame.to_dict("records")

    def select_reference(self, regime_id: int, phase_id: int, crp_id: int) -> dict:
        """Exact, diagnostic-only SARG admission; never a flight/FINAL authority.

        A reference is eligible only if the FMI-produced record explicitly
        reports VALID/VALID_LOW_CONF and both Wing and MESA are OK. An exact
        invalid key is returned as *ineligible*, without a cross-regime retry.
        """
        if regime_id is None:
            raise SARGSelectionError("explicit_regime_required_no_fallback")
        record = self.select(regime_id, phase_id, crp_id)
        needed = ("record_status", "phase_used", "wing_status", "mesa_status", "flight_ok", "nav_ok")
        missing = [k for k in needed if k not in record or pd.isna(record[k])]
        if missing:
            raise SARGSelectionError(f"admission_evidence_missing:{','.join(missing)}")
        blockers = []
        def n(key):
            v = record[key]
            x = float(v)
            if not np.isfinite(x) or x != int(x):
                raise SARGSelectionError(f"admission_invalid_status:{key}")
            return int(x)
        status = n("record_status")
        if status not in (0, 1): blockers.append(f"record_status:{status}")
        if n("phase_used") != 1: blockers.append("phase_not_triggered")
        if n("wing_status") != 0: blockers.append("wing_not_ok")
        if n("mesa_status") != 0: blockers.append("mesa_not_ok")
        if n("flight_ok") != 1: blockers.append("flight_not_ok")
        if n("nav_ok") != 1: blockers.append("navigation_not_ok")
        if "record_confidence" not in record or pd.isna(record["record_confidence"]):
            blockers.append("record_confidence_missing")
        else:
            conf = float(record["record_confidence"])
            if not np.isfinite(conf) or not (0.0 <= conf <= 1.0):
                blockers.append("record_confidence_invalid")
        return {
            "schema": "TDDT-SARG-ReferenceAdmission-v1",
            "canonical_key": [int(regime_id), int(phase_id), int(crp_id)],
            "record_id": int(record["record_id"]),
            "record_status": status,
            "reference_admissible": not blockers,
            "reference_mode": "admissible_prior_only" if not blockers else "diagnostic_only",
            "blockers": blockers,
            "no_implicit_cross_regime_fallback": True,
            "current_navigation_state": False,
            "native_controller_certified": False,
            "flight_authorized": False,
            "source": "exact_sarg_dataset_record_not_observation",
        }
