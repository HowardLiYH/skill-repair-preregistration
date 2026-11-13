"""Read-only wave validation. No editor is reachable from this module."""
import argparse
import json
from pathlib import Path
from .freeze import verify_manifest

def run_frozen(manifest,run_trial,save_row,conditions=None):
    verify_manifest(manifest)
    chosen=set(conditions or {c['condition'] for c in manifest['expected_cells']})
    known={c['condition'] for c in manifest['expected_cells']}
    if not chosen<=known:raise ValueError('Condition not present in frozen wave')
    candidates={(c['family'],c['condition']):c for c in manifest['candidates']}
    rows=[]
    for cell in manifest['expected_cells']:
        if cell['condition'] not in chosen:continue
        verify_manifest(manifest)
        candidate=candidates[cell['family'],cell['condition']]
        if candidate['status']=='infeasible':
            row={**cell,'status':'infeasible','candidate_id':candidate.get('candidate_id'),
                 'native_reward':None,'executed':False,'ineligibility_reasons':candidate['ineligibility_reasons']}
        else:row=run_trial(cell,candidate)
        if any(row.get(k)!=v for k,v in cell.items()):raise ValueError('Returned trial cell mismatch')
        save_row(row);rows.append(row)
        if row['status']=='infra_error':raise RuntimeError('Unresolved infrastructure error blocks wave')
    return rows

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--frozen',required=True);p.add_argument('--conditions',nargs='+')
    p.add_argument('--plan',action='store_true');p.add_argument('--execute',action='store_true')
    a=p.parse_args(argv);m=json.loads(Path(a.frozen).read_text());verify_manifest(m)
    known={c['condition'] for c in m['expected_cells']}
    if a.conditions and not set(a.conditions)<=known:raise SystemExit('Unknown condition in frozen wave')
    cells=[c for c in m['expected_cells'] if not a.conditions or c['condition'] in a.conditions]
    if a.execute:raise SystemExit('Live wave dispatch awaits reviewed local/provider adapter integration.')
    print(json.dumps({'evidence_kind':'plan','executed':False,'wave':m['wave'],'logical_rows':len(cells),'expected_cells':cells},indent=2))
if __name__=='__main__':main()
