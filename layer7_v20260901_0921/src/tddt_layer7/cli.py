from __future__ import annotations
import argparse, json, os, sys, importlib.util
from pathlib import Path
from .config import load_config
from .tdoc_adapter import Layer7SideModule
from .ptlib_adapter import PTLibExternalAdapter

def demo_payload():
    sem = {
      'x': {'quantity':'position','unit':'m','frame':'ENU'},
      'energy': {'quantity':'energy','unit':'J','frame':'scalar'}
    }
    return {
      'transaction_id':'demo-001','v':'7',
      'ExecutionEvidence':{'y_real':{'x':10.8,'energy':4.4},'u_cmd':{'thr':0.5},'u_applied':{'thr':0.49},
         'x_obs':{'x':10.8},'timestamp':100.0,'planned_timestamp':99.95,'version':'7','evidence_type':'field',
         'observation_id':'obs-1','input_observation_id':'obs-1',
         'decision_id':'dec-1','correlates_to_decision_id':'dec-1',
         'command_id':'cmd-1','correlates_to_actuator_command_id':'cmd-1',
         'output_semantics':sem},
      'PredictionEvidence':{'y_hat_td':{'x':10.0,'energy':4.0},'x_hat_td':{'x':10.1},'sigma_pred':{'x':0.2,'energy':0.2},
         'timestamp':100.0,'version':'7','output_semantics':sem},
      'NetworkSnapshot':{'std':{'x':10.1},'weights':{'MESA':0.34,'WingMPET':0.33,'MicroMet':0.33},
         'twin_validity':{'MESA':1.0,'WingMPET':1.0,'MicroMet':1.0},
         'twin_quality':{'MESA':1.0,'WingMPET':1.0,'MicroMet':1.0},
         'twin_freshness':{'MESA':1.0,'WingMPET':1.0,'MicroMet':1.0},
         'couplings':{'MESA->WingMPET':0.5},'topology':[['MESA','WingMPET']], 'coc_cost':0.2,'margin':0.5,
         'uncertainty':0.25,'timestamp':100.0,'version':'7','boundary_safe':True},
      'DecisionTrace':{'real_cost':1.0,'feasible_reference_cost':0.9,'timestamp':100.0,'version':'7'},
      'AdaptationHints':{'model_error':{'MESA':0.4},'source_quality':1.0,'source_validity':1.0,'source_freshness':1.0,
         'source_credibility':1.0,'maturity':0.3,'structural_error':0.2,'tep_centroids':{'MESA':[[10.0]]},
         'tep_descriptor':{'MESA':[10.1]},'tep_max_distance':{'MESA':2.0},
         'crp_id':'CRP-demo','sarg_id':'SARG-demo','crp_match_quality':1.0,'mrg_confidence':1.0,
         'mrg':{'guidance':'demo','confidence':1.0}}
    }

def preflight(config_path=None):
    cfg=load_config(config_path)
    pt=PTLibExternalAdapter()
    project_root=Path(__file__).resolve().parents[2]
    packaging_ok=(project_root/'pyproject.toml').is_file() or (project_root/'setup.py').is_file()
    return {
      'ok': (not cfg.direct_actuation_allowed) and packaging_ok,
      'tdoc_managed':cfg.tdoc_managed,'direct_actuation_allowed':cfg.direct_actuation_allowed,
      'TDDT_TDOC_ROOT':os.getenv('TDDT_TDOC_ROOT'),'TDDT_PTLIB_ROOT':os.getenv('TDDT_PTLIB_ROOT'),
      'TDDT_KSTORE_ROOT':os.getenv('TDDT_KSTORE_ROOT'),'ptlib_external_available':pt.available(),
      'ptlib_contract':str(pt.locate_contract()) if pt.locate_contract() else None,
      'project_root':str(project_root),
      'pyproject_present':(project_root/'pyproject.toml').is_file(),
      'setup_py_present':(project_root/'setup.py').is_file(),
      'pytest_available':importlib.util.find_spec('pytest') is not None,
      'console_entrypoint':'tddt-layer7',
      'note':'PT_LIB and TDOC are external dependencies; they are not vendored.'
    }

def main(argv=None):
    p=argparse.ArgumentParser(prog='tddt-layer7')
    p.add_argument('--config')
    sp=p.add_subparsers(dest='cmd',required=True)
    sp.add_parser('preflight'); sp.add_parser('demo');
    q=sp.add_parser('process'); q.add_argument('--input',required=True); q.add_argument('--output')
    o=sp.add_parser('offline'); o.add_argument('--input',required=True); o.add_argument('--output')
    sp.add_parser('manifest')
    args=p.parse_args(argv); cfg=load_config(args.config)
    if args.cmd=='preflight': out=preflight(args.config)
    elif args.cmd=='demo': out=Layer7SideModule(cfg).process(demo_payload())
    elif args.cmd=='manifest': out=Layer7SideModule.registration_descriptor()
    elif args.cmd=='offline':
        payload=json.loads(Path(args.input).read_text(encoding='utf-8')); out=Layer7SideModule(cfg).process_offline(payload)
    else:
        payload=json.loads(Path(args.input).read_text(encoding='utf-8'))
        out=Layer7SideModule(cfg).process(payload)
    text=json.dumps(out,indent=2,sort_keys=True,default=str)
    if getattr(args,'output',None): Path(args.output).write_text(text+'\n',encoding='utf-8')
    else: print(text)
    return 0

if __name__=='__main__': raise SystemExit(main())
