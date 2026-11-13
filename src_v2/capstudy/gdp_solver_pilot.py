"""Prepare or run ONE scoped GDP feasibility trial through the central gateway.

No local budget ledger or provider-key loading. Default prepares a reviewable
configuration only. --execute additionally requires an explicit launch attestation,
verified network policy, injected trial relay token, and independent token recount.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
from .contracts import write_json_atomic, canonical_hash, sha256_file
from .deepseek_runtime import make_mapping
from .pilot_network import POLICY, policy_hash, verify_installed
from .runner import run_trial, tree_hash

ROOT=Path(__file__).resolve().parents[2]
FAMILY='weighted-gdp-calculation'
INSTANCE=FAMILY+'-1'
LAYER_SHA256='3b2871ab78c1c001c36814a5c88c9decc8d066888836ddc24adcdf3440fcabb7'


def prepare_config(root, *, trial_id, condition, mock_capture_path, max_turns=5, skill_loading_policy=None):
    root=Path(root).resolve()
    if condition not in ('full_skill','head_truncation'):raise ValueError('Only the two scoped GDP pilot conditions')
    if type(max_turns) is not int or max_turns not in (5,100):raise ValueError('Reviewed pilot turn choices are 5 or 100')
    layer=root/'configs/runtime/node24-claude2-amd64.layer'
    if sha256_file(layer)!=LAYER_SHA256:raise ValueError('Pinned runtime layer changed')
    tools=['Bash','Read','Write','Edit','Glob','Grep','Skill']
    mapping=make_mapping(relay_url='http://172.30.53.1:18080',cli_path='/usr/local/bin/claude',
        cli_version='2.1.296',allowed_tools=tools,max_turns=max_turns,network_policy_sha256=policy_hash(),
        mock_capture_sha256=sha256_file(mock_capture_path), skill_loading_policy=skill_loading_policy)
    return {'schema_version':1,'campaign_id':'deepseek-calibration-pilot','budget_usd':5,
        'purpose':'gdp_feasibility_only_not_full_study','pilot_trial_id':trial_id,'pilot_condition':condition,
        'data_root':str(root/'milestone 1/data'),'artifact_root':str(root/'private_artifacts/gdp-solver-pilot'),
        'executor':{'agent_id':'claude-code','provider':'deepseek','model_id':'deepseek-flash',
            'model_revision':'DeepSeek-V4.1-Flash-1017','cli_version':'2.1.296','max_steps':max_turns},
        'runtime':{'node_version':'24.11.1','dependency_layer_file':str(layer),
            'dependency_layer_sha256':LAYER_SHA256,'cpus':1,'memory':'4g','network':POLICY['network'],
            'build_timeout_seconds':600,'deepseek_mapping':mapping,
            'launch_guard':{'timeout_seconds':3000,'minimum_free_bytes':20*1024**3,
                'disk_paths':[str(root),'/var/lib/docker']}},
        'tool_policy':{'allowed_tools':tools,'disallowed_tools':mapping['disallowed_tools']},
        'cache_regime':'isolated_solver_no_shared_conversation_provider_cache_measured',
        'external_input_versions':{'benchmark_commit':'726e1f79b96b0cb3d729d0e55a1c847e0b5353ae'},
        'pilot_tokenizer_path':str(root/'private_artifacts/external/deepseek-tokenizer/tokenizer.json'),
        'budget_enforcement':'one_central_mac_journal_per_physical_gateway_request',
        'formal_g1_complete':False}


def instance(config):
    rows=[json.loads(line) for line in (Path(config['data_root'])/'train/instances.jsonl').read_text().splitlines() if line.strip()]
    return next(row for row in rows if row['instance_id']==INSTANCE and row['family']==FAMILY)


def eligibility(candidate,row,config):
    from .bundles import load_bundle, validate_candidate
    from .deepseek_tokenization import DeepSeekOfflineCounter,OFFICIAL_MODEL,OFFICIAL_REVISION,OFFICIAL_TOKENIZER_SHA256
    source=load_bundle(Path(config['data_root'])/row['skill_bundle_path'])
    actual=load_bundle(candidate['bundle_path'])
    if candidate.get('method')!='head_truncation' or candidate.get('purpose')!='g1_pilot_only':
        raise ValueError('Expected the independently prepared head candidate')
    if candidate.get('source_bundle_sha256')!=source.hashes()['source_bundle_sha256']:
        raise ValueError('Candidate source binding mismatch')
    if actual.hashes()['file_sha256']!=candidate.get('file_sha256'):
        raise ValueError('Actual selected candidate bytes differ')
    counter=DeepSeekOfflineCounter(config['pilot_tokenizer_path'],model_id=OFFICIAL_MODEL,
        revision=OFFICIAL_REVISION,expected_sha256=OFFICIAL_TOKENIZER_SHA256,expected_runtime_version='0.22.2')
    original=sum(counter(text) for text in source.files.values())
    result=validate_candidate(source,actual.files,counter,original//2,candidate_root=actual.root)
    if actual.files!=candidate.get('files') or result['candidate_tokens']!=candidate.get('candidate_tokens'):
        raise ValueError('Candidate metadata does not match independent recount')
    return result


def admission_callback(config, attestation_path):
    path=Path(attestation_path)
    if path.is_symlink() or path.stat().st_mode&0o077:raise ValueError('Owner-only launch attestation required')
    attestation=json.loads(path.read_text())
    required={'campaign_id':config['campaign_id'],'trial_id':config['pilot_trial_id'],
              'condition':config['pilot_condition'],'config_sha256':canonical_hash(config),
              'gateway_enforces_per_request_budget':True,'central_ledger_only':True,
              'model':'deepseek-flash','approved_ceiling_usd':5,'ready':True}
    if any(attestation.get(key)!=value for key,value in required.items()):
        raise ValueError('Central gateway launch attestation does not match this trial')
    if not attestation.get('gateway_policy_sha256') or not attestation.get('reviewer'):
        raise ValueError('Gateway policy hash and named independent review required')
    def admit(signature):
        # This is a route attestation, NOT a monetary whole-trial reservation.
        # Only the central gateway can reserve each physical request's charge.
        return {'reservation_id':'central-gateway-route:'+config['pilot_trial_id'],
                'admission_kind':'central_per_request_gateway','money_reserved_by_remote':False,
                'trial_id':config['pilot_trial_id'],'execution_signature':signature,
                'attestation_path':str(path.resolve()),'attestation_sha256':sha256_file(path),
                'gateway_policy_sha256':attestation['gateway_policy_sha256']}
    return admit


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--trial-id');p.add_argument('--condition',choices=['full_skill','head_truncation'])
    p.add_argument('--mock-capture',type=Path)
    p.add_argument('--max-turns',type=int,choices=[5,100],default=5)
    p.add_argument('--skill-loading-policy',choices=['invoke-installed-before-task-v1'])
    p.add_argument('--candidate',type=Path);p.add_argument('--candidate-bundle',type=Path)
    p.add_argument('--execute',action='store_true');p.add_argument('--network-state',type=Path)
    p.add_argument('--admission',type=Path)
    p.add_argument('--relay-token-file',type=Path)
    args=p.parse_args(argv)
    if not args.execute:
        if not all((args.trial_id,args.condition,args.mock_capture)):p.error('Preparation requires --trial-id --condition --mock-capture')
        if args.config.exists():raise ValueError('Do not overwrite reviewed trial configuration')
        config=prepare_config(ROOT,trial_id=args.trial_id,condition=args.condition,mock_capture_path=args.mock_capture,max_turns=args.max_turns,skill_loading_policy=args.skill_loading_policy)
        write_json_atomic(args.config,config)
        print(json.dumps({'prepared':True,'executed':False,'config_path':str(args.config),'config_sha256':canonical_hash(config)}));return 0
    if not args.network_state or not args.admission:p.error('Execution requires network state and central admission')
    config=json.loads(args.config.read_text());row=instance(config)
    if config.get('purpose')!='gdp_feasibility_only_not_full_study' or config.get('pilot_condition') not in ('full_skill','head_truncation'):
        raise ValueError('Only prepared GDP feasibility configurations may execute')
    verify_installed(args.network_state)
    if args.relay_token_file:
        token_path=args.relay_token_file
        if token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_mode&0o077:
            raise ValueError('Owner-only dedicated relay token file required')
        token=token_path.read_text().strip()
        if not re.fullmatch(r'capstudy-relay-[A-Za-z0-9_-]{32,256}',token):
            raise ValueError('Dedicated relay token required; provider keys prohibited')
        os.environ['CAPSTUDY_DEEPSEEK_RELAY_TOKEN']=token
        os.environ['CAPSTUDY_DEEPSEEK_RELAY_URL']='http://172.30.53.1:18080'
    candidate=None
    if config['pilot_condition']=='head_truncation':
        if not args.candidate or not args.candidate_bundle:p.error('Head trial requires candidate record and transferred bundle')
        candidate=json.loads(args.candidate.read_text())
        # Relocation is recorded locally without overwriting the original record.
        candidate={**candidate,'bundle_path':str(args.candidate_bundle.resolve()),
                   'original_record_path':str(args.candidate.resolve()),'original_record_sha256':sha256_file(args.candidate)}
    record=run_trial(row,config['pilot_condition'],candidate,config,execute=True,
        eligibility_check=eligibility,admission=admission_callback(config,args.admission))
    print(json.dumps({'trial_id':record['trial_id'],'status':record['status'],
        'native_reward':record['native_reward'],'skill_use':record.get('skill_use'),
        'provider_usage_attachment_status':record.get('provider_usage_attachment_status')}))
    return 2 if record['status']=='infra_error' else 0


if __name__=='__main__':raise SystemExit(main())
