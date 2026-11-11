"""Compatibility shim for editable installs.

The canonical packaging metadata lives in pyproject.toml.  Keeping this
minimal setup.py improves compatibility with older tooling and makes the
project root unambiguously installable by pip.
"""
from setuptools import setup

if __name__ == "__main__":
    setup()
