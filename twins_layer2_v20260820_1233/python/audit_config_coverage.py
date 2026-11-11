from pathlib import Path
import json,sys
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'python'))
from drone_config import DroneConfig
cfg=DroneConfig(ROOT/'config/phoenix_drone_v3.yaml')
report={}
for model in ['MESA_NAV','WingMPET']:
 cat=json.loads((ROOT/'resources'/f'{model}_formula_catalog.json').read_text())['operations']
 inputs=sorted({x for op in cat.values() for x in op.get('inputs',[])})
 resolved={x:cfg.resolve(x) for x in inputs if cfg.resolve(x) is not None}
 direct=cfg.direct_parameter_paths(model)
 present=[p for p in direct if p in cfg.flat]
 report[model]={'declared_direct_parameters':len(direct),'present_in_yaml':len(present),'named_fmi_inputs_resolved':len(resolved),'resolved_inputs':resolved,'missing_yaml':[p for p in direct if p not in cfg.flat]}
 if report[model]['missing_yaml']: raise SystemExit(f'{model} missing {report[model]["missing_yaml"]}')
(ROOT/'tests'/'configuration_coverage_audit.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
print(json.dumps({k:{a:v[a] for a in ['declared_direct_parameters','present_in_yaml','named_fmi_inputs_resolved','missing_yaml']} for k,v in report.items()},indent=2,ensure_ascii=False))
