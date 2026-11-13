"""Regrade one already-loaded immutable image under an exact frozen policy.

No solver/model dispatch, image commits, snapshot replacement or historical
writes. Callers must verify/materialize/load the persisted archive first.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import subprocess
import uuid
from .contracts import canonical_hash, write_json_atomic
from .formal_verifier import validate_policy
from .native_evidence import ADDOPTS, classify_junit, reconcile_record


def validate_regrade_policy(policy, expected_sha256, task_path):
    if (not isinstance(expected_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_sha256)
            or not isinstance(policy,dict) or canonical_hash(policy)!=expected_sha256):
        raise ValueError('Independent expected formal verifier policy hash required')
    return validate_policy(policy,task_path)[0]


def regrade_loaded_image(image_id,task_path,workdir,*,policy,expected_policy_sha256,
                         native_classifier,timeout,runner=None):
    policy=copy.deepcopy(policy);task_path=Path(task_path);logs=Path(workdir)
    task_policy=validate_regrade_policy(policy,expected_policy_sha256,task_path)
    if (not callable(native_classifier) or type(timeout) is not int or timeout<=0
            or not isinstance(image_id,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id)):
        raise ValueError('Exact loaded image, native classifier and timeout required')
    if logs.is_symlink() or not logs.is_dir() or any(logs.iterdir()):
        raise ValueError('Fresh owned regrading logs directory required')
    run=runner or subprocess.run;name='capstudy-regrade-'+uuid.uuid4().hex
    probe_name=name+'-output-probe';common={'capture_output':True,'text':True}
    resources=['--cpus',str(policy['cpus']),'--memory',policy['memory'],'--security-opt','no-new-privileges']
    phase='image_identity';cleanup=[]
    write_json_atomic(logs/'regrade-binding.json',{'image_id':image_id,
        'formal_verifier_policy_sha256':expected_policy_sha256,'task_id':task_path.parent.name+'/'+task_path.name,
        'native_tests_or_reward_modified':False,'model_calls':0})
    try:
        inspected=run(['docker','image','inspect','--format','{{.Id}}',image_id],check=True,timeout=30,**common)
        if inspected.stdout.strip()!=image_id:raise ValueError('Loaded image does not match persisted identity')
        phase='pregrade_probe';paths=task_policy['required_output_paths']
        code='import os,json; print(json.dumps({p:{"exists":os.path.lexists(p)} for p in '+repr(paths)+'}))'
        probe=run(['docker','run','--rm','--name',probe_name,'--network','none','--read-only',
            '--cap-drop','ALL',*resources,'--entrypoint','python3',image_id,'-c',code],check=True,timeout=60,**common)
        (logs/'output-probe.stdout').write_text(probe.stdout or '')
        (logs/'output-probe.stderr').write_text(probe.stderr or '')
        observed=json.loads(probe.stdout)
        if (not isinstance(observed,dict) or set(observed)!=set(paths)
                or any(not isinstance(v,dict) or set(v)!={'exists'} or type(v['exists']) is not bool
                       for v in observed.values()) or probe.returncode!=0):
            raise ValueError('Independent pregrade absence probe malformed')
        absence={'image_id':image_id,'capture_phase':'before_grading','probe_exit':probe.returncode,'paths':observed}
        write_json_atomic(logs/'absence.json',absence)
        phase='native_grading'
        result=run(['docker','run','--rm','--name',name,*resources,
            '-e','PYTEST_ADDOPTS='+ADDOPTS,'--entrypoint','bash',
            '-v',f'{task_path/"tests"}:/tests:ro','-v',f'{logs}:/logs/verifier',image_id,'/tests/test.sh'],
            timeout=timeout,**common)
        (logs/'process-stdout.txt').write_text(result.stdout or '')
        (logs/'process-stderr.txt').write_text(result.stderr or '')
        raw=native_classifier(task_path,logs,result.returncode)
        reviewed=classify_junit(task_path,logs,result.returncode,
            expected_tests=task_policy['expected_tests'],expected_grader_hashes=task_policy['grader_hashes'],
            absence_evidence=absence,missing_contract=task_policy.get('missing_output_contract'),expected_image_id=image_id)
        write_json_atomic(logs/'native-evidence-review.json',reconcile_record(raw,reviewed))
        return reviewed
    except Exception as exc:
        incident={'phase':phase,'error_category':type(exc).__name__}
        if isinstance(exc,subprocess.TimeoutExpired):
            for suffix,content in [('stdout',exc.stdout),('stderr',exc.stderr)]:
                (logs/(phase+'-timeout.'+suffix)).write_text(content.decode(errors='replace') if isinstance(content,bytes) else content or '')
        try:write_json_atomic(logs/'regrade-incident.json',incident)
        except OSError:pass  # Preserve primary failure even if the disk filled.
        raise
    finally:
        for owned in (name,probe_name):
            try:
                result=run(['docker','rm','-f',owned],timeout=30,**common)
                cleanup.append({'container':owned,'returncode':result.returncode})
            except (OSError,subprocess.SubprocessError) as exc:
                cleanup.append({'container':owned,'error_category':type(exc).__name__})
        try:write_json_atomic(logs/'regrade-cleanup.json',{'resources':cleanup})
        except OSError:pass
        # Loaded image may serve other frozen cells. No image removal or prune.
