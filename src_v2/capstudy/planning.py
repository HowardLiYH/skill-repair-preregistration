"""Read-only matrices and explicit readiness gates."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def load_config(path):
    c=json.loads(Path(path).read_text());c['_repo_root']=str(ROOT);return c

def instances(config,split):
    root=Path(config['data_root'])
    if not root.is_absolute():root=Path(config.get('_repo_root',ROOT))/root
    return [json.loads(x) for x in (root/split/'instances.jsonl').read_text().splitlines() if x.strip()]

def blockers(config,phase):
    issues=[]
    # No assumption that a positive fee budget is required for a verified local backend.
    if config.get('budget_usd') is None:issues.append('numeric spending ceiling unresolved')
    if config.get('budget_usd')==0 and config.get('execution_backend')!='validated_local':
        issues.append('zero-cost local model backend has not been selected and validated')
    for key in ('artifact_root','chosen_strategy') if phase in ('dev','test') else ('artifact_root',):
        if config.get(key) is None:issues.append(key+' unresolved')
    for group,keys in {'executor':('model_id','cli_version','effective_limits_file','tool_policy_file'),
                       'editor':('provider','model_id','max_output_tokens'),
                       'tokenizer':('model_id','revision')}.items():
        for key in keys:
            if config.get(group,{}).get(key) is None:issues.append(group+'.'+key+' unresolved')
    required=['host_validated','budget_approved']
    if phase!='pilot':required+=['g1_passed','g2_passed','mentor_extension2_approved']
    if phase in ('dev','test'):required+=['g3_frozen']
    for k in required:
        if not config.get('readiness',{}).get(k):issues.append(k+' not demonstrated')
    return issues

def require_ready(config,phase):
    problems=blockers(config,phase)
    if problems:raise ValueError('Execution blocked: '+'; '.join(problems))

def phase_plan(config,phase):
    cases=instances(config,'train' if phase=='calibration' else 'dev')
    return {'evidence_kind':'plan','executed':False,'phase':phase,
            'case_count':len(cases),'family_count':len({x['family'] for x in cases}),
            'maximum_logical_executions':len(cases)*(6 if phase=='calibration' else 6),
            'maximum_editor_generations':32,'budget_usd':config.get('budget_usd'),
            'blockers':blockers(config,phase),'instance_ids':[x['instance_id'] for x in cases]}
