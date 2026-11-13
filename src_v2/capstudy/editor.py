"""Stateless allowlisted editor inputs and deterministic bounded feedback."""
from __future__ import annotations
import copy
import hashlib
import json
import math

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

COMMON = ('Return one JSON object with the schema {"files": {"path/SKILL.md": "complete text"}}, containing every original SKILL.md path. '
          'Preserve original front matter byte-for-byte and unchanged support assets. '
          'Restore reusable procedures, not practice answers. Obey the family token cap. '
          'Do not invent APIs, thresholds, paths, or rules unsupported by the original source.')

def build_request(original_files, initial_files, current_files, shared_dev_instructions,
                  mode, token_cap, attempt_index, diagnostics=None, feedback=None):
    if mode not in ('feedback_repair','retry_no_feedback'):
        raise ValueError('Unknown editing arm')
    if mode == 'retry_no_feedback' and feedback is not None:
        raise ValueError('Retry must never receive feedback')
    if len(shared_dev_instructions)!=2 or len({x['instance_id'] for x in shared_dev_instructions})!=2:
        raise ValueError('Both assigned dev instructions are required')
    instructions = [{'instance_id':x['instance_id'], 'instruction':x['instruction']}
                    for x in sorted(shared_dev_instructions,key=lambda x:x['instance_id'])]
    # Reject rather than pass arbitrary candidate/evaluator metadata into either arm.
    allowed = {'token_count','token_cap','missing_paths','front_matter_valid','code_fences_valid',
               'local_references_valid','parse_error','support_unchanged'}
    if set(diagnostics or {}) - allowed:
        raise ValueError('Diagnostics contain non-allowlisted fields')
    texts = {'original':copy.deepcopy(original_files),'initial':copy.deepcopy(initial_files)}
    current = current_files if current_files is not None else original_files
    if current == initial_files and initial_files is not None:
        texts['current_ref']='initial'
    elif current == original_files:
        texts['current_ref']='original'
    else:
        texts['current']=copy.deepcopy(current)
    request={'system':COMMON,'bundle_context':texts,'token_cap':token_cap,
             'shared_dev_instructions':instructions,'source_diagnostics':copy.deepcopy(diagnostics or {}),
             'directive':'Produce another concise version using the source and current text above.'}
    if mode=='feedback_repair' and feedback is not None:
        request['directive']='Use failed practice execution evidence to restore missing or distorted general instructions. Do not assume every execution error was caused by compression.'
        request['dev_feedback']=copy.deepcopy(feedback)
    # Attempt/mode provenance is stored outside provider input, not a failure notification.
    return {'request':request,'manifest':{'mode':mode,'attempt_index':attempt_index,
            'request_sha256':digest(request),'instruction_hashes':{x['instance_id']:digest(x['instruction']) for x in instructions}}}

def assemble_feedback(failed_trials, counter, max_tokens=4000):
    """Counter takes serialized text. Whole-field trimming; equal per-case shares."""
    failures=sorted(failed_trials,key=lambda t:t['instance_id'])
    if not failures:
        return None
    if len(failures)>2 or any(t['status']!='task_fail' for t in failures):
        raise ValueError('Only actual, resolved failed dev trials may supply feedback')
    packets=[]
    share=(max_tokens-counter(json.dumps({'cases':[]})))//len(failures)
    for t in failures:
        p={'instance_id':t['instance_id'],'shared_instruction_id':t['instance_id'],
           'candidate_id':t['candidate_id'],'native_reward':0,'omissions':[]}
        for key in ('failed_checks','failure_summary','trace_excerpt','artifact_observations','redactions'):
            if key in t:
                p[key]=copy.deepcopy(t[key])
        # Remove low-priority fields first, leaving complete JSON and identities.
        for key in ('artifact_observations','trace_excerpt','failure_summary','failed_checks','redactions'):
            if counter(json.dumps(p,sort_keys=True))<=share:
                break
            if key in p:
                del p[key]
                p['omissions'].append(key)
        if counter(json.dumps(p,sort_keys=True))>share:
            raise ValueError('Minimum feedback identities cannot fit equal allocation')
        packets.append(p)
    result={'cases':packets}
    if counter(json.dumps(result,sort_keys=True))>max_tokens:
        raise ValueError('Serialized feedback exceeds configured cap')
    return result

def check_capacity(request, counter, context_limit, max_output_tokens, headroom=.10):
    for name, value in (('context_limit', context_limit), ('max_output_tokens', max_output_tokens)):
        if type(value) is not int or value <= 0:
            raise ValueError(name + ' must be a positive integer')
    if isinstance(headroom,bool) or not isinstance(headroom,(int,float)) or not math.isfinite(headroom) or not 0 <= headroom < 1:
        raise ValueError('headroom must be finite and in [0,1)')
    count=counter(json.dumps(request,sort_keys=True,ensure_ascii=False))
    if type(count) is not int or count < 0:
        raise ValueError('Editor count must be a nonnegative integer')
    if count+max_output_tokens > int(context_limit*(1-headroom)):
        raise ValueError('context_limit')
    return count
