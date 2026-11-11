#!/usr/bin/env python3
"""Compatibility entry point; implementation is entirely in layer6.client."""
from layer6.client import demo_main, _ground_safe

__all__ = ["demo_main", "_ground_safe"]

if __name__ == "__main__":
    raise SystemExit(demo_main())
