"""The same dev controller is exercised with fixture or live boundary adapters."""
from __future__ import annotations
import argparse
import copy
import json
from .editor import build_request, assemble_feedback
from .selection import dev_passes, select_candidate, fallback_selection, UnresolvedTrial

def run_family(original_files, initial, dev_instances, *, config, validate, run_trial,
               edit, counter, save_event):
    """Adapters: validate(candidate)->candidate; edit(request,mode,index)->candidate.

    Every candidate is independently validated. run_trial(instance,condition,candidate)
    returns a normalized trial. save_event must durably append, or the call aborts.
    """
    cases=sorted(dev_instances,key=lambda x:x['instance_id'])
    if len(cases)!=2 or len({x['instance_id'] for x in cases})!=2 or any(x['split']!='dev' for x in cases):
        raise ValueError('Exactly the two assigned dev cases are required')
    if len({x['family'] for x in cases})!=1 or initial['family']!=cases[0]['family']:
        raise ValueError('Family mismatch')
    if config.get('evidence_kind')!='fixture':
        from .planning import instances
        from pathlib import Path
        assigned={x['instance_id']:x for x in instances(config,'dev') if x['family']==initial['family']}
        if {x['instance_id'] for x in cases}!=set(assigned):
            raise ValueError('Dev IDs differ from the assigned frozen split')
        data=Path(config['data_root'])
        if not data.is_absolute():data=Path(config['_repo_root'])/data
        for x in cases:
            if x['instruction']!=(data/assigned[x['instance_id']]['instruction_path']).read_text():
                raise ValueError('Dev instruction text differs from assigned source bytes')
    if config.get('max_additional_attempts_per_arm')!=2:
        raise ValueError('The frozen protocol allows exactly two additional attempts')
    if config.get('evidence_kind')!='fixture' and not config.get('readiness',{}).get('g3_frozen'):
        raise ValueError('Real dev requires the G3 protocol freeze')
    common=[{'instance_id':x['instance_id'],'instruction':x['instruction']} for x in cases]
    initial=validate(copy.deepcopy(initial))
    if initial['attempt_index']!=0:
        raise ValueError('Shared initial candidate must have generation index zero')
    save_event({'event':'initial_validation','candidate':initial})
    def evaluate(candidate,condition):
        trials=[]
        for x in cases:
            t=run_trial(x,condition,candidate)
            if t.get('instance_id')!=x['instance_id'] or t.get('family')!=x['family']:
                raise ValueError('Runner returned a different assigned case')
            if candidate is not None and t.get('candidate_id')!=candidate['candidate_id']:
                raise ValueError('Runner returned a different candidate')
            if t.get('condition')!=condition:
                raise ValueError('Runner returned a different condition')
            trials.append(t)
        return trials
    full=evaluate(None,'full_skill')
    dev_passes(full)
    save_event({'event':'full_skill_reference','trials':full})
    shared=evaluate(initial,'unchanged_rewrite') if initial['eligible'] else []
    if shared:
        dev_passes(shared)
    save_event({'event':'shared_initial_trials','trials':shared})
    outcomes={initial['candidate_id']:shared}
    selections={}
    for mode in ('feedback_repair','retry_no_feedback'):
        attempts=[initial]
        current=initial.get('files')
        latest=shared
        if not (initial['eligible'] and dev_passes(shared)==2):
            for index in (1,2):
                feedback=assemble_feedback([t for t in latest if t['status']=='task_fail'],counter,
                                           config.get('feedback_max_editor_tokens',4000)) if mode=='feedback_repair' else None
                envelope=build_request(original_files,initial.get('files'),current,common,mode,
                                       initial['token_cap'],index,initial.get('source_diagnostics',{}),feedback)
                save_event({'event':'editor_scheduled','mode':mode,'attempt_index':index,**envelope})
                # Dispatched/lost generation exceptions are not silently converted into scientific zeros.
                try:
                    candidate=validate(edit(envelope['request'],mode,index))
                except Exception as exc:
                    save_event({'event':'editor_incident','mode':mode,'attempt_index':index,
                                'consumed_attempt':True,'error_type':type(exc).__name__})
                    raise
                if candidate['attempt_index']!=index or candidate['family']!=initial['family']:
                    raise ValueError('Editor candidate identity mismatch')
                if any(candidate['candidate_id']==c['candidate_id'] for c in attempts):
                    raise ValueError('Each attempt requires a unique candidate record ID')
                attempts.append(candidate)
                save_event({'event':'candidate_validation','mode':mode,'candidate':candidate})
                if candidate.get('structurally_valid') and candidate.get('files') is not None:
                    current=candidate['files']
                if candidate['eligible']:
                    trials=evaluate(candidate,mode)
                    score=dev_passes(trials)
                    outcomes[candidate['candidate_id']]=trials
                    latest=trials
                    save_event({'event':'candidate_dev_trials','mode':mode,'trials':trials})
                    if score==2:
                        break
        selections[mode]=select_candidate(attempts,outcomes,config)
        save_event({'event':'arm_selection','mode':mode,'selection':selections[mode]})
    selections['full_skill_fallback']=fallback_selection(initial,shared)
    save_event({'event':'family_complete','selections':selections})
    return selections

def main():
    from .planning import load_config, phase_plan, require_ready
    p=argparse.ArgumentParser();p.add_argument('--config',required=True)
    p.add_argument('--plan',action='store_true');p.add_argument('--execute',action='store_true')
    a=p.parse_args();c=load_config(a.config)
    if a.execute:
        require_ready(c,'dev')
        raise SystemExit('Use run_family with reviewed, persisted model/runtime adapters; live integration is not validated yet.')
    print(json.dumps(phase_plan(c,'dev'),indent=2))

if __name__=='__main__':main()
