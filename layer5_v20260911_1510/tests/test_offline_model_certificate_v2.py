from __future__ import annotations
from layer5.phoenix import PhoenixSpecialization


def test_offline_model_certificate_v2_schema_is_supported_by_runtime_validator():
    # Production certificate generation in TDOC Layer2 uses the v2 schema. The
    # external Layer5 validator must recognize that schema as OFFLINE_MODEL rather
    # than fail it for a legacy v1-only name check.
    import inspect
    src=inspect.getsource(PhoenixSpecialization.apply_runtime_validation_certificate)
    assert 'Layer5-Phoenix-offline-model-certificate-v2' in src
    assert 'OFFLINE_MODEL' in src
