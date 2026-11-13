"""Prepare the actual source-only matrix; execute only under frozen approval.

Preparation reads the existing ledger without modifying it and never reads keys.
It renders/counts all 32 requests using the same bounded production transport.
"""
from __future__ import annotations
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from .contracts import canonical_hash,sha256_file,write_json_atomic
from .source_only_generation import SourceOnlyMatrixGenerator
from .deepseek_tokenization import DeepSeekOfflineCounter,OFFICIAL_MODEL,OFFICIAL_REVISION,OFFICIAL_TOKENIZER_SHA256
from .transports import OpenAICompatibleTransport,post_json_once_bounded
from .editor_pilot import RequestEstimate,checked_accounting,RESERVE,MAX_OUTPUT
from .editor import check_capacity
from .compress.xing import render_prompt,STRATEGIES
from .dispatch_storage import validate_storage_policy
from .formal_calibration_gateway import ACCOUNTING_CAMPAIGN,validate_scope

ROOT=Path(__file__).resolve().parents[2]


def existing_journal_readonly():
    path=ROOT/'private_artifacts/deepseek-pilot/ledger.sqlite'
    if path.is_symlink() or not path.is_file():raise ValueError('Existing central ledger required')
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    metadata=dict(connection.execute('SELECT key,value FROM metadata'))
    if metadata.get('campaign_id')!=ACCOUNTING_CAMPAIGN or metadata.get('budget_usd')!='5' or metadata.get('evidence_kind')!='experiment':
        connection.close();raise ValueError('Existing accounting identity differs')
    return SimpleNamespace(path=path,connection=connection,budget=Decimal(5))


def make_generator(scope_path,base_path,journal,*,planning_only,protocol_sha256):
    scope_path=Path(scope_path).resolve();scope=json.loads(scope_path.read_text())
    if scope.get('source_generation_adoption'):
        raise ValueError('Adopted runtime-v2 matrix cannot create new generation slots')
    disk=scope['dispatch_storage_policy'];artifact=Path(disk['root'])/'generation'
    validate_storage_policy(disk,evidence_kind='experiment',artifact_root=artifact)
    counter=DeepSeekOfflineCounter(ROOT/'private_artifacts/external/deepseek-tokenizer/tokenizer.json',
        model_id=OFFICIAL_MODEL,revision=OFFICIAL_REVISION,expected_sha256=OFFICIAL_TOKENIZER_SHA256,
        expected_runtime_version='0.22.2')
    def admission(_):
        if planning_only:raise ValueError('Read-only preview never authorizes dispatch')
        validate_scope(scope_path,base_path)
        return {'admitted':True,'admission_id':'formal-source-only-generation-v1',
            'maximum_incremental_charge_usd':RESERVE,'billing_mode':'metered_api',
            'evidence_refs':[str(scope_path),str(Path(base_path).resolve())]}
    transport=OpenAICompatibleTransport('deepseek','deepseek-flash','https://api.deepseek.com/v1',
        'CAPSTUDY_DEEPSEEK_KEY',MAX_OUTPUT,admission=admission,
        decoding_settings={'thinking':{'type':'disabled'},'temperature':0,'response_format':{'type':'json_object'}},
        post=post_json_once_bounded)
    return SourceOnlyMatrixGenerator(data_root=ROOT/'milestone 1/data',
        train_manifest=ROOT/'milestone 1/data/train/instances.jsonl',
        train_manifest_sha256=sha256_file(ROOT/'milestone 1/data/train/instances.jsonl'),
        scientific_campaign_id=scope['scientific_campaign_id'],accounting_campaign_id=ACCOUNTING_CAMPAIGN,
        accounting_journal_path=journal.path,generation_config_sha256=protocol_sha256,
        artifact_root=artifact,journal=journal,counter=counter,transport=transport,
        editor_counter=RequestEstimate(counter),context_limit=131072,max_output_tokens=MAX_OUTPUT,
        rewrite_prompt_version='v2',upper_charge_usd=RESERVE,cost_resolver=checked_accounting,
        generation_admission={'descriptor_path':str(scope_path),'base_config_path':str(Path(base_path).resolve())},
        planning_only=planning_only)


