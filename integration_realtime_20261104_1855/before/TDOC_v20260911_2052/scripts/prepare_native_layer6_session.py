#!/usr/bin/env python3
"""Prepare native start/forcing binding from preserved Offline knowledge; no flight."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.native_environment import prepare_binding

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",default="configs/tdoc.yaml")
    p.add_argument("--offline-knowledge-manifest")
    p.add_argument('--plant-epoch-s',type=float,default=0.,help='Declared plant-time anchor in seconds; not proof of runtime synchronization')
    p.add_argument('--output',help='Optional isolated binding output; default is the native runtime binding path')
    a=p.parse_args()
    result=prepare_binding(TDOCConfig.from_yaml(a.config),manifest_path=a.offline_knowledge_manifest,
        plant_epoch_s=a.plant_epoch_s,output_path=a.output)
    print(json.dumps({"ok":True,"binding":result,"flight_started":False,"knowledge_modified":False},indent=2))

if __name__=="__main__": main()
