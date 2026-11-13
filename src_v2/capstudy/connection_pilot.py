"""One bounded provider connectivity check. No benchmark inputs or tool access."""
from __future__ import annotations
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
from .contracts import write_json_atomic
from .dispatch import dispatch_editor
from .journal import Journal,AdmissionBlocked
from .transports import OpenAICompatibleTransport

ROOT=Path(__file__).resolve().parents[2]

def read_private_key(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Regular local secret file required')
    if path.stat().st_mode & 0o077:raise ValueError('Secret file must be readable only by its owner')
    values=[]
    for line in path.read_text().splitlines():
        if line.startswith('DEEPSEEK_API_KEY='):values.append(line.split('=',1)[1].strip())
    if len(values)!=1 or not values[0]:raise ValueError('DeepSeek key has not been saved in the private file')
    value=values[0]
    if len(value)<16 or len(value)>512 or any(c.isspace() for c in value):
        raise ValueError('Local key format is invalid; do not paste it in chat')
    return value

def peak_accounting(result):
    """Upper tariff applied to measured usage; this is not a claimed invoice."""
    result=dict(result)
    usage=result.get('usage')
    if not isinstance(usage,dict) or usage.get('valid') is False:
        return result
    raw=usage.get('raw')
    if not isinstance(raw,dict):return result
    counts={k:raw.get(k) for k in ('prompt_tokens','completion_tokens')}
    if any(type(v) is not int or v<0 for v in counts.values()):return result
    hit=raw.get('prompt_cache_hit_tokens',0)
    miss=raw.get('prompt_cache_miss_tokens',counts['prompt_tokens']-hit if type(hit)is int else None)
    if any(type(v)is not int or v<0 for v in (hit,miss)) or hit+miss!=counts['prompt_tokens']:
        return result
    # If hit fields are unavailable, counting every input token as uncached is
    # deliberately conservative and explicitly identified below.
    amount=(Decimal(miss)*Decimal('.30')+Decimal(hit)*Decimal('.006')+
            Decimal(counts['completion_tokens'])*Decimal('1.20'))/Decimal(1000000)
    result['accounted_charge_usd']=str(amount)
    result['actual_incremental_charge_usd']=None
    result['cost_metadata']={**result.get('cost_metadata',{}),
        'accounting_basis':'observed_usage_at_published_peak_tariff_upper_bound',
        'pricing_url':'https://api-docs.deepseek.com/quick_start/pricing/',
        'pricing_date':'2026-11-06','model':'deepseek-flash',
        'input_uncached_usd_per_million':.30,'input_cached_usd_per_million':.006,
        'output_usd_per_million':1.20,'provider_invoice_observed':False,
        'cache_fields_available':'prompt_cache_hit_tokens' in raw}
    return result

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default=str(ROOT/'configs/pilot-deepseek.proposed.json'))
    p.add_argument('--execute',action='store_true')
    args=p.parse_args(argv);config=json.loads(Path(args.config).read_text())
    request={'system':'This is a connectivity check. Return only READY. Do not use tools.',
             'prompt':'Reply with READY.'}
    reserve=.005
    summary={'evidence_kind':'provider_connection_preflight','executed':False,
             'model':'deepseek-flash','max_output_tokens':16,'thinking':'disabled',
             'upper_reservation_usd':reserve,'pilot_ceiling_usd':config['approved_incremental_budget_usd'],
             'tools':[],'benchmark_data_sent':False}
    if not args.execute:print(json.dumps(summary,indent=2));return 0
    if config.get('approval_status')!='user_approved_pilot_only_2026-11-06' or config['approved_incremental_budget_usd']!=5:
        raise AdmissionBlocked('This pilot requires the recorded $5 scoped approval')
    secret=ROOT/config['secret_file'];key=read_private_key(secret)
    destination=ROOT/'private_artifacts/deepseek-pilot';destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    journal=Journal(destination/'ledger.sqlite',5,campaign_id=config['campaign_id'])
    def admission(_):return {'admitted':True,'admission_id':'deepseek-connection-v1',
        'maximum_incremental_charge_usd':reserve,'billing_mode':'metered_api',
        'evidence_refs':[str(Path(args.config).resolve())]}
    transport=OpenAICompatibleTransport('deepseek','deepseek-flash','https://api.deepseek.com/v1',
        'CAPSTUDY_DEEPSEEK_KEY',16,admission=admission,decoding_settings={'thinking':{'type':'disabled'}})
    prior=os.environ.get('CAPSTUDY_DEEPSEEK_KEY')
    try:
        os.environ['CAPSTUDY_DEEPSEEK_KEY']=key
        result=dispatch_editor(journal,{'phase':'connection_preflight','attempt_index':0,'version':1},
            request,transport,artifact_root=destination,upper_charge_usd=reserve,cost_resolver=peak_accounting)
        summary.update(executed=True,response_matches=result.get('response_text','').strip()=='READY',
            generation_status=result.get('generation_status'),returned_model_id=result.get('model_id'),
            usage=result.get('usage'),accounted_upper_charge_usd=result.get('accounted_charge_usd'),
            actual_invoice_charge_usd=None,ledger_path=str(destination/'ledger.sqlite'))
        write_json_atomic(ROOT/'results/study-v1/preflight/deepseek_connection.json',summary)
        print(json.dumps(summary,indent=2));return 0 if summary['response_matches'] else 2
    finally:
        if prior is None:os.environ.pop('CAPSTUDY_DEEPSEEK_KEY',None)
        else:os.environ['CAPSTUDY_DEEPSEEK_KEY']=prior
        journal.close()

if __name__=='__main__':raise SystemExit(main())
