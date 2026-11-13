"""Reproduce a scoped planning forecast from saved evidence; never dispatch.

Only completed, hash-bound trial/usage pairs become observed trial rows. Token
and time scenarios are assumptions, not confidence bounds or provider quotes.
The campaign ledger is opened read-only and no credential file is accessed.
"""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from .contracts import canonical_hash, sha256_file, write_json_atomic

ROOT = Path(__file__).resolve().parents[2]
RATES = {'uncached_input': Decimal('.30'), 'cached_input': Decimal('.006'),
         'output': Decimal('1.20')}
PHASES = {'calibration': {'logical_executions': 48, 'generation_calls': 32},
          'dev': {'logical_executions': 96, 'generation_calls': 32},
          'wave_a': {'logical_executions': 95, 'generation_calls': 0},
          'wave_b': {'logical_executions': 76, 'generation_calls': 8}}
SCENARIOS = {
    'light_assumption': {'executor_tokens': (25000, 50000, 2000),
                        'generation_tokens': (5000, 0, 1500), 'minutes_per_execution': 3},
    'planning_assumption': {'executor_tokens': (100000, 300000, 10000),
                           'generation_tokens': (25000, 0, 10000), 'minutes_per_execution': 10},
    'stress_assumption_not_worst_case': {'executor_tokens': (500000, 1000000, 50000),
                                       'generation_tokens': (100000, 0, 16000), 'minutes_per_execution': 30}}


def cost(tokens):
    return sum(Decimal(n)*RATES[k] for n,k in zip(tokens, RATES)) / Decimal(1000000)


def load(path):
    return json.loads(Path(path).read_text())


def completed_trials(root):
    rows = []
    configs = {canonical_hash(load(p)): (p, load(p)) for p in
               (root/'private_artifacts/remote-gdp-solver-pilot').glob('*config*.json')}
    for trial_path in sorted((root/'private_artifacts').glob('gdp-*-evidence/completed.json')):
        usage_path = trial_path.parent/'central-usage.json'
        if not usage_path.exists():
            continue  # A partial/live run is not a completed cost observation.
        trial, usage = load(trial_path), load(usage_path)
        if (trial.get('job_status') != 'completed' or trial.get('executed') is not True
                or trial.get('status') not in ('pass','task_fail')
                or usage.get('trial_sha256') != sha256_file(trial_path)
                or usage.get('trial_id') != trial.get('trial_id')
                or usage.get('attachment_sha256') != canonical_hash({k:v for k,v in usage.items() if k!='attachment_sha256'})):
            raise ValueError('Completed pilot evidence binding failed')
        final = trial.get('trace_evidence',{}).get('raw_result_event') or {}
        config_path, config = configs[trial['config_sha256']]
        config_turns = config['executor']['max_steps']
        rows.append({'trial_id':trial['trial_id'], 'condition':trial['condition'],
            'native_status':trial['status'], 'native_reward':trial['native_reward'],
            'skill_use':trial.get('skill_use','unknown'),
            'executor_health':trial.get('executor_health'), 'configured_max_turns':config_turns,
            'cli_num_turns':final.get('num_turns'), 'termination':final.get('subtype'),
            'whole_trial_seconds':trial['duration_seconds'],
            'cli_duration_seconds':final['duration_ms']/1000 if 'duration_ms' in final else None,
            'cli_api_duration_seconds_diagnostic':final['duration_api_ms']/1000 if 'duration_api_ms' in final else None,
            'physical_calls':usage['physical_call_count'], 'usage':usage['usage'],
            'accounted_upper_usd':usage['accounted_upper_usd'], 'actual_invoice_usd':None,
            'sources':{str(trial_path.relative_to(root)):sha256_file(trial_path),
                       str(usage_path.relative_to(root)):sha256_file(usage_path),
                       str(config_path.relative_to(root)):sha256_file(config_path)}})
    return rows


