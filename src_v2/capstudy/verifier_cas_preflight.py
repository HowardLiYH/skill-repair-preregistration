"""Model-free verifier/CAS/regrade integration on two existing calibration images.

No rebuild, image pull, solver, provider key, or model request. The outer record
is preflight evidence even though it exercises the prospective formal sidecar.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess
import time
from .contracts import canonical_hash,sha256_file,write_json_atomic
from .formal_verifier import run_formal_verifier,classify_saved_formal
from .native_evidence import POLICY,grader_hashes,_terminal_counts
from .runner import classify_native,regrade_saved,tree_hash,_load_health

CASES={'scheduler-reference':('schedule-planning',2,'pass','/root/results.json'),
       'gdp-preserved-failure':('weighted-gdp-calculation',27,'task_fail','/root/gdp.xlsx')}
SECRET_NAMES=('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','CAPSTUDY_PREFLIGHT_SECRET')


def _json_file(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Regular source evidence required')
    raw=path.read_bytes();return json.loads(raw),hashlib.sha256(raw).hexdigest()


def prepare_plan(descriptor_path,output):
    descriptor_path=Path(descriptor_path).resolve();descriptor,descriptor_sha=_json_file(descriptor_path)
    if descriptor.get('case') not in CASES:raise ValueError('Only the two reviewed integration controls are supported')
    family,tests,status,required=CASES[descriptor['case']]
    task=Path(descriptor['task_path']).resolve();output=Path(output).resolve()
    if task.name!=family+'-1' or task.parent.name!=family:raise ValueError('Exact calibration task required')
    if grader_hashes(task)!=descriptor.get('grader_hashes'):raise ValueError('Native grader source differs from reviewed descriptor')
    source_path=Path(descriptor['source_evidence_path']).resolve();source,source_sha=_json_file(source_path)
    if source_sha!=descriptor.get('source_evidence_sha256'):raise ValueError('Historical source evidence changed')
    if descriptor['case']=='scheduler-reference':
        rows=[row for row in source.get('controls',[]) if row.get('mode')=='healthy_reference']
        if len(rows)!=1 or source.get('executed_model') is not False or source.get('checkpoint_passed') is not True:
            raise ValueError('Healthy scheduler reference checkpoint evidence required')
        expected=rows[0]['native_health'];image=source.get('checkpoint_image',{}).get('Id')
    else:
        if source.get('family')!=family or source.get('instance_id')!=family+'-1' or source.get('status')!='task_fail':
            raise ValueError('Preserved GDP calibration failure required')
        expected=source.get('verifier_health',{});image=source.get('evaluator_snapshot',{}).get('image_id')
    if (image!=descriptor.get('image_id') or not isinstance(image,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image)
        or expected.get('healthy') is not True or expected.get('status')!=status or expected.get('executed_tests')!=tests
        or expected.get('native_reward')!=int(status=='pass')):
        raise ValueError('Historical exact-image or healthy native grade binding differs')
    source_stdout=Path(descriptor['source_native_stdout_path']).resolve()
    source_stdout_sha=sha256_file(source_stdout)
    recorded_logs=source.get('evidence_files',[]) if descriptor['case']=='scheduler-reference' else source.get('artifacts',[])
    suffix='healthy_reference/verifier/process-stdout.txt' if descriptor['case']=='scheduler-reference' else '/verifier/process-stdout.txt'
    proofs=[row for row in recorded_logs if row.get('path','').endswith(suffix)]
    if (source_stdout.is_symlink() or source_stdout.name!='process-stdout.txt'
        or source_stdout_sha!=descriptor.get('source_native_stdout_sha256')
        or len(proofs)!=1 or proofs[0].get('sha256')!=source_stdout_sha):
        raise ValueError('Historical terminal counts lack an exact source-record hash binding')
    counts=_terminal_counts(source_stdout.parent,task)
    if counts['error'] or counts['passed']+counts['failure']!=tests or int(counts['failure']==0)!=expected['native_reward']:
        raise ValueError('Historical terminal counts disagree with healthy source grade')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,65}',output.name):raise ValueError('Safe new preflight output name required')
    policy={'id':POLICY,'scope':'formal_calibration','campaign_id':'verifier-cas-integration-preflight',
        'protocol_sha256':canonical_hash({'descriptor_sha256':descriptor_sha,'helper_sha256':sha256_file(__file__)}),
        'cpus':1,'memory':'4g','tasks':{family+'/'+family+'-1':{'expected_tests':tests,
            'grader_hashes':descriptor['grader_hashes'],'required_output_paths':[required]}},
        'storage':{'id':'verified-oci-cas-v1','remove_verified_raw_duplicate':True,
            'store_path':str(output/'cas'),'scratch_path':str(output/'scratch'),'receipts_path':str(output/'receipts'),
            'store_quota_bytes':16*1024**3,'scratch_quota_bytes':8*1024**3,
            'minimum_free_bytes':20*1024**3,'receipt_quota_bytes':16*1024**2}}
    return {'schema_version':1,'evidence_kind':'verifier_cas_regrade_integration_preflight',
        'scientific_performance_result':False,'formal_trial_claimed':False,'executed_model':False,
        'model_calls':0,'case':descriptor['case'],'descriptor_path':str(descriptor_path),'descriptor_sha256':descriptor_sha,
        'source_evidence_path':str(source_path),'source_evidence_sha256':source_sha,'input_image_id':image,
        'source_native_stdout_path':str(source_stdout),'source_native_stdout_sha256':source_stdout_sha,
        'task_path':str(task),'instance_id':family+'-1','expected_grade':{**{key:expected[key] for key in
            ('status','healthy','native_reward','executed_tests')},'passed_tests':counts['passed'],
            'failed_tests':counts['failure'],'setup_error_tests':0},'policy':policy,'policy_sha256':canonical_hash(policy),
        'native_timeout_seconds':900,'output':str(output),'container_name':'capstudy-vcr-'+output.name}


def _grade_shape(row):
    return {key:row.get(key) for key in ('status','healthy','native_reward','executed_tests','passed_tests','failed_tests','setup_error_tests')}


def run_preflight(descriptor_path,output,*,runner=None,verifier=None,regrader=None,reader=None):
    plan=prepare_plan(descriptor_path,output);out=Path(plan['output'])
    # Deployment packages must include the archived native classifier before
    # creating any owned directory or Docker resource.
    if runner is None:_load_health()
    if out.exists():raise ValueError('Fresh owned output directory required; no resume or overwrites')
    run=runner or subprocess.run;verify=verifier or run_formal_verifier
    regrade=regrader or regrade_saved;read_grade=reader or classify_saved_formal
    out.mkdir(parents=True,mode=0o700);write_json_atomic(out/'plan.json',plan)
    policy=plan['policy'];task=Path(plan['task_path']);container=plan['container_name'];image=plan['input_image_id']
    trial=out/'trial';trial.mkdir(mode=0o700)
    write_json_atomic(out/'policy.json',policy)
    record={**plan,'completed':False,'new_containers_created':False};started=time.monotonic();phase='input_image_identity'
    common={'capture_output':True,'text':True};success=False
    try:
        inspected=run(['docker','image','inspect','--format','{{.Id}}',image],check=True,timeout=30,**common)
        if inspected.stdout.strip()!=image:raise ValueError('Required exact input image is not already loaded')
        phase='create_evaluator_clone'
        # Explicit entrypoint prevents the source image's original solver/reference
        # command from running. No task generation happens in this clone.
        run(['docker','create','--pull=never','--name',container,'--network','none','--cpus','1','--memory','4g',
            '--cap-drop','ALL','--security-opt','no-new-privileges','-e','CAPSTUDY_PREFLIGHT_SECRET=synthetic-scrub-check',
            '--entrypoint','/bin/sh',image,'-c','sleep infinity'],check=True,timeout=60,**common)
        record['new_containers_created']=True
        run(['docker','start',container],check=True,timeout=30,**common)
        phase='prospective_verifier_and_exact_cas_retention'
        process=verify(container,task,trial,SECRET_NAMES,timeout=900,policy=policy,native_classifier=classify_native)
        initial=read_grade(task,trial/'verifier',process.returncode,policy)
        record['initial_native_review']=initial
        if _grade_shape(initial)!=plan['expected_grade']:raise ValueError('Initial review differs from preserved source grade')
        snapshot=json.loads((trial/'evaluator_snapshot.json').read_text())
        if snapshot.get('kind')!='oci-cas-v1' or snapshot.get('raw_original_present') is not False:
            raise ValueError('Exact CAS retention/removal path was not exercised')
        if snapshot.get('image_id')==image:raise ValueError('Fresh scrubbed clone must have a distinct image identity before owned-tag cleanup')
        output_record={'instance_id':plan['instance_id'],'evaluator_snapshot':snapshot,'formal_verifier_policy':policy}
        gold={'instance_id':plan['instance_id'],'task_path':str(task),'reference_sha256':tree_hash(task/'tests'),
            'verifier_seconds':900,'formal_verifier_policy':policy,'formal_verifier_policy_sha256':plan['policy_sha256']}
        write_json_atomic(out/'output_record.json',output_record);write_json_atomic(out/'gold_record.json',gold)
        phase='materialize_load_and_native_regrade'
        again=regrade(output_record,gold,out/'regrade');record['regraded_native_review']=again
        if _grade_shape(again)!=_grade_shape(initial):raise ValueError('Exact retained-image regrade differs from initial review')
        # Historical evidence, native grader bytes, and original image remain intact.
        if (sha256_file(plan['source_evidence_path'])!=plan['source_evidence_sha256']
            or sha256_file(plan['source_native_stdout_path'])!=plan['source_native_stdout_sha256']
            or grader_hashes(task)!=plan['policy']['tasks'][task.parent.name+'/'+task.name]['grader_hashes']):
            raise ValueError('Immutable source evidence changed during preflight')
        inspected=run(['docker','image','inspect','--format','{{.Id}}',image],check=True,timeout=30,**common)
        if inspected.stdout.strip()!=image:raise ValueError('Original image was not preserved')
        record.update(completed=True,source_grade_initial_regrade_agree=True,input_image_preserved=True,
            source_evidence_preserved=True,evaluator_snapshot=snapshot)
        success=True
    except BaseException as exc:
        record.update(failed_phase=phase,error_category=type(exc).__name__)
        raise
    finally:
        cleanup=[]
        commands=[['docker','rm','-f',container]] if record['new_containers_created'] else []
        # CAS regrade can restore our fresh commit tag. Never touch input image
        # IDs/tags; keep failed snapshots for the owner's recovery review.
        if success:commands.append(['docker','image','rm','-f','--no-prune',container.lower()+'-verification:saved'])
        for command in commands:
            try:
                item=run(command,timeout=30,**common);cleanup.append({'command':command,'returncode':item.returncode})
            except (OSError,subprocess.SubprocessError) as exc:cleanup.append({'command':command,'error_category':type(exc).__name__})
        record.update(cleanup=cleanup,duration_seconds=time.monotonic()-started)
        write_json_atomic(out/'result.json',record)
    return record


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--descriptor',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--execute',action='store_true');a=p.parse_args(argv)
    if not a.execute:print(json.dumps({'executed':False,'plan':prepare_plan(a.descriptor,a.output)},indent=2));return
    def stop(_signal,_frame):raise TimeoutError('Preflight interrupted; retain existing evidence')
    signal.signal(signal.SIGTERM,stop)
    result=run_preflight(a.descriptor,a.output)
    print(json.dumps({'completed':result['completed'],'model_calls':0,'output':str(a.output)}))


if __name__=='__main__':main()
