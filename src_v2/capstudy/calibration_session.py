"""Persist calibration order and replay verified receipts through the controller.

This command never calls a model or launches a task. All 32 source-only editor
slots must already be complete. Separate one-trial launchers fill its manifest.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import random
import uuid
from .contracts import canonical_hash,sha256_file,write_json_atomic
from .compress.xing import STRATEGIES
from .source_only_generation import FAMILIES
from .formal_generation import make_generator
from .formal_calibration_gateway import validate_scope,ACCOUNTING_CAMPAIGN
from .formal_gateway_server import open_existing_journal
from .calibration_controller import run_calibration
from .formal_receipt import load_bound,check_seal,make_receipt_verifier
from .selection import UnresolvedTrial


def execution_order(cases,candidates,config):
    cases=sorted(copy.deepcopy(cases),key=lambda r:r['family'])
    if len(cases)!=8 or [x['family'] for x in cases]!=list(FAMILIES):raise ValueError('Exactly eight train families required')
    if any(x['instance_id']!=x['family']+'-1' or x['split']!='train' for x in cases):raise ValueError('Calibration train records only')
    expected={(f,s) for f in FAMILIES for s in STRATEGIES}
    if set(candidates)!=expected:raise ValueError('All32 candidate attempts must resolve before any execution')
    ids=[c['candidate_id'] for c in candidates.values()]
    if len(ids)!=len(set(ids)):raise ValueError('Distinct source-only attempt identities required')
    jobs=[];infeasible=[]
    for case in cases:
        for strategy in STRATEGIES:
            candidate=candidates[case['family'],strategy]
            if (candidate.get('family')!=case['family'] or candidate.get('strategy_id')!=strategy
                    or candidate.get('attempt_index')!=0 or type(candidate.get('eligible')) is not bool):
                raise ValueError('Resolved candidate identity differs')
            if candidate['eligible']:jobs.append((case,'unchanged_rewrite',strategy,candidate))
            else:infeasible.append({'family':case['family'],'strategy_id':strategy,'candidate_id':candidate['candidate_id'],
                'status':'infeasible','executed':False,'ineligibility_reasons':candidate['ineligibility_reasons']})
        jobs.extend((case,condition,None,None) for condition in ('full_skill','no_skill'))
    if type(config.get('run_order_seed')) is not int:raise ValueError('Frozen integer seed required')
    random.Random(config['run_order_seed']).shuffle(jobs)
    ordered=[]
    for index,(case,condition,strategy,candidate) in enumerate(jobs):
        identity={'campaign_id':config['campaign_id'],'config_sha256':canonical_hash(config),
            'instance_id':case['instance_id'],'family':case['family'],'condition':condition,'strategy_id':strategy,
            'candidate_id':candidate['candidate_id'] if candidate else None,
            'candidate_manifest_sha256':canonical_hash(candidate),'repeat_id':1}
        ordered.append({**identity,'order_index':index,'physical_trial_id':'cal-'+canonical_hash(identity)[:32]})
    return {'schema_version':1,'kind':'formal-calibration-execution-order-v1',
        'scientific_campaign_id':config['campaign_id'],'config_sha256':canonical_hash(config),
        'run_order_seed':config['run_order_seed'],'maximum_logical_cells':48,'jobs':ordered,'infeasible':infeasible}


def load_completed(scope_path,base_path,journal):
    scope,_=validate_scope(scope_path,base_path)
    if scope.get('source_generation_adoption'):
        from .source_adoption import load_adopted
        return load_adopted(scope,json.loads(Path(base_path).read_text()))
    root=Path(scope['dispatch_storage_policy']['root'])/'generation'
    summary_path=root/'generation_completed.json'
    if not summary_path.is_file() or summary_path.is_symlink():raise ValueError('All32 initial generations must complete first')
    summary=json.loads(summary_path.read_text())
    if summary.get('completed') is not True or len(summary.get('cells',[]))!=32:raise ValueError('Incomplete generation matrix')
    generator=make_generator(scope_path,base_path,journal,planning_only=False,
        protocol_sha256=scope['source_only_generation']['generation_config_sha256'])
    generator._assert_binding()
    if summary['binding_sha256']!=generator.binding['binding_sha256']:raise ValueError('Generation summary binding changed')
    candidates={}
    for family in FAMILIES:
        for strategy in STRATEGIES:
            cell=json.loads((root/family/strategy/'cell.json').read_text())
            candidate=load_bound({'path':cell['candidate_path'],'sha256':cell['candidate_sha256']})
            service=generator.services[family,strategy]
            expected_path=service.root/'candidates'/candidate['candidate_id']/'candidate.json'
            if (Path(cell['candidate_path'])!=expected_path or not expected_path.resolve().is_relative_to(service.root)
                    or Path(candidate['request_path'])!=expected_path.with_name('request.json')):
                raise ValueError('Candidate path differs from its immutable generation service')
            completion=json.loads(expected_path.with_name('completion.json').read_text())
            if (completion.get('candidate_sha256')!=cell['candidate_sha256']
                    or completion.get('binding_sha256')!=sha256_file(expected_path.with_name('binding.json'))):
                raise ValueError('Generation completion seal changed')
            candidate=service.validate(candidate)
            expected_cell={'schema_version':1,'binding_sha256':generator.binding['binding_sha256'],
                'scientific_campaign_id':generator.scientific_campaign_id,'accounting':generator.binding['accounting'],
                'family':family,'strategy_id':strategy,'candidate_id':candidate['candidate_id'],
                'candidate_path':str(expected_path),'candidate_sha256':sha256_file(expected_path),
                'independent_source_tokens':candidate['original_tokens'],
                'independent_candidate_tokens':candidate['candidate_tokens'] if candidate.get('bundle_path') else None,
                'eligible':candidate['eligible'],'ineligibility_reasons':candidate['ineligibility_reasons']}
            if cell!=expected_cell:raise ValueError('Persisted independent generation receipt differs')
            matching=[r for r in summary['cells'] if r['family']==family and r['strategy_id']==strategy]
            expected_summary={k:candidate[k] for k in ('family','strategy_id','candidate_id','eligible','candidate_tokens','ineligibility_reasons')}
            if matching!=[expected_summary]:
                raise ValueError('Completed generation cell differs')
            candidates[family,strategy]=candidate
    if summary.get('candidate_ids')!=[candidates[f,s]['candidate_id'] for f in FAMILIES for s in STRATEGIES]:
        raise ValueError('Completed matrix candidate inventory differs')
    return generator,candidates


def record_for_job(job,refs):
    physical_id=job['physical_trial_id']
    if physical_id not in refs:raise UnresolvedTrial('Missing independently sealed physical receipt: '+physical_id)
    receipt=load_bound(refs[physical_id]);check_seal(receipt,'receipt_sha256')
    record=receipt['record']
    if record.get('trial_id')!=physical_id or record.get('actual_trial_id')!=physical_id:
        raise ValueError('Receipt index cannot alias another physical trial')
    return copy.deepcopy(record)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scope',type=Path,required=True);p.add_argument('--base-config',type=Path,required=True)
    p.add_argument('--session-root',type=Path,required=True);p.add_argument('--receipt-index',type=Path)
    p.add_argument('--analyze',action='store_true');a=p.parse_args(argv)
    scope,_=validate_scope(a.scope,a.base_config);config=json.loads(a.base_config.read_text())
    # The existing ledger is opened only after approved scope and matrix presence.
    if not scope.get('source_generation_adoption') and not (Path(scope['dispatch_storage_policy']['root'])/'generation/generation_completed.json').is_file():
        raise ValueError('Complete source-only generation matrix first')
    accounting=Path(scope['accounting']['journal_path'])
    if not accounting.is_absolute():accounting=a.scope.resolve().parent/accounting
    journal=open_existing_journal(accounting.resolve())
    try:
        generator,candidates=load_completed(a.scope,a.base_config,journal)
        cases=[json.loads(line) for line in generator.train_manifest.read_text().splitlines() if line.strip()]
        order=execution_order(cases,candidates,config)
        a.session_root.mkdir(parents=True,exist_ok=True,mode=0o700)
        manifest=a.session_root/'execution-order.json'
        if manifest.exists():
            if json.loads(manifest.read_text())!=order:raise ValueError('Frozen execution order changed')
        else:write_json_atomic(manifest,order)
        if not a.analyze:
            print(json.dumps({'model_calls':0,'physical_trials':len(order['jobs']),'infeasible_cells':len(order['infeasible']),
                'manifest':str(manifest.resolve())}));return 0
        if a.receipt_index is None:p.error('--analyze requires --receipt-index')
        refs=json.loads(a.receipt_index.read_text());verify=make_receipt_verifier(refs)
        by_cell={(r['instance_id'],r['condition'],r['candidate_id']):r for r in order['jobs']}
        def physical(case,condition,candidate):
            job=by_cell[case['instance_id'],condition,candidate['candidate_id'] if candidate else None]
            return record_for_job(job,refs)
        def frozen(current):
            validate_scope(a.scope,a.base_config)
            return {'verified':canonical_hash(current)==scope['base_config_sha256'],'config_sha256':canonical_hash(current)}
        review=a.session_root/('review-'+uuid.uuid4().hex);review.mkdir(mode=0o700)
        events=[]
        def event(value):
            events.append(value);write_json_atomic(review/'events.json',events)
        result=run_calibration(cases,config=config,generate=lambda case,strategy:copy.deepcopy(candidates[case['family'],strategy]),
            validate=lambda c:generator.services[c['family'],c['strategy_id']].validate(c),run_trial=physical,save_event=event,
            verify_base_settings=frozen,verify_trial_receipt=verify)
        write_json_atomic(review/'calibration-result.json',result)
        print(json.dumps({'model_calls':0,'complete':True,'result':str((review/'calibration-result.json').resolve())}));return 0
    finally:journal.close()


if __name__=='__main__':raise SystemExit(main())
