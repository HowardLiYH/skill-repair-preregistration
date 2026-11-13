"""Model-free JUnit controls on retained, explicitly bound evaluator images.

No rebuilds or model calls. Mutations are committed to separate evaluator-only
images before an independent read-only absence probe and fresh native grading.
Historical evidence stays untouched; missing-output normalization is disabled.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time
import tomllib

from .contracts import write_json_atomic, canonical_hash
from .g2_preflight import RESERVE, control_expectation, fixture_command, validate_fixture
from .grader_preflight import monitored_process
from .native_evidence import ADDOPTS, POLICY, classify_junit, grader_hashes, reconcile_record
from .native_reference_preflight import FAMILIES as NATIVE, OUTPUT, fault_command, stable_hashes
from .runner import ROOT, classify_native, sha256_file

FIXTURE = ('schedule-planning','temperature-simulation','organize-messy-files')
GDP = 'weighted-gdp-calculation'
MODES = ('healthy_reference','missing_required_output','incomplete_output','dependency_failure','untouched_input')


def image_info(reference, expected_id=None):
    item=json.loads(subprocess.check_output(['docker','image','inspect',reference],timeout=30))[0]
    if expected_id is not None and item['Id']!=expected_id:
        raise ValueError('Retained evaluator image identity changed')
    return item


def probe_code(paths):
    if not paths or any(not p.startswith('/') or '..' in Path(p).parts for p in paths):
        raise ValueError('Explicit absolute required-output paths only')
    # lexists counts directories and dangling symlinks as present; only an absent
    # filesystem entry can establish the reviewed missing-output predicate.
    return 'import os,json; print(json.dumps({p:{"exists":os.path.lexists(p)} for p in '+repr(paths)+'}))'


def run_container(image, name, logs, shell, task, fixture=None, reference_state=None, network_none=False):
    logs.mkdir(parents=True,exist_ok=True)
    (logs/'verifier').mkdir(exist_ok=True);(logs/'reference').mkdir(exist_ok=True)
    command=['docker','run','--name',name,'--cpus','1','--memory','4g',
             '--security-opt','no-new-privileges','--entrypoint','bash',
             '-v',str(task/'tests')+':/tests:ro',
             '-v',str(logs/'verifier')+':/logs/verifier','-v',str(logs/'reference')+':/logs/reference']
    if (task/'solution').is_dir():command+=['-v',str(task/'solution')+':/solution:ro']
    if fixture:command+=['-v',str(fixture)+':/reference:ro']
    if reference_state:command+=['-v',str(reference_state)+':/reference-state:ro']
    if network_none:command+=['--network','none']
    return command+[image,'-lc',shell]


def execute(family, prior_path, output, fixture=None):
    if family not in (*FIXTURE,*NATIVE,GDP):raise ValueError('Unknown calibration family')
    prior_path=Path(prior_path).resolve();output=Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT/'private_artifacts') or not output.name.replace('-','').isalnum():
        raise ValueError('New private sidecar directory required')
    task=ROOT/'milestone 1/data/train/tasks'/family/(family+'-1')
    prior=json.loads(prior_path.read_text());source_hashes=stable_hashes(task)
    inventory=json.loads((ROOT/'results/study-v1/preflight/g2_readiness_inventory.json').read_text())
    item=next(row for row in inventory['families'] if row['family']==family)
    expected_hashes={r['path'].removeprefix('tests/'):r['sha256'] for r in item['grader']['evaluator_assets']}
    if grader_hashes(task)!=expected_hashes:raise ValueError('Native grader differs from independently pinned inventory')
    expected_tests=item['grader']['collection']['static_case_count']
    fixture=Path(fixture).resolve() if fixture else None
    reference_state=None; reference_state_sha256=None
    if family==GDP:
        original_input_attested=(prior.get('image_kind')=='original built input image, not solver snapshot'
            or (prior.get('purpose')=='preserve_built_input_parent_cache_only'
                and prior.get('solver_output_image') is False
                and prior.get('source_built_image_tag')=='evaluation_weighted-gdp-calculation-weighted-gdp-calculation-1_full_skill:gdp-full-100-v1'))
        if not original_input_attested:
            raise ValueError('GDP requires retained original input-image attestation')
        base=image_info(prior['cache_tag'],prior['image_id']);checkpoint=None;paths=['/root/gdp.xlsx']
        runtime_origin='retained original GDP input image including pinned CLI/selected skill; no solver changes'
    else:
        if prior.get('family')!=family:raise ValueError('Prior result family mismatch')
        base=image_info(prior['base_image_tag'],prior['base_image']['Id'])
        checkpoint=image_info(prior['checkpoint_image_tag'],prior['checkpoint_image']['Id'])
        runtime_origin='retained original native base and generated pre-grader reference checkpoint'
        if family in FIXTURE:
            if fixture is None:raise ValueError('Explicit source-bound evaluator fixture required')
            validate_fixture(task,fixture)
            if sha256_file(fixture/'manifest.json')!=prior['fixture_manifest_sha256']:
                raise ValueError('Evaluator fixture changed after reference generation')
            reference_state=prior_path.parent/'generation/reference'
            reference_state_sha256=sha256_file(reference_state/'reference_manifest.json')
            expected_state=next(r['sha256'] for r in prior['evidence_files']
                                if r['path']=='generation/reference/reference_manifest.json')
            if reference_state_sha256!=expected_state:
                raise ValueError('Reference state manifest differs from prior saved evidence')
            state=json.loads((reference_state/'reference_manifest.json').read_text())
            paths=['/root/'+r['path'] for r in state['outputs']]
        else:
            if prior['source_hashes']!=source_hashes:raise ValueError('Native source changed since prior controls')
            paths=[OUTPUT[family]]
    cfg=tomllib.loads((task/'task.toml').read_text());output.mkdir(parents=True)
    record={'evidence_kind':'native_junit_calibration_controls','family':family,'split':'train',
            'executed_model':False,'provider_requests':0,'source_result_sha256':sha256_file(prior_path),
            'source_result_path':str(prior_path),'source_hashes':source_hashes,
            'reference_state_manifest_sha256':reference_state_sha256,
            'source_inventory_sha256':sha256_file(ROOT/'results/study-v1/preflight/g2_readiness_inventory.json'),
            'expected_grader_hashes':expected_hashes,'expected_tests':expected_tests,
            'base_image_id':base['Id'],'runtime_origin':runtime_origin,'policy':POLICY,
            'policy_implementation_sha256':sha256_file(Path(__file__).with_name('native_evidence.py')),
            'controller_sha256':sha256_file(Path(__file__)),'missing_contract':None,
            'started_unix':time.time(),'controls':[],'all_controls_verified':False,'g2_gate_claimed':False}
    write_json_atomic(output/'pending.json',record);current=None
    def monitor(command,logs,timeout,name=None):
        return monitored_process(command,logs,reserve_bytes=RESERVE,timeout=timeout,container_name=name)
    def remove(name):subprocess.run(['docker','rm','-f',name],check=True,capture_output=True,timeout=30)
    try:
        if family==GDP:
            current='capstudy-'+output.name+'-reference';logs=output/'generation'
            process=monitor(run_container(base['Id'],current,logs,'bash /solution/solve.sh',task),
                            logs/'process',900,current)
            if process.returncode:raise RuntimeError('GDP native oracle generation failed')
            tag='capstudy-sidecar-reference:'+output.name
            subprocess.run(['docker','commit',current,tag],check=True,capture_output=True,timeout=120)
            checkpoint=image_info(tag);remove(current);current=None
        record['reference_checkpoint_image_id']=checkpoint['Id']
        for mode in MODES:
            logs=output/mode;logs.mkdir()
            selected=base if mode=='untouched_input' else checkpoint
            if mode in ('missing_required_output','incomplete_output'):
                if family in FIXTURE:mutation=fixture_command(mode)
                elif family in NATIVE:mutation=fault_command(family,mode)
                elif mode=='missing_required_output':mutation="python3 -c \"from pathlib import Path; Path('/root/gdp.xlsx').unlink()\""
                else:
                    code='from openpyxl import load_workbook; w=load_workbook("/root/gdp.xlsx"); s=w["Task"]; [(setattr(s.cell(r,c),"value",None)) for r in range(35,41) for c in range(8,13)]; w.save("/root/gdp.xlsx")'
                    mutation='python3 -c '+shlex.quote(code)
                current='capstudy-'+output.name+'-'+mode.replace('_','-')+'-prepare'
                made=monitor(run_container(checkpoint['Id'],current,logs/'mutation',mutation,task,
                                           fixture,reference_state,True),logs/'mutation/process',120,current)
                if made.returncode:raise RuntimeError('Source-bound negative fixture mutation failed')
                tag='capstudy-sidecar-control:'+output.name+'-'+mode.replace('_','-')
                subprocess.run(['docker','commit',current,tag],check=True,capture_output=True,timeout=120)
                selected=image_info(tag);remove(current);current=None
            # Probe a fresh, read-only clone of the exact committed grading state.
            probe_name='capstudy-'+output.name+'-'+mode.replace('_','-')+'-probe'
            probed=monitor(['docker','run','--rm','--name',probe_name,'--network','none','--read-only',
                           '--cap-drop','ALL','--security-opt','no-new-privileges','--cpus','1','--memory','256m',
                           '--entrypoint','python3',selected['Id'],'-c',probe_code(paths)],
                          logs/'absence-process',60,probe_name)
            if probed.returncode:raise RuntimeError('Independent pregrade output probe failed')
            observed=json.loads((logs/'absence-process/process-stdout.txt').read_text())
            absence={'image_id':selected['Id'],'capture_phase':'before_grading','probe_exit':probed.returncode,'paths':observed}
            write_json_atomic(logs/'absence.json',absence)
            flags=ADDOPTS+(' --capstudy-g2-deliberate-invalid-option' if mode=='dependency_failure' else '')
            shell='env PYTEST_ADDOPTS='+shlex.quote(flags)+' bash /tests/test.sh'
            current='capstudy-'+output.name+'-'+mode.replace('_','-')+'-grade'
            process=monitor(run_container(selected['Id'],current,logs,shell,task),logs/'process',
                            min(1800,cfg['verifier']['timeout_sec']),current)
            for name in ('process-stdout.txt','process-stderr.txt'):
                (logs/'verifier'/name).write_bytes((logs/'process'/name).read_bytes())
            raw=classify_native(task,logs/'verifier',process.returncode)
            reviewed=classify_junit(task,logs/'verifier',process.returncode,expected_tests=expected_tests,
                expected_grader_hashes=expected_hashes,absence_evidence=absence,
                missing_contract=None,expected_image_id=selected['Id'])
            text='\n'.join(p.read_text(errors='replace') for p in (logs/'verifier').glob('*.txt'))
            row={'mode':mode,'image_id':selected['Id'],'absence_evidence_sha256':canonical_hash(absence),
                 'process_exit':process.returncode,**reconcile_record(raw,reviewed),
                 **control_expectation(mode,reviewed,text)}
            record['controls'].append(row);write_json_atomic(output/'progress.json',record)
            remove(current);current=None
            if mode=='healthy_reference' and not row['expectation_met']:
                raise RuntimeError('JUnit positive reference did not verify; negatives not admitted')
        record['final_source_hashes']=stable_hashes(task)
        if record['final_source_hashes']!=source_hashes:raise RuntimeError('Native sources changed during sidecar controls')
        record['all_controls_verified']=all(r['expectation_met'] for r in record['controls'])
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        record.update(error_category=type(exc).__name__,error_reason=str(exc))
    finally:
        if current:
            try:remove(current)
            except (OSError,subprocess.SubprocessError) as exc:record['cleanup_error_category']=type(exc).__name__
        record['finished_unix']=time.time();write_json_atomic(output/'result.json',record)
    return record


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--family',choices=(*FIXTURE,*NATIVE,GDP),required=True)
    p.add_argument('--prior',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--fixture',type=Path);p.add_argument('--execute',action='store_true')
    a=p.parse_args()
    if not a.execute:print(json.dumps({'executed':False,'executed_model':False,'policy':POLICY}));return
    r=execute(a.family,a.prior,a.output,a.fixture)
    print(json.dumps({'family':r['family'],'all_controls_verified':r['all_controls_verified'],'error':r.get('error_reason')}))


if __name__=='__main__':main()
