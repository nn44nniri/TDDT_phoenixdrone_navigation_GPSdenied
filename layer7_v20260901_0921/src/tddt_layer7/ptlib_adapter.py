from __future__ import annotations
from pathlib import Path
import os


class PTLibExternalAdapter:
    """Non-vendoring PT_LIB boundary with explicit correlation validation."""
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv('TDDT_PTLIB_ROOT', '')) if (root or os.getenv('TDDT_PTLIB_ROOT')) else None

    def available(self) -> bool:
        return bool(self.root and self.root.exists())

    def locate_contract(self) -> Path | None:
        if not self.available(): return None
        for rel in ['resources/data_contract_v1.json', 'docs/Data_Contract_v1.md']:
            p = self.root / rel
            if p.exists(): return p
        return None

    @staticmethod
    def normalize_record(record: dict) -> dict:
        keys = ['observation_id','decision_id','command_id','input_observation_id',
                'input_environment_truth_id','correlates_to_decision_id',
                'correlates_to_actuator_command_id','actuator_feedback_id',
                'host_time_utc','source','truth']
        return {k: record.get(k) for k in keys if k in record}

    @staticmethod
    def validate_correlation_chain(normalized: dict) -> tuple[bool, list[str]]:
        reasons = []
        pairs = [('observation_id','input_observation_id'), ('decision_id','correlates_to_decision_id'),
                 ('command_id','correlates_to_actuator_command_id')]
        for a, b in pairs:
            av, bv = normalized.get(a), normalized.get(b)
            if not isinstance(av, str) or not av or not isinstance(bv, str) or not bv:
                reasons.append(f'missing:{a}/{b}')
            elif av != bv:
                reasons.append(f'mismatch:{a}/{b}')
        return not reasons, reasons
