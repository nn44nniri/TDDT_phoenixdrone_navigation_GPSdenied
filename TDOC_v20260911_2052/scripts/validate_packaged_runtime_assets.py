#!/usr/bin/env python3
"""Fail-closed check that runtime datasets required by preflight survived packaging."""
from __future__ import annotations
import json, os, sys
from pathlib import Path

def root(name: str) -> Path:
    v=os.environ.get(name)
    return Path(v).resolve() if v else Path('__UNSET__')

checks = {
    'map_root': root('TDDT_MAP_ROOT'),
    'crp_root': root('TDDT_CRP_ROOT'),
    'sarg_root': root('TDDT_SARG_ROOT'),
    'mesa_tep_manifest': root('TDDT_TWINS_ROOT')/'resources/tddt_layer2/MESA_NAV/tep/manifest.json',
    'wingmpet_tep_manifest': root('TDDT_TWINS_ROOT')/'resources/tddt_layer2/WingMPET/tep/manifest.json',
}
climate=root('TDDT_CLIMATE_ROOT')/'climate_dataset'
central_climate=root('TDDT_CLIMATE_INTERNET_DATASETS_ROOT')
forcing_candidates=[central_climate/'tdoc_active_forcing.json', *sorted(central_climate.glob('openmeteo_*h.csv')), climate/'tdoc_active_forcing.json', *sorted(climate.glob('openmeteo_*h.csv'))]
forcing_ok=any(p.is_file() for p in forcing_candidates)
result={'schema':'TDOC-packaged-runtime-assets-v1','checks':{},'climate_forcing_candidates':[str(p) for p in forcing_candidates]}
for k,p in checks.items():
    ok=p.is_dir() if k.endswith('_root') else p.is_file()
    result['checks'][k]={'path':str(p),'ok':ok}
result['checks']['climate_forcing']={'path':str(climate),'ok':forcing_ok}
result['ok']=all(v['ok'] for v in result['checks'].values())
print(json.dumps(result,indent=2))
sys.exit(0 if result['ok'] else 2)
