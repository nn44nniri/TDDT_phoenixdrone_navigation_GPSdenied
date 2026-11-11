from __future__ import annotations
import argparse, csv, json, math, sys
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from formula_fmi import FormulaFMU
from drone_config import DroneConfig, DEFAULT_CONFIG


def sample_value(name: str, index: int = 1) -> float:
    n=name.lower()
    # Safe, deterministic engineering test values. Specific rules precede generic ones.
    if n in {"dt","flow_dt"}: return 0.02
    if "timestamp" in n: return 10.0
    if "event_age" in n: return 0.05
    if "late_max" in n: return 0.2
    if "future_tolerance" in n: return 0.05
    if "altitude" in n or n in {"state_h","h","goal_h","target_h"}: return 1000.0 if "terrain" not in n else 900.0
    if "terrain_height" in n: return 900.0
    if "agl" in n: return 100.0
    if "temperature" in n: return 293.15
    if "absolute_pressure" in n or n=="external_pressure": return 90000.0
    if "log_pressure_prior" in n: return math.log(90000.0)
    if "log_scale_height" in n: return math.log(8400.0)
    if "pressure_time_scale" in n: return 30.0
    if "pressure" in n and "variance" not in n and "grad" not in n: return 500.0
    if "air_density" in n or "external_density" in n: return 1.1
    if "relative_humidity" in n: return 0.45
    if "airspeed" in n and "variance" not in n: return 25.0
    if "stall_speed" in n or "min_airspeed" in n: return 12.0
    if "max_airspeed" in n: return 45.0
    if "groundspeed" in n: return 24.0
    if "mass" in n: return 5.0 if "fuel" not in n else 1.2
    if "dry_mass" in n: return 4.0
    if "wing_area" in n: return 0.8
    if n=="cd0": return 0.03
    if "induced_drag_factor" in n: return 0.06
    if "cl_max" in n or "max_cl" in n: return 1.4
    if n=="cl": return 0.6
    if "bank_angle" in n: return 0.15
    if "max_bank" in n: return 0.7
    if "flightpath_angle" in n: return 0.03
    if "min_flightpath" in n: return -0.35
    if "max_flightpath" in n: return 0.35
    if "load_factor" in n: return 1.05
    if "max_load_factor" in n: return 2.5
    if "sideslip" in n and "max_" not in n: return 0.02
    if "max_sideslip" in n: return 0.2
    if "velocity_x" in n: return 24.0
    if "velocity_y" in n: return 2.0
    if "velocity_h" in n: return 0.5
    if "wind_x" in n or "wind_n" in n: return 4.0
    if "wind_y" in n or "wind_e" in n: return 1.5
    if "wind_h" in n or "wind_d" in n: return 0.2
    if "flow_bx" in n: return -20.0
    if "flow_by" in n: return 1.0
    if "flow_bz" in n: return 0.3
    if "mag_bx" in n or "map_mag_n" in n: return 20.0
    if "mag_by" in n or "map_mag_e" in n: return 2.0
    if "mag_bz" in n or "map_mag_d" in n: return 42.0
    if "variance" in n: return 1.0
    if "nis_gate" in n: return 6.0
    if "student_nu" in n: return 4.0
    if "residual" in n: return 0.2
    if "jacobian_n" in n: return 1.0
    if "jacobian_e" in n: return 0.5
    if "information" in n or n.startswith("info_"): return 2.0 if n.endswith("11") else (1.5 if n.endswith("22") else 0.1)
    if "condition_score" in n: return 0.25
    if "parameter_index" in n: return 1.0
    if "basis_value" in n: return 1.2
    if n=="measurement": return 2.4
    if "forgetting_factor" in n: return 0.99
    if "time_constant" in n or "filter_tau" in n: return 10.0
    if "descriptor_scale" in n: return 1.0
    if "acceptance_threshold" in n: return 0.5
    if "store_descriptor" in n: return 1.0
    if "descriptor_" in n: return 0.05*index
    if "relative_displacement_n" in n: return 5.0
    if "relative_displacement_e" in n: return 2.0
    if "candidate_count" in n: return 3.0
    if "candidate_" in n and "info11" in n: return 2.0+0.1*index
    if "candidate_" in n and "info22" in n: return 1.5+0.08*index
    if "candidate_" in n and "info12" in n: return 0.1
    if n in {"x","state_x"}: return 0.0
    if n in {"y","state_y"}: return 0.0
    if "goal_x" in n or "target_x" in n: return 1000.0
    if "goal_y" in n or "target_y" in n: return 200.0
    if "goal_tolerance" in n: return 10.0
    if "initial_goal_distance" in n: return 1020.0
    if "propeller_diameter" in n: return 0.45
    if "rotation_hz" in n: return 90.0
    if n.startswith("ct0"): return 0.11
    if n.startswith("ct1"): return -0.08
    if n.startswith("ct2"): return -0.02
    if n.startswith("cp0"): return 0.05
    if n.startswith("cp1"): return -0.03
    if n.startswith("cp2"): return 0.01
    if "efficiency" in n: return 0.85
    if "battery_ocv" in n: return 22.2
    if "battery_resistance" in n: return 0.08
    if "inverter_resistance" in n: return 0.01
    if "battery_capacity_ah" in n: return 10.0
    if n=="soc": return 0.8
    if "auxiliary_power" in n: return 15.0
    if "switch_frequency" in n: return 20000.0
    if "switch_time" in n: return 1e-7
    if "motor_resistance" in n: return 0.05
    if "motor_torque_constant" in n: return 0.08
    if "motor_thermal_capacity" in n: return 500.0
    if "cooling_coefficient" in n: return 5.0
    if "peukert_exponent" in n: return 1.05
    if "sea_level_power" in n: return 2500.0
    if "density_ratio" in n: return 0.9
    if "bsfc" in n: return 2.5e-7
    if "throttle" in n: return 0.65
    if "power_lapse_exponent" in n: return 1.0
    if n=="thrust": return 25.0
    if n=="drag": return 12.0
    if n=="lift" or "lift_command" in n: return 49.0
    if "clearance" in n: return 50.0
    if "obstacle_distance" in n: return 100.0
    if "safe_radius" in n: return 20.0
    if "sigma" in n: return 2.0
    if "inflation_factor" in n: return 3.0
    if "path_length" in n: return 1000.0
    if "time_prev" in n: return 40.0
    if "energy_prev" in n: return 50000.0
    if "fuel_prev" in n: return 0.05
    if n=="power": return 600.0
    if "fuel_rate" in n: return 1e-4
    if "reference" in n: return 1000.0
    if "weight_" in n: return 1.0
    if "safety_penalty" in n: return 0.0
    if "minimum_length" in n: return 950.0
    if "epsilon_length" in n: return 100.0
    if "mission_time" in n: return 45.0
    if n=="energy": return 60000.0
    if "_min" in n: return -1.0
    if "_max" in n: return 1.0
    if n.startswith("control_"): return 0.1*index
    if "sample_count" in n: return 10.0
    if "segment_margin" in n: return 30.0-index*0.2
    if "replan_margin_threshold" in n: return 10.0
    if "error_threshold" in n: return 5.0
    if "wind_model_error" in n or "state_error" in n: return 1.0
    if "margin" in n: return 1.0
    if "valid" in n: return 1.0
    return 0.1*index


