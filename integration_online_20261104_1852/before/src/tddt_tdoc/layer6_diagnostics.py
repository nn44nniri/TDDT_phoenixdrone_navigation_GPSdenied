"""Non-actuating signature inspection for the R1.03 TDOC/Layer6 boundary.

The call shapes cover the production adapter and bridge. Values are placeholders;
inspect.Signature.bind never invokes a constructor or method. Passing this probe
does not validate telemetry, native prediction, safety or execution.
"""
from __future__ import annotations

import inspect


# Representative (positional labels, keyword names), from the R1.03 matrix.
# All distinct call shapes must bind, including calls omitting optional inputs.
CLIENT_CALLS = {
    "__init__": ((), ("endpoint", "gui_endpoint", "timeout", "start_if_needed", "flightstack_dir", "startup_timeout")),
    "submit": (("packet",), ("timeout",)),
    "cancel": ((), ("transaction_id", "reason")),
    "gui_status": ((), ("timeout", "include_imu_samples")),
    "telemetry_history": ((), ("after_seq", "limit", "channels", "session_id", "through_seq", "timeout")),
    "segment_status": ((), ("transaction_id", "command_instance_id", "timeout")),
    "native_session_status": ((), ()),
    "ensure_mavlink": ((), ("timeout",)),
    "wait_fcu_ready": ((), ("timeout",)),
    "build_layer7_input": (("packet",), ("status", "ack", "prediction_evidence", "network_snapshot")),
    "stack_responding": ((), ()),
    "ensure_stack_started": ((), ("timeout",)),
    "stop_stack": ((), ()),
    "close": ((), ()),
}
CLIENT_CALLS = {name: [shape] for name, shape in CLIENT_CALLS.items()}
CLIENT_CALLS["gui_status"].insert(0, ((), ("timeout",)))
CLIENT_CALLS["build_layer7_input"].insert(0, (("packet",), ("status", "ack")))


class Layer6PackageError(RuntimeError):
    def __init__(self, code, **details):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {details}")


def inspect_client_api(client_class, required_api):
    result = {"api_ok": False, "api_inspected": False, "required_api": list(required_api),
              "checked_api": list(CLIENT_CALLS), "missing_api": [], "signature_mismatches": [],
              "api_inspection_errors": [], "actual_signatures": {},
              "signature_scope": "call_shape_only; no constructor or method invoked"}
    if not inspect.isclass(client_class):
        result["missing_api"] = ["Layer6Client"]
        return result
    result["api_inspected"] = True
    for name in dict.fromkeys([*required_api, *CLIENT_CALLS]):
        try:
            method = getattr(client_class, name, None)
            if not callable(method):
                result["missing_api"].append(name)
                continue
            if name not in CLIENT_CALLS:
                result["api_inspection_errors"].append({"method": name, "error": "no declared call shape"})
                continue
            # A class signature excludes self; inspecting it does not instantiate it.
            target = client_class if name == "__init__" else method
            sig = inspect.signature(target)
            result["actual_signatures"][name] = str(sig)
            raw = inspect.getattr_static(client_class, name)
            receiver = () if name == "__init__" or isinstance(raw, (staticmethod, classmethod)) else (object(),)
            for positional, keywords in CLIENT_CALLS[name]:
                expected_call = name + "(" + ", ".join([*positional, *(key + "=..." for key in keywords)]) + ")"
                try:
                    sig.bind(*receiver, *[object() for _ in positional], **{key: object() for key in keywords})
                except TypeError as exc:
                    result["signature_mismatches"].append({"method": name, "expected_call": expected_call,
                        "expected_positional": list(positional), "expected_keywords": list(keywords),
                        "actual_signature": str(sig), "error": str(exc)})
        except Exception as exc:
            result["api_inspection_errors"].append({"method": name, "error": repr(exc)})
    result["api_ok"] = not (result["missing_api"] or result["signature_mismatches"] or result["api_inspection_errors"])
    return result
