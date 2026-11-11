#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED_FIELDS={'value','unit','frame','timestamp','source','quality','uncertainty','validity'}

def audit(path: Path):
    with zipfile.ZipFile(path) as z:
        root=ET.fromstring(z.read('modelDescription.xml'))
        schema=json.loads(z.read('resources/signal_schema.json'))
    vars_=root.find('ModelVariables').findall('ScalarVariable')
    missing_units=[]; missing_schema=[]; bad_schema=[]
    defined_units={u.attrib['name'] for u in root.find('UnitDefinitions').findall('Unit')}
    unit_use={}
    for sv in vars_:
        child=next(iter(sv),None); name=sv.attrib['name']
        unit=child.attrib.get('unit') if child is not None else None
        if not unit: missing_units.append(name)
        else: unit_use[unit]=unit_use.get(unit,0)+1
        if unit and unit not in defined_units: bad_schema.append({'name':name,'reason':f'undefined unit {unit}'})
        meta=schema.get('signals',{}).get(name)
        if meta is None: missing_schema.append(name); continue
        miss=REQUIRED_FIELDS-set(meta)
        if miss: bad_schema.append({'name':name,'reason':f'missing fields {sorted(miss)}'})
        if unit and meta.get('unit') != unit:
            bad_schema.append({'name':name,'reason':f'unit mismatch xml={unit} schema={meta.get("unit")}'})
    return {
      'fmu':path.name,'variables':len(vars_),'variables_with_unit':len(vars_)-len(missing_units),
      'unit_coverage_percent':100*(len(vars_)-len(missing_units))/max(1,len(vars_)),
      'defined_units':sorted(defined_units),'unit_usage':dict(sorted(unit_use.items())),
      'schema_signals':len(schema.get('signals',{})),'schema_fields':schema.get('fields',[]),
      'missing_units':missing_units,'missing_schema_entries':missing_schema,'schema_errors':bad_schema,
      'passed':not missing_units and not missing_schema and not bad_schema and REQUIRED_FIELDS.issubset(schema.get('fields',[]))
    }

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default=Path(__file__).resolve().parents[1]); ap.add_argument('--output')
    a=ap.parse_args(); root=Path(a.root)
    result={m:audit(root/'fmus'/f'{m}.fmu') for m in ['MESA_NAV','WingMPET']}
    result['passed']=all(v['passed'] for k,v in result.items() if isinstance(v,dict))
    text=json.dumps(result,indent=2,ensure_ascii=False)
    if a.output: Path(a.output).write_text(text)
    print(text)
    raise SystemExit(0 if result['passed'] else 1)
if __name__=='__main__': main()
