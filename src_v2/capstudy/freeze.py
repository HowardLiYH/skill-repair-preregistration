"""Immutable protocol/candidate evidence and split-derived expected cells."""
from __future__ import annotations
import argparse
import hashlib
import json
import random
from pathlib import Path
from .editor import digest
from .planning import instances,load_config,require_ready

def required_protocol_files(config):
    root=Path(config['_repo_root'])
    files=[root/'pyproject.toml',root/'configs/experiment.json',root/'configs/source_versions.json']
    for directory,pattern in (('src/capstudy','*.py'),('prompts','*'),('schemas','*.json')):
        files.extend(p for p in (root/directory).rglob(pattern) if p.is_file())
    data=Path(config['data_root'])
    if not data.is_absolute():data=root/data
    files.extend(data/s/'instances.jsonl' for s in ('train','dev','test'))
    files.extend(data/p for p in ('evaluation_manifest.json','practice_coverage.json'))
    for key in ('feedback_policy_file','sentence_policy_file','infra_retry_policy_file','pricing_record_file'):
        value=config.get(key)
        if not value:raise ValueError('Required frozen policy unresolved: '+key)
        p=Path(value);files.append(p if p.is_absolute() else root/p)
    for key in ('effective_limits_file','tool_policy_file'):
        p=Path(config['executor'][key]);files.append(p if p.is_absolute() else root/p)
    return {str(p.resolve()) for p in files}

def file_evidence(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file():raise ValueError('Freeze requires real regular files: '+str(p))
    return {'path':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
            'mode':p.stat().st_mode & 0o777}

def expected_cells(config,wave):
    if wave not in ('A','B'):raise ValueError('Unknown wave')
    cases=instances(config,'test')
    if len(cases)!=19 or len({c['instance_id'] for c in cases})!=19:
        raise ValueError('Primary split must have exactly 19 unique cases')
    cells=[{'instance_id':c['instance_id'],'family':c['family'],'condition':m,'repeat_id':1}
           for c in cases for m in config['test_waves'][wave]]
    random.Random(config['run_order_seed']).shuffle(cells)
    return cells

def create_manifest(config,wave,candidates,protocol_files,project_commit,previous=None,
                    *, validate_candidate, cell_bindings, trial_records_path):
    require_ready(config,'test')
    if not project_commit or len(project_commit)!=40:raise ValueError('Exact project commit required')
    cells=expected_cells(config,wave)
    expected={(c['family'],c['condition']) for c in cells}
    keys=[(c['family'],c['condition']) for c in candidates]
    if len(keys)!=len(set(keys)) or set(keys)!=expected:
        raise ValueError('Every family/condition requires one explicit candidate or control')
    supplied={str(Path(p).resolve()) for p in protocol_files}
    missing=required_protocol_files(config)-supplied
    if missing:raise ValueError('Missing required protocol files: '+', '.join(sorted(missing)))
    evidence=[file_evidence(p) for p in sorted(supplied)]
    if not evidence:raise ValueError('Protocol files must include code, prompts, config and source revisions')
    protocol=digest(evidence)
    if wave=='B' and (previous is None or previous['protocol_sha256']!=protocol):
        raise ValueError('Wave B must retain Wave A protocol exactly')
    frozen=[]
    for c in candidates:
        c=dict(c)
        if c.get('status') not in ('eligible','infeasible','control'):
            raise ValueError('Unresolved candidate status')
        if c['status']=='control' and c['condition'] not in ('full_skill','no_skill','full_skill_fallback'):
            raise ValueError('Compressed condition cannot bypass eligibility as control')
        if c['condition']=='full_skill_fallback':
            if not c.get('dev_only_reason'):raise ValueError('Fallback needs a dev-only decision reason')
            if c['status']=='control' and c.get('uses_full_skill') is not True:
                raise ValueError('Compressed fallback requires independent eligibility validation')
        if c['status']=='infeasible' and not c.get('ineligibility_reasons'):
            raise ValueError('Infeasibility requires reasons')
        if c['status']=='eligible':
            checked=validate_candidate(c)
            if not checked['eligible'] or checked['candidate_tokens']>checked['token_cap']:
                raise ValueError('Independent candidate validation rejected freeze')
            for key in ('candidate_tokens','token_cap'):
                if checked[key]!=c[key]:raise ValueError('Candidate validation/count disagreement')
        if c['status']!='infeasible' and c['condition']!='no_skill':
            bundle=Path(c['bundle_path'])
            paths=sorted(p for p in bundle.rglob('*') if p.is_file())
            if not paths:raise ValueError('Missing candidate bytes')
            c['files']=[file_evidence(p) for p in paths]
        frozen.append(c)
    by_key={(c['family'],c['condition']):c for c in frozen}
    bindings={(b['instance_id'],b['condition'],b['repeat_id']):b for b in cell_bindings}
    if len(bindings)!=len(cell_bindings):raise ValueError('Duplicate scoring binding')
    for cell in cells:
        candidate=by_key[cell['family'],cell['condition']]
        if candidate['status']=='infeasible':cell['status']='infeasible'
        else:
            binding=bindings.get((cell['instance_id'],cell['condition'],cell['repeat_id']),{})
            for key in ('reference_sha256','execution_signature'):
                value=binding.get(key)
                if not isinstance(value,str) or len(value)!=64:raise ValueError('Missing frozen '+key)
                cell[key]=value
    if not trial_records_path:raise ValueError('Persistent trial ledger path required before freeze')
    data=Path(config['data_root'])
    if not data.is_absolute():data=Path(config['_repo_root'])/data
    evaluation=json.loads((data/'evaluation_manifest.json').read_text())
    sensitivity=[x['instance_id'] for x in evaluation['instances'] if x['changed_input_sensitivity']]
    if len(sensitivity)!=14:raise ValueError('Sensitivity split mismatch')
    manifest={'schema_version':1,'evidence_kind':'experiment_manifest','wave':wave,
              'project_commit':project_commit,'protocol_sha256':protocol,'protocol_files':evidence,
              'config_sha256':digest({k:v for k,v in config.items() if not k.startswith('_')}),
              'candidates':frozen,'expected_cells':cells,'trial_records_path':str(trial_records_path),
              'sensitivity_instance_ids':sensitivity}
    manifest['manifest_sha256']=digest(manifest)
    return manifest

def verify_manifest(manifest):
    body={k:v for k,v in manifest.items() if k!='manifest_sha256'}
    if digest(body)!=manifest.get('manifest_sha256'):raise ValueError('Manifest hash changed')
    if digest(manifest['protocol_files'])!=manifest['protocol_sha256']:raise ValueError('Protocol hash mismatch')
    for f in manifest['protocol_files']+[f for c in manifest['candidates'] for f in c.get('files',[])]:
        if file_evidence(f['path'])!=f:raise ValueError('Frozen file changed: '+f['path'])
    for c in manifest['candidates']:
        if c.get('files'):
            current={str(p.resolve()) for p in Path(c['bundle_path']).rglob('*') if p.is_file()}
            if current!={f['path'] for f in c['files']}:raise ValueError('Frozen bundle file set changed')
    return True

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--wave',choices=['A','B'],required=True)
    p.add_argument('--output',required=True);a=p.parse_args();c=load_config(a.config)
    require_ready(c,'test')
    raise SystemExit('Candidate and protocol evidence must be reviewed before create_manifest can freeze a wave.')
if __name__=='__main__':main()
