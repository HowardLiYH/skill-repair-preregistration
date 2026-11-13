"""Read-only adoption of an entire completed source matrix; never dispatches.

Original candidate records keep their original campaign, configuration and IDs.
The prospective scope/base seals a separate adoption manifest and v2 runtime.
"""
from __future__ import annotations
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from .contracts import canonical_hash, sha256_file
from .source_only_generation import FAMILIES
from .compress.xing import STRATEGIES

ROOT=Path(__file__).resolve().parents[2]
ORIGIN='formal-calibration-v1'
DESTINATION='formal-calibration-v2'
KIND='complete-source-generation-adoption-v1'
PRESERVED=('executor','editor','editor_bundle_mode','editor_context_headroom_fraction',
           'tokenizer','cap_fraction','front_matter_policy','strategy_candidates',
           'run_order_seed','calibration_reference_conditions','external_input_versions')

# This process imports only the original package. It cannot read credential files,
# open sockets, write files, register jobs, or call a model. Existing-directory
# mkdir calls in frozen constructors become existence assertions, not mutations.
ORIGIN_PROGRAM=r'''
import contextlib,json,os,sys
from pathlib import Path
from types import SimpleNamespace
sys.dont_write_bytecode=True
root=Path(sys.argv[1]);sys.path.insert(0,str(root/'src'))
def audit(event,args):
    if event.startswith('socket.') or event in ('subprocess.Popen','os.system','os.rename','os.remove','os.rmdir','os.chmod','os.mkdir'):
        raise RuntimeError('Read-only origin validation forbids '+event)
    if event=='open':
        path,mode,flags=args
        if isinstance(path,(str,bytes,os.PathLike)) and 'private_artifacts/secrets' in str(Path(os.fsdecode(path)).resolve()):
            raise RuntimeError('Credentials excluded from source adoption')
        if isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
            raise RuntimeError('Origin validation cannot write')
sys.addaudithook(audit)
def existing(self,*args,**kwargs):
    if self.is_symlink() or not self.is_dir():raise RuntimeError('Existing original artifact directory required')
Path.mkdir=existing
from capstudy.formal_generation import existing_journal_readonly
from capstudy.calibration_session import load_completed
from capstudy.contracts import sha256_file,canonical_hash
from capstudy.transports import OpenAICompatibleTransport
def forbidden(*args,**kwargs):raise RuntimeError('Source adoption cannot dispatch')
OpenAICompatibleTransport.__call__=forbidden
j=existing_journal_readonly()
@contextlib.contextmanager
def transaction():yield
j.transaction=transaction
try:
    g,candidates=load_completed(Path(sys.argv[2]),Path(sys.argv[3]),j)
    values=[]
    for (family,strategy),c in candidates.items():
        path=g.services[family,strategy].root/'candidates'/c['candidate_id']/'candidate.json'
        values.append({'family':family,'strategy_id':strategy,'candidate':c,
                       'record':{'path':str(path),'sha256':sha256_file(path)}})
    print(json.dumps({'binding':g.binding,'train_manifest':str(g.train_manifest),'cells':values}))
finally:j.connection.close()
'''


def ref(path):
    path=Path(path).resolve()
    if path.is_symlink() or not path.is_file():raise ValueError('Regular immutable evidence required')
    return {'path':str(path),'sha256':sha256_file(path)}


def bound(reference,base=ROOT):
    p=Path(reference['path']);p=p if p.is_absolute() else Path(base)/p
    if p.is_symlink() or not p.is_file():raise ValueError('Missing regular adoption evidence')
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=reference['sha256']:raise ValueError('Adoption evidence bytes changed')
    return json.loads(raw),p


def original_readback(scope,base,*,python=sys.executable):
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('KEY','TOKEN','SECRET','PASSWORD'))}
    env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
    result=subprocess.run([str(python),'-B','-c',ORIGIN_PROGRAM,str(ROOT),str(Path(scope).resolve()),str(Path(base).resolve())],
                          cwd=ROOT,env=env,capture_output=True,text=True,timeout=300,check=True)
    return json.loads(result.stdout)


