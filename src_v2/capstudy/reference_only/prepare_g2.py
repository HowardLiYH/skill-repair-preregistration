"""Prepare source-bound evaluator-only fixtures; no models, keys or Docker."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
from .fixture_runtime import constants

ROOT=Path(__file__).resolve().parents[3]
FAMILIES=('schedule-planning','temperature-simulation','organize-messy-files')


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(destination, family):
    if family not in FAMILIES:raise ValueError('Only the three reviewed calibration fixtures are supported')
    destination=Path(destination).resolve()
    private=(ROOT/'private_artifacts').resolve()
    if not destination.is_relative_to(private):raise ValueError('Reference fixtures must remain under private_artifacts')
    if destination.exists():raise ValueError('New reference directory required; preserve earlier evidence')
    task=ROOT/'milestone 1/data/train/tasks'/family/(family+'-1')
    destination.mkdir(parents=True,mode=0o700)
    runtime=Path(__file__).with_name('fixture_runtime.py')
    shutil.copyfile(runtime,destination/'reference_fixture.py')
    shutil.copyfile(task/'instruction.md',destination/'instruction.md')
    source_bindings=[{'root':'fixture','path':'instruction.md','sha256':sha(task/'instruction.md')}]
    source_bindings += [{'root':'tests','path':str(p.relative_to(task/'tests')),'sha256':sha(p)}
        for p in sorted((task/'tests').rglob('*')) if p.is_file()
        and '__pycache__' not in p.relative_to(task/'tests').parts
        and p.suffix not in ('.pyc', '.pyo')]
    inputs=[]
    for p in sorted((task/'environment').rglob('*')):
        rel=p.relative_to(task/'environment')
        if not p.is_file() or p.name=='Dockerfile' or 'skills' in rel.parts:continue
        target=str(rel)
        if family=='organize-messy-files':target='papers/all/'+p.name
        inputs.append({'root':'binary' if family=='temperature-simulation' and rel.as_posix()=='glm' else 'workdir',
            'path':target,'sha256':sha(p),'source_path':str(p.relative_to(ROOT))})
    reconciliation=None
    if family=='organize-messy-files':
        data=ROOT/'milestone 1/data'
        cache=json.loads((data/'external/pdf_manifest.json').read_text())
        rows=[r for r in cache['files'] if family+'-1' in r['instances']]
        if len(rows)!=100:raise ValueError('Exactly100 selected PDFs required')
        for r in rows:
            p=data/r['path']
            if r['status']!='validated' or sha(p)!=r['sha256']:raise ValueError('Selected PDF hash mismatch')
            inputs.append({'root':'workdir','path':'papers/all/'+p.name,'sha256':r['sha256'],
                'source_path':str(p.relative_to(ROOT))})
        native=constants(task/'solution/solution.py',['SUBJECT_TO_PAPERS'])['SUBJECT_TO_PAPERS']
        expected=constants(task/'tests/test_outputs.py',['SUBJECT_TO_PAPERS'])['SUBJECT_TO_PAPERS']
        before={x:subject for subject,names in native.items() for x in names}
        after={x:subject for subject,names in expected.items() for x in names}
        reconciliation={'native_reference_sha256':sha(task/'solution/solution.py'),
            'native_grader_sha256':sha(task/'tests/test_outputs.py'),
            'removed_reference_only_names':sorted(set(before)-set(after)),
            'added_calibration_input_names':sorted(set(after)-set(before)),
            'changed_common_assignments':[name for name in sorted(set(before)&set(after)) if before[name]!=after[name]],
            'archive_modified':False,'scope':'evaluator fixture only; not a solver method or task data change'}
        (destination/'filename_reconciliation.json').write_text(json.dumps(reconciliation,sort_keys=True,indent=2)+'\n')
    manifest={'schema_version':1,'family':family,'instance_id':family+'-1','split':'train',
        'scope':'calibration_evaluator_only','executed':False,'executed_model':False,'native_grader_pass_claimed':False,
        'fixture_origin':'native evaluator expectations for controlled validation; not independent task solving',
        'runtime_sha256':sha(runtime),'task_config_sha256':sha(task/'task.toml'),
        'native_dockerfile_sha256':sha(task/'environment/Dockerfile'),
        'instruction_source_path':str((task/'instruction.md').relative_to(ROOT)),
        'instruction_sha256':sha(task/'instruction.md'),'source_bindings':source_bindings,'input_bindings':inputs,
        'modes':['healthy_reference','missing_required_output','incomplete_output','dependency_failure'],
        'fourth_mode_semantics':'deterministic pytest runtime-option fault, not a missing solver output',
        'negative_control_prerequisite':'Fresh snapshot of generated reference after separate native grader pass; mount generation reference_manifest.json read-only.',
        'temperature_policy':{'metrics':'exact hash-bound native metric functions applied to real CPU GLM output',
            'seed':5300,'default_max_evaluations':80,'default_total_seconds':720,'per_glm_timeout_seconds':90,
            'failure_semantics':'nonpassing measurements remain failures; no fabricated metrics or healthy-reference claim'} if family=='temperature-simulation' else None,
        'reconciliation_sha256':sha(destination/'filename_reconciliation.json') if reconciliation else None}
    (destination/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    (destination/'README.txt').write_text('EVALUATOR ONLY. Do not mount or copy this directory into solver/editor environments.\n'
        'Run reference_fixture.py, preserve its exit status and evidence, then run original /tests/test.sh separately.\n'
        'Generation completion alone is not a native grader pass. See docs/G2_REFERENCE_FIXTURES.md.\n')
    return {'family':family,'output':str(destination),'manifest_sha256':sha(destination/'manifest.json'),
        'runtime_sha256':sha(runtime),'executed':False}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--family',choices=FAMILIES,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args(argv)
    print(json.dumps(prepare(args.output,args.family),indent=2))

if __name__=='__main__':main()
