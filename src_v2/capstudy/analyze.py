"""Manifest-driven paired family analysis; refuses incomplete matrices."""
from __future__ import annotations
import argparse
import csv
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from .contracts import STATUSES, canonical_hash, read_jsonl, write_json_atomic

class IncompleteMatrix(ValueError): pass

def cell_key(row):
    return (row['instance_id'], row['condition'], row.get('repeat_id', 1))

def validate_matrix(rows, expected_cells, allow_fixtures=False):
    expected = {cell_key(r): r for r in expected_cells}
    if len(expected)!=len(expected_cells): raise IncompleteMatrix('duplicate expected cells')
    seen = {}
    for row in rows:
        key = cell_key(row)
        if key in seen: raise IncompleteMatrix('duplicate result: ' + str(key))
        if key not in expected: raise IncompleteMatrix('unexpected result: ' + str(key))
        if row['family'] != expected[key]['family']: raise IncompleteMatrix('family mismatch: ' + str(key))
        if row['status'] not in STATUSES: raise IncompleteMatrix('invalid status')
        if row['status']=='infra_error': raise IncompleteMatrix('unresolved infrastructure: ' + str(key))
        if row.get('evidence_kind')=='fixture' and not allow_fixtures: raise IncompleteMatrix('fixture is not experimental data')
        seen[key]=row
    missing=set(expected)-set(seen)
    if missing: raise IncompleteMatrix('missing results: '+str(sorted(missing)))
    kinds={r.get('evidence_kind','experiment') for r in rows}
    if len(kinds)>1: raise IncompleteMatrix('mixed fixture and experiment records')
    return seen

def summarize(rows, expected_cells, allow_fixtures=False):
    validate_matrix(rows, expected_cells, allow_fixtures)
    tasks=defaultdict(list)
    for r in rows: tasks[(r['condition'],r['family'],r['instance_id'])].append(int(r['status']=='pass'))
    families=defaultdict(list)
    for (condition,family,instance), values in tasks.items(): families[(condition,family)].append(sum(values)/len(values))
    family_rows=[]; methods=[]
    for (condition,family), values in sorted(families.items()):
        matching=[r for r in rows if r['condition']==condition and r['family']==family]
        family_rows.append(dict(condition=condition,family=family,assigned_tasks=len(values),successes=sum(values),rate=sum(values)/len(values),logical_rows=len(matching),infeasible_rows=sum(r['status']=='infeasible' for r in matching)))
    for condition in sorted({r['condition'] for r in rows}):
        group=[r for r in family_rows if r['condition']==condition]
        assigned=sum(r['assigned_tasks'] for r in group)
        methods.append(dict(condition=condition,macro_rate=sum(r['rate'] for r in group)/len(group),pooled_rate=sum(r['successes'] for r in group)/assigned,family_count=len(group),assigned_tasks=assigned,logical_rows=sum(r['logical_rows'] for r in group),infeasible_rows=sum(r['infeasible_rows'] for r in group)))
    return {'methods':methods,'families':family_rows,'evidence_kind':rows[0].get('evidence_kind','experiment') if rows else None}

def percentile(values, q):
    values=sorted(values); pos=(len(values)-1)*q; lo=math.floor(pos); hi=math.ceil(pos)
    return values[lo]+(values[hi]-values[lo])*(pos-lo)

def exact_sign_test(differences):
    positive=sum(v>0 for v in differences); negative=sum(v<0 for v in differences); n=positive+negative
    p=1.0 if not n else min(1.0, 2*sum(math.comb(n,j) for j in range(min(positive,negative)+1))/2**n)
    return dict(wins=positive,losses=negative,ties=len(differences)-n,non_tied_families=n,p_two_sided=p)

