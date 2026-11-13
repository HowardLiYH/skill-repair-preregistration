"""One explicitly attested remote calibration trial; no provider key or ledger.

Planning is read-only. Execution consumes a new physical slot exactly once and
uses only a dedicated relay token. Mac evidence paths remain provenance strings.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
from .baselines import configured_counter
from .bundles import load_bundle,validate_candidate
from .contracts import canonical_hash,sha256_file,write_json_atomic
from .deepseek_runtime import validate_mapping,URL_ENV,TOKEN_ENV
from .formal_calibration_gateway import ACCOUNTING_CAMPAIGN,TRAIN_IDS,CONDITIONS,PHASE
from .compress.xing import STRATEGIES
from .formal_verifier import validate_policy
from .pilot_network import POLICY,policy_hash,verify_installed
from .runner import plan_trial,run_trial,execution_config_hash,runner_implementation_hash,tree_hash,ROOT


def _load(path,*,private=False):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Regular immutable file required')
    if private and (path.stat().st_uid!=os.getuid() or path.stat().st_mode&0o077):
        raise ValueError('Owner-only private file required')
    raw=path.read_bytes();return json.loads(raw),hashlib.sha256(raw).hexdigest()


def _inside(root,relative):
    path=Path(root)/relative
    if path.is_symlink() or path.resolve()!=path or not path.is_relative_to(root):
        raise ValueError('Exact nonsymlink input inside data root required')
    return path


def _data_root(config):
    path=Path(config['data_root'])
    if not path.is_absolute():path=ROOT/path
    if path.resolve()!=path:raise ValueError('Canonical nonsymlink data root required')
    return path


def _policies(config,row):
    if (config.get('campaign_id')==ACCOUNTING_CAMPAIGN or not config.get('campaign_id')
        or 'pilot_trial_id' in config or config.get('budget_usd')!=5
        or config.get('executor',{}).get('provider')!='deepseek'
        or config['executor'].get('model_id')!='deepseek-flash'
        or config['executor'].get('agent_id')!='claude-code'):
        raise ValueError('Distinct formal campaign with existing $5 central route required')
    runtime=config.get('runtime',{});mapping=validate_mapping(runtime.get('deepseek_mapping',{}))
    if (runtime.get('execution_profile')!='native-task-limits-v1'
        or runtime.get('staging_policy')!='single-active-instance-v1'
        or 'build_timeout_seconds' in runtime or runtime.get('network')!=POLICY['network']
        or mapping.get('network_policy_sha256')!=policy_hash()
        or mapping.get('max_turns')!=config['executor'].get('max_steps')):
        raise ValueError('Formal native limits, one-task staging and verified local-tool route required')
    tool_policy=config.get('tool_policy',{})
    for key in ('allowed_tools','disallowed_tools'):
        value=tool_policy.get(key)
        if not isinstance(value,list) or len(value)!=len(set(value)) or set(value)!=set(mapping[key]):
            raise ValueError('Effective local tool policy differs from frozen mapping')
    if tool_policy.get('skill_loading_policy') not in (None,mapping.get('skill_loading_policy')):
        raise ValueError('Skill loading policy differs from frozen mapping')
    for field in ('cpus','build_cpus'):
        if type(runtime.get(field)) is not int or not 1<=runtime[field]<=8:raise ValueError('Bounded CPU policy required')
    for field in ('memory','build_memory'):
        if not re.fullmatch(r'[1-9]\d*[gGmM]',str(runtime.get(field))):raise ValueError('Bounded memory policy required')
    guard=runtime.get('launch_guard',{})
    if (type(guard.get('timeout_seconds')) is not int or not 1<=guard['timeout_seconds']<=10800
        or type(guard.get('minimum_free_bytes')) is not int or guard['minimum_free_bytes']<20*1024**3
        or not isinstance(guard.get('disk_paths'),list) or not guard['disk_paths']):
        raise ValueError('Reviewed whole-trial time and disk guard required')
    layer=Path(runtime.get('dependency_layer_file',''))
    if layer.is_symlink() or not layer.is_file() or sha256_file(layer)!=runtime.get('dependency_layer_sha256'):
        raise ValueError('Pinned runtime layer differs')
    verifier=runtime.get('formal_verifier_policy',{})
    if verifier.get('scope')!='formal_calibration' or verifier.get('campaign_id')!=config['campaign_id']:
        raise ValueError('Formal native verifier science binding differs')
    validate_policy(verifier,_data_root(config)/row['path'])
    if config.get('tokenizer',{}).get('id')!='deepseek-official-local-tokenizer':
        raise ValueError('Explicit pinned offline executor token convention required')


def prepare_trial(config_path,*,physical_trial_id,instance_id,condition,strategy_id=None,
                  candidate_record_path=None,candidate_bundle=None):
    config_path=Path(config_path);config,file_hash=_load(config_path);config_path=config_path.resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',physical_trial_id):raise ValueError('Stable physical identity required')
    if instance_id not in TRAIN_IDS or condition not in CONDITIONS:raise ValueError('Only eight calibration train cells supported')
    if config.get('physical_trial_id') not in (None,physical_trial_id):raise ValueError('Physical config identity differs')
    if (condition=='unchanged_rewrite' and strategy_id not in STRATEGIES) or (condition!='unchanged_rewrite' and strategy_id is not None):
        raise ValueError('Strategy identity differs from calibration condition')
    root=_data_root(config)
    manifest=_inside(root,'train/instances.jsonl');raw=manifest.read_bytes()
    rows=[json.loads(line) for line in raw.splitlines() if line.strip()]
    if (len(rows)!=8 or sorted(r.get('instance_id','') for r in rows)!=list(TRAIN_IDS)
        or any(r.get('split')!='train' or r.get('family')!=r['instance_id'][:-2] for r in rows)):
        raise ValueError('Exact eight-row train manifest required')
    row=next(r for r in rows if r['instance_id']==instance_id)
    _inside(root,row['path']);source=load_bundle(_inside(root,row['skill_bundle_path']))
    _policies(config,row)
    counter=configured_counter(config,'deepseek-flash',None,config_path.parent)
    original=sum(counter(t) for t in source.files.values());candidate=None;candidate_sha=None;eligibility=None
    if condition=='unchanged_rewrite':
        if candidate_record_path is None or candidate_bundle is None:raise ValueError('Original candidate record and relocated bundle required')
        candidate_path=Path(candidate_record_path);candidate,candidate_sha=_load(candidate_path);candidate_path=candidate_path.resolve()
        candidate_bundle=Path(candidate_bundle)
        if not candidate_bundle.is_absolute() or candidate_bundle.resolve()!=candidate_bundle:
            raise ValueError('Canonical nonsymlink relocated candidate root required')
        actual=load_bundle(candidate_bundle)
        if config.get('source_generation_adoption'):
            from .source_adoption import verify_candidate
            verify_candidate(config,candidate,candidate_sha)
        elif candidate.get('campaign_id')!=config['campaign_id']:
            raise ValueError('Candidate scientific campaign differs')
        if (candidate.get('family')!=row['family']
            or candidate.get('strategy_id')!=strategy_id or candidate.get('method')!='unchanged_rewrite'
            or candidate.get('attempt_index')!=0 or candidate.get('pilot_attempt_namespace') is not None
            or candidate.get('eligible') is not True or candidate.get('ineligibility_reasons')!=[]
            or not isinstance(candidate.get('candidate_id'),str) or not candidate['candidate_id']
            or candidate.get('source_bundle_sha256')!=source.hashes()['source_bundle_sha256']
            or actual.files!=candidate.get('files') or actual.hashes()['file_sha256']!=candidate.get('file_sha256')):
            raise ValueError('Candidate is not the exact eligible source-only formal initial rewrite')
        eligibility=validate_candidate(source,actual.files,counter,original//2,actual.root)
        if (not eligibility['eligible'] or any(candidate.get(k)!=eligibility[k] for k in
            ('original_tokens','candidate_tokens','token_cap')) or candidate.get('tokenizer_id')!=counter.tokenizer_id
            or candidate.get('tokenizer_revision')!=counter.revision):
            raise ValueError('Independent candidate eligibility/count provenance differs')
        candidate={**candidate,'bundle_path':str(actual.root),'original_record_path':str(candidate_path),
            'original_record_sha256':candidate_sha}
    elif candidate_record_path is not None or candidate_bundle is not None:raise ValueError('Reference controls cannot receive candidate metadata')
    plan=plan_trial(row,condition,candidate,config)
    return {'schema_version':1,'kind':'formal_calibration_remote_plan','executed':False,
        'physical_trial_id':physical_trial_id,'instance_id':instance_id,'condition':condition,'repeat_id':1,
        'strategy_id':strategy_id,'candidate_id':candidate.get('candidate_id') if candidate else None,
        'actual_bundle_hash':plan['bundle_sha256'],'config_sha256':canonical_hash(config),
        'config_file_sha256':file_hash,'execution_config_sha256':execution_config_hash(config),
        'mapping_sha256':canonical_hash(config['runtime']['deepseek_mapping']),
        'train_manifest_sha256':hashlib.sha256(raw).hexdigest(),
        'runner_implementation_sha256':runner_implementation_hash(),
        'source_execution_signature':plan['source_execution_signature'],
        'original_candidate_record_sha256':candidate_sha,'original_tokens':original,
        'tokenizer_provenance':copy.deepcopy(counter.provenance),'independent_eligibility':eligibility,
        'source_bundle_sha256':source.hashes()['source_bundle_sha256'],
        'instance_record':row,'candidate_projection':candidate,'runner_plan':plan}


def validate_attestation(path,plan,config,*,now=None):
    attestation,digest=_load(path,private=True);binding=attestation.get('execution_binding',{})
    if not isinstance(binding,dict):raise ValueError('Complete central execution binding required')
    seal=binding.get('binding_sha256')
    if seal!=canonical_hash({k:v for k,v in binding.items() if k!='binding_sha256'}):raise ValueError('Central binding seal changed')
    expected={'schema_version':1,'kind':'formal_calibration_remote_route',
        'execution_binding_sha256':canonical_hash(binding),'central_ledger_only':True,
        'gateway_enforces_per_request_budget':True,'approved_ceiling_usd':5,
        'provider_key_location':'mac_only','remote_config_sha256':plan['config_sha256'],'ready':True,
        'gateway_policy_sha256':sha256_file(Path(__file__).with_name('deepseek_gateway.py'))}
    expected.update({k:plan[k] for k in ('original_candidate_record_sha256','train_manifest_sha256',
        'runner_implementation_sha256','source_execution_signature')})
    if any(attestation.get(k)!=v for k,v in expected.items()):raise ValueError('Owner route attestation differs from exact remote plan')
    bound={'phase':PHASE,'scientific_campaign_id':config['campaign_id'],'accounting_campaign_id':ACCOUNTING_CAMPAIGN,
        'physical_trial_id':plan['physical_trial_id'],'instance_id':plan['instance_id'],'family':plan['instance_record']['family'],
        'split':'train','condition':plan['condition'],'strategy_id':plan['strategy_id'],'candidate_id':plan['candidate_id'],
        'candidate_sha256':plan['actual_bundle_hash'],'trial_config_sha256':plan['config_sha256'],
        'trial_config_file_sha256':plan['config_file_sha256'],'execution_config_sha256':plan['execution_config_sha256'],
        'mapping_sha256':plan['mapping_sha256'],'physical_attempt_index':1}
    if any(binding.get(k)!=v for k,v in bound.items()):raise ValueError('Central scientific/physical/configuration binding differs')
    if binding.get('evidence_kind')!='experiment':raise ValueError('Remote dispatch requires real reviewed experiment scope')
    for key in ('descriptor_sha256','base_config_file_sha256','base_config_sha256','base_execution_config_sha256'):
        if not re.fullmatch(r'[0-9a-f]{64}',binding.get(key) or ''):raise ValueError('Central frozen approval provenance missing')
    if binding['base_execution_config_sha256']!=plan['execution_config_sha256']:raise ValueError('Frozen runtime differs')
    if not re.fullmatch(r'[0-9a-f]{64}',attestation.get('gateway_state_sha256') or '') or not attestation.get('reviewer'):
        raise ValueError('Gateway state hash and named review required')
    expires=datetime.fromisoformat(attestation['expires_at']);now=now or datetime.now(timezone.utc)
    if expires.tzinfo is None or not 0<(expires-now).total_seconds()<=7200:raise ValueError('Route expired or lifetime exceeds fixed registration')
    return {'attestation_path':str(Path(path).resolve()),'attestation_sha256':digest,'binding_sha256':seal,
        'gateway_policy_sha256':attestation['gateway_policy_sha256'],'expires_at':attestation['expires_at']}


def execute_trial(config_path,*,attestation_path,network_state,relay_token_file,**selection):
    plan=prepare_trial(config_path,**selection);config,_=_load(config_path)
    proof=validate_attestation(attestation_path,plan,config)
    artifact=Path(config['artifact_root'])
    if not artifact.is_absolute() or artifact.resolve()!=artifact:raise ValueError('Explicit canonical artifact root required')
    physical=plan['physical_trial_id'];job=artifact/'jobs'/physical
    state=artifact/'remote_admissions'/physical
    if job.exists() or job.is_symlink() or state.exists() or state.is_symlink():raise ValueError('Physical slot already used; no automatic retries or adoption')
    network=verify_installed(network_state)
    if network.get('verified') is not True or network.get('policy_sha256')!=policy_hash():raise ValueError('Network proof failed')
    token_path=Path(relay_token_file)
    if token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_uid!=os.getuid() or token_path.stat().st_mode&0o077:
        raise ValueError('Owner-only dedicated relay-token file required')
    token=token_path.read_text().strip()
    if not re.fullmatch(r'capstudy-relay-[A-Za-z0-9_-]{32,256}',token):raise ValueError('Dedicated relay token required; upstream credentials prohibited')
    relay=f'http://{POLICY["gateway"]}:{POLICY["relay_port"]}'
    if hashlib.sha256(relay.encode()).hexdigest()!=config['runtime']['deepseek_mapping']['relay_url_sha256']:
        raise ValueError('Relay origin differs from frozen mapping')
    state.mkdir(parents=True,mode=0o700)
    write_json_atomic(state/'plan.json',plan)
    write_json_atomic(state/'admission.json',{**proof,'network':network,'physical_trial_id':physical,
        'money_reserved_by_remote':False,'status':'consumed_before_runner'})
    if plan['candidate_projection'] is not None:write_json_atomic(state/'relocated-candidate.json',plan['candidate_projection'])
    def recheck():
        current=prepare_trial(config_path,**selection)
        if current!=plan:raise ValueError('Reviewed remote inputs changed before dispatch')
        if validate_attestation(attestation_path,current,config)!=proof:
            raise ValueError('Immutable owner attestation changed before dispatch')
    def admit(signature):
        recheck()
        result={**proof,'reservation_id':'central-formal-route:'+physical,
            'admission_kind':'central_per_request_gateway','money_reserved_by_remote':False,
            'trial_id':physical,'execution_signature':signature}
        write_json_atomic(state/'staged-admission.json',result);return result
    def eligible(candidate,row,cfg):
        recheck()
        if candidate!=plan['candidate_projection'] or row!=plan['instance_record'] or cfg!=config:raise ValueError('Runner selection changed')
        return plan['independent_eligibility']
    prior={key:os.environ.get(key) for key in (TOKEN_ENV,URL_ENV)}
    try:
        os.environ[TOKEN_ENV]=token;os.environ[URL_ENV]=relay
        result=run_trial(plan['instance_record'],plan['condition'],plan['candidate_projection'],config,
            execute=True,physical_trial_id=physical,eligibility_check=eligible,admission=admit)
        write_json_atomic(state/'result.json',{'trial_id':physical,'status':result.get('status'),
            'usage_attachment':'pending_central_gateway_readback','model_calls_not_inferred':True})
        return result
    finally:
        for key,value in prior.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('config','physical-trial-id','instance-id','condition'):p.add_argument('--'+flag,required=True)
    p.add_argument('--strategy-id');p.add_argument('--candidate-record-path');p.add_argument('--candidate-bundle')
    p.add_argument('--execute',action='store_true');p.add_argument('--attestation-path');p.add_argument('--network-state');p.add_argument('--relay-token-file')
    a=p.parse_args(argv);selection={k:getattr(a,k) for k in ('physical_trial_id','instance_id','condition','strategy_id','candidate_record_path','candidate_bundle')}
    if a.execute:
        if not all((a.attestation_path,a.network_state,a.relay_token_file)):p.error('Execution requires attestation, network state and dedicated relay token')
        result=execute_trial(a.config,attestation_path=a.attestation_path,network_state=a.network_state,relay_token_file=a.relay_token_file,**selection)
        print(json.dumps({'trial_id':result['trial_id'],'status':result['status']}));return
    print(json.dumps(prepare_trial(a.config,**selection),indent=2))


if __name__=='__main__':main()
