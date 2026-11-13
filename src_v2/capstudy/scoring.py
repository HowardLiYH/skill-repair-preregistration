"""Artifact scoring against evaluator-only references, not submitted pass claims."""
from __future__ import annotations
import argparse
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path
from .analyze import cell_key, summarize
from .contracts import canonical_hash, read_jsonl, sha256_file, write_json_atomic

def verify_files(records):
    if not records: raise ValueError('missing hashed evidence')
    for item in records:
        if sha256_file(item['path'])!=item['sha256']: raise ValueError('artifact hash mismatch: '+item['path'])

def score_outputs(outputs,gold,expected_cells,*,regrade=None,fast=False,allow_fixtures=False,workdir=None):
    gold_by_id={g['instance_id']:g for g in gold}
    if len(gold_by_id)!=len(gold): raise ValueError('duplicate gold instance')
    expected={cell_key(c):c for c in expected_cells}; seen=set(); rows=[]; unresolved=[]
    if len(expected)!=len(expected_cells): raise ValueError('duplicate expected cell')
    for output in outputs:
        key=cell_key(output)
        if key not in expected or key in seen: raise ValueError('unexpected/duplicate output cell')
        seen.add(key); cell=expected[key]
        if output['family']!=cell['family']: raise ValueError('output family mismatch')
        if output.get('evidence_kind')=='fixture' and not allow_fixtures: raise ValueError('fixture cannot be scored as experiment')
        if cell.get('infeasible') is True or cell.get('status')=='infeasible':
            health={'status':'infeasible','native_reward':None,'healthy':None}
        else:
            try:
                g=gold_by_id[output['instance_id']]
                # Frozen references bind the gold bytes, independently of submissions.
                if not cell.get('reference_sha256') or cell['reference_sha256']!=g['reference_sha256']: raise ValueError('unbound or mismatched reference hash')
                if not cell.get('execution_signature') or output.get('execution_signature')!=cell['execution_signature']: raise ValueError('execution signature mismatch')
                # Empty outputs can be a genuine task failure; the complete saved
                # evaluator state still receives native grading.
                if output['output_artifacts']: verify_files(output['output_artifacts'])
                if fast:
                    path=output['grading_record_path']
                    if not cell.get('grading_record_sha256') or sha256_file(path)!=cell['grading_record_sha256']: raise ValueError('grading record not independently attested')
                    record=json.loads(Path(path).read_text()); verify_files(record['evidence_files'])
                    if record['reference_sha256']!=g['reference_sha256'] or record['execution_signature']!=cell['execution_signature']: raise ValueError('grading provenance mismatch')
                    from .runner import tree_hash
                    if tree_hash(Path(g['task_path'])/'tests')!=g['reference_sha256']: raise ValueError('reference bytes changed')
                    module_path=Path(__file__).resolve().parents[2]/'milestone 1/code/verifier_health.py'
                    spec=importlib.util.spec_from_file_location('capstudy_native_health',module_path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                    health=module.classify_verifier(g['task_path'],record['verifier_log_path'],record['grader_exit'])
                else:
                    if regrade is None:
                        from .runner import regrade_saved
                        regrade=regrade_saved
                    cell_workdir=Path(workdir)/canonical_hash(list(key)) if workdir is not None else None
                    health=regrade(output,g,cell_workdir)
                if not health.get('healthy') or health['status'] not in ('pass','task_fail'): raise ValueError('unhealthy native grading: '+str(health.get('reason')))
                if health.get('native_reward') != int(health['status']=='pass'): raise ValueError('native reward/status disagreement')
            except (ValueError,KeyError,OSError,RuntimeError,subprocess.SubprocessError) as e:
                health={'status':'infra_error','native_reward':None};unresolved.append({'cell':list(key),'reason':str(e)})
        rows.append({**output,'status':health['status'],'native_reward':health['native_reward']})
    for key in sorted(set(expected)-seen): unresolved.append({'cell':list(key),'reason':'missing_output'})
    if unresolved: return {'complete':False,'unresolved_rows':unresolved,'resolved_rows':len(rows)-sum(r['status']=='infra_error' for r in rows),'scores':None}
    return {'complete':True,'unresolved_rows':[],'scores':summarize(rows,expected_cells,allow_fixtures),'reference_hashes':{g['instance_id']:g['reference_sha256'] for g in gold},'scoring_mode':'verified_cached_native_evidence' if fast else 'native_regrade'}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--outputs',required=True);p.add_argument('--gold',required=True);p.add_argument('--frozen',required=True);p.add_argument('--output',required=True);p.add_argument('--fast',action='store_true');args=p.parse_args(argv)
    frozen=json.loads(Path(args.frozen).read_text())
    from .freeze import verify_manifest
    verify_manifest(frozen)
    with tempfile.TemporaryDirectory(prefix='capstudy-score-') as work:
        result=score_outputs(read_jsonl(args.outputs),read_jsonl(args.gold),frozen['expected_cells'],fast=args.fast,workdir=work)
    write_json_atomic(args.output,result);print(json.dumps({'complete':result['complete'],'unresolved_rows':len(result['unresolved_rows'])}))
    return 0 if result['complete'] else 2
if __name__=='__main__':raise SystemExit(main())