def completed_editor_checks(root):
    rows = []
    for summary_path in sorted((root/'results/study-v1/preflight').glob('editor_*.json')):
        summary = load(summary_path)
        candidate_path = Path(summary['candidate_path'])
        candidate = load(candidate_path)
        call_path = Path(candidate['call_record_path'])
        call = load(call_path)
        if not call.get('completed_at') or call['call_id'] not in summary['call_ids']:
            raise ValueError('Editor completion binding failed')
        raw = call['usage']['raw']
        rows.append({'family':summary['family'], 'candidate_id':candidate['candidate_id'],
            'prompt_version':summary['binding'].get('rewrite_prompt_version','v1'), 'eligible':summary['eligible'],
            'ineligibility_reasons':summary['ineligibility_reasons'],
            'candidate_tokens':summary['candidate_tokens'], 'token_cap':summary['token_cap'],
            'uncached_input_tokens':raw['prompt_cache_miss_tokens'],
            'cached_input_tokens':raw['prompt_cache_hit_tokens'], 'output_tokens':raw['completion_tokens'],
            'accounted_upper_usd':call['accounted_charge_usd'], 'actual_invoice_usd':None,
            'sources':{str(p.relative_to(root)):sha256_file(p) for p in (summary_path,candidate_path,call_path)}})
    return rows


def ledger_snapshot(root):
    path = root/'private_artifacts/deepseek-pilot/ledger.sqlite'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Existing regular journal required')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON'); conn.execute('BEGIN')
        metadata = dict(conn.execute('SELECT key,value FROM metadata'))
        jobs = [dict(r) for r in conn.execute('SELECT * FROM jobs ORDER BY job_id')]
        events = [dict(r) for r in conn.execute('SELECT * FROM events ORDER BY sequence')]
    spent = sum((Decimal(r['charge']) for r in jobs if r['charge'] is not None),Decimal(0))
    reserved = sum((Decimal(r['reservation']) for r in jobs if r['state'] in
                    ('pending','running','needs_reconciliation','usage_unavailable')),Decimal(0))
    return {'observed_at_utc':datetime.now(timezone.utc).isoformat(),
        'campaign_id':metadata['campaign_id'], 'approved_api_pilot_ceiling_usd':metadata['budget_usd'],
        'accounted_upper_spend_usd':str(spent), 'reserved_usd':str(reserved),
        'uncommitted_headroom_usd':str(Decimal(metadata['budget_usd'])-spent-reserved),
        'job_states':dict(Counter(r['state'] for r in jobs)), 'actual_invoice_usd':None,
        'snapshot_sha256':canonical_hash({'metadata':metadata,'jobs':jobs,'events':events}),
        'api_accounting_snapshot_does_not_establish_trial_completion':True}


