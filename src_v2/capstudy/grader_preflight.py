"""GDP calibration-only native grader check without any agent or model.

Default is plan-only. The optional execution builds the original environment
(with empty skill directories), then checks untouched, reference-produced and
intentionally incomplete artifacts in separate fresh containers. Reference
material remains evaluator-only; none of these are model performance results.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
import tomllib
import uuid
from .contracts import write_json_atomic
from .runner import ROOT, classify_native, sha256_file, tree_hash

INSTANCE = 'weighted-gdp-calculation-1'
FAMILY = 'weighted-gdp-calculation'
MODES = ('untouched_input', 'reference_solution', 'reference_incomplete_output')


def disk_admission(measurement, available_bytes):
    # Measured apt size plus explicit headroom; this is a forecast, not a peak guarantee.
    estimated_image = measurement['base_size_bytes'] + measurement['apt_additional_installed_bytes'] + 256 * 1024**2
    reserve = 6 * 1024**3
    required = 3 * estimated_image + measurement['apt_archive_bytes'] + reserve
    return {'allowed': available_bytes >= required, 'available_bytes': available_bytes,
            'estimated_image_bytes': estimated_image, 'working_copy_allowance': 3,
            'unmeasured_extra_dependency_allowance_bytes': 256 * 1024**2,
            'reserved_free_bytes': reserve, 'required_available_bytes': required,
            'forecast_not_guaranteed_peak': True}


def normalize_architecture(value):
    return {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(value, value)


def validate_footprint(record, measurement, image_metadata, daemon_info):
    if measurement['native_dockerfile_sha256'] != record['native_dockerfile_sha256']:
        raise ValueError('Footprint measurement is for a different native Dockerfile')
    if sha256_file(measurement['apt_log_path']) != measurement['apt_log_sha256']:
        raise ValueError('Footprint evidence hash mismatch')
    expected = normalize_architecture(measurement.get('architecture'))
    if expected not in ('amd64', 'arm64'):
        raise ValueError('Footprint architecture is missing or unsupported')
    if expected != normalize_architecture(image_metadata['Architecture']):
        raise ValueError('Footprint/base-image architecture mismatch')
    if expected != normalize_architecture(daemon_info['Architecture']):
        raise ValueError('Footprint must be measured for this native Docker architecture')
    if daemon_info.get('OSType') != 'linux':
        raise ValueError('Native Linux Docker server required')
    if measurement['base_image_id'] != image_metadata['Id']:
        raise ValueError('Base image changed after footprint measurement')
    return True


def monitored_process(command, logs, *, reserve_bytes, timeout, disk_paths=None, container_name=None):
    """Monitor the entire process lifetime, including Docker container startup.

    Output goes straight to persistent files, so a timeout/disk interruption
    cannot lose the native process evidence. Only the named job is stopped.
    """
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    paths = [Path(x) for x in (disk_paths or [logs])]
    started = time.monotonic()
    status = {'command': command, 'container_name': container_name, 'returncode': None,
              'monitor_interval_seconds': 2, 'reserved_free_bytes': reserve_bytes,
              'minimum_free_bytes': None, 'interrupted_reason': None}
    process = None
    try:
        with (logs / 'disk_monitor.jsonl').open('w') as monitor, \
             (logs / 'process-stdout.txt').open('w') as stdout, \
             (logs / 'process-stderr.txt').open('w') as stderr:
            while True:
                readings = {str(path): shutil.disk_usage(path).free for path in paths}
                free = min(readings.values())
                prior = status['minimum_free_bytes']
                status['minimum_free_bytes'] = min(prior, free) if prior is not None else free
                elapsed = time.monotonic() - started
                monitor.write(json.dumps({'elapsed_seconds': elapsed, 'free_bytes_by_path': readings}) + '\n')
                monitor.flush()
                if free < reserve_bytes:
                    status['interrupted_reason'] = 'disk_reserve_reached'
                    raise RuntimeError('Disk reserve reached during process startup/execution')
                if elapsed > timeout:
                    status['interrupted_reason'] = 'time_limit_reached'
                    raise RuntimeError('Native process time limit reached')
                if process is None:
                    process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                code = process.poll()
                if code is not None:
                    status['returncode'] = code
                    return subprocess.CompletedProcess(command, code)
                time.sleep(2)
    except BaseException:
        if container_name and process is not None:
            try:
                subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                status['cleanup_error_category'] = type(cleanup_error).__name__
        if process is not None and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=30)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        raise
    finally:
        status['duration_seconds'] = time.monotonic() - started
        write_json_atomic(logs / 'process_status.json', status)


def plan(data_root=None):
    data = Path(data_root) if data_root else ROOT / 'milestone 1/data'
    task = data / 'train/tasks' / FAMILY / INSTANCE
    cfg = tomllib.loads((task / 'task.toml').read_text())
    return {'evidence_kind': 'grader_calibration_plan', 'executed': False,
            'executed_model': False, 'instance_id': INSTANCE, 'split': 'train',
            'task_path': str(task.resolve()), 'variants': list(MODES),
            'native_dockerfile_sha256': sha256_file(task / 'environment/Dockerfile'),
            'native_reference_sha256': tree_hash(task / 'tests'),
            'reference_solution_sha256': sha256_file(task / 'solution/solve.sh'),
            'task_declared_resources': cfg['environment'],
            'estimated_build_bytes': None,
            'estimate_limitation': 'APT transitive dependencies, layers, build cache and saved images not measured',
            'disk_admission': 'Requires checked apt size measurement plus three working copies and 6 GiB reserve',
            'model_api_required': False, 'network_required': 'Ubuntu packages and pinned uv/pytest dependencies',
            'source_bytes': sum(p.stat().st_size for p in task.rglob('*') if p.is_file()),
            'grading_time_limit_seconds': cfg['verifier']['timeout_sec'],
            'scientific_accuracy_evidence': False}


def grade_command(image, task, logs, mode, container_name):
    if mode not in MODES:
        raise ValueError('Unknown evaluator fixture mode')
    prefix = ''
    if mode != 'untouched_input':
        prefix = 'bash /oracle/solve.sh && '
    if mode == 'reference_incomplete_output':
        # Deliberately missing valid workbook output, not a broken ZIP/parser.
        # All native files remain unchanged. This is a labeled grader fixture.
        code = 'from openpyxl import load_workbook; w=load_workbook("/root/gdp.xlsx"); s=w["Task"]; [(setattr(s.cell(r,c),"value",None)) for r in range(35,41) for c in range(8,13)]; w.save("/root/gdp.xlsx")'
        import shlex
        prefix += 'python3 -c ' + shlex.quote(code) + ' && '
    return ['docker', 'run', '--rm', '--name', container_name, '--cpus', '1', '--memory', '4g',
            '--entrypoint', 'bash', '-v', f'{task / "tests"}:/tests:ro',
            '-v', f'{task / "solution"}:/oracle:ro', '-v', f'{logs}:/logs/verifier',
            image, '-c', prefix + 'bash /tests/test.sh']


def execute(output, *, data_root=None, measurement_path=None):
    record = plan(data_root)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new persistent preflight directory')
    if measurement_path is None:
        raise ValueError('Provide the saved native apt footprint measurement')
    measurement = json.loads(Path(measurement_path).read_text())
    inspect_base = subprocess.run(['docker', 'image', 'inspect', 'ubuntu:24.04'], capture_output=True, text=True, check=True)
    image_metadata = json.loads(inspect_base.stdout)[0]
    daemon = subprocess.run(['docker', 'info', '--format', '{{json .}}'], capture_output=True, text=True, check=True)
    daemon_info = json.loads(daemon.stdout)
    validate_footprint(record, measurement, image_metadata, daemon_info)
    disk_paths = [output.parent]
    docker_root = Path(daemon_info['DockerRootDir'])
    if docker_root.exists(): disk_paths.append(docker_root)
    available = min(shutil.disk_usage(path).free for path in disk_paths)
    admission = disk_admission(measurement, available)
    if not admission['allowed']:
        raise ValueError('Insufficient free disk for measured build plus recorded headroom')
    output.mkdir(parents=True)
    record.update(evidence_kind='native_grader_calibration', executed=True,
                  started_at_unix=time.time(), source_free_bytes=available, variants=[],
                  disk_admission=admission, footprint_measurement=measurement)
    write_json_atomic(output / 'pending.json', record)
    task = Path(record['task_path'])
    context = output / 'build_context'
    shutil.copytree(task / 'environment', context, ignore=shutil.ignore_patterns('skills'))
    (context / 'skills').mkdir()
    tag = 'capstudy-gdp-grader:' + uuid.uuid4().hex[:12]
    record['image_tag'] = tag
    try:
        command = ['docker', 'build', '-t', tag, str(context)]
        record['build_command'] = command
        build_started = time.monotonic()
        build = monitored_process(command, output / 'build', reserve_bytes=admission['reserved_free_bytes'],
                                  timeout=record['task_declared_resources']['build_timeout_sec'], disk_paths=disk_paths)
        record['build_seconds'] = time.monotonic() - build_started
        if build.returncode:
            raise RuntimeError('Native image build failed; inspect saved logs')
        inspect = subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True, text=True, check=True)
        metadata = json.loads(inspect.stdout)[0]
        record.update(image_id=metadata['Id'], image_size_bytes=metadata['Size'], architecture=metadata['Architecture'])
        for mode in MODES:
            if shutil.disk_usage(output).free < admission['reserved_free_bytes']:
                raise RuntimeError('Disk reserve reached; stop before next grader fixture')
            logs = output / mode
            logs.mkdir()
            container = 'capstudy-gdp-grade-' + uuid.uuid4().hex[:12]
            try:
                grader_command = grade_command(tag, task, logs, mode, container)
                graded_started = time.monotonic()
                result = monitored_process(grader_command, logs, reserve_bytes=admission['reserved_free_bytes'],
                                           timeout=record['grading_time_limit_seconds'], disk_paths=disk_paths,
                                           container_name=container)
                health = classify_native(task, logs, result.returncode)
                expected = 'pass' if mode == 'reference_solution' else 'task_fail'
                row = {'variant': mode, 'expected_status': expected, 'command': grader_command,
                       'duration_seconds': time.monotonic()-graded_started, 'native_health': health,
                       'expectation_met': health['status'] == expected, 'model_execution': False,
                       'artifact_hashes': [{'path': str(p), 'sha256': sha256_file(p)} for p in sorted(logs.iterdir()) if p.is_file()]}
                record['variants'].append(row)
                write_json_atomic(output / 'progress.json', record)
            finally:
                subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
        record['grader_checkpoint_passed'] = all(row['expectation_met'] for row in record['variants'])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        record.update(grader_checkpoint_passed=False, error_category=type(exc).__name__, error_reason=str(exc) if isinstance(exc, RuntimeError) else None)
    finally:
        record['finished_at_unix'] = time.time()
        write_json_atomic(output / 'result.json', record)
        # Only this preflight's image tag; preserve shared bases/build cache.
        subprocess.run(['docker', 'image', 'rm', tag], capture_output=True)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--footprint-measurement', type=Path)
    args = parser.parse_args(argv)
    if args.execute and args.output is None:
        parser.error('--execute needs a new persistent --output directory')
    record = execute(args.output, measurement_path=args.footprint_measurement) if args.execute else plan()
    if args.output and not args.execute:
        write_json_atomic(args.output, record)
    print(json.dumps(record, indent=2))

if __name__ == '__main__':
    main()