def matrix_rows(readback):
    cells=readback['cells'];expected=[(f,s) for f in FAMILIES for s in STRATEGIES]
    if [(r['family'],r['strategy_id']) for r in cells]!=expected:raise ValueError('All original32 cells in fixed order required')
    rows=[];ids=set()
    for cell in cells:
        c=cell['candidate']
        if (c['campaign_id']!=ORIGIN or c['family']!=cell['family'] or c['strategy_id']!=cell['strategy_id']
            or c['attempt_index']!=0 or c.get('pilot_attempt_namespace') is not None
            or c.get('evidence_kind')!='experiment' or type(c['eligible']) is not bool or c['candidate_id'] in ids):
            raise ValueError('Original complete experiment matrix identity differs')
        ids.add(c['candidate_id'])
        rows.append({**{k:c[k] for k in ('family','strategy_id','candidate_id','eligible','ineligibility_reasons',
            'original_tokens','candidate_tokens','token_cap','source_bundle_sha256','tokenizer_id','tokenizer_revision')},
            'record':cell['record'],'candidate_manifest_sha256':canonical_hash(c)})
    return rows


def _publish_new(path,value):
    """Publish immutable JSON without an overwrite race or partial final file."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,indent=2,sort_keys=True,allow_nan=False)
            out.write('\n');out.flush();os.fsync(out.fileno())
        os.link(name,path)  # Atomic no-replace publication; existing leaves fail.
    finally:os.unlink(name)


def prepare_adoption(origin_scope,origin_base,output,*,python=sys.executable):
    """Construct prospective evidence only. No v2 scope approval is fabricated."""
    output=Path(output)
    if output.exists():raise ValueError('Fresh immutable adoption manifest required')
    readback=original_readback(origin_scope,origin_base,python=python)
    old=json.loads(Path(origin_base).read_text());binding=readback['binding']
    if binding['scientific_campaign_id']!=ORIGIN:raise ValueError('Unexpected origin campaign')
    if old.get('cap_fraction')!=0.5:raise ValueError('Original50percentcap required')
    editor=old['editor']
    for key in ('provider','model_id','context_limit','max_output_tokens','decoding_settings','rewrite_prompt_version'):
        if editor[key]!=binding['editor'][key]:raise ValueError('Origin base editor differs from generation protocol')
    if old['editor_context_headroom_fraction']!=binding['editor']['headroom']:
        raise ValueError('Origin editor headroom differs')
    if any(old['tokenizer'][k]!=binding['tokenizer'][j] for k,j in (('id','id'),('revision','revision'))):
        raise ValueError('Origin tokenizer differs from source generation')
    generation=Path(binding['artifact_root'])
    result={'schema_version':1,'kind':KIND,'origin_scientific_campaign_id':ORIGIN,
        'destination_scientific_campaign_id':DESTINATION,'model_calls':0,'new_generations':0,
        'candidate_records_modified':False,'prior_executor_results_adopted':False,
        'origin_scope':ref(origin_scope),'origin_base_config':ref(origin_base),
        'origin_generation_binding':ref(generation/'generation_binding.json'),
        'origin_generation_summary':ref(generation/'generation_completed.json'),
        'train_manifest':ref(readback['train_manifest']),
        'preserved_configuration':{k:copy.deepcopy(old.get(k)) for k in PRESERVED},
        'cells':matrix_rows(readback)}
    result['adoption_sha256']=canonical_hash(result)
    _publish_new(output,result)
    return result


def load_manifest(config,*,root=ROOT,verify_origin=False):
    policy=config.get('source_generation_adoption',{})
    if set(policy)!={'manifest_path','manifest_sha256','origin_scientific_campaign_id','destination_scientific_campaign_id'}:
        raise ValueError('Exact source-generation adoption policy required')
    p=Path(policy['manifest_path'])
    if p.is_absolute() or '..' in p.parts:raise ValueError('Portable repository-relative adoption manifest required')
    manifest,_=bound({'path':str(Path(root)/p),'sha256':policy['manifest_sha256']})
    if (config.get('campaign_id')!=DESTINATION or policy['origin_scientific_campaign_id']!=ORIGIN
        or policy['destination_scientific_campaign_id']!=DESTINATION
        or manifest.get('kind')!=KIND or manifest.get('schema_version')!=1
        or manifest.get('origin_scientific_campaign_id')!=ORIGIN
        or manifest.get('destination_scientific_campaign_id')!=DESTINATION
        or manifest.get('model_calls')!=0 or manifest.get('new_generations')!=0
        or manifest.get('candidate_records_modified') is not False
        or manifest.get('prior_executor_results_adopted') is not False
        or manifest.get('adoption_sha256')!=canonical_hash({k:v for k,v in manifest.items() if k!='adoption_sha256'})):
        raise ValueError('Adoption identity/seal/policy differs')
    if manifest['preserved_configuration']!={k:config.get(k) for k in PRESERVED}:
        raise ValueError('Source/editor/counting scientific configuration changed')
    rows=manifest['cells']
    if ([(r['family'],r['strategy_id']) for r in rows]!=[(f,s) for f in FAMILIES for s in STRATEGIES]
        or len({r['candidate_id'] for r in rows})!=32):raise ValueError('Whole32 source matrix required')
    if verify_origin:
        origin_base,_=bound(manifest['origin_base_config'])
        if manifest['preserved_configuration']!={k:origin_base.get(k) for k in PRESERVED}:
            raise ValueError('Adoption preservation claims differ from original base configuration')
        for key in ('origin_scope','origin_base_config','origin_generation_binding','origin_generation_summary'):
            bound(manifest[key])
        # JSONL train evidence is intentionally not parsed as JSON.
        if ref(manifest['train_manifest']['path'])!=manifest['train_manifest']:raise ValueError('Train source changed')
        for row in rows:
            candidate,_=bound(row['record']);verify_candidate(config,candidate,row['record']['sha256'],manifest=manifest)
            for path,digest in candidate.get('evidence_sha256',{}).items():
                if ref(path)['sha256']!=digest:raise ValueError('Original generation evidence changed')
            if candidate.get('response_path') and ref(candidate['response_path'])['sha256']!=candidate['response_sha256']:
                raise ValueError('Original provider response changed')
    return manifest


def verify_candidate(config,candidate,record_sha256=None,*,manifest=None,root=ROOT):
    manifest=manifest or load_manifest(config,root=root)
    matches=[r for r in manifest['cells'] if r['candidate_id']==candidate.get('candidate_id')]
    if len(matches)!=1:raise ValueError('Candidate is not in the adopted full matrix')
    row=matches[0]
    if (candidate.get('campaign_id')!=ORIGIN or canonical_hash(candidate)!=row['candidate_manifest_sha256']
        or (record_sha256 is not None and record_sha256!=row['record']['sha256'])):
        raise ValueError('Original adopted candidate was relabeled or changed')
    for key in ('family','strategy_id','eligible','ineligibility_reasons','original_tokens','candidate_tokens',
                'token_cap','source_bundle_sha256','tokenizer_id','tokenizer_revision'):
        if candidate.get(key)!=row[key]:raise ValueError('Adopted candidate provenance differs')
    return candidate


def load_adopted(scope,config,*,python=sys.executable):
    """Replay original validation; return unchanged records and read-only services."""
    manifest=load_manifest(config,verify_origin=True)
    declaration=scope.get('source_generation_adoption',{})
    expected={'manifest':{'path':config['source_generation_adoption']['manifest_path'],
                         'sha256':config['source_generation_adoption']['manifest_sha256']},
              'origin_scope':manifest['origin_scope'],'origin_base_config':manifest['origin_base_config']}
    if declaration!=expected:raise ValueError('Approved source adoption differs from frozen base')
    checked=original_readback(manifest['origin_scope']['path'],manifest['origin_base_config']['path'],python=python)
    if matrix_rows(checked)!=manifest['cells']:raise ValueError('Origin matrix independent replay changed')
    candidates={(r['family'],r['strategy_id']):r['candidate'] for r in checked['cells']}
    def validate(candidate):
        verify_candidate(config,candidate,manifest=manifest)
        saved,_=bound(next(r['record'] for r in manifest['cells'] if r['candidate_id']==candidate['candidate_id']))
        if saved!=candidate:raise ValueError('Original candidate bytes differ')
        return copy.deepcopy(candidate)
    generator=SimpleNamespace(train_manifest=Path(checked['train_manifest']),
        services={key:SimpleNamespace(validate=validate) for key in candidates},
        binding={'adoption_sha256':manifest['adoption_sha256']})
    return generator,candidates