def build(root=ROOT):
    root=Path(root)
    host=load(root/'results/study-v1/preflight/remote_host.json')
    inventory=load(root/'results/study-v1/preflight/g2_readiness_inventory.json')
    hourly=Decimal(str(host['displayed_combined_hourly_usd_ex_tax_approx']))
    scenarios=[]
    for name, assumption in SCENARIOS.items():
        executor, editor = cost(assumption['executor_tokens']), cost(assumption['generation_tokens'])
        phases={p:str(v['logical_executions']*executor+v['generation_calls']*editor) for p,v in PHASES.items()}
        hours=Decimal(315*assumption['minutes_per_execution'])/60+4
        scenarios.append({'name':name,'evidence_kind':'explicit_assumption_not_measurement',
            'token_tuple_order':['uncached_input','cached_input','output'], **assumption,
            'executor_cost_usd':str(executor),'generation_cost_usd':str(editor),'phase_api_costs_usd':phases,
            'total_api_usd':str(sum(map(Decimal,phases.values()))),
            'future_nonexecution_hours_assumed':4,'serial_host_hours_assumed':str(hours),
            'host_cost_ex_tax_approx_usd':str(hours*hourly),
            'optional_76_repeat_execution_api_usd':str(76*executor)})
    cases=Counter(json.loads(line)['family'] for line in
        (root/'milestone 1/data/test/instances.jsonl').read_text().splitlines() if line.strip())
    family_counts={family:18+9*n for family,n in sorted(cases.items())}
    assert sum(family_counts.values()) == sum(p['logical_executions'] for p in PHASES.values()) == 315
    assert sum(p['generation_calls'] for p in PHASES.values()) == 72
    result={'schema_version':1,'evidence_kind':'preliminary_planning_forecast',
        'formal_phase_authorization_granted':False,'workflow_complete':False,
        'sources':{p:sha256_file(root/p) for p in ('docs/EXECUTION_PLAN.md',
            'results/study-v1/preflight/remote_host.json',
            'results/study-v1/preflight/g2_readiness_inventory.json','milestone 1/data/test/instances.jsonl')},
        'tariff':{'observed_date':'2026-11-06','source':'https://api-docs.deepseek.com/quick_start/pricing/',
            'model':'deepseek-flash','peak_usd_per_million':{k:str(v) for k,v in RATES.items()},
            'actual_invoice_known':False},
        'workload':{'phases':PHASES,'max_logical_executions':315,'max_generation_calls':72,
            'per_family_logical_executions':family_counts,'optional_repeat_logical_executions':76,
            'excluded':['setup pilots','infrastructure retries','optional analyses','unplanned regeneration'],
            'reuse_and_ineligibility_can_reduce_physical_work':True,
            'turn_limit_is_not_a_proven_physical_request_count_limit':True},
        'observed_completed_trials':completed_trials(root),
        'observed_completed_editor_checks':completed_editor_checks(root),
        'family_readiness_not_measurements':[{'family':f['family'],
            'logical_executions':family_counts[f['family']],
            'declared_limits_seconds':f['declared_limits_seconds'],
            'declared_resources':f['declared_resources'],
            'g2_status':f['g2_status'], 'observed_cost_time':None}
            for f in inventory['families']],
        'ledger_snapshot':ledger_snapshot(root),
        'scenarios':scenarios,
        'hosting':{'separate_approved_ceiling_usd':10,'approx_hourly_ex_tax_usd':str(hourly),
            'server_plus_ipv4_monthly_cap_ex_tax_usd':str(Decimal(str(host['server_monthly_cap_usd_ex_tax']))+
                Decimal(str(host['ipv4_monthly_cap_usd_ex_tax']))),
            '75_billed_hours_ex_tax_usd':str(75*hourly),'automatic_deletion':False,
            'billing_rule_source':'https://docs.hetzner.com/cloud/billing/faq/',
            'existing_elapsed_idle_hours_and_tax_excluded_from_future_scenarios':True},
        'smallest_next_authorization_proposal':{
            'scope':'formal calibration only, after G2 and prospective protocol freeze requirements pass',
            'proposed_combined_api_ceiling_usd':5,'new_money_required_now':False,
            'interpretation':'Request scope extension to use remaining existing pilot allowance; do not assume approved.',
            'completion_guaranteed':False,'stop_if_remaining_reservation_does_not_fit':True,
            'full_study_budget_decision':'Defer until all eight families supply calibration cost/time observations.'},
        'limitations':['Completed GDP observations cover one family only; these are not cross-family cost quantiles.',
            'Scenario tokens/time are assumptions, not confidence limits or worst-case bounds.',
            'Editor feasibility revisions are separate pilot attempts; their validity does not choose the formal strategy.',
            'Cold-build and cached-build timings must not be conflated.',
            'Budget is admission control, not a provider-enforced invoice guarantee.',
            'CNY100 funded balance is not approval to increase the scoped $5 pilot ceiling.',
            'Full-study evaluator snapshot retention needs deduplication/offload; per-trial archives cannot scale unbounded.']}
    result['forecast_sha256']=canonical_hash(result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=ROOT/'results/study-v1/preflight/preliminary_cost_forecast.json')
    a=p.parse_args();result=build();write_json_atomic(a.output,result)
    print(json.dumps({'output':str(a.output),'completed_trial_observations':len(result['observed_completed_trials']),
        'logical_executions':315,'generation_calls':72,'api_spend_snapshot':result['ledger_snapshot']['accounted_upper_spend_usd']}))

if __name__=='__main__':main()
