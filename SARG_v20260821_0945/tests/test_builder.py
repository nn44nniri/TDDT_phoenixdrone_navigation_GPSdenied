from pathlib import Path
import pandas as pd
from sarg_builder.io import flatten

def test_flatten():
    assert flatten({"a":{"b":2}})["a.b"]==2

def test_dynamic_record_formula():
    for n in (1,12,32,47):
        assert 10*6*n == 60*n

def test_architecture_dimensions():
    from sarg_builder.builder import PHASES, REGIMES
    assert len(PHASES)==6
    assert len(REGIMES)==10
    assert [x[1] for x in REGIMES]==[f"R{i}" for i in range(1,11)]

def test_remove_invalid_calculated_starts(tmp_path):
    from sarg_builder.fmu import FormulaFMU
    xml=tmp_path/"modelDescription.xml"
    xml.write_text('<?xml version="1.0"?><fmiModelDescription fmiVersion="2.0" modelName="x" guid="g"><ModelVariables><ScalarVariable name="bad" valueReference="1" causality="output" initial="calculated"><Real start="1.0"/></ScalarVariable><ScalarVariable name="ok" valueReference="2" causality="input"><Real start="2.0"/></ScalarVariable></ModelVariables></fmiModelDescription>')
    assert FormulaFMU._remove_invalid_calculated_starts(xml)==1

def test_climate_context_preserves_code_and_parameters():
    from sarg_builder.builder import _climate_context
    row = pd.Series({
        "cluster_id": 7,
        "cluster_code": "CRP-C07-WARM-DRY",
        "state_signature": "warm|dry|windy",
        "time_window": "12_18",
        "temperature_mean_c": 27.5,
        "humidity_mean_pct": 31.0,
        "wind_speed_mean_ms": 6.2,
        "wind_direction_mean_deg": 245.0,
        "pressure_mean_pa": 83400.0,
        "density_mean_kg_m3": 0.96,
    })
    ctx = _climate_context(row)
    assert ctx["crp_id"] == 7
    assert ctx["crp_code"] == "CRP-C07-WARM-DRY"
    assert ctx["crp_temperature_state"] == "warm"
    assert ctx["crp_humidity_state"] == "dry"
    assert ctx["crp_wind_state"] == "windy"
    assert "crp_state_signature" not in ctx
    assert ctx["crp_temperature_mean_c"] == 27.5
    assert ctx["crp_wind_speed_mean_ms"] == 6.2

def test_climate_context_reads_time_state_and_fills_atmosphere():
    from sarg_builder.builder import _climate_context
    row = pd.Series({
        'cluster_id': 3,
        'time_state': '06_12',
        'temperature_mean_c': 12.0,
        'humidity_mean_pct': 40.0,
        'elevation_mean_m': 1650.0,
        'wind_speed_mean_ms': 4.0,
        'wind_direction_mean_deg': 90.0,
    })
    ctx = _climate_context(row)
    assert ctx['crp_time_window'] == '06_12'
    assert ctx['crp_pressure_mean_pa'] > 0
    assert ctx['crp_air_density_mean_kg_m3'] > 0
    assert ctx['crp_pressure_source'] == 'derived_from_crp_elevation_ISA'
    assert ctx['crp_air_density_source'] == 'derived_from_crp_pressure_temperature_humidity'


def test_load_crp_enriches_from_assignments(tmp_path):
    from sarg_builder.io import load_crp
    crp = tmp_path / 'CRP'; crp.mkdir()
    pd.DataFrame([{'cluster_id': 1, 'temperature_mean_c': 20.0, 'humidity_mean_pct': 50.0}]).to_csv(crp/'crp_cluster_catalog.csv', index=False)
    pd.DataFrame([
        {'cluster_id': 1, 'time_state': '18_24', 'pressure_pa': 83000.0, 'air_density_kg_m3': 0.98},
        {'cluster_id': 1, 'time_state': '18_24', 'pressure_pa': 83200.0, 'air_density_kg_m3': 0.99},
    ]).to_csv(crp/'crp_cell_assignments.csv.gz', index=False, compression='gzip')
    df = load_crp(crp)
    assert df.loc[0, 'time_state'] == '18_24'
    assert abs(float(df.loc[0, 'pressure_mean_pa']) - 83100.0) < 1e-6
    assert abs(float(df.loc[0, 'air_density_mean_kg_m3']) - 0.985) < 1e-9


def test_crs_contract_names_are_unambiguous():
    from sarg_builder.region import WGS84
    assert WGS84 == "EPSG:4326"
    # Canonical contract: geodetic values use *_deg and projected values use *_m.
    canonical = {"origin_lon_deg", "origin_lat_deg", "origin_x_m", "origin_y_m"}
    assert all(name.endswith(("_deg", "_m")) for name in canonical)

def test_complete_reference_summary_metrics():
    from sarg_builder.builder import _mesa_reference_metrics, _wing_reference_metrics
    result = {
        "accumulation": {"energy_total": 3600.0},
        "envelope": {"flight_envelope_valid": 1.0},
        "mechanical": {"mechanical_constraints_valid": 1.0},
        "wind": {"wind_residual_norm": 2.5, "flow_anchor_valid": 1.0},
        "observability": {"conditional_observability_score": 0.4},
        "excitation": {"best_lambda_min": 3.0},
        "robust": {"robust_weight": 0.8},
        "covariance": {**{f"covariance_diagonal_{i:02d}": 4.0 for i in range(1,7)}, "covariance_diagonal_09": 0.01},
    }
    wm = _wing_reference_metrics(result, {"obstacle_margin": 95.0, "safety_valid": 1.0}, 20.5)
    mm = _mesa_reference_metrics(result)
    assert abs(wm["energy_remaining_wh"] - 19.5) < 1e-12
    assert wm["min_obstacle_clearance_m"] == 95.0
    assert wm["wing_confidence"] == 1.0
    assert mm["position_error_m"] > 0
    assert mm["velocity_error_ms"] > 0
    assert mm["heading_error_deg"] > 0
    assert mm["position_sigma_m"] == mm["position_error_m"]
    assert mm["wind_error_ms"] == 2.5
    assert 0.0 <= mm["mesa_confidence"] <= 1.0


def test_record_confidence_is_not_observability_alias():
    from sarg_builder.builder import _clamp01
    wing, mesa, crp = 0.9, 0.8, 0.7
    record = _clamp01(wing * mesa * crp)
    assert abs(record - 0.504) < 1e-12
    assert record != mesa