def prepare(scope_path,base_path,output):
    output=Path(output)
    if output.exists():raise ValueError('Fresh preparation directory required')
    journal=existing_journal_readonly()
    try:
        generator=make_generator(scope_path,base_path,journal,planning_only=True,protocol_sha256='0'*64)
        binding={k:v for k,v in generator.binding.items() if k not in (
            'generation_config_sha256','binding_sha256','generation_admission','generation_admission_proof')}
        scope=json.loads(Path(scope_path).read_text())
        protocol={'schema_version':1,'purpose':'formal_source_only_generation',
            'generation_binding':binding,'dispatch_storage_policy':scope['dispatch_storage_policy']}
        capacities=[];requests=[]
        for source in binding['source_families']:
            family=source['family'];original=source['original_tokens']
            for strategy in STRATEGIES:
                prompt=render_prompt(generator.sources[family],strategy,original,original//2,
                    f'{generator.counter.tokenizer_id}@{generator.counter.revision}; complete-file sum including frozen wrappers',
                    prompt_version='v2')
                request={'system':'Compress only the provided source bundle. Return the requested complete JSON files mapping.','prompt':prompt}
                payload=generator.transport.plan(request)['payload']
                count=check_capacity(payload,RequestEstimate(generator.counter),131072,MAX_OUTPUT,.10)
                payload_bytes=len(json.dumps(payload,ensure_ascii=False).encode())
                if payload_bytes>1048576:raise ValueError('Prepared source request exceeds formal byte bound')
                capacities.append({'family':family,'strategy':strategy,'request_tokens_estimate':count,
                    'request_bytes':payload_bytes,'max_output_tokens':MAX_OUTPUT,'fits':True,
                    'provider_counted':False,'request_sha256':canonical_hash(request)})
                requests.append((family,strategy,request))
        output.mkdir(parents=True,mode=0o700)
        write_json_atomic(output/'protocol.json',protocol)
        for family,strategy,request in requests:write_json_atomic(output/'requests'/family/(strategy+'.json'),request)
        summary={'evidence_kind':'actual_source_only_capacity_preparation','model_calls':0,'executed':False,
            'formal_approval_granted':False,'matrix_cells':32,'all_requests_fit':True,
            'protocol_sha256':canonical_hash(protocol),'protocol_file_sha256':sha256_file(output/'protocol.json'),
            'capacity':capacities,'plan':generator.plan()}
        write_json_atomic(output/'preparation.json',summary)
        return summary
    finally:journal.connection.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scope',type=Path,required=True);p.add_argument('--base-config',type=Path,required=True)
    p.add_argument('--output',type=Path);p.add_argument('--execute',action='store_true');a=p.parse_args(argv)
    if not a.execute:
        if a.output is None:p.error('Preparation requires --output')
        result=prepare(a.scope,a.base_config,a.output)
        print(json.dumps({k:result[k] for k in ('executed','model_calls','matrix_cells','all_requests_fit','protocol_sha256')}));return 0
    scope,_=validate_scope(a.scope,a.base_config)
    from .formal_gateway_server import open_existing_journal
    from .connection_pilot import read_private_key
    journal=open_existing_journal(ROOT/'private_artifacts/deepseek-pilot/ledger.sqlite')
    prior=os.environ.get('CAPSTUDY_DEEPSEEK_KEY')
    try:
        generator=make_generator(a.scope,a.base_config,journal,planning_only=False,
            protocol_sha256=scope['source_only_generation']['generation_config_sha256'])
        os.environ['CAPSTUDY_DEEPSEEK_KEY']=read_private_key(ROOT/'private_artifacts/secrets/deepseek.env')
        generator.generate_all()
        print(json.dumps({'executed':True,'matrix_cells':32,'scientific_campaign_id':scope['scientific_campaign_id']}))
    finally:
        if prior is None:os.environ.pop('CAPSTUDY_DEEPSEEK_KEY',None)
        else:os.environ['CAPSTUDY_DEEPSEEK_KEY']=prior
        journal.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
