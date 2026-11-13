"""Staged overlay: preserve a stopped solver image before isolated native grading.

The snapshot is evaluator-only, kept with restrictive permissions and never
passed to a solver/editor. It may contain task data and must not be published.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess


def run_isolated_verifier(container, task_path, trial_path, secret_env_names=(), *, timeout=1800, runner=None, formal_policy=None):
    if formal_policy is not None:
        from capstudy.formal_verifier import run_formal_verifier
        # The archived adapter copies this standalone module into each stage.
        # Direct package calls use the same archived classifier via its loader.
        if Path(__file__).with_name('verifier_health.py').is_file():
            from verifier_health import classify_verifier as native_classifier
        else:
            from capstudy.runner import classify_native as native_classifier
        return run_formal_verifier(container,task_path,trial_path,secret_env_names,
            timeout=timeout,policy=formal_policy,native_classifier=native_classifier,runner=runner)
    run = runner or subprocess.run
    task_path, trial_path = Path(task_path), Path(trial_path)
    logs = trial_path / 'verifier'
    logs.mkdir(parents=True, exist_ok=True)
    for name in ('reward.txt', 'ctrf.json', 'test-stdout.txt', 'process-stdout.txt', 'process-stderr.txt'):
        (logs / name).unlink(missing_ok=True)
    snapshot = container.lower() + '-verification:saved'
    name = container + '-verifier'
    common = {'capture_output': True, 'text': True}
    try:
        run(['docker', 'stop', '--time', '1', container], check=True, **common)
        changes = []
        for key in sorted(set(secret_env_names)):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError('Invalid credential environment name')
            changes.extend(['--change', f'ENV {key}='])
        run(['docker', 'commit', *changes, container, snapshot], check=True, **common)
        archive = trial_path / 'evaluator-image.tar'
        # Precreate with private permissions, including on hosts with permissive umask.
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        run(['docker', 'image', 'save', '-o', str(archive), snapshot], check=True, **common)
        digest = hashlib.sha256()
        with archive.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        inspect = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', snapshot], check=True, **common)
        metadata = {'path': str(archive.resolve()), 'sha256': digest.hexdigest(),
                    'image_id': inspect.stdout.strip(), 'image_tag': snapshot,
                    'evaluator_only': True, 'capture_phase': 'before_grading'}
        (trial_path / 'evaluator_snapshot.json').write_text(json.dumps(metadata, indent=2) + '\n')
        completed = run(['docker', 'run', '--rm', '--name', name, '--entrypoint', 'bash',
                         '-v', f'{task_path / "tests"}:/tests:ro',
                         '-v', f'{logs}:/logs/verifier', snapshot, '/tests/test.sh'],
                        timeout=timeout, **common)
        (logs / 'process-stdout.txt').write_text(completed.stdout or '')
        (logs / 'process-stderr.txt').write_text(completed.stderr or '')
        return completed
    except subprocess.TimeoutExpired as exc:
        for filename, content in [('process-stdout.txt', exc.stdout), ('process-stderr.txt', exc.stderr)]:
            (logs / filename).write_text(content.decode(errors='replace') if isinstance(content, bytes) else content or '')
        raise
    finally:
        run(['docker', 'rm', '-f', name], **common)
        run(['docker', 'image', 'rm', '-f', snapshot], **common)
