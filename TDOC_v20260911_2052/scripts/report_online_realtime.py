#!/usr/bin/env python3
"""Generate reports from existing Native evidence; never start or control SITL."""
import argparse
from pathlib import Path
from tddt_tdoc.config import TDOCConfig
from online_reports.generate import generate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-dir', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--evidence-dir', type=Path)
    p.add_argument('--config', default=str(Path(__file__).resolve().parents[1] / 'configs/tdoc.yaml'))
    a = p.parse_args()
    result = generate(a.source_dir, a.output_dir, evidence=a.evidence_dir,
                      config=TDOCConfig.from_yaml(Path(a.config).resolve()))
    print('Reports:', a.output_dir, 'qualification:', result['qualification']['status'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
