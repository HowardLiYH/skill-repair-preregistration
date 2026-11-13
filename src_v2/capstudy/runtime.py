"""Runtime probes and checked, staged-only changes to the pinned harness.

No probe launches a model. No compatibility or egress guarantee is inferred from
version strings; live calibration must establish those properties.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


def probe_runtime() -> dict:
    result = {'evidence_kind': 'local_readiness_probe', 'executed_model': False,
              'platform': platform.platform(), 'architecture': platform.machine(),
              'python': sys.version, 'tools': {},
              'credential_presence': {k: bool(os.environ.get(k)) for k in
                                      ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY')}}
    for name, args in [('docker', ['version']), ('claude', ['--version']), ('node', ['--version'])]:
        path = shutil.which(name)
        item = result['tools'][name] = {'path': path}
        if path:
            try:
                process = subprocess.run([path, *args], capture_output=True, text=True, timeout=15)
                item.update(returncode=process.returncode, stdout=process.stdout, stderr=process.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                item['error'] = type(exc).__name__
    result['docker_server_available'] = result['tools']['docker'].get('returncode') == 0
    result['live_readiness_established'] = False
    return result


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError('Pinned staged source mismatch; refusing ambiguous runtime patch')
    return text.replace(old, new, 1)


def pin_staged_runtime(stage: Path, config: dict) -> None:
    """Pin an already-staged harness; never touch archived assets.

    Runtime layer is supplied as a reviewed Dockerfile RUN block. Its hash and
    exact Node/CLI versions are required; no guessed tested version is shipped.
    """
    runtime = config['runtime']
    node, cli = runtime['node_version'], config['executor']['cli_version']
    if not all(re.fullmatch(r'\d+\.\d+\.\d+', x or '') for x in (node, cli)):
        raise ValueError('Exact Node and CLI versions required')
    layer_path = Path(runtime['dependency_layer_file'])
    layer = layer_path.read_text()
    if hashlib.sha256(layer.encode()).hexdigest() != runtime['dependency_layer_sha256']:
        raise ValueError('Runtime dependency layer hash mismatch')
    if re.search(r'\blatest\b', layer, re.I):
        raise ValueError('Unpinned latest dependency is prohibited')
    if not layer.strip().startswith('RUN '):
        raise ValueError('Dependency layer must begin with RUN')
    dockerfile_updates = []
    for dockerfile in sorted((stage / 'tasks').glob('*/*/environment/Dockerfile')):
        content = dockerfile.read_text()
        match = re.search(r'^COPY\s+(?:--\S+\s+)*skills(?:/|\s)', content, re.M | re.I)
        if not match:
            raise ValueError(f'No skill COPY insertion point: {dockerfile}')
        validation = (f'\nRUN test "$(node --version)" = "v{node}" && '
                      f'test "$(claude --version | cut -d \' \' -f 1)" = "{cli}"\n'
                      'ENV DISABLE_AUTOUPDATER=1\n')
        neutral=''
        cache=runtime.get('stock_dependency_cache')
        if cache is not None and dockerfile.parents[2].name=='stock-data-visualization':
            if runtime.get('staging_policy')!='single-active-instance-v1':
                raise ValueError('Neutral dependencies require isolated instance staging')
            from .stock_cache_staging import apply_staged_cache
            binding_path=Path(cache['descriptor_path'])
            if binding_path.is_symlink() or not binding_path.is_file():
                raise ValueError('Frozen neutral dependency descriptor changed')
            binding_bytes=binding_path.read_bytes()
            if hashlib.sha256(binding_bytes).hexdigest()!=cache['descriptor_file_sha256']:
                raise ValueError('Frozen neutral dependency descriptor changed')
            binding=json.loads(binding_bytes)
            neutral=apply_staged_cache(dockerfile.parent,Path(cache['cache_path']),binding,
                cache['descriptor_sha256'])
        dockerfile_updates.append((dockerfile, content[:match.start()] + layer.rstrip() + validation + neutral + content[match.start():]))
    source = stage / 'core/eval_runner.py'
    text = source.read_text()
    text = replace_once(text, '    prebuilt_has_agent: bool = False,', '    prebuilt_has_agent: bool = True,')
    cpu, memory = runtime['cpus'], runtime['memory']
    if not isinstance(cpu, (int, float)) or isinstance(cpu, bool) or not math.isfinite(cpu) or cpu <= 0:
        raise ValueError('Positive CPU limit required')
    if not re.fullmatch(r'[1-9]\d*(?:[kmgKMG])?', str(memory)):
        raise ValueError('Explicit Docker memory limit required')
    network = runtime.get('network')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', network or ''):
        raise ValueError('Explicit Docker network required')
    text = replace_once(text, '                "-d",\n',
                        f'                "-d",\n                "--cpus", {str(cpu)!r},\n                "--memory", {str(memory)!r},\n                "--network", {network!r},\n')
    if config['executor'].get('provider') == 'deepseek':
        profile=runtime.get('execution_profile','bounded-pilot-v1')
        if profile not in ('bounded-pilot-v1','native-task-limits-v1'):
            raise ValueError('Unknown execution resource profile')
        if profile=='bounded-pilot-v1':
            build_timeout=runtime.get('build_timeout_seconds')
            if type(build_timeout) is not int or not 1<=build_timeout<=600:
                raise ValueError('DeepSeek pilot requires an explicit build timeout <=600 seconds')
            text=replace_once(text,
                '                ["docker", "build", "-t", image_tag, str(build_env)],\n                capture_output=True,\n                text=True,\n                timeout=limits["build_seconds"],',
                '                ["docker", "build", "-t", image_tag, str(build_env)],\n                capture_output=True,\n                text=True,\n                timeout=min(limits["build_seconds"], '+str(build_timeout)+'),')
        else:
            # A prospective formal profile retains each native task's declared
            # build/agent/verifier times, instead of inheriting GDP pilot caps.
            if 'build_timeout_seconds' in runtime:
                raise ValueError('Native task profile cannot silently override declared build time')
            build_cpus=runtime.get('build_cpus'); build_memory=runtime.get('build_memory')
            if type(build_cpus) is not int or not 1<=build_cpus<=8:
                raise ValueError('Explicit bounded integer build CPUs required')
            if not re.fullmatch(r'[1-9]\d*(?:[kmgKMG])?', str(build_memory)):
                raise ValueError('Explicit build memory limit required')
            caches=runtime.get('build_cache_from',[])
            if (not isinstance(caches,list) or len(caches)>8
                    or any(not isinstance(v,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',v) for v in caches)
                    or caches!=sorted(set(caches))):
                raise ValueError('At most eight unique immutable input-image cache IDs required')
            flags=['docker','build','--memory',str(build_memory),'--memory-swap',str(build_memory),
                   '--cpu-period','100000','--cpu-quota',str(build_cpus*100000)]
            for image_id in caches:flags.extend(['--cache-from',image_id])
            text=replace_once(text,'import json_repair\n',
                'import json_repair\nfrom capstudy_build_runtime import run_build\n')
            text=replace_once(text,
                '            r = subprocess.run(\n                ["docker", "build", "-t", image_tag, str(build_env)],\n'
                '                capture_output=True,\n                text=True,\n'
                '                timeout=limits["build_seconds"],\n            )',
                '            r = run_build(\n                '+repr(flags)+' + ["-t", image_tag, str(build_env)],\n'
                '                timeout=limits["build_seconds"], log_dir=trial_path,\n            )')
            text=replace_once(text,
                'subprocess.run(["docker", "image", "rm", "-f", image_tag], capture_output=True)',
                'subprocess.run(["docker", "image", "rm", "-f", "--no-prune", image_tag], capture_output=True)')
        text=replace_once(text, '                "-d",\n',
            '                "-d",\n                "--cap-drop", "ALL",\n                "--security-opt", "no-new-privileges",\n                "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",\n                "--sysctl", "net.ipv6.conf.default.disable_ipv6=1",\n')
    denied = config['tool_policy']['disallowed_tools']
    if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', x) for x in denied):
        raise ValueError('Tool deny list must contain tool names')
    flags = ' --disallowedTools ' + ','.join(sorted(denied)) if denied else ''
    text = replace_once(text, '            _extra_flags = ""  # same settings for every instance; pin executor in calibration',
                        '            _extra_flags = ' + repr(flags))
    formal_verifier=runtime.get('formal_verifier_policy')
    if formal_verifier is not None:
        if (runtime.get('execution_profile')!='native-task-limits-v1'
                or runtime.get('staging_policy')!='single-active-instance-v1'):
            raise ValueError('Formal verifier requires native limits and single-instance staging')
        from .formal_verifier import validate_policy
        for task_path in sorted((stage/'tasks').glob('*/*')):
            if (task_path/'task.toml').is_file():validate_policy(formal_verifier,task_path)
        text=replace_once(text,
            '                    timeout=limits["verifier_seconds"],\n                )',
            '                    timeout=limits["verifier_seconds"],\n'
            '                    formal_policy='+repr(formal_verifier)+',\n                )')
        text=replace_once(text,'import json_repair\n',
            'import json_repair\nfrom capstudy.formal_verifier import classify_saved_formal\n')
        text=replace_once(text,
            '            health = classify_verifier(task_path, trial_path / "verifier", verifier.returncode)',
            '            health = classify_saved_formal(task_path, trial_path / "verifier", verifier.returncode, '+repr(formal_verifier)+')')
    compile(text, str(source), 'exec')
    for dockerfile, content in dockerfile_updates:
        dockerfile.write_text(content)
    source.write_text(text)
    shutil.copy2(Path(__file__).with_name('isolated_verifier.py'), stage / 'project_isolation.py')
    if runtime.get('execution_profile')=='native-task-limits-v1':
        shutil.copy2(Path(__file__).with_name('build_runtime.py'),stage/'capstudy_build_runtime.py')
    if formal_verifier is not None:
        support=stage/'capstudy';support.mkdir(exist_ok=False)
        (support/'__init__.py').write_text('"""Pinned evaluator-only runtime support."""\n')
        for name in ('formal_verifier','native_evidence','snapshot_retention','oci_archive_store','saved_image_identity','contracts','launcher_guard'):
            shutil.copy2(Path(__file__).with_name(name+'.py'),support/(name+'.py'))
    if config['executor'].get('provider') == 'deepseek':
        from .deepseek_runtime import apply_staged_mapping
        apply_staged_mapping(stage, config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    record = probe_runtime()
    text = json.dumps(record, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text)

if __name__ == '__main__':
    main()
