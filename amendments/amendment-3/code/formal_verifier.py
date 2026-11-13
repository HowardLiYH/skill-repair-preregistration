"""Opt-in native sidecar and exact retained snapshot for new formal trials.

Called only by a separately admitted runner. No model access, global cleanup,
historical mutation, or permission inference occurs here.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
from .contracts import sha256_file, write_json_atomic
from .native_evidence import ADDOPTS, POLICY, classify_junit, grader_hashes, reconcile_record
from .snapshot_retention import SnapshotRetention, create_owned_formal_directory
from .oci_archive_store import _locked_store, _capacity, _inventory


def validate_policy(policy, task_path):
    if (not isinstance(policy,dict) or policy.get('id')!=POLICY
            or policy.get('scope') not in ('formal_calibration','formal_dev','formal_test')
            or not isinstance(policy.get('campaign_id'),str) or not policy['campaign_id']
            or not re.fullmatch(r'[0-9a-f]{64}',policy.get('protocol_sha256') or '')
            or type(policy.get('cpus')) is not int or not 1<=policy['cpus']<=8
            or not re.fullmatch(r'[1-9]\d*[gGmM]',str(policy.get('memory')))):
        raise ValueError('Explicit frozen formal native-verifier policy required')
    task_path=Path(task_path)
    task_id=task_path.parent.name+'/'+task_path.name
    tasks=policy.get('tasks',{})
    if task_id not in tasks:raise ValueError('Task absent from frozen verifier policy')
    task_policy=tasks[task_id]
    if (type(task_policy.get('expected_tests')) is not int or task_policy['expected_tests']<=0
            or task_policy.get('grader_hashes')!=grader_hashes(task_path)):
        raise ValueError('Native grader differs from frozen policy')
    missing=task_policy.get('missing_output_contract')
    if missing is not None and (missing.get('family')!=task_path.parent.name
            or missing.get('instance_id')!=task_path.name):
        raise ValueError('Missing-output contract is not reviewed for this exact task')
    paths=task_policy.get('required_output_paths')
    if (not isinstance(paths,list) or not paths or any(not isinstance(p,str) or not p.startswith('/')
            or '..' in Path(p).parts for p in paths) or len(paths)!=len(set(paths))):
        raise ValueError('Exact absolute required output paths required')
    storage=policy.get('storage')
    if not isinstance(storage,dict) or storage.get('id')!='verified-oci-cas-v1':
        raise ValueError('Formal verifier requires bounded exact CAS retention')
    if type(storage.get('remove_verified_raw_duplicate')) is not bool:
        raise ValueError('Explicit duplicate archive retention choice required')
    keys=('store_path','scratch_path','receipts_path','store_quota_bytes',
          'scratch_quota_bytes','minimum_free_bytes','receipt_quota_bytes')
    if any(k not in storage for k in keys):raise ValueError('Complete retention quota/path policy required')
    return task_policy,{k:storage[k] for k in keys}


def run_formal_verifier(container,task_path,trial_path,secret_env_names=(),*,timeout,policy,native_classifier,runner=None,save_runner=None):
    run=runner or subprocess.run
    if not callable(native_classifier):raise ValueError('Original native health classifier required')
    task_path,trial_path=Path(task_path),Path(trial_path)
    task_policy,storage=validate_policy(policy,task_path)
    logs=trial_path/'verifier';logs.mkdir(parents=True,exist_ok=True)
    # Refuse stale native outputs instead of deleting previous trial evidence.
    if any(logs.iterdir()):raise ValueError('Fresh verifier evidence directory required')
    trial_id=trial_path.name
    snapshot_directory=trial_path/'snapshot'
    create_owned_formal_directory(snapshot_directory,campaign_id=policy['campaign_id'],
        trial_id=trial_id,scope=policy['scope'],protocol_sha256=policy['protocol_sha256'])
    retention=SnapshotRetention(**storage)
    tag=container.lower()+'-verification:saved';name=container+'-verifier'
    probe_name=container+'-output-probe'
    common={'capture_output':True,'text':True}
    snapshot_safely_retained=False
    resources=['--cpus',str(policy['cpus']),'--memory',policy['memory'],
               '--security-opt','no-new-privileges']
    phase='snapshot_commit'
    try:
        run(['docker','stop','--time','1',container],check=True,timeout=30,**common)
        changes=[]
        for key in sorted(set(secret_env_names)):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key):raise ValueError('Invalid credential environment name')
            changes.extend(['--change',f'ENV {key}='])
        run(['docker','commit',*changes,container,tag],check=True,timeout=120,**common)
        inspected=run(['docker','image','inspect','--format','{{.Id}}',tag],check=True,timeout=30,**common)
        image_id=inspected.stdout.strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id):raise ValueError('Immutable snapshot image ID required')
        inspected_env=run(['docker','image','inspect','--format','{{json .Config.Env}}',image_id],check=True,timeout=30,**common)
        environment=json.loads(inspected_env.stdout)
        if not isinstance(environment,list) or any(not isinstance(x,str) for x in environment):
            raise ValueError('Committed image environment cannot be verified')
        scrubbed={key:all(item.split('=',1)[1]=='' for item in environment
            if item.startswith(key+'=')) for key in sorted(set(secret_env_names))}
        if not all(scrubbed.values()):raise ValueError('Committed image still contains a requested secret environment value')
        write_json_atomic(trial_path/'snapshot-secret-scrub.json',
            {'image_id':image_id,'requested_names_blank_or_absent':scrubbed,'secret_values_recorded':False})
        phase='snapshot_capacity_admission'
        sized=run(['docker','image','inspect','--format','{{.Size}}',image_id],check=True,timeout=30,**common)
        image_size=int(sized.stdout.strip())
        # Admit raw archive, worst-case new CAS objects and a reconstruction.
        # The bound includes tar/config metadata slack. The save is also watched
        # under the host reserve; the observed archive must fit this bound.
        archive_bound=image_size+16*1024*1024
        if (image_size<=0 or _inventory(retention.scratch)['allocated_file_bytes']+archive_bound>storage['scratch_quota_bytes']
                or shutil.disk_usage(snapshot_directory).free<storage['minimum_free_bytes']+3*archive_bound):
            raise ValueError('Insufficient bounded working space for archive plus exact reconstruction')
        with _locked_store(retention.store) as store:
            _capacity(store,archive_bound,storage['store_quota_bytes'],storage['minimum_free_bytes'])
        archive=snapshot_directory/'evaluator-image.tar'
        with archive.open('xb') as handle:archive.chmod(0o600)
        if save_runner is None:
            from .launcher_guard import guarded_launch
            save_runner=guarded_launch
        phase='snapshot_save'
        saved=save_runner(['docker','image','save','-o',str(archive),image_id],cwd=trial_path,
            logs=trial_path/'snapshot-save',container_names=[],timeout_seconds=600,
            minimum_free_bytes=storage['minimum_free_bytes'],disk_paths=[str(snapshot_directory)])
        if saved.returncode or archive.stat().st_size>archive_bound:
            raise RuntimeError('Snapshot save failed or exceeded its admitted bound')
        snapshot={'path':str(archive.resolve()),'sha256':sha256_file(archive),'image_id':image_id,
            'image_tag':tag,'evaluator_only':True,'capture_phase':'before_grading'}
        write_json_atomic(trial_path/'raw_evaluator_snapshot.json',snapshot)
        phase='pregrade_output_probe'
        paths=task_policy['required_output_paths']
        code='import os,json; print(json.dumps({p:{"exists":os.path.lexists(p)} for p in '+repr(paths)+'}))'
        probe=run(['docker','run','--rm','--name',probe_name,'--network','none','--read-only',
            '--cap-drop','ALL',*resources,'--entrypoint','python3',image_id,'-c',code],
            check=True,timeout=60,**common)
        (logs/'output-probe.stdout').write_text(probe.stdout or '')
        (logs/'output-probe.stderr').write_text(probe.stderr or '')
        observed=json.loads(probe.stdout)
        if (not isinstance(observed,dict) or set(observed)!=set(paths) or any(not isinstance(v,dict) or set(v)!={'exists'}
                or type(v['exists']) is not bool for v in observed.values())):
            raise ValueError('Independent output probe is malformed')
        absence={'image_id':image_id,'capture_phase':'before_grading','probe_exit':probe.returncode,'paths':observed}
        write_json_atomic(logs/'absence.json',absence)
        phase='native_grading'
        # Native test commands, expected answers and reward logic are untouched.
        completed=run(['docker','run','--rm','--name',name,*resources,
            '-e','PYTEST_ADDOPTS='+ADDOPTS,'--entrypoint','bash',
            '-v',f'{task_path / "tests"}:/tests:ro','-v',f'{logs}:/logs/verifier',image_id,'/tests/test.sh'],
            timeout=timeout,**common)
        (logs/'process-stdout.txt').write_text(completed.stdout or '')
        (logs/'process-stderr.txt').write_text(completed.stderr or '')
        raw=native_classifier(task_path,logs,completed.returncode)
        reviewed=classify_junit(task_path,logs,completed.returncode,
            expected_tests=task_policy['expected_tests'],expected_grader_hashes=task_policy['grader_hashes'],
            absence_evidence=absence,missing_contract=task_policy.get('missing_output_contract'),expected_image_id=image_id,
            skip_rule=policy.get('native_skip_rule'))
        write_json_atomic(logs/'native-evidence-review.json',reconcile_record(raw,reviewed))
        phase='exact_cas_retention'
        retained=retention.retain(snapshot,trial_id=trial_id,
            remove_raw=policy['storage']['remove_verified_raw_duplicate'],owned_formal_directory=snapshot_directory)
        receipt_path=Path(retained['receipt_path'])
        final={'kind':'oci-cas-v1','path':str(receipt_path),'sha256':sha256_file(receipt_path),
            'image_id':image_id,'evaluator_only':True,'capture_phase':'before_grading',
            'original_archive_sha256':snapshot['sha256'],'storage_config':storage,
            'raw_original_present':retained['raw_original_present']}
        write_json_atomic(trial_path/'evaluator_snapshot.json',final)
        snapshot_safely_retained=True
        return completed
    except Exception as exc:
        if isinstance(exc,subprocess.TimeoutExpired):
            for suffix,content in [('stdout',exc.stdout),('stderr',exc.stderr)]:
                (logs/(phase+'-timeout.'+suffix)).write_text(content.decode(errors='replace') if isinstance(content,bytes) else content or '')
        try:write_json_atomic(trial_path/'verifier-incident.json',{'phase':phase,'error_category':type(exc).__name__})
        except OSError:pass
        raise
    finally:
        cleanup=[]
        commands=[['docker','rm','-f',owned] for owned in (name,probe_name)]
        if snapshot_safely_retained:commands.append(['docker','image','rm','-f','--no-prune',tag])
        for command in commands:
            try:
                result=run(command,timeout=30,**common)
                cleanup.append({'command':command,'returncode':result.returncode})
            except (OSError,subprocess.SubprocessError) as exc:
                cleanup.append({'command':command,'error_category':type(exc).__name__})
        try:
            write_json_atomic(trial_path/'verifier-cleanup.json',{'resources':cleanup,
                'snapshot_safely_retained':snapshot_safely_retained,
                'recovery_image_tag_if_present':None if snapshot_safely_retained else tag,
                'committed_image_cleanup_deferred':not snapshot_safely_retained})
        except OSError:pass  # Preserve the primary failure if evidence storage is unavailable.


def classify_saved_formal(task_path,logs,returncode,policy):
    """Recompute the sidecar grade; never trust a previously saved pass flag."""
    task_policy,_=validate_policy(policy,task_path)
    logs=Path(logs)
    snapshot=json.loads((logs.parent/'evaluator_snapshot.json').read_text())
    if snapshot.get('kind')!='oci-cas-v1' or sha256_file(snapshot['path'])!=snapshot['sha256']:
        raise ValueError('Bound retained evaluator snapshot required')
    receipt=json.loads(Path(snapshot['path']).read_text())
    from .contracts import canonical_hash
    if (receipt.get('receipt_sha256')!=canonical_hash({k:v for k,v in receipt.items() if k!='receipt_sha256'})
            or receipt.get('image_id')!=snapshot.get('image_id')):
        raise ValueError('Retained image binding changed')
    absence=json.loads((logs/'absence.json').read_text())
    if json.loads((logs/'output-probe.stdout').read_text())!=absence.get('paths'):
        raise ValueError('Independent output probe differs from recorded absence')
    return classify_junit(task_path,logs,returncode,expected_tests=task_policy['expected_tests'],
        expected_grader_hashes=task_policy['grader_hashes'],absence_evidence=absence,
        missing_contract=task_policy.get('missing_output_contract'),expected_image_id=snapshot['image_id'],
        skip_rule=policy.get('native_skip_rule'))
