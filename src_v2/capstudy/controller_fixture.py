"""Persistent Section 8.5 demonstration through the real controller/service.

Synthetic source, fixture tokenizer and model/runner boundaries only. No native
benchmark execution, secret reads, network requests, or paid model calls.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from .bundles import load_bundle
from .contracts import canonical_hash, sha256_file, write_json_atomic
from .develop import run_family
from .editor_service import EditorService
from .journal import Journal

MODES = ('feedback_repair', 'retry_no_feedback')
LABELS = ('initial', 'repair1', 'repair2', 'retry01', 'retry02')
CONFIG = {'evidence_kind': 'fixture', 'max_additional_attempts_per_arm': 2,
          'feedback_max_editor_tokens': 4000}
CASES = [{'instance_id':'fixture-family-'+str(i),'family':'fixture-family','split':'dev',
          'instruction':'Synthetic practice instruction '+str(i)} for i in (2,3)]
SCENARIOS = {
    'initial_perfect': {'sizes':[80], 'scores':{'initial':2}, 'expected_calls':1},
    'repair_trigger_independent_retry': {'sizes':[80,80,None,100],
        'labels':['initial','repair1','retry01','retry02'],
        'scores':{'initial':1,'repair1':2,'retry02':0}, 'expected_calls':4},
    'all_ineligible': {'sizes':[None,1500,None,1500,None], 'scores':{}, 'expected_calls':5},
    'length_then_generation_tie': {'sizes':[80,120,80,120,80],
        'scores':{label:1 for label in LABELS}, 'expected_calls':5},
    'all_zero_shortest': {'sizes':[80,120,20,120,30], 'scores':{}, 'expected_calls':5},
}


def append(path, record):
    with Path(path).open('a') as f:
        f.write(json.dumps(record, sort_keys=True, allow_nan=False)+'\n')
        f.flush(); os.fsync(f.fileno())


def demonstrate_scenario(root, name):
    spec = SCENARIOS[name]
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    source_path=root/'source';source_path.mkdir()
    (source_path/'SKILL.md').write_text('---\nname: fixture\n---\n'+'Original reusable procedure.\n'*40)
    (source_path/'support.py').write_text('FIXTURE_SUPPORT = True\n')
    source=load_bundle(source_path)
    journal=Journal(root/'journal.sqlite',0,campaign_id='fixture-'+name,evidence_kind='fixture')
    responses=[]
    for label,size in zip(spec.get('labels',LABELS),spec['sizes']):
        text=None if size is None else '---\nname: fixture\n---\n# '+label+'\n'+'x'*size+'\n'
        responses.append('intentionally malformed fixture JSON' if text is None else json.dumps({'files':{'SKILL.md':text}}))
    calls=[];runner_calls=[];candidate_labels={}
    def transport(request):
        if not responses:raise AssertionError('Unexpected additional fixture generation')
        calls.append(request)
        append(root/'model_calls.jsonl',{'evidence_kind':'fixture','ordinal':len(calls),'request':request})
        return {'response_text':responses.pop(0),'usage':{'input_tokens':13,'output_tokens':5},
                'request_id':'fixture-model-call-'+str(len(calls)),'model_id':'fixture-model',
                'actual_incremental_charge_usd':0,'unavailable_fields':[]}
    def make_service():
        return EditorService(source=source,family='fixture-family',campaign_id='fixture-'+name,
            strategy_id='workflow',config_sha256=canonical_hash(CONFIG),counter=len,
            tokenizer_id='fixture-character-count-not-provider',tokenizer_revision='fixture-v1',
            journal=journal,transport=transport,artifact_root=root/'editor',upper_charge_usd=0,
            payment_mode='fixture',evidence_kind='fixture')
    def run_fixture(case,condition,candidate):
        identity={'instance_id':case['instance_id'],'condition':condition,
                  'candidate_id':candidate['candidate_id'] if candidate else None}
        path=root/'runner'/(canonical_hash(identity)+'.json')
        if path.exists():return json.loads(path.read_text())
        if candidate:
            assert candidate['eligible'], 'Ineligible candidate dispatched to runner'
            label=candidate['files']['SKILL.md'].split('# ',1)[1].splitlines()[0]
            candidate_labels[candidate['candidate_id']]=label
            passes=spec['scores'].get(label,0)
        else:
            label='full';passes=0  # A failed full reference must not remove either case.
        status='pass' if CASES.index(case)<passes else 'task_fail'
        result={'evidence_kind':'fixture',**identity,'family':case['family'],'status':status,
                'native_reward':int(status=='pass'),'fixture_label':label,
                'failed_checks':['synthetic assertion'] if status=='task_fail' else [],
                'trace_excerpt':'Synthetic fixture trace; not a benchmark observation.'}
        write_json_atomic(path,result);runner_calls.append(identity)
        return result
    try:
        service=make_service();initial=service.generate_initial()
        first_events=[]
        def save(event):
            first_events.append(event);append(root/'controller_events.jsonl',{'evidence_kind':'fixture',**event})
        selection=run_family(source.files,initial,CASES,config=CONFIG,validate=service.validate,
            run_trial=run_fixture,edit=service.development_adapter(initial),counter=len,save_event=save)
        write_json_atomic(root/'selections.json',{'evidence_kind':'fixture','selections':selection})
        assert len(calls)==spec['expected_calls'] and not responses
        scheduled=[e for e in first_events if e['event']=='editor_scheduled']
        validated=[e for e in first_events if e['event']=='candidate_validation']
        for e in validated:
            c=e['candidate']
            if not c['eligible']:assert c['candidate_id'] not in candidate_labels
        if name=='initial_perfect':
            assert not scheduled
            assert all(selection[m]['candidate_id']==initial['candidate_id'] for m in MODES)
        elif name=='repair_trigger_independent_retry':
            assert [e['mode'] for e in scheduled]==['feedback_repair','retry_no_feedback','retry_no_feedback']
            assert candidate_labels[selection['feedback_repair']['candidate_id']]=='repair1'
            assert selection['retry_no_feedback']['candidate_id']==initial['candidate_id']
            assert 'dev_feedback' in scheduled[0]['request']
            assert all('dev_feedback' not in e['request'] for e in scheduled[1:])
            assert all(e['request']['shared_dev_instructions']==scheduled[0]['request']['shared_dev_instructions'] for e in scheduled)
        elif name=='all_ineligible':
            assert all(selection[m]['status']=='infeasible' for m in MODES)
            assert len(runner_calls)==2 and initial['files'] is None
            assert all('dev_feedback' not in e['request'] for e in scheduled)
        elif name=='length_then_generation_tie':
            assert all(selection[m]['candidate_id']==initial['candidate_id'] for m in MODES)
        elif name=='all_zero_shortest':
            assert candidate_labels[selection['feedback_repair']['candidate_id']]=='repair2'
            assert candidate_labels[selection['retry_no_feedback']['candidate_id']]=='retry02'
            assert all(selection[m]['dev_passes']==0 for m in MODES)
        # Replay through new service/history objects and persisted fixture trial
        # boundary. This does not claim the production trial-resume gate passes.
        prior_model=len(calls);prior_runner=len(runner_calls)
        service=make_service();again=service.generate_initial()
        replay=run_family(source.files,again,CASES,config=CONFIG,validate=service.validate,
            run_trial=run_fixture,edit=service.development_adapter(again),counter=len,
            save_event=lambda e:append(root/'replay_events.jsonl',{'evidence_kind':'fixture',**e}))
        assert replay==selection and len(calls)==prior_model and len(runner_calls)==prior_runner
        candidates=[json.loads(p.read_text()) for p in (root/'editor/candidates').glob('*/candidate.json')]
        return {'fixture_id':name,'passed':True,'evidence_kind':'fixture','model_boundary_calls':len(calls),
                'runner_boundary_calls':len(runner_calls),'editor_scheduled_attempts':len(scheduled),
                'candidate_count':len(candidates),'invalid_candidate_count':sum(not c['structurally_valid'] for c in candidates),
                'over_cap_candidate_count':sum('over_cap' in c['ineligibility_reasons'] for c in candidates),
                'source_only_initial_count':1,'additional_calls_on_replay':0,
                'selections':selection,'artifact_directory':str(root.resolve())}
    finally:
        journal.close()


def demonstrate(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    results=[demonstrate_scenario(output/name,name) for name in SCENARIOS]
    hashes={str(p.relative_to(output)):sha256_file(p) for p in sorted(output.rglob('*')) if p.is_file()}
    record={'schema_version':1,'evidence_kind':'fixture','entry_point':'capstudy.develop.run_family',
        'editor_adapter':'capstudy.editor_service.EditorService.development_adapter',
        'journal':'capstudy.journal.Journal','token_counter':'synthetic character count; not scientific tokens',
        'model_calls':0,'native_benchmark_executions':0,'scenarios':results,'artifact_sha256':hashes,
        'fixture_demonstration_passed':all(r['passed'] for r in results),'g2_passed':False,
        'limitations':['Synthetic model and normalized runner boundaries; no native grading claimed.',
            'Fixture runner persistence does not validate production executor resume or readiness.',
            'Broader G2 family/capacity/isolation/cost/forecast gates remain separate.']}
    write_json_atomic(output/'demonstration.json',record)
    return record


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(argv);result=demonstrate(args.output)
    print(json.dumps({'fixture_demonstration_passed':result['fixture_demonstration_passed'],
        'scenarios':len(result['scenarios']),'model_calls':0,'g2_passed':False}))

if __name__=='__main__':main()
