"""One isolated trial, explicit live gates, and native saved-output regrading.

`run_trial(..., execute=False)` is the default. Injected boundaries are for
labeled fixtures, not experimental evidence. Historical source is read only.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
import platform
from pathlib import Path
import re
import shutil
import tarfile
import subprocess
import sys
import time
import uuid

CONDITIONS = {'feedback_repair', 'retry_no_feedback', 'unchanged_rewrite', 'head_truncation',
              'sentence_extraction', 'llmlingua2', 'full_skill', 'no_skill', 'full_skill_fallback'}
ROOT = Path(__file__).resolve().parents[2]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_manifest(root, *, exclude_skills=False):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('Expected an existing non-symlink directory')
    rows = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if exclude_skills and 'skills' in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError('Symlinks prohibited in execution inputs: ' + str(path))
        if path.is_file():
            rows.append({'path': relative.as_posix(), 'mode': path.stat().st_mode & 0o777,
                         'sha256': sha256_file(path)})
    return rows


def tree_hash(root, *, exclude_skills=False):
    return canonical_hash(file_manifest(root, exclude_skills=exclude_skills))


def verify_artifacts(records):
    try:
        return bool(records) and all(not Path(r['path']).is_symlink() and Path(r['path']).is_file() and
                                     sha256_file(r['path']) == r['sha256'] for r in records)
    except (KeyError, TypeError, OSError):
        return False


def _load_health():
    path = ROOT / 'milestone 1/code/verifier_health.py'
    spec = importlib.util.spec_from_file_location('capstudy_native_health', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_native(task_path, log_path, returncode):
    return _load_health().classify_verifier(task_path, log_path, returncode)


def execution_config_hash(config):
    # Reporting/pricing/strategy labels do not affect execution. All effective
    # model/runtime/tool/cache/external-data settings do.
    return canonical_hash({key: config.get(key) for key in
        ('executor', 'runtime', 'tool_policy', 'external_input_versions', 'cache_regime')})


def runner_implementation_hash():
    paths = [Path(__file__), Path(__file__).with_name('runtime.py'),
             Path(__file__).with_name('launcher_guard.py'),
             Path(__file__).with_name('deepseek_runtime.py'),
             Path(__file__).with_name('saved_image_identity.py'),
             Path(__file__).with_name('oci_archive_store.py'),
             Path(__file__).with_name('build_runtime.py'),
             Path(__file__).with_name('single_stage.py'),
             Path(__file__).with_name('stock_cache_staging.py'),
             Path(__file__).with_name('stock_dependency_cache.py'),
             Path(__file__).with_name('formal_verifier.py'),
             Path(__file__).with_name('formal_regrade.py'),
             Path(__file__).with_name('native_evidence.py'),
             Path(__file__).with_name('snapshot_retention.py'),
             Path(__file__).with_name('contracts.py'),
             Path(__file__).with_name('isolated_verifier.py'),
             ROOT / 'milestone 1/code/runner_adapter.py',
             ROOT / 'milestone 1/code/verifier_health.py']
    return canonical_hash({str(path.relative_to(ROOT)): sha256_file(path) for path in paths})


def execution_signature(instance_record, task_path, bundle_path, config, repeat_id):
    task_path = Path(task_path)
    return canonical_hash({'split': instance_record['split'], 'instance_id': instance_record['instance_id'],
        'instruction_sha256': sha256_file(task_path / 'instruction.md'),
        'environment_sha256': tree_hash(task_path / 'environment', exclude_skills=True),
        'task_config_sha256': sha256_file(task_path / 'task.toml') if (task_path / 'task.toml').exists() else None,
        'runner_implementation_sha256': runner_implementation_hash(),
        'bundle_sha256': tree_hash(bundle_path) if bundle_path else None,
        'execution_config_sha256': execution_config_hash(config), 'repeat_id': repeat_id})


def extract_claude_evidence(path, skill_paths=()):
    """Only last result event supplies cumulative usage/cost; never sum it."""
    path = Path(path)
    if not path.is_file():
        return {'usage': None, 'cost_usd': None, 'usage_source': None,
                'skill_use': 'unknown', 'raw_result_event': None, 'skill_events': [], 'executor_error_events': []}
    last, observed, parsed = None, [], 0
    executor_errors = []
    for line in path.read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        parsed += 1
        if ((event.get('type') == 'assistant' and event.get('error'))
                or event.get('type') == 'error'
                or (event.get('type') == 'system' and event.get('subtype') == 'api_error')):
            # Do not copy arbitrary error text: it may contain credentials.
            known = {'authentication_failed', 'billing_error', 'rate_limit',
                     'invalid_request', 'server_error', 'unknown'}
            code = event.get('error')
            category = code if isinstance(code, str) and code in known else 'unknown_executor_error'
            executor_errors.append({'event_index': parsed, 'category': category})
        if event.get('type') == 'result':
            last = event
        message = event.get('message', {})
        content = message.get('content', []) if isinstance(message, dict) else []
        for item in content:
            if not isinstance(item, dict) or item.get('type') != 'tool_use':
                continue
            inp = item.get('input', {})
            skill_names = {Path(x).parent.name for x in skill_paths}
            if (item.get('name') == 'Skill' and inp.get('skill') in skill_names) or (item.get('name') == 'Read' and
                    inp.get('file_path') in skill_paths):
                observed.append({'event_type': event.get('type'), 'tool': item['name'], 'input': inp})
    cost = last.get('total_cost_usd') if last else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        cost = None
    return {'usage': last.get('usage') if last and isinstance(last.get('usage'), dict) else None,
            'cost_usd': cost, 'usage_source': 'claude_result' if last else None,
            'cost_field': 'total_cost_usd' if cost is not None else None,
            'skill_use': 'observed' if observed else ('not_observed' if parsed and skill_paths else 'unknown'),
            'raw_result_event': last, 'skill_events': observed, 'executor_error_events': executor_errors}


def classify_executor(raw, evidence, *, max_turns, launcher_exit):
    """Structured executor health, independent of native grading."""
    def verdict(valid, reason):
        return {'valid_execution': valid, 'reason': reason}
    if evidence.get('executor_error_events'):
        return verdict(False, 'executor_api_or_transport_error')
    if raw.get('error'):
        return verdict(False, 'executor_harness_error')
    if launcher_exit not in (0, 1):
        return verdict(False, 'launcher_infrastructure_failure')
    result = evidence.get('raw_result_event')
    if result is not None:
        subtype = result.get('subtype')
        if subtype not in ('success', 'error_max_turns'):
            return verdict(False, 'unresolved_executor_result')
        if result.get('is_error') is not False and subtype != 'error_max_turns':
            return verdict(False, 'executor_result_error')
    if raw.get('agent_timed_out') is True:
        return verdict(True, 'frozen_agent_time_limit')
    if result is None:
        return verdict(False, 'missing_executor_result')
    if result.get('subtype') == 'error_max_turns':
        turns = result.get('num_turns')
        if (type(max_turns) is int and max_turns > 0 and type(turns) is int
                and turns >= max_turns and result.get('is_error') is True):
            return verdict(True, 'frozen_agent_turn_limit')
        return verdict(False, 'unconfirmed_executor_turn_limit')
    if type(raw.get('agent_exit')) is not int or raw['agent_exit'] != 0:
        return verdict(False, 'unexplained_executor_exit')
    return verdict(True, 'executor_completed')


def reusable_trial(record, signature):
    snapshot = record.get('evaluator_snapshot')
    return (record.get('job_status') == 'completed' and record.get('status') in ('pass', 'task_fail')
            and record.get('execution_signature') == signature
            and record.get('verifier_health', {}).get('healthy') is True
            and record.get('native_reward') == int(record.get('status') == 'pass')
            and snapshot is not None and verify_artifacts([snapshot])
            and verify_artifacts(record.get('output_artifacts', [])))


def _atomic_json(path, record):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temp.open('x') as handle:
        json.dump(record, handle, indent=2, allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Input path escapes data root')
    return path


def plan_trial(instance_record, condition, candidate_or_none, config, repeat_id=1):
    if condition not in CONDITIONS:
        raise ValueError('Unknown condition')
    if not isinstance(repeat_id, int) or isinstance(repeat_id, bool) or repeat_id < 1:
        raise ValueError('repeat_id must be a positive integer')
    data_root = Path(config['data_root'])
    if not data_root.is_absolute():
        data_root = ROOT / data_root
    split = instance_record['split']
    if split not in ('train', 'dev', 'test'):
        raise ValueError('Unknown split')
    source_records = [json.loads(line) for line in (data_root / split / 'instances.jsonl').read_text().splitlines() if line.strip()]
    if instance_record not in source_records:
        raise ValueError('Instance record is not the exact active split record')
    task_path = _safe_path(data_root, instance_record['path'])
    bundle = None
    if condition == 'no_skill':
        if candidate_or_none is not None:
            raise ValueError('no_skill must not receive a candidate or support files')
    elif condition == 'full_skill':
        if candidate_or_none is not None:
            raise ValueError('full_skill resolves the original source; do not supply candidate metadata')
        bundle = _safe_path(data_root, instance_record['skill_bundle_path'])
    elif candidate_or_none is not None and candidate_or_none.get('bundle_path'):
        bundle = Path(candidate_or_none['bundle_path']).resolve()
        if candidate_or_none.get('family') != instance_record['family']:
            raise ValueError('Candidate family mismatch')
    if bundle:
        if not list(bundle.rglob('SKILL.md')):
            raise ValueError('No SKILL.md in selected bundle')
        file_manifest(bundle)
    return {'executed': False, 'instance_id': instance_record['instance_id'], 'split': split,
            'family': instance_record['family'], 'condition': condition, 'repeat_id': repeat_id,
            'task_path': str(task_path), 'skill_source': str(bundle) if bundle else None,
            'instruction_sha256': sha256_file(task_path / 'instruction.md'),
            'environment_sha256': tree_hash(task_path / 'environment', exclude_skills=True),
            'bundle_sha256': tree_hash(bundle) if bundle else None,
            'source_execution_signature': execution_signature(instance_record, task_path, bundle, config, repeat_id),
            'shared_family_prebuilt_image': False, 'verifier_visible_during_solving': False}


def _check_live(config, split):
    budget = config.get('budget_usd')
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
        raise ValueError('Live dispatch requires an approved positive numeric budget')
    if not config.get('artifact_root'):
        raise ValueError('Persistent artifact_root required')
    executor = config.get('executor', {})
    if executor.get('agent_id') != 'claude-code':
        raise ValueError('Only the planned Claude Code adapter is implemented')
    if not re.fullmatch(r'[A-Za-z0-9_.:/-]+', executor.get('model_id') or ''):
        raise ValueError('Explicit safe model ID required')
    if not isinstance(executor.get('max_steps'), int) or executor['max_steps'] <= 0:
        raise ValueError('Explicit positive max_steps required')
    if executor.get('provider') == 'deepseek':
        from .deepseek_runtime import solver_environment
        solver_environment(os.environ, config.get('runtime', {}).get('deepseek_mapping', {}))
    elif not os.environ.get('ANTHROPIC_API_KEY'):
        raise ValueError('ANTHROPIC_API_KEY unavailable')
    for required in ('runtime', 'tool_policy', 'cache_regime', 'external_input_versions'):
        if not config.get(required):
            raise ValueError('Unresolved execution setting: ' + required)
    if split != 'train' and not config.get('frozen_protocol_sha256'):
        raise ValueError('Dev/test require the G3 frozen protocol')
    if split == 'test' and not config.get('selected_before_test_manifest_id'):
        raise ValueError('Test requires a selected-before-test manifest')
    probe = subprocess.run(['docker', 'info'], capture_output=True, timeout=15)
    if probe.returncode:
        raise RuntimeError('Docker server unavailable')


def _approved(result, field):
    return result is True or (isinstance(result, dict) and result.get(field) is True)


def run_trial(instance_record, condition, candidate_or_none, config, repeat_id=1, *, execute=False,
              eligibility_check=None, admission=None, frozen_check=None, physical_trial_id=None):
    """Run through the existing staged adapter; hooks enforce independent gates.

    `admission(signature)` must either reserve campaign budget or attest the
    independently enforced central per-request gateway admission boundary.
    A live caller must supply eligibility/freeze checks;
    this runner does not trust candidate self-reported eligibility.
    """
    if physical_trial_id is not None:
        if not isinstance(physical_trial_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', physical_trial_id):
            raise ValueError('Invalid explicit physical trial identity')
        if config.get('pilot_trial_id') is not None:
            raise ValueError('Choose explicit physical identity or legacy pilot identity, not both')
    plan = plan_trial(instance_record, condition, candidate_or_none, config, repeat_id)
    if not execute:
        return plan
    _check_live(config, instance_record['split'])
    if admission is None:
        raise ValueError('Live dispatch requires a persistent budget reservation callback')
    if instance_record['split'] != 'train':
        if frozen_check is None:
            raise ValueError('Live dev/test requires independent freeze verification')
        verified = frozen_check(config, instance_record, candidate_or_none)
        if not _approved(verified, 'verified'):
            raise ValueError('Independent freeze verification did not explicitly pass')
    if condition not in ('full_skill', 'no_skill'):
        if eligibility_check is None:
            raise ValueError('Independent candidate eligibility check required')
        eligible = eligibility_check(candidate_or_none, instance_record, config)
        if not _approved(eligible, 'eligible') or plan['skill_source'] is None:
            raise ValueError('Ineligible candidate: controller must record infeasible without dispatch')
    artifact_root = Path(config['artifact_root']).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    trial_id = physical_trial_id if physical_trial_id is not None else (config.get('pilot_trial_id') or 'trial-' + uuid.uuid4().hex)
    if not isinstance(trial_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', trial_id):
        raise ValueError('Invalid stable physical trial identity')
    job = artifact_root / 'jobs' / trial_id
    job.mkdir(parents=True, mode=0o700)
    base = {'schema_version': 1, 'campaign_id': config['campaign_id'], 'trial_id': trial_id,
            'actual_trial_id': trial_id, 'reused_from_trial_id': None,
            'instance_id': instance_record['instance_id'], 'family': instance_record['family'],
            'split': instance_record['split'], 'condition': condition, 'repeat_id': repeat_id,
            'candidate_id': candidate_or_none.get('candidate_id') if candidate_or_none else None,
            'candidate_sha256': plan['bundle_sha256'], 'plan': plan, 'executed': False,
            'config_sha256': canonical_hash(config), 'execution_config_sha256': execution_config_hash(config),
            'created_at': datetime.now(timezone.utc).isoformat(), 'call_ids': [],
            'selected_before_test_manifest_id': config.get('selected_before_test_manifest_id')}
    from .contracts import TRIAL_FIELDS
    for field in TRIAL_FIELDS:
        base.setdefault(field, None)
    base.update(candidate_hash=plan['bundle_sha256'], actual_bundle_hash=plan['bundle_sha256'],
                source_environment_sha256=plan['environment_sha256'], architecture=platform.machine(),
                effective_tools=config.get('tool_policy'), agent_timed_out=False, cost_available=False,
                selection_manifest_id=config.get('selected_before_test_manifest_id'), output_artifacts=[])
    git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    base['git_commit'] = git.stdout.strip() if git.returncode == 0 else None
    _atomic_json(job / 'pending.json', dict(base, job_status='pending'))
    began = time.monotonic()
    try:
        # Freeze the exact selected bytes in this private job before dispatch.
        if plan['skill_source'] is not None:
            selected = Path(plan['skill_source'])
            if tree_hash(selected) != plan['bundle_sha256']:
                raise ValueError('Selected bundle changed after planning')
            snapshot_bundle = job / 'selected_bundle'
            shutil.copytree(selected, snapshot_bundle)
            if tree_hash(snapshot_bundle) != plan['bundle_sha256']:
                raise ValueError('Selected bundle changed during snapshot')
            plan = dict(plan, skill_source=str(snapshot_bundle))
            base['plan'] = plan
            base['candidate_snapshot_path'] = str(snapshot_bundle)
        stage = job / 'stage'
        if config['runtime'].get('staging_policy')=='single-active-instance-v1':
            from .single_stage import stage_instance
            stage_instance(instance_record,(ROOT/config['data_root']).resolve(),stage)
        elif config['runtime'].get('staging_policy') is None:
            subprocess.run([sys.executable, str(ROOT / 'milestone 1/code/stage_skilllearnbench.py'),
                            '--split', instance_record['split'], '--data', str((ROOT / config['data_root']).resolve()),
                            '--output', str(stage)], check=True, capture_output=True, text=True)
        else:raise ValueError('Unknown staging policy')
        from .runtime import pin_staged_runtime
        pin_staged_runtime(stage, config)
        task = stage / 'tasks' / instance_record['family'] / instance_record['instance_id']
        base['staged_environment_sha256'] = tree_hash(task / 'environment', exclude_skills=True)
        base['execution_signature'] = execution_signature(instance_record, task, plan['skill_source'], config, repeat_id)
        base['reservation'] = admission(base['execution_signature'])
        if not isinstance(base['reservation'], dict) or not base['reservation'].get('reservation_id'):
            raise ValueError('Budget admission did not return a recorded reservation ID')
        _atomic_json(job / 'running.json', dict(base, job_status='running'))
        launcher = stage / 'capstudy_trial_launch.py'
        payload = {'task_id': instance_record['family'] + '/' + instance_record['instance_id'],
                   'agent_id': config['executor']['agent_id'], 'model': config['executor']['model_id'],
                   'skill_config': condition, 'skill_source_dir': plan['skill_source'],
                   'trial_id': trial_id, 'max_steps': config['executor']['max_steps']}
        (stage / 'capstudy_launch.json').write_text(json.dumps(payload))
        launcher.write_text('''import json\nfrom pathlib import Path\nfrom core.eval_runner import run_task\na=json.loads(Path("capstudy_launch.json").read_text())\na["skill_source_dir"]=Path(a["skill_source_dir"]) if a["skill_source_dir"] else None\ncode,result=run_task(**a)\nPath("capstudy_raw_result.json").write_text(json.dumps(result,indent=2))\nraise SystemExit(code)\n''')
        # This records a launched harness attempt, not a claim that a model API
        # request occurred. Real physical API calls are reconciled separately.
        # A later archive/grader exception must not erase the fact of launch.
        base.update(executed=True,launcher_started=True,model_request_count=None)
        _atomic_json(job/'launching.json',dict(base,job_status='running'))
        guard = config['runtime'].get('launch_guard')
        if guard:
            from .launcher_guard import guarded_launch
            container = 'evaluation_' + payload['task_id'].replace('/', '-') + '_' + condition + '_' + trial_id.lower()
            process = guarded_launch([sys.executable, str(launcher)], cwd=stage, logs=job,
                container_names=[container, container + '-verifier'],
                timeout_seconds=guard['timeout_seconds'], minimum_free_bytes=guard['minimum_free_bytes'],
                disk_paths=guard['disk_paths'])
        else:
            with (job / 'launcher.stdout').open('w') as stdout, (job / 'launcher.stderr').open('w') as stderr:
                process = subprocess.run([sys.executable, str(launcher)], cwd=stage, stdout=stdout, stderr=stderr)
        raw_path = stage / 'capstudy_raw_result.json'
        raw = json.loads(raw_path.read_text()) if raw_path.exists() else {'error': 'missing_native_result'}
        trial = stage / 'output/evaluation_log' / condition / instance_record['family'] / instance_record['instance_id'] / trial_id
        health = classify_native(task, trial / 'verifier', raw.get('verifier_exit'))
        formal_policy=config['runtime'].get('formal_verifier_policy')
        if formal_policy is not None:
            from .formal_verifier import classify_saved_formal
            base['raw_verifier_health']=health
            base['formal_verifier_policy']=formal_policy
            health=classify_saved_formal(task,trial/'verifier',raw.get('verifier_exit'),formal_policy)
        trace = trial / 'agent/claude-code.txt'
        skill_paths = []
        if plan['skill_source']:
            for skill in Path(plan['skill_source']).rglob('SKILL.md'):
                relative = skill.relative_to(Path(plan['skill_source'])).as_posix()
                skill_paths.extend([f'/root/.claude/skills/{relative}', f'/root/verifier-skills/{relative}'])
                mapping = config.get('runtime', {}).get('deepseek_mapping')
                if mapping:
                    skill_paths.append(f'{mapping["skill_directory"]}/{relative}')
        evidence = extract_claude_evidence(trace, skill_paths)
        snapshot_path = trial / 'evaluator_snapshot.json'
        snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else None
        if health['healthy'] and snapshot is None:
            health = dict(status='infra_error', healthy=False, native_reward=health['native_reward'], reason='missing_evaluator_snapshot')
        artifacts = [{'path': str(p.resolve()), 'sha256': sha256_file(p)} for p in sorted(trial.rglob('*')) if p.is_file()] if trial.exists() else []
        executor_health = classify_executor(raw, evidence,
            max_turns=config['executor']['max_steps'], launcher_exit=process.returncode)
        final_status = health['status'] if executor_health['valid_execution'] else 'infra_error'
        base.update(executed=True, status=final_status, native_reward=health['native_reward'],
                    executor_health=executor_health,
                    verifier_health=health, grader_exit=raw.get('verifier_exit'),
                    agent_exit=raw.get('agent_exit'), agent_timed_out=bool(raw.get('agent_timed_out', False)),
                    diagnostic_category=(health['reason'] if executor_health['valid_execution'] else executor_health['reason']),
                    agent_trace_path=str(trace), verifier_log_path=str(trial / 'verifier'),
                    instruction_path=str(task / 'instruction.md'), instruction_sha256=sha256_file(task / 'instruction.md'),
                    artifacts=artifacts, output_artifacts=artifacts, evaluator_snapshot=snapshot,
                    image_digest=snapshot.get('image_id') if snapshot else None, cost_available=evidence['cost_usd'] is not None, reference_sha256=tree_hash(task / 'tests'),
                    task_path=str(task), native_result=raw, usage=evidence['usage'], cost_usd=evidence['cost_usd'],
                    usage_source=evidence['usage_source'], skill_use=evidence['skill_use'], trace_evidence=evidence,
                    model_id=config['executor']['model_id'], cli_version=config['executor']['cli_version'],
                    effective_limits=raw.get('effective_limits'), launcher_exit=process.returncode)
        if config['executor'].get('provider') == 'deepseek':
            # CLI estimates use Anthropic model pricing; only the centrally
            # recorded gateway usage/peak-tariff accounting is authoritative.
            base.update(cost_available=False, cost_usd=None,
                        provider_usage_attachment_status='awaiting_central_gateway_audit',
                        cli_usage_diagnostic=evidence['usage'], usage=None,
                        usage_source='central_gateway_attachment_pending')
    except Exception as exc:
        # Exception text may contain credentials in subprocess arguments: retain
        # its category, not an unsafe stringification of an arbitrary exception.
        base.update(status='infra_error', diagnostic_category=type(exc).__name__,
                    usage=None, cost_usd=None, native_reward=None)
    base['duration_seconds'] = time.monotonic() - began
    base['job_status'] = 'infra_error' if base['status'] == 'infra_error' else 'completed'
    from .contracts import validate_trial
    validate_trial(base)
    _atomic_json(job / 'completed.json', base)
    return base


@contextmanager
def open_saved_snapshot(snapshot):
    """Yield the actual verified archive; CAS scratch lives through Docker load."""
    from .saved_image_identity import inspect_saved_identity
    if not snapshot or not verify_artifacts([snapshot]):
        raise ValueError('Missing or mismatched evaluator snapshot')
    if snapshot.get('kind')=='oci-cas-v1':
        from .snapshot_retention import SnapshotRetention
        if (not isinstance(snapshot.get('original_archive_sha256'),str)
                or not re.fullmatch(r'[0-9a-f]{64}',snapshot['original_archive_sha256'])):
            raise ValueError('CAS snapshot requires bound original archive hash')
        retention=SnapshotRetention(**snapshot['storage_config'])
        with retention.materialize(snapshot['path']) as materialized:
            if (materialized['image_id']!=snapshot['image_id']
                    or materialized['sha256']!=snapshot['original_archive_sha256']):
                raise ValueError('CAS image/archive identity mismatch')
            yield materialized
    else:
        inspect_saved_identity(snapshot)
        yield snapshot


def verify_saved_image_identity(snapshot):
    """Verify raw legacy/OCI or retained CAS identity without a Docker call."""
    # The legacy helper historically accepts identity-only raw descriptors; its
    # outer callers verify archive hashes separately. Preserve that API.
    if snapshot.get('kind')!='oci-cas-v1':
        from .saved_image_identity import inspect_saved_identity
        inspect_saved_identity(snapshot)
    else:
        with open_saved_snapshot(snapshot):pass
    return True


def regrade_saved(output_record, gold_record, workdir):
    """Rerun native grading on the actual verified persisted archive."""
    snapshot=output_record.get('evaluator_snapshot')
    if not snapshot or not verify_artifacts([snapshot]):
        raise ValueError('Missing or mismatched evaluator snapshot')
    task=Path(gold_record['task_path']).resolve()
    if tree_hash(task/'tests')!=gold_record['reference_sha256']:
        raise ValueError('Native reference/grader hash mismatch')
    if output_record.get('instance_id')!=gold_record.get('instance_id'):
        raise ValueError('Output/reference instance mismatch')
    formal=(snapshot.get('kind')=='oci-cas-v1' or output_record.get('formal_verifier_policy') is not None
            or gold_record.get('formal_verifier_policy') is not None
            or gold_record.get('formal_verifier_policy_sha256') is not None)
    policy=None
    if formal:
        from .contracts import canonical_hash as evidence_hash
        from .formal_regrade import validate_regrade_policy
        policy=gold_record.get('formal_verifier_policy');expected=gold_record.get('formal_verifier_policy_sha256')
        validate_regrade_policy(policy,expected,task)
        if (not isinstance(output_record.get('formal_verifier_policy'),dict)
                or evidence_hash(output_record['formal_verifier_policy'])!=expected):
            raise ValueError('Output formal policy differs from independent frozen reference')
    if workdir is None:raise ValueError('Native regrading requires an explicit new workdir')
    workdir=Path(workdir).resolve()
    workdir.mkdir(parents=True,exist_ok=False)
    with open_saved_snapshot(snapshot) as actual:
        load_options={'timeout':600} if formal else {}
        subprocess.run(['docker','image','load','-i',actual['path']],check=True,capture_output=True,**load_options)
        image_id=actual['image_id']
        if formal:
            from .formal_regrade import regrade_loaded_image
            return regrade_loaded_image(image_id,task,workdir,policy=policy,
                expected_policy_sha256=expected,native_classifier=classify_native,
                timeout=gold_record.get('verifier_seconds',1800))
        name='capstudy-regrade-'+uuid.uuid4().hex
        try:
            process=subprocess.run(['docker','run','--rm','--name',name,'--entrypoint','bash',
                '-v',f'{task/"tests"}:/tests:ro','-v',f'{workdir}:/logs/verifier',image_id,'/tests/test.sh'],
                capture_output=True,text=True,timeout=gold_record.get('verifier_seconds',1800))
            (workdir/'process-stdout.txt').write_text(process.stdout or '')
            (workdir/'process-stderr.txt').write_text(process.stderr or '')
            return classify_native(task,workdir,process.returncode)
        finally:
            subprocess.run(['docker','rm','-f',name],capture_output=True)
            # Imported images may be shared across scoring jobs; do not prune.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--condition', choices=sorted(CONDITIONS), required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--split', choices=['train', 'dev', 'test'], default='train')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    data = Path(config['data_root'])
    if not data.is_absolute(): data = ROOT / data
    rows = [json.loads(x) for x in (data / args.split / 'instances.jsonl').read_text().splitlines()]
    row = next(r for r in rows if r['instance_id'] == args.instance)
    candidate = json.loads(args.candidate.read_text()) if args.candidate else None
    print(json.dumps(run_trial(row, args.condition, candidate, config), indent=2))

if __name__ == '__main__':
    main()
