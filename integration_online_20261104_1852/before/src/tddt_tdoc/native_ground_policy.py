"""R3.05 fixture retirement, based on measured native four-foot support."""
import hashlib
import json
from pathlib import Path

REVISION='phoenix-fourfoot-no-fixture-r3.05-v1'
POLICY_SHA256='3a65f51c5f4df895a14356d250c36b7e7c56a8de7817b524d09851632454afcd'


def ground_policy():
    path=Path(__file__).resolve().parent/'resources/phoenix_fourfoot/r3_05_v1/NATIVE_GROUND_POLICY.json'
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=POLICY_SHA256:
        raise ValueError('native ground policy hash mismatch')
    return json.loads(raw)


def reject_fixture_environment(environment):
    if isinstance(environment,dict) and any(key in environment for key in ('launch_fixture','launch_fixture_state')):
        raise ValueError('retired launch fixture environment is not admissible; native four-foot contact required')


def require_native_contacts(config,request=None):
    legacy=config.get('layers.layer6.online.ideal_launch_fixture',False)
    if legacy is not False and legacy is not None:
        raise ValueError('ideal_launch_fixture is retired by R3.05; remove the legacy option')
    if config.get('layers.layer6.online.ground_contact_policy',REVISION)!=REVISION:
        raise ValueError('unsupported native ground contact policy revision')
    request=request or {}
    reject_fixture_environment(request)
    context=request.get('context') or {}
    reject_fixture_environment(context.get('environment_contract'))
    ingress=(context.get('online_state_ingress') or {}).get('ptlib_ingress') or {}
    reject_fixture_environment(ingress.get('environment_contract'))
    reject_fixture_environment((ingress.get('gui_state') or {}).get('environment_contract'))
    return ground_policy()


def retired_fixture_replay(record):
    return bool('ideal-SITL-launch-fixture' in str(record.get('source') or '')
                or record.get('launch_fixture') is not None
                or record.get('launch_fixture_state') is not None
                or record.get('launch_fixture_evidence_valid')
                or record.get('launch_fixture_contract_hash'))
