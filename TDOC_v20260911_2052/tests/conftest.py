from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"src"
if str(SRC) not in sys.path: sys.path.insert(0,str(SRC))


import pytest

@pytest.fixture(autouse=True)
def isolated_mutable_knowledge_roots(tmp_path, monkeypatch):
    """Do not let test adapters consume or replace a sourced production KStore.

    Source libraries and explicit evidence/report fixtures remain visible. A test
    needing a special knowledge fixture can override these temporary roots itself.
    """
    for key, rel in {
        "TDDT_LAYER5_KSTORE_ROOT": "layer5/kstore",
        "TDDT_KSTORE_ROOT": "layer5/kstore",
        "TDDT_LAYER7_KSTORE_BASE": "layer7/kstore",
        "TDDT_LAYER7_STATE_ROOT": "layer7/state",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / "isolated_runtime" / rel))

@pytest.fixture(autouse=True)
def isolated_dynamic_import_paths():
    """Adapters add exact-root import paths; a fake test root must not leak.

    Keep production exact-origin rejection enabled and restore only process-local
    import search paths, not any source or runtime evidence.
    """
    previous = list(sys.path)
    try:
        yield
    finally:
        sys.path[:] = previous
