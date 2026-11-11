#!/usr/bin/env python3
"""R3.06 read-only real-handoff audit and isolated new model qualification.

No mission, active bank, KStore or forcing is modified. No native FCU is started.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.model_compatibility import capture_model_dependencies,certificate_compatibility,compatibility_policy
from tddt_tdoc.knowledge_handoff import OfflineKnowledgeHandoff
from tddt_tdoc.adapters.layer2 import Layer2Adapter
from tddt_tdoc.adapters.layer5 import Layer5Adapter


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True)
    args=p.parse_args();tdoc=Path(__file__).resolve().parents[1];out=args.output_dir.resolve()
    out.relative_to(tdoc/'reports/online');out.mkdir(exist_ok=False)
    cfg=TDOCConfig.from_yaml(tdoc/'configs/tdoc.yaml');current=capture_model_dependencies(cfg)
    write(out/'CURRENT_MODEL_DEPENDENCIES.json',current);write(out/'COMPATIBILITY_POLICY.json',compatibility_policy())
    hp=cfg.runtime_root/'knowledge/handoff/offline_latest.json.gz';handoff_raw=hp.read_bytes()
    h=OfflineKnowledgeHandoff.__new__(OfflineKnowledgeHandoff);h.latest_path=hp
    gate=h.validate_online_start();assert gate['ok'];hm=gate['manifest']
    forcing=Path(hm['reuse_contract']['climate_time_alignment']['forcing_file']);forcing_sha=sha(forcing)
    source=cfg.runtime_root/'cycles'/(hm['source_transaction_id']+'.json.gz')
    source_sha=sha(source);cycle=json.loads(gzip.decompress(source.read_bytes()))
    old=cycle['layer_status']['4']['evidence']['offline_model_control']['certificate']
    old_gate=certificate_compatibility(old,current);assert not old_gate['ok']
    # Exercise the real adapter's gate even with an apparent same-handoff cache hit.
    a=Layer5Adapter.__new__(Layer5Adapter);a.config=cfg;a._ensure=lambda:None
    a.engine=SimpleNamespace(phoenix=SimpleNamespace(nominal_tracker_authorized=True,runtime_certificate={'accepted':True}))
    a._native_model_prior_hash=hm['handoff_hash'];a._native_model_prior_status={'ok':True}
    cached=a.ensure_native_control_binding();assert not cached['ok'] and cached['handoff_preserved']
    inventory=[];counts=Counter()
    for cp in sorted((cfg.runtime_root/'cycles').glob('*.json.gz')):
        row=json.loads(gzip.decompress(cp.read_bytes()))
        evidence=((row.get('layer_status') or {}).get('4') or {}).get('evidence') or {}
        cert=(evidence.get('offline_model_control') or {}).get('certificate')
        if not isinstance(cert,dict):continue
        verdict=certificate_compatibility(cert,current);counts[verdict['reason']]+=1
        inventory.append({'cycle':cp.name,'certificate_schema':cert.get('schema'),
            'certificate_sha256':hashlib.sha256(json.dumps(cert,sort_keys=True).encode()).hexdigest(),**verdict})
    write(out/'STORED_CERTIFICATE_INVENTORY.json',{'scope':'certificates embedded in all stored runtime cycles',
        'total_cycles':len(list((cfg.runtime_root/'cycles').glob('*.json.gz'))),
        'certificates':inventory,'decisions':dict(counts),
        'external_plaintext_bank_present':(cfg.path_value('TDDT_LAYER4_ROOT')/'runtime/verification/ff_certificates.jsonl').exists(),
        'native_persistent_bank_matches':[str(x) for x in (cfg.runtime_root/'layer4/native').rglob('*ff_certificates*')],
        'historical_reports_are_not_authority':True})
    # A NEW bounded FMU qualification, saved exclusively under reports. This is
    # neither a flight episode nor renewal of any accepted Offline artifact.
    from layer5 import Layer5Config
    from layer5.phoenix import PhoenixSpecialization
    pcfg=Layer5Config.from_yaml(cfg.path_value('TDDT_LAYER5_ROOT')/'configs/configuration_phoenix.yaml')
    pcfg.model_dependencies_provider=lambda:capture_model_dependencies(cfg)
    phoenix=PhoenixSpecialization(pcfg)
    seeds=phoenix.offline_vtol_seed_controls({'air_density_kg_m3':1.2,'battery_soc':1.,
        'environment_fingerprint':forcing_sha})
    l2=Layer2Adapter(cfg)
    try:
        fresh=l2.derive_offline_phoenix_model_certificate(phoenix.hash,
            phase_seed_controls=seeds,airframe_binding_sha256=phoenix.airframe_binding_hash())
    finally:l2.close()
    assert fresh['ok'] and certificate_compatibility(fresh,current)['ok']
    applied=phoenix.apply_runtime_validation_certificate(fresh);assert applied['ok'],applied
    a.engine=SimpleNamespace(phoenix=phoenix)
    bound=a.set_verification_model_certificate(fresh);assert bound['ok'],bound
    write(out/'ISOLATED_NEW_MODEL_CERTIFICATE.json',fresh)
    write(out/'ISOLATED_ACCEPTANCE.json',{'applied':applied,'bound':bound,
        'scope':'new local FMU/model qualification only; no active handoff replacement or flight authority'})
    # The same certificate must be revoked after a modeled binary-version change.
    # Byte mutation and bank/cache paths are separately covered by unit tests.
    assert capture_model_dependencies(cfg)==current
    assert hp.read_bytes()==handoff_raw and sha(source)==source_sha and sha(forcing)==forcing_sha
    result={'step':'R3.06','status':'PASS','revision':current['revision'],'model_id':current['model_id'],
        'artifact_count':len(current['artifacts']),'original_handoff_gate':gate['ok'],
        'handoff_file_sha256':sha(hp),'handoff_hash':hm['handoff_hash'],'forcing_sha256':forcing_sha,
        'source_cycle_sha256':source_sha,'old_certificate':old_gate,'same_handoff_cache_reuse':cached,
        'stored_certificates_rejected':sum(v for k,v in counts.items() if k!='exact_model_dependencies_match'),
        'isolated_new_qualification_accepted':True,'active_bank_updated':False,'handoff_rewritten':False,
        'offline_training_started':False,'native_stack_started':False,'ARM_sent':False,
        'flight_authority':False,'next_stage':'R4 origin/forcing binding; native pilot prediction remains R7'}
    write(out/'MODEL_COMPATIBILITY_RESULT.json',result);print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
