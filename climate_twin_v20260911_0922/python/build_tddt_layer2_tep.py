#!/usr/bin/env python3
"""Stable compatibility entry point for the current production MicroMet TEP builder.

This generic command intentionally delegates directly to the v3/current builder.
It must never delegate to a historical reduced-output builder because the target
path is the production TEP directory.
"""
from build_tddt_layer2_tep_v3 import main


if __name__ == "__main__":
    main()
