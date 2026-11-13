"""Prospective single-instance staging under the archived adapter's allowlist.

Only one exact active split row is staged. Solutions/root-level answers and all
unselected skill libraries are excluded. Native tests remain evaluator-only.
"""
import importlib.util
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
from .contracts import canonical_hash, sha256_file, write_json_atomic

ROOT = Path(__file__).resolve().parents[2]


def _inside(root, relative):
    path = root / relative
    if path.resolve() != path or not path.resolve().is_relative_to(root):
        raise ValueError('Staging input must be a nonsymlink path inside the data root')
    return path


def _file_record(path, relative):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Regular immutable staging file required')
    return {'path':relative,'mode':path.stat().st_mode & 0o777,'sha256':sha256_file(path)}


def _manifest(root, *, exclude_skills=False):
    root=Path(root)
    if root.is_symlink() or not root.is_dir():raise ValueError('Regular staging directory required')
    rows=[]
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        # Match copytree's ignore policy before touching excluded paths.
        if any(p=='__pycache__' or p.endswith(('.pyc','.pyo')) for p in relative.parts):continue
        if exclude_skills and 'skills' in relative.parts:continue
        if path.is_symlink():raise ValueError('Symlink in staging source')
        if path.is_file():rows.append(_file_record(path,relative.as_posix()))
    return rows


