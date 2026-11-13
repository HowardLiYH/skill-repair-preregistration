"""Complete calibration matrix through injected, persisted boundary adapters.

No credentials, provider calls, Docker commands, or approval changes here.
Production adapters and readiness must be reviewed separately before dispatch.
"""
from __future__ import annotations
import copy
import math
import random
from .contracts import canonical_hash
from .planning import instances, require_ready
from .selection import STRATEGY_ORDER, UnresolvedTrial, choose_strategy


def run_calibration(cases, *, config, generate, validate, run_trial, save_event,
                    verify_base_settings=None, verify_trial_receipt=None):
    """Generate once per family/strategy, then execute a fixed shuffled matrix.

    generate(case,strategy) and run_trial(case,condition,candidate) must be durable
    and idempotent. validate independently checks actual candidate bytes/counts.
    run_trial returns resolved native status and explicitly qualified execution
    cost, including null when unavailable. A failing boundary pauses selection.
    """
    cases=sorted(copy.deepcopy(cases),key=lambda x:x['family'])
    families=[c['family'] for c in cases]
    if not cases or len(set(families))!=len(cases) or len({c['instance_id'] for c in cases})!=len(cases) or any(c['split']!='train' for c in cases):
        raise ValueError('Exactly one distinct calibration case per family required')
    if tuple(config.get('strategy_candidates',()))!=STRATEGY_ORDER:
        raise ValueError('The four fixed strategy identities/order are required')
    if tuple(config.get('calibration_reference_conditions',()))!=('full_skill','no_skill'):
        raise ValueError('Both prescribed calibration references are required')
    seed=config.get('run_order_seed')
    if type(seed) is not int:raise ValueError('Explicit integer run-order seed required')
    if config.get('evidence_kind')!='fixture':
        require_ready(config,'calibration')
        if not callable(verify_trial_receipt):raise ValueError('Independent production trial receipt verifier required')
        assigned=sorted(instances(config,'train'),key=lambda x:x['family'])
        if len(cases)!=8 or cases!=assigned:raise ValueError('Calibration cases differ from eight assigned source records')
        if not callable(verify_base_settings):raise ValueError('Independent frozen-base-setting verification required')
        proof=verify_base_settings(config)
        if not isinstance(proof,dict) or proof.get('verified') is not True or proof.get('config_sha256')!=canonical_hash(config):
            raise ValueError('Frozen calibration base settings failed verification')
    configuration_hash=canonical_hash(config)
    candidates={}; rows={}; references={}; jobs=[]; ids=set(); trial_ids=set()
    save_event({'event':'calibration_started','config_sha256':configuration_hash,
        'instance_ids':[c['instance_id'] for c in cases],'strategy_order':list(STRATEGY_ORDER)})
    # All generation is source-only and finished before task outcomes exist.
    for case in cases:
        for strategy in STRATEGY_ORDER:
            if canonical_hash(config)!=configuration_hash:raise ValueError('Calibration base settings changed')
            save_event({'event':'candidate_scheduled','family':case['family'],'strategy_id':strategy,'attempt_index':0})
            candidate=validate(copy.deepcopy(generate(case,strategy)))
            if (candidate.get('family')!=case['family'] or candidate.get('strategy_id')!=strategy
                    or candidate.get('attempt_index')!=0 or not candidate.get('candidate_id')
                    or candidate['candidate_id'] in ids or type(candidate.get('eligible')) is not bool):
                raise ValueError('Candidate identity/attempt/eligibility differs from scheduled calibration slot')
            ids.add(candidate['candidate_id']);key=(case['family'],strategy);candidates[key]=candidate
            rows[key]={'family':case['family'],'instance_id':case['instance_id'],'strategy_id':strategy,
                'candidate_id':candidate['candidate_id'],'eligible':candidate['eligible'],
                'status':'infeasible' if not candidate['eligible'] else 'pending',
                'executed':False,'cost_usd':None,'execution_cost_basis':None,'trial_id':None}
            save_event({'event':'candidate_validated','candidate':candidate})
            if candidate['eligible']:jobs.append((case,'unchanged_rewrite',strategy))
        jobs.extend((case,condition,None) for condition in ('full_skill','no_skill'))
    random.Random(seed).shuffle(jobs)
    save_event({'event':'execution_order_frozen','seed':seed,'jobs':[{'instance_id':c['instance_id'],
        'condition':condition,'strategy_id':strategy} for c,condition,strategy in jobs]})
    for case,condition,strategy in jobs:
        candidate=candidates[(case['family'],strategy)] if strategy else None
        # Check mutable adapters did not silently change the common settings.
        if canonical_hash(config)!=configuration_hash:raise ValueError('Calibration base settings changed')
        candidate_hash=canonical_hash(candidate)
        result=run_trial(copy.deepcopy(case),condition,copy.deepcopy(candidate))
        if canonical_hash(candidate)!=candidate_hash:raise ValueError('Validated candidate changed')
        if canonical_hash(config)!=configuration_hash:raise ValueError('Calibration base settings changed')
        expected={'instance_id':case['instance_id'],'family':case['family'],'condition':condition,
            'candidate_id':candidate['candidate_id'] if candidate else None}
        if any(result.get(k)!=v for k,v in expected.items()):raise ValueError('Runner returned wrong calibration cell')
        if result.get('status') not in ('pass','task_fail'):
            save_event({'event':'calibration_trial_recorded','strategy_id':strategy,'trial':result})
            raise UnresolvedTrial('Unresolved native execution; preserve incident and pause calibration selection')
        if config.get('evidence_kind')!='fixture':
            from .trial_registry import validate_resolved_record
            validate_resolved_record(result)
            if (result.get('evidence_kind')!='experiment' or result.get('scope')!='formal_calibration'
                    or result.get('campaign_id')!=config.get('campaign_id')
                    or result.get('config_sha256')!=configuration_hash
                    or result.get('usage_settled') is not True
                    or result['trial_id'] in trial_ids):
                raise ValueError('Verified unique formal trial identity/config/settled usage required')
            expected={**expected,'candidate_manifest_sha256':candidate_hash}
            proof=verify_trial_receipt(copy.deepcopy(result),copy.deepcopy(expected),configuration_hash)
            if (not isinstance(proof,dict) or proof.get('verified') is not True
                    or proof.get('trial_sha256')!=canonical_hash(result)
                    or proof.get('cell_sha256')!=canonical_hash(expected)
                    or proof.get('config_sha256')!=configuration_hash
                    or not isinstance(proof.get('receipt_sha256'),str) or not proof['receipt_sha256']):
                raise ValueError('Independent production trial receipt verification failed')
            trial_ids.add(result['trial_id'])
        save_event({'event':'calibration_trial_recorded','strategy_id':strategy,'trial':result})
        if result.get('status') not in ('pass','task_fail'):
            raise UnresolvedTrial('Unresolved native execution; preserve incident and pause calibration selection')
        cost=result.get('cost_usd')
        if cost is not None and (type(cost) not in (int,float) or not math.isfinite(cost) or cost<0):
            raise ValueError('Execution cost must be finite nonnegative measurement or null')
        if strategy:
            rows[(case['family'],strategy)].update(status=result['status'],executed=True,
                trial_id=result.get('trial_id'),cost_usd=result.get('cost_usd'),
                execution_cost_basis=result.get('execution_cost_basis'))
        else:references[(case['family'],condition)]=result
    matrix=[rows[(f,s)] for f in families for s in STRATEGY_ORDER]
    # Cost ties are comparable only under one explicit common measurement basis.
    bases={r['execution_cost_basis'] for r in matrix if r['cost_usd'] is not None}
    ranking_rows=copy.deepcopy(matrix)
    if len(bases)!=1 or None in bases:
        for row in ranking_rows:row['cost_usd']=None
    selection=choose_strategy(ranking_rows,families);winner=selection['chosen_strategy'];opportunities=[]
    for case in cases:
        f=case['family'];candidate=candidates[(f,winner)];rewrite=rows[(f,winner)]
        full=references[(f,'full_skill')];none=references[(f,'no_skill')]
        opportunities.append({'family':f,'calibration_instance_id':case['instance_id'],
            'config_sha256':configuration_hash,'selected_candidate_id':candidate['candidate_id'],
            'full_skill_trial_id':full.get('trial_id'),'no_skill_trial_id':none.get('trial_id'),
            'rewrite_trial_id':rewrite['trial_id'],'full_skill_status':full['status'],
            'no_skill_status':none['status'],'rewrite_status':rewrite['status'],
            'rewrite_eligible':candidate['eligible'],'original_count':candidate.get('original_tokens'),
            'candidate_count':candidate.get('candidate_tokens'),'cap':candidate.get('token_cap'),
            'observed_skill_gain':full['status']=='pass' and none['status']=='task_fail',
            'observed_compression_regression':full['status']=='pass' and rewrite['status']=='task_fail'
                if rewrite['executed'] else None})
    result={'config_sha256':configuration_hash,'selection':selection,'candidate_summary':matrix,
        'reference_results':[references[(f,c)] for f in families for c in ('full_skill','no_skill')],
        'opportunity':opportunities,'selected_candidate_ids':{f:candidates[(f,winner)]['candidate_id'] for f in families},
        'evidence_kind':config.get('evidence_kind','experiment'),'g3_frozen':False}
    save_event({'event':'calibration_complete',**result})
    return result
