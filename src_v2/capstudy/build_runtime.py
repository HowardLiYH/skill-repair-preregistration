"""Staged stdlib-only bounded legacy Docker build with durable diagnostics."""
import json
import os
from pathlib import Path
import subprocess
import time


def run_build(command, *, timeout, log_dir):
    logs = Path(log_dir)
    if not logs.is_dir() or logs.is_symlink():
        raise ValueError('Existing trial-owned log directory required')
    if (command[:2] != ['docker', 'build'] or '--cpu-quota' not in command
            or '--memory' not in command or '--memory-swap' not in command):
        raise ValueError('Explicit resource-bounded Docker build required')
    env = dict(os.environ, DOCKER_BUILDKIT='0')
    started = time.monotonic()
    info = {'builder_backend': 'docker-legacy', 'DOCKER_BUILDKIT': '0',
            'command': command, 'timeout_seconds': timeout}
    def save(stdout, stderr):
        for name, content in [('build.stdout', stdout), ('build.stderr', stderr)]:
            (logs / name).write_text(content.decode(errors='replace')
                if isinstance(content, bytes) else content or '')
        info['duration_seconds'] = time.monotonic() - started
        (logs / 'build.json').write_text(json.dumps(info, indent=2) + '\n')
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        info.update(timed_out=True, returncode=None)
        save(exc.stdout, exc.stderr)
        raise
    except OSError as exc:
        info.update(timed_out=False, returncode=None, error_category=type(exc).__name__)
        save('', '')
        raise
    info.update(timed_out=False, returncode=result.returncode)
    save(result.stdout, result.stderr)
    return result
