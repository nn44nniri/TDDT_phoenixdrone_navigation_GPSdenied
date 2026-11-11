#!/usr/bin/env python3
"""Semantic records for MicroMetTwin FMI values. Non-breaking optional layer."""
from __future__ import annotations
import json, math
from pathlib import Path

def load_schema(path=None):
    if path is None:
        path=Path(__file__).resolve().parents[1]/"docs"/"fmi_semantic_schema.json"
    with open(path,"r",encoding="utf-8") as f: return json.load(f)

def annotate_values(values, timestamp, *, formula_valid=None, wind_3dw_valid=None, schema=None):
    schema=schema or load_schema()
    out={}
    fv=True if formula_valid is None else bool(round(float(formula_valid)))
    wv=True if wind_3dw_valid is None else bool(round(float(wind_3dw_valid)))
    for name,value in values.items():
        spec=schema.get("variables",{}).get(name)
        if spec is None: continue
        finite=isinstance(value,(int,float)) and math.isfinite(float(value))
        rule=spec["validity"]
        if rule=="self_describing_status": valid=finite
        elif "wind_3dw_valid" in rule: valid=finite and fv and wv
        else: valid=finite and fv
        out[name]={
            "value": value, "unit": spec["unit"], "frame": spec["frame"],
            "timestamp": timestamp, "source": spec["source"], "quality": spec["quality"],
            "uncertainty": spec["uncertainty"], "validity": bool(valid),
        }
    return out
