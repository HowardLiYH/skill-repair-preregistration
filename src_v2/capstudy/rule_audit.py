"""Source-grounded bounded semantic audit inventory; no automatic truth labels."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from .analyze import write_csv
from .contracts import canonical_hash, write_json_atomic
AUDIT_CONDITIONS=('unchanged_rewrite','feedback_repair','retry_no_feedback','head_truncation','sentence_extraction','llmlingua2')

def inventory(coverage, data_root):
    rows=[]
    for i, group in enumerate(coverage['rule_groups']):
        path=Path(data_root)/group['source']; lines=path.read_text().splitlines(); line=group['source_line']
        if not 1<=line<=len(lines): raise ValueError('invalid source location')
        rows.append({'family':group['family'],'rule_id':f'R{i+1:03d}','source_file':group['source'],'source_location':line,'original_rule':group['rule_group'],'source_excerpt':lines[line-1],'source_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'dev_coverage_direct_partial_none':('direct' if group['status']=='direct output check' else 'partial' if group['status'] in ('partial output check','indirect outcome only') else 'none')})
    return rows

def make_audit_sheet(rules,candidates):
    """Candidate map (family,condition)->record or None. Missing is pending."""
    rows=[]; labels={}; aliases={}
    for rule in rules:
        for condition in AUDIT_CONDITIONS:
            candidate=candidates.get((rule['family'],condition))
            if candidate is None:
                status='pending'; candidate_id=None; digest=None
            elif not candidate.get('eligible'):
                status='not_available'; candidate_id=candidate['candidate_id']; digest=candidate.get('instruction_sha256')
            else:
                status='unreviewed'; candidate_id=candidate['candidate_id']; digest=candidate['instruction_sha256']
            blind_key=canonical_hash([rule['family'],digest if digest else candidate_id,condition if candidate is None else None])[:16]
            labels.setdefault(blind_key,[]).append(condition)
            key=(rule['rule_id'],blind_key)
            if key in aliases: continue
            aliases[key]=True
            rows.append({**rule,'blind_candidate_id':blind_key,'candidate_sha256':digest,'candidate_evidence':'','retained_or_changed_or_missing':status,'invented_rule':'unreviewed' if status=='unreviewed' else status,'reviewer':'','second_reviewer':'','disagreement_note':''})
    return rows, {key:sorted(set(v)) for key,v in labels.items()}

def review_sample(rows,seed=5300):
    flags=[r for r in rows if r['retained_or_changed_or_missing'] in ('missing','changed') or r.get('invented_rule')=='yes']
    retained=[r for r in rows if r['retained_or_changed_or_missing']=='retained' and r.get('invented_rule')!='yes']
    retained.sort(key=lambda r:(r['family'],r['rule_id'],r['blind_candidate_id']))
    sample=random.Random(seed).sample(retained,math.ceil(.2*len(retained)))
    return flags+sample

def summarize_audit(rows):
    if any(r['retained_or_changed_or_missing'] in ('pending','unreviewed') for r in rows): raise ValueError('audit judgments incomplete')
    allowed={'retained','changed','missing','not_available'}
    if any(r['retained_or_changed_or_missing'] not in allowed for r in rows): raise ValueError('invalid audit label')
    for r in rows:
        if r['retained_or_changed_or_missing']!='not_available' and (not r['reviewer'] or not r['candidate_evidence'] or r.get('invented_rule') not in ('yes','no')): raise ValueError('audit needs evidence and two-way claim scan')
    groups=defaultdict(list)
    for r in rows: groups[r['family']].append(r)
    result=[]
    for family, group in sorted(groups.items()):
        c=Counter(r['retained_or_changed_or_missing'] for r in group); available=sum(c[k] for k in ('retained','changed','missing'))
        result.append({'family':family,'retained':c['retained'],'changed':c['changed'],'missing':c['missing'],'not_available':c['not_available'],'available':available,'retention_fraction':c['retained']/available if available else None,'unsupported_addition_flags':sum(r.get('invented_rule')=='yes' for r in group)})
    return result

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--data-root',required=True);p.add_argument('--output',required=True);args=p.parse_args(argv)
    root=Path(args.data_root); rules=inventory(json.loads((root/'practice_coverage.json').read_text()),root)
    rows,labels=make_audit_sheet(rules,{})
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);write_csv(out/'rule_inventory.csv',rules);write_csv(out/'rule_audit_pending.csv',rows)
    write_json_atomic(out/'inventory_status.json',{'rule_groups':len(rules),'conditions':len(AUDIT_CONDITIONS),'planned_judgments':len(rules)*len(AUDIT_CONDITIONS),'status':'pending_candidates_and_human_or_simulated_review','inventory_sha256':canonical_hash(rules),'complete':False})
    print(json.dumps({'rule_groups':len(rules),'planned_judgments':len(rows),'complete':False}))
if __name__=='__main__':main()
