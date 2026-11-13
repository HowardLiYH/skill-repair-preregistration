"""Scoped GDP/largest-bundle editor preflight; dry-run unless --execute is given.

Uses the one existing $5 journal. No executor tools, task answers, dev/test text,
remote credentials, or automatic regeneration. Local counts are not billed usage.
"""
from __future__ import annotations
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
from .bundles import load_bundle
from .connection_pilot import read_private_key, peak_accounting
from .contracts import canonical_hash, write_json_atomic
from .deepseek_tokenization import (DeepSeekOfflineCounter, OFFICIAL_MODEL,
    OFFICIAL_REVISION, OFFICIAL_TOKENIZER_SHA256)
from .editor_service import EditorService
from .journal import Journal, AdmissionBlocked
from .transports import OpenAICompatibleTransport

ROOT = Path(__file__).resolve().parents[2]
MAX_OUTPUT = 16000
MAX_INPUT = 1048576
RESERVE = float((Decimal(MAX_INPUT)*Decimal('.30') +
                 Decimal(MAX_OUTPUT)*Decimal('1.20')) / Decimal(1000000))
FAMILIES = ('weighted-gdp-calculation', 'organize-messy-files')

class RequestEstimate:
    """Serialized payload estimate with 10% headroom; not a provider count."""
    model_id = 'deepseek-flash'
    def __init__(self, counter):
        self.counter = counter
        self.tokenizer_id = counter.tokenizer_id + '-serialized-json-estimate'
        self.revision = counter.revision + ':json-payload-v1'
    def __call__(self, text):
        return self.counter(text)

def checked_accounting(result):
    if result.get('model_id') not in ('deepseek-flash', 'DeepSeek-V4.1-Flash-1017'):
        raise AdmissionBlocked('Unexpected provider model; retain reservation for review')
    raw = (result.get('usage') or {}).get('raw', {})
    for name, maximum in [('prompt_tokens', MAX_INPUT), ('completion_tokens', MAX_OUTPUT)]:
        value = raw.get(name)
        if type(value) is not int or not 0 <= value <= maximum:
            raise AdmissionBlocked('Usage outside reviewed bound; retain reservation')
    accounted = peak_accounting(result)
    if accounted.get('accounted_charge_usd') is None:
        raise AdmissionBlocked('Missing billable usage; retain reservation')
    return accounted

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=FAMILIES, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--prompt-version', choices=('v1', 'v2'), default='v1')
    args = parser.parse_args(argv)
    config_path = ROOT/'configs/pilot-deepseek.proposed.json'
    config = json.loads(config_path.read_text())
    adoption = config.get('pilot_tokenizer', {})
    if (config.get('approved_incremental_budget_usd') != 5 or
        config.get('approval_status') != 'user_approved_pilot_only_2026-11-06' or
        adoption.get('adopted_for_pilot') is not True or
        adoption.get('runtime_version') != '0.22.2'):
        raise AdmissionBlocked('Recorded pilot budget and counting adoption required')
    counter = DeepSeekOfflineCounter(ROOT/'private_artifacts/external/deepseek-tokenizer/tokenizer.json',
        model_id=OFFICIAL_MODEL, revision=OFFICIAL_REVISION,
        expected_sha256=OFFICIAL_TOKENIZER_SHA256, expected_runtime_version='0.22.2',
        cache_dir=ROOT/'private_artifacts/deepseek-pilot/count_cache')
    source = load_bundle(ROOT/'milestone 1/data/shared/skills/human_authored'/args.family)
    binding = {'purpose':'pilot_feasibility_only_not_strategy_selection',
        'editor':config['editor_candidate'], 'tokenizer':adoption,
        'strategy':'workflow', 'context_limit':131072, 'headroom':.10,
        'reservation_usd':RESERVE, 'model_input_max':MAX_INPUT}
    namespace = 'editor-feasibility-v2' if args.prompt_version == 'v2' else None
    if namespace:
        binding.update(pilot_attempt_namespace=namespace, rewrite_prompt_version='v2',
            response_format={'type':'json_object'})
    destination = ROOT/'private_artifacts/deepseek-pilot'
    journal = Journal(destination/'ledger.sqlite', 5, campaign_id=config['campaign_id'])
    def admission(_):
        return {'admitted':True, 'admission_id':'deepseek-editor-feasibility-'+args.prompt_version,
            'maximum_incremental_charge_usd':RESERVE, 'billing_mode':'metered_api',
            'evidence_refs':[str(config_path), str(ROOT/'docs/DECISIONS.md')]}
    decoding = {'thinking':{'type':'disabled'}, 'temperature':0}
    if namespace:
        decoding['response_format'] = {'type':'json_object'}
    transport = OpenAICompatibleTransport('deepseek', 'deepseek-flash',
        'https://api.deepseek.com/v1', 'CAPSTUDY_DEEPSEEK_KEY', MAX_OUTPUT,
        admission=admission, decoding_settings=decoding)
    # Construction counts source bytes but never calls a model.
    service = EditorService(source=source, family=args.family,
        campaign_id=config['campaign_id'], strategy_id='workflow',
        config_sha256=canonical_hash(binding), counter=counter,
        tokenizer_id=counter.tokenizer_id, tokenizer_revision=counter.revision,
        journal=journal, transport=transport, artifact_root=destination/('editor-preflight-v2' if namespace else 'editor-preflight'),
        upper_charge_usd=RESERVE, cost_resolver=checked_accounting,
        editor_counter=RequestEstimate(counter), context_limit=131072,
        max_output_tokens=MAX_OUTPUT, headroom=.10,
        rewrite_prompt_version=args.prompt_version, pilot_attempt_namespace=namespace)
    summary = {'evidence_kind':'editor_capacity_pilot', 'family':args.family,
        'executed':False, 'original_tokens':service.original_tokens,
        'token_cap':service.token_cap, 'reservation_usd':RESERVE,
        'binding':binding, 'scientific_strategy_selection':False,
        'budget_usd':5, 'provider_counted':False}
    previous = os.environ.get('CAPSTUDY_DEEPSEEK_KEY')
    try:
        if args.execute:
            os.environ['CAPSTUDY_DEEPSEEK_KEY'] = read_private_key(ROOT/config['secret_file'])
            candidate = service.generate_initial()
            summary.update(executed=True, candidate_id=candidate['candidate_id'],
                candidate_tokens=candidate.get('candidate_tokens'), eligible=candidate['eligible'],
                ineligibility_reasons=candidate['ineligibility_reasons'],
                editor_request_tokens_estimate=candidate.get('editor_request_tokens'),
                call_ids=candidate['call_ids'], candidate_path=str(service.root/'candidates'/candidate['candidate_id']/'candidate.json'),
                ledger_totals={k:str(v) if isinstance(v,Decimal) else v for k,v in journal.totals().items()})
            suffix = '_v2' if namespace else ''
            write_json_atomic(ROOT/'results/study-v1/preflight'/('editor_'+args.family+suffix+'.json'),summary)
        print(json.dumps(summary,indent=2))
    finally:
        if previous is None: os.environ.pop('CAPSTUDY_DEEPSEEK_KEY',None)
        else: os.environ['CAPSTUDY_DEEPSEEK_KEY']=previous
        journal.close()
    return 0

if __name__ == '__main__': raise SystemExit(main())
