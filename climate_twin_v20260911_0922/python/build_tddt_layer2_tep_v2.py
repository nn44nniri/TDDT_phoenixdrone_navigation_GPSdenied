#!/usr/bin/env python3
"""Deprecated compatibility shim for the historical MicroMet TEP-v2 entry point.

Safety invariant
----------------
This file MUST NOT contain or execute the historical six-output TEP builder and
MUST NOT write an independent TEP into ``resources/tddt_layer2/tep``.  It
forwards to the current v3 builder so accidental invocation cannot downgrade
the production TEP covariance/state uncertainty contract.
"""
from __future__ import annotations

import sys
from build_tddt_layer2_tep_v3 import main as _current_main


def main() -> None:
    print(
        "DEPRECATION WARNING: build_tddt_layer2_tep_v2.py is a compatibility "
        "entry point; forwarding to build_tddt_layer2_tep_v3.py (current "
        "11-output production TEP builder).",
        file=sys.stderr,
    )
    _current_main()


if __name__ == "__main__":
    main()