def stage_instance(record, data_root, output):
    data_root=Path(data_root).resolve();output=Path(output)
    if output.exists() or output.is_symlink():raise ValueError('New stage directory required')
    if record.get('split') not in ('train','dev','test'):raise ValueError('Active split required')
    manifest=_inside(data_root,record['split']+'/instances.jsonl')
    manifest_bytes=manifest.read_bytes();manifest_sha=hashlib.sha256(manifest_bytes).hexdigest()
    rows=[json.loads(line) for line in manifest_bytes.splitlines() if line.strip()]
    if rows.count(record)!=1:raise ValueError('Exact unique active instance record required')
    for key in ('family','instance_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]+',record.get(key,'')):raise ValueError('Safe instance identity required')
    source=_inside(data_root,record['path']);harness=_inside(data_root,'harness')
    adapter=ROOT/'milestone 1/code/runner_adapter.py';adapter_before=sha256_file(adapter)
    implementation_before=sha256_file(__file__)
    def source_binding():
        return {'harness':_manifest(harness),
            'metadata':[_file_record(source/name,name) for name in ('instruction.md','task.toml')],
            'environment':_manifest(source/'environment',exclude_skills=True),'tests':_manifest(source/'tests')}
    before=source_binding()
    if any(Path(row['path']).parts[0] in ('tasks','skills') for row in before['harness']):
        raise ValueError('Harness may not contain preselected task or skill data')
    pdfs=[];pdf_before=[];pdf_manifest=None;pdf_manifest_sha=None
    if record['family']=='organize-messy-files':
        pdf_manifest=_inside(data_root,'external/pdf_manifest.json')
        pdf_manifest_bytes=pdf_manifest.read_bytes();pdf_manifest_sha=hashlib.sha256(pdf_manifest_bytes).hexdigest()
        pdfs=[p for p in json.loads(pdf_manifest_bytes)['files'] if record['instance_id'] in p['instances']]
        if len(pdfs)!=100 or any(p['status']!='validated' for p in pdfs):
            raise ValueError('Exactly 100 validated instance-specific cached PDFs required')
        names=set()
        for item in pdfs:
            path=_inside(data_root,item['path']);bound=_file_record(path,item['path'])
            if path.name in names or bound['sha256']!=item['sha256']:
                raise ValueError('Cached PDF identity/hash collision')
            names.add(path.name);pdf_before.append(bound)
    shutil.copytree(harness,output,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo'))
    if _manifest(output)!=before['harness']:raise ValueError('Copied harness differs from source binding')
    (output/'skills').mkdir(exist_ok=True)
    destination=output/'tasks'/record['family']/record['instance_id'];destination.mkdir(parents=True)
    for name in ('instruction.md','task.toml'):shutil.copy2(source/name,destination/name)
    shutil.copytree(source/'environment',destination/'environment',
        ignore=shutil.ignore_patterns('skills','__pycache__','*.pyc','*.pyo'))
    (destination/'environment/skills').mkdir()
    shutil.copytree(source/'tests',destination/'tests',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo'))
    copied={'metadata':[_file_record(destination/name,name) for name in ('instruction.md','task.toml')],
        'environment':_manifest(destination/'environment',exclude_skills=True),'tests':_manifest(destination/'tests')}
    if any(copied[key]!=before[key] for key in copied):raise ValueError('Copied task bytes/modes differ from source binding')
    pdf_binding=[];expected_environment=copy.deepcopy(before['environment'])
    if pdfs:
        cache=destination/'environment/__project_pdfs';cache.mkdir()
        for item,bound in zip(pdfs,pdf_before):
            path=_inside(data_root,item['path']);target=cache/path.name
            shutil.copy2(path,target)
            actual=_file_record(target,item['path'])
            if actual!=bound:raise ValueError('Copied PDF bytes/modes differ from source binding')
            pdf_binding.append({'name':path.name,'sha256':bound['sha256'],'mode':bound['mode']})
        dockerfile=destination/'environment/Dockerfile'
        text,n=re.subn(r"RUN <<'EOF'\n.*?\nEOF",'COPY __project_pdfs/ /root/papers/all/',
                       dockerfile.read_text(),count=1,flags=re.S)
        if n!=1:raise ValueError('Pinned organizer external-input staging target changed')
        dockerfile.write_text(text)
        for row in expected_environment:
            if row['path']=='Dockerfile':row['sha256']=hashlib.sha256(text.encode()).hexdigest()
        expected_environment += [{'path':'__project_pdfs/'+r['name'],'mode':r['mode'],'sha256':r['sha256']}
                                 for r in pdf_binding]
        expected_environment.sort(key=lambda r:r['path'])
    spec=importlib.util.spec_from_file_location('capstudy_archived_runner_adapter',adapter)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.adapt(output)
    if ([_file_record(destination/name,name) for name in ('instruction.md','task.toml')]!=before['metadata']
            or _manifest(destination/'tests')!=before['tests']
            or _manifest(destination/'environment',exclude_skills=True)!=expected_environment):
        raise ValueError('Staged task changed after verified copy/transformation')
    _inside(data_root,record['path']);_inside(data_root,'harness')
    _inside(data_root,record['split']+'/instances.jsonl')
    after=source_binding()
    if before!=after or sha256_file(manifest)!=manifest_sha:
        raise ValueError('Source/harness/split manifest changed while staging')
    if sha256_file(adapter)!=adapter_before or sha256_file(__file__)!=implementation_before:
        raise ValueError('Staging implementation changed during copy')
    if pdfs:
        _inside(data_root,'external/pdf_manifest.json')
        pdf_after=[_file_record(_inside(data_root,item['path']),item['path']) for item in pdfs]
        if pdf_after!=pdf_before or sha256_file(pdf_manifest)!=pdf_manifest_sha:
            raise ValueError('External PDF source/manifest changed while staging')
        # Recheck copied files after all transformations, not just each copy.
        expected=[{'path':r['name'],'mode':r['mode'],'sha256':r['sha256']} for r in pdf_binding]
        if _manifest(destination/'environment/__project_pdfs')!=sorted(expected,key=lambda r:r['path']):
            raise ValueError('Staged PDF bytes/modes changed before sealing')
    marker={'split':record['split'],'instance_ids':[record['instance_id']],
        'staging_policy':'single-active-instance-v1','source_manifest_sha256':manifest_sha,
        'archived_adapter_sha256':adapter_before,'implementation_sha256':implementation_before,
        'source_binding_before':before,'source_binding_after':after,
        'source_binding_sha256':canonical_hash(before),'copied_task_before_transform':copied,
        'staged_tree_sha256':canonical_hash(_manifest(output)),
        'external_manifest_sha256':pdf_manifest_sha,'external_source_binding':pdf_before,
        'cached_external_inputs':pdf_binding,'solutions_staged':False,
        'unselected_skill_libraries_staged':False}
    write_json_atomic(output/'PROJECT_SPLIT.json',marker)
    return marker
