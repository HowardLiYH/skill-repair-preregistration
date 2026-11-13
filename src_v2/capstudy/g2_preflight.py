"""Bounded, model-free native grader controls for evaluator-only G2 fixtures.

This never runs an agent. Native Dockerfiles and graders are copied unchanged;
only skills are empty. A generated reference is checkpointed before any grade.
Every control receives a fresh clone and separate logs. Images stay remote.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib

from .contracts import write_json_atomic
from .grader_preflight import monitored_process
from .runner import ROOT, classify_native, sha256_file

FAMILIES = ('schedule-planning', 'temperature-simulation', 'organize-messy-files')
CONTROLS = ('healthy_reference', 'missing_required_output', 'incomplete_output', 'dependency_failure', 'untouched_input')
RESERVE = 30 * 1024**3


def stage_cached_pdfs(data, context, instance_id):
    """Apply the already reviewed project cache substitution with fresh hashes."""
    manifest_path = data/'external/pdf_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    records = [r for r in manifest['files'] if instance_id in r['instances']]
    if len(records) != 100 or any(r['status'] != 'validated' for r in records):
        raise ValueError('Exactly 100 validated calibration PDFs required')
    cache = context/'__project_pdfs'
    cache.mkdir()
    evidence = []
    for row in records:
        source = (data/row['path']).resolve()
        source.relative_to(data.resolve())
        if sha256_file(source) != row['sha256'] or (cache/source.name).exists():
            raise ValueError('Cached PDF content or filename identity mismatch')
        shutil.copy2(source, cache/source.name)
        evidence.append({'filename':source.name,'sha256':row['sha256'],'bytes':source.stat().st_size})
    dockerfile = context/'Dockerfile'
    text, count = re.subn(r"RUN <<'EOF'\n.*?\nEOF", 'COPY __project_pdfs/ /root/papers/all/',
                         dockerfile.read_text(), count=1, flags=re.S)
    if count != 1:
        raise ValueError('Native PDF download block changed')
    dockerfile.write_text(text)
    return {'pdf_manifest_sha256':sha256_file(manifest_path),'files':evidence,
            'staged_dockerfile_sha256':sha256_file(dockerfile),
            'transformation':'Existing project cache COPY replaces native arXiv download block only'}


def validate_fixture(task, fixture):
    manifest = json.loads((fixture / 'manifest.json').read_text())
    if manifest.get('scope') != 'calibration_evaluator_only' or manifest.get('split') != 'train':
        raise ValueError('Only evaluator-only train fixtures may be executed')
    bindings = {'native_dockerfile_sha256': task/'environment/Dockerfile',
                'task_config_sha256': task/'task.toml', 'instruction_sha256': task/'instruction.md',
                'runtime_sha256': fixture/'reference_fixture.py'}
    for key, path in bindings.items():
        if sha256_file(path) != manifest[key]:
            raise ValueError('Fixture source hash mismatch: ' + key)
    for item in manifest['source_bindings']:
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts or '__pycache__' in relative.parts or relative.suffix == '.pyc':
            raise ValueError('Fixture must bind stable source files only')
        if item['root'] not in ('tests', 'fixture'):
            raise ValueError('Unexpected fixture source root')
        path = (task/'tests' if item['root'] == 'tests' else fixture) / relative
        if path.is_symlink() or sha256_file(path) != item['sha256']:
            raise ValueError('Fixture source binding mismatch')
    return manifest


def container_command(image, name, task, fixture, logs, command, *, reference_state=None):
    command_line = ['docker', 'run', '--name', name, '--cpus', '1', '--memory', '4g',
                    '--security-opt', 'no-new-privileges', '--entrypoint', 'bash',
                    '-v', f'{task/"tests"}:/tests:ro', '-v', f'{fixture}:/reference:ro',
                    '-v', f'{logs/"reference"}:/logs/reference', '-v', f'{logs/"verifier"}:/logs/verifier']
    if reference_state:
        command_line += ['-v', f'{reference_state}:/reference-state:ro']
    return command_line + [image, '-lc', command]


def fixture_command(mode):
    if mode not in CONTROLS or mode == 'untouched_input':
        raise ValueError('Invalid reference fixture mode')
    command = 'python3 /reference/reference_fixture.py --mode ' + mode + ' --workdir /root --tests /tests --logs /logs/reference'
    if mode != 'healthy_reference':
        command += ' --reference-state /reference-state/reference_manifest.json'
    return command


def control_expectation(mode, health, log_text):
    """A generic infrastructure failure cannot satisfy the intentional fault."""
    expected = 'pass' if mode == 'healthy_reference' else ('infra_error' if mode == 'dependency_failure' else 'task_fail')
    fault_marker = bool(re.search(r'unrecognized arguments?:[^\n]*--capstudy-g2-deliberate-invalid-option', log_text))
    unrelated_setup_error = bool(re.search(
        r'Failed to fetch|Could not resolve|Could not connect|Temporary failure resolving|'
        r'ModuleNotFoundError|No module named|command not found|ERROR: Could not|ERROR: No matching distribution',
        log_text, re.IGNORECASE))
    met = health['status'] == expected and not unrelated_setup_error
    if mode == 'dependency_failure': met = met and fault_marker
    return {'expected_status': expected, 'expectation_met': met,
            'intentional_runtime_fault_marker': fault_marker if mode == 'dependency_failure' else None,
            'unrelated_setup_failure_observed': unrelated_setup_error}


def execute(family, fixture, output):
    if family not in FAMILIES:
        raise ValueError('Unsupported bounded fixture family')
    fixture, output = Path(fixture).resolve(), Path(output).resolve()
    task = ROOT/'milestone 1/data/train/tasks'/family/(family+'-1')
    manifest = validate_fixture(task, fixture)
    if manifest['family'] != family or output.exists() or not output.is_relative_to(ROOT/'private_artifacts'):
        raise ValueError('Use matching fixture and new output directory')
    if shutil.disk_usage(output.parent).free < RESERVE + 10*1024**3:
        raise ValueError('Require 30 GiB reserve plus 10 GiB bounded working allowance')
    cfg = tomllib.loads((task/'task.toml').read_text())
    output.mkdir(parents=True)
    suffix = output.name.lower()
    if not suffix.replace('-', '').isalnum():
        raise ValueError('Output name must be alphanumeric with hyphens')
    tag = 'capstudy-g2:' + suffix
    checkpoint = 'capstudy-g2-reference:' + suffix
    record = {'scope': 'calibration_evaluator_only', 'family': family, 'executed_model': False,
              'scientific_performance_result': False, 'started_unix': time.time(),
              'controller_sha256': sha256_file(Path(__file__)),
              'fixture_manifest_sha256': sha256_file(fixture/'manifest.json'),
              'native_dockerfile_sha256': manifest['native_dockerfile_sha256'],
              'reference_snapshot_before_grading': True, 'base_image_tag': tag,
              'checkpoint_image_tag': checkpoint, 'controls': [], 'checkpoint_passed': False}
    write_json_atomic(output/'pending.json', record)
    context = output/'build_context'
    shutil.copytree(task/'environment', context, ignore=shutil.ignore_patterns('skills', '__pycache__', '*.pyc'))
    (context/'skills').mkdir()
    if family == 'organize-messy-files':
        record['pdf_cache'] = stage_cached_pdfs(ROOT/'milestone 1/data', context, family+'-1')
    current_container = None
    try:
        build = monitored_process(['docker', 'build', '--memory', '4g', '--memory-swap', '4g',
                                   '--cpu-period', '100000', '--cpu-quota', '100000', '-t', tag, str(context)], output/'build',
            reserve_bytes=RESERVE, timeout=min(900, cfg['environment']['build_timeout_sec']))
        if build.returncode:
            raise RuntimeError('Native build failed; preserved logs are authoritative')
        record['base_image'] = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag], timeout=30))[0]
        generation = output/'generation'
        for subdir in ('reference', 'verifier'): (generation/subdir).mkdir(parents=True)
        # Record the actual native package versions before reference generation.
        # The unchanged grader provisions its own pytest dependencies afterward.
        command = 'python3 -m pip freeze && ' + fixture_command('healthy_reference')
        current_container = 'capstudy-g2-' + suffix + '-generation'
        generated = monitored_process(container_command(tag, current_container, task, fixture, generation, command),
            generation/'process', reserve_bytes=RESERVE, timeout=900, container_name=current_container)
        record['generation_exit'] = generated.returncode
        if generated.returncode:
            raise RuntimeError('Reference generation failed; no healthy-reference claim or negative clones')
        subprocess.run(['docker', 'commit', current_container, checkpoint], check=True, capture_output=True, timeout=120)
        record['checkpoint_image'] = json.loads(subprocess.check_output(['docker', 'image', 'inspect', checkpoint], timeout=30))[0]
        subprocess.run(['docker', 'rm', current_container], check=True, capture_output=True, timeout=30)
        current_container = None
        for mode in CONTROLS:
            logs = output/mode
            for subdir in ('reference', 'verifier'): (logs/subdir).mkdir(parents=True)
            current_container = 'capstudy-g2-' + suffix + '-' + mode.replace('_', '-')
            prefix = fixture_command(mode) + ' && ' if mode not in ('healthy_reference', 'untouched_input') else ''
            grader = 'env PYTEST_ADDOPTS=--capstudy-g2-deliberate-invalid-option bash /tests/test.sh' if mode == 'dependency_failure' else 'bash /tests/test.sh'
            image = tag if mode == 'untouched_input' else checkpoint
            command = container_command(image, current_container, task, fixture, logs, prefix+grader,
                                        reference_state=generation/'reference')
            result = monitored_process(command, logs/'process', reserve_bytes=RESERVE,
                timeout=min(900, cfg['verifier']['timeout_sec']), container_name=current_container)
            # Native parser looks for the enclosing process log alongside reports.
            shutil.copy2(logs/'process/process-stdout.txt', logs/'verifier/process-stdout.txt')
            shutil.copy2(logs/'process/process-stderr.txt', logs/'verifier/process-stderr.txt')
            health = classify_native(task, logs/'verifier', result.returncode)
            log_text = '\n'.join(p.read_text(errors='replace') for p in (logs/'verifier').glob('*.txt'))
            row = {'mode': mode, 'native_health': health, 'process_exit': result.returncode,
                   **control_expectation(mode, health, log_text)}
            record['controls'].append(row)
            write_json_atomic(output/'progress.json', record)
            subprocess.run(['docker', 'rm', current_container], check=True, capture_output=True, timeout=30)
            current_container = None
            if mode == 'healthy_reference' and health['status'] != 'pass':
                raise RuntimeError('Native healthy-reference control did not pass; stop negative controls')
        record['checkpoint_passed'] = len(record['controls']) == len(CONTROLS) and all(row['expectation_met'] is True for row in record['controls'])
        validate_fixture(task, fixture)
        if sha256_file(fixture/'manifest.json') != record['fixture_manifest_sha256']:
            raise RuntimeError('Fixture manifest changed during controls')
        record['final_source_bindings_verified'] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        record.update(error_category=type(exc).__name__, error_reason=str(exc))
        record['checkpoint_passed'] = False
    finally:
        if current_container:
            try:
                cleanup = subprocess.run(['docker', 'rm', '-f', current_container], capture_output=True, timeout=30)
                record['cleanup_exit'] = cleanup.returncode
            except (OSError, subprocess.SubprocessError) as exc:
                record['cleanup_error_category'] = type(exc).__name__
        record['finished_unix'] = time.time()
        record['evidence_files'] = [{'path': str(p.relative_to(output)), 'bytes': p.stat().st_size,
                                    'sha256': sha256_file(p)} for p in sorted(output.rglob('*'))
                                   if p.is_file() and 'build_context' not in p.parts]
        write_json_atomic(output/'result.json', record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=FAMILIES, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'executed': False, 'family': args.family, 'controls': CONTROLS,
                          'reserved_free_bytes': RESERVE, 'executed_model': False}))
        return
    result = execute(args.family, args.fixture, args.output)
    print(json.dumps({'result_path': str(args.output/'result.json'), 'checkpoint_passed': result['checkpoint_passed'],
                      'error_reason': result.get('error_reason')}))


if __name__ == '__main__':
    main()