def paired_analysis(rows, repair='feedback_repair', retry='retry_no_feedback', seed=5300, samples=10000):
    relevant=[r for r in rows if r['condition'] in (repair,retry)]
    if not relevant: raise IncompleteMatrix('no paired methods')
    index={cell_key(r):r for r in relevant}
    if len(index)!=len(relevant): raise IncompleteMatrix('duplicate paired rows')
    pair_sets={m:{(r['family'],r['instance_id'],r.get('repeat_id',1)) for r in relevant if r['condition']==m} for m in (repair,retry)}
    if not pair_sets[repair] or pair_sets[repair]!=pair_sets[retry]: raise IncompleteMatrix('paired cells differ')
    task_diff=defaultdict(list)
    for family,instance,repeat in sorted(pair_sets[repair]):
        a=index[(instance,repair,repeat)]; b=index[(instance,retry,repeat)]
        if a['status'] not in ('pass','task_fail','infeasible') or b['status'] not in ('pass','task_fail','infeasible'): raise IncompleteMatrix('unresolved paired result')
        for key in ('instruction_sha256','staged_environment_sha256','execution_config_sha256'):
            if (a.get('evidence_kind')!='fixture' or b.get('evidence_kind')!='fixture'
                    or key in a or key in b):
                if not a.get(key) or a.get(key)!=b.get(key): raise IncompleteMatrix('paired input mismatch: '+key)
        task_diff[(family,instance)].append(int(a['status']=='pass')-int(b['status']=='pass'))
    family_diff=defaultdict(list)
    for (family,_), values in task_diff.items(): family_diff[family].append(sum(values)/len(values))
    effects={f:sum(v)/len(v) for f,v in sorted(family_diff.items())}
    values=list(effects.values()); rng=random.Random(seed)
    if not isinstance(samples,int) or samples<1: raise ValueError('positive bootstrap samples required')
    draws=[sum(rng.choice(values) for _ in values)/len(values) for _ in range(samples)]
    return dict(effect=sum(values)/len(values),effect_percentage_points=100*sum(values)/len(values),family_differences=effects,confidence_interval_95=[percentile(draws,.025),percentile(draws,.975)],bootstrap_seed=seed,bootstrap_samples=samples,bootstrap_unit='family',**exact_sign_test(values))

def write_csv(path, rows):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='') as f:
        if rows:
            w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--frozen',action='append',required=True); p.add_argument('--output',required=True); args=p.parse_args(argv)
    manifests=[json.loads(Path(x).read_text()) for x in args.frozen]
    from .freeze import verify_manifest
    for manifest in manifests: verify_manifest(manifest)
    protocols={m['protocol_sha256'] for m in manifests}
    if len(protocols)!=1: raise ValueError('wave protocol mismatch')
    rows=[]; expected=[]
    for path,m in zip(args.frozen,manifests):
        expected.extend(m['expected_cells'])
        source=Path(m['trial_records_path'])
        if not source.is_absolute(): source=Path(path).resolve().parent/source
        rows.extend(read_jsonl(source))
    # Primary analysis is exactly one execution per task. Additional repeats stay separate.
    primary=[r for r in rows if r.get('repeat_id',1)==1]; primary_expected=[r for r in expected if r.get('repeat_id',1)==1]
    validate_matrix(rows,expected)
    result=summarize(primary,primary_expected); out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
    write_csv(out/'method_summary.csv',result['methods']); write_csv(out/'family_results.csv',result['families'])
    conditions={r['condition'] for r in rows}
    if {'feedback_repair','retry_no_feedback'}<=conditions: write_json_atomic(out/'paired_effects.json',paired_analysis(primary))
    subset_ids=set(manifests[0]['sensitivity_instance_ids'])
    sensitivity=[r for r in primary if r['instance_id'] in subset_ids]
    ss=summarize(sensitivity,[r for r in primary_expected if r['instance_id'] in subset_ids])
    write_csv(out/'sensitivity_14.csv',ss['methods'])
    if {'feedback_repair','retry_no_feedback'}<=conditions: write_json_atomic(out/'sensitivity_paired_effects.json',paired_analysis(sensitivity))
    if any(r.get('repeat_id',1)>1 for r in rows): write_json_atomic(out/'repeat_sensitivity_paired_effects.json',paired_analysis(rows))
    write_json_atomic(out/'analysis_provenance.json',{'manifest_sha256':[canonical_hash(m) for m in manifests],'protocol_sha256':next(iter(protocols)),'evidence_kind':'experiment','primary_repeat_id':1})
    print(json.dumps({'status':'complete','logical_rows':len(primary),'methods':len(result['methods'])}))

if __name__=='__main__': main()