def run_all(model: str, config_path=DEFAULT_CONFIG):
    cfg=DroneConfig(config_path)
    fmu_path=ROOT/'fmus'/f'{model}.fmu'
    rows=[]; op_summary=[]
    with FormulaFMU(fmu_path) as fmu:
        for op in sorted(fmu.operations):
            sig=fmu.operation_signature(op)
            inputs=cfg.overlay(sig['inputs'], sample_value)
            outputs=fmu.evaluate_operation(op, **inputs)
            if not outputs:
                raise RuntimeError(f'Operation {op} has no FMI outputs; formula interface is incomplete')
            finite=sum(math.isfinite(float(v)) for v in outputs.values())
            for name,value in outputs.items():
                rows.append({'operation':op,'output':name,'value':float(value),'finite':math.isfinite(float(value))})
            op_summary.append({'operation':op,'input_count':len(inputs),'output_count':len(outputs),
                               'finite_count':finite,'all_finite':finite==len(outputs),
                               'inputs':inputs,'outputs':outputs})
    return rows,op_summary


def save_results(model, rows, summary, graph_path):
    outdir=ROOT/'tests'; outdir.mkdir(exist_ok=True)
    with open(outdir/f'{model}_all_formula_results.csv','w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['operation','output','value','finite']); w.writeheader(); w.writerows(rows)
    (outdir/f'{model}_all_formula_results.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    values=np.array([r['value'] for r in rows],dtype=float)
    finite=np.isfinite(values)
    transformed=np.sign(np.nan_to_num(values))*np.log10(1+np.abs(np.nan_to_num(values)))
    ops=np.array([r['operation'] for r in rows])
    fig=plt.figure(figsize=(16,9))
    ax1=fig.add_axes([0.07,0.55,0.89,0.36])
    ax1.plot(np.arange(len(rows)),transformed,marker='.',linestyle='none',markersize=4)
    ax1.axhline(0,linewidth=0.8)
    ax1.set_title(f'{model}: all FMI formula outputs (signed log10(1+|value|))')
    ax1.set_xlabel('Formula-output sequence'); ax1.set_ylabel('Signed log magnitude'); ax1.grid(True,alpha=.25)
    ax2=fig.add_axes([0.07,0.10,0.89,0.32])
    unique=sorted(set(ops.tolist()))
    rates=[]; counts=[]
    for op in unique:
        mask=ops==op; counts.append(int(mask.sum())); rates.append(float(finite[mask].mean()*100) if mask.any() else 100.0)
    ax2.bar(unique,rates)
    for x,y,c in zip(unique,rates,counts): ax2.text(x,y+1,f'{c}',ha='center',va='bottom',fontsize=7)
    ax2.set_ylim(0,108); ax2.set_xticks(unique)
    ax2.set_title('Finite-output validation by operation (labels show output count)')
    ax2.set_xlabel('operation_code'); ax2.set_ylabel('Finite outputs (%)'); ax2.grid(True,axis='y',alpha=.25)
    fig.text(.5,.015,f'Total outputs: {len(rows)} | finite: {int(finite.sum())} | non-finite: {int((~finite).sum())}',ha='center')
    fig.savefig(graph_path,dpi=180,bbox_inches='tight'); plt.close(fig)
    if not finite.all():
        bad=[rows[i] for i in np.where(~finite)[0]]
        raise RuntimeError(f'Non-finite outputs: {bad[:10]}')


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args=parser.parse_args()
    rows,summary=run_all("WingMPET", args.config)
    graph=ROOT/"graphs"/"WingMPET_all_formula_validation.png"
    graph.parent.mkdir(exist_ok=True)
    save_results("WingMPET",rows,summary,graph)
    op19 = next(s for s in summary if s['operation'] == 19)
    print("WingMPET: operations=",len(summary),"outputs=",len(rows),"all_finite=",all(r["finite"] for r in rows))
    print("operation_19_outputs=", op19['output_count'], "operation_19_all_finite=", op19['all_finite'])
    print("graph=",graph)
