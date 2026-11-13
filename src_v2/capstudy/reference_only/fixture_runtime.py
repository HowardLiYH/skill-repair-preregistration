"""Standalone evaluator-only calibration fixture. Never mount into a solver.

No network, model, secrets or outbound-message functionality. Temperature runs
only the supplied CPU GLM binary and derives every metric from its real output.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def constants(path, names):
    found = {}
    for node in ast.parse(Path(path).read_text()).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names:
                found[target.id] = ast.literal_eval(node.value)
    if set(found) != set(names):
        raise ValueError('Expected evaluator constants changed')
    return found


def verify_files(records, workdir, tests, fixture_root):
    roots = {'workdir': workdir, 'tests': tests, 'fixture': fixture_root, 'binary': Path('/usr/local/bin')}
    for record in records:
        root = roots[record['root']]
        path = root / record['path']
        try: path.resolve().relative_to(root.resolve())
        except ValueError: raise ValueError('Source binding escapes approved root')
        if path.is_symlink() or sha(path) != record['sha256']:
            raise ValueError('Source binding mismatch: ' + record['root'] + '/' + record['path'])


def scheduling(workdir, tests):
    values = constants(tests/'test_outputs.py', ['EXPECTED_DATES', 'EXPECTED_TIMES', 'EXPECTED_TOS'])
    dates, times, recipients = [values[name] for name in ('EXPECTED_DATES', 'EXPECTED_TIMES', 'EXPECTED_TOS')]
    if not len(dates) == len(times) == len(recipients) == 3:
        raise ValueError('Pinned scheduling fixture shape changed')
    records = []
    for index, (date, slot, recipient) in enumerate(zip(dates, times, recipients), 1):
        name = 'reference_reply_' + str(index) + '.txt'
        (workdir/name).write_text('Hi,\n\nThank you for your meeting request.\n\nI can be available:\n\nDate: ' + date + '\nTime: ' + slot + '\n\nBest regards,\nConSkillBench\n')
        records.append({'filename': name, 'recipient': recipient})
    write_json(workdir/'results.json', {'sent_results': records})
    return [workdir/'results.json'] + [workdir/record['filename'] for record in records]


def organization(workdir, tests):
    maps = constants(tests/'test_outputs.py', ['SUBJECT_TO_PAPERS', 'SUBJECT_TO_PPTX', 'SUBJECT_TO_DOCX'])
    papers = workdir/'papers'
    targets = []
    for mapping in maps.values():
        for subject, names in mapping.items():
            if not re.fullmatch(r'[A-Za-z0-9_]+', subject): raise ValueError('Unsafe subject directory')
            (papers/subject).mkdir(exist_ok=True)
            for name in names:
                if Path(name).name != name: raise ValueError('Unsafe reference filename')
                source, destination = papers/'all'/name, papers/subject/name
                if not source.is_file() or destination.exists(): raise ValueError('Fresh complete input required: ' + name)
                shutil.move(str(source), str(destination))
                targets.append(destination)
    if len(targets) != 103 or len(set(targets)) != 103:
        raise ValueError('Pinned organization artifact count changed')
    return targets


def parameter_text(baseline, values, bounds):
    if set(values) != set(bounds): raise ValueError('Exactly the native allowed parameters are required')
    text = baseline
    for name in sorted(values):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not bounds[name][0] <= value <= bounds[name][1]:
            raise ValueError('Parameter outside native bounds: ' + name)
        text, count = re.subn(r'(?m)^(\s*' + re.escape(name) + r'\s*=\s*)[^\n]*$',
            lambda match: match.group(1) + format(value, '.12g'), text)
        if count != 1: raise ValueError('Parameter assignment is not unique: ' + name)
    return text


def temperature(workdir, tests, logs, max_evaluations, max_seconds, glm_timeout):
    if max_evaluations < 2:
        raise ValueError('At least one search and one final verification invocation required')
    import numpy as np
    from scipy.optimize import differential_evolution
    spec = importlib.util.spec_from_file_location('evaluator_only_native_metrics', str(tests/'test_outputs.py'))
    native = importlib.util.module_from_spec(spec); spec.loader.exec_module(native)
    # Importing this reviewed, hash-bound verifier defines functions/classes only.
    # No test cases are invoked and no thresholds, matching or metrics are changed.
    baseline = (workdir/'glm3.nml').read_text()
    bounds = native.PARAM_BOUNDS
    names = sorted(bounds)
    thresholds = {'overall_rmse': native.OVERALL_RMSE_THRESHOLD,
                  'annual_deep_rmse': native.ANNUAL_DEEP_RMSE_THRESHOLD,
                  'summer_deep_rmse': native.SUMMER_DEEP_RMSE_THRESHOLD}
    observations = native.read_observations(str(workdir/'field_temp_oxy.csv'))
    glm = Path('/usr/local/bin/glm')
    if not glm.is_file(): raise ValueError('Native CPU GLM executable missing')
    started = time.monotonic(); history = []; best = [None]
    class SearchComplete(Exception): pass
    def evaluate(vector, final=False):
        search_seconds = max_seconds - min(glm_timeout, max_seconds * .25)
        if not final and (len(history) >= max_evaluations - 1 or time.monotonic()-started >= search_seconds):
            raise SearchComplete()
        values = {name: float(value) for name, value in zip(names, vector)}
        text = parameter_text(baseline, values, bounds)
        (workdir/'glm3.nml').write_text(text)
        output = workdir/'output/output.nc'
        if output.exists(): output.unlink()
        index = len(history); row = {'index': index, 'parameters': values, 'config_sha256': sha(workdir/'glm3.nml'),
            'metrics': None, 'final_verification_run': final}
        remaining = max_seconds - (time.monotonic()-started)
        if remaining <= 0: raise SearchComplete()
        with (logs/('glm-'+str(index)+'.stdout')).open('w') as stdout, (logs/('glm-'+str(index)+'.stderr')).open('w') as stderr:
            try:
                process = subprocess.run([str(glm)], cwd=str(workdir), stdout=stdout, stderr=stderr,
                    timeout=min(glm_timeout, remaining), env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(workdir),'LANG':'C.UTF-8'})
                row['returncode'] = process.returncode
                if process.returncode or not output.is_file(): raise RuntimeError('GLM failed or did not produce output.nc')
                metrics = native.calculate_task_metrics(native.read_glm_output(str(output)), observations)
                if any(not math.isfinite(float(value)) for value in metrics.values()): raise RuntimeError('Nonfinite measured metric')
                row.update(metrics=metrics, output_sha256=sha(output),
                    score=max(metrics[name]/threshold for name, threshold in thresholds.items()),
                    satisfies_native_thresholds=all(metrics[name] < threshold for name, threshold in thresholds.items()))
            except (subprocess.TimeoutExpired, RuntimeError, OSError, ValueError) as exc:
                row['error_category'] = type(exc).__name__
        history.append(row); write_json(logs/'glm-search.json', {'executed_model':False,'evaluations':history})
        if row['metrics'] is None: return 1000000.0
        if best[0] is None or row['score'] < best[0]['score']:
            best[0] = row.copy()
        if row['satisfies_native_thresholds'] and not final: raise SearchComplete()
        return row['score']
    try:
        base_values = native.load_numeric_params(str(workdir/'glm3.nml'), names)
        evaluate([base_values[name] for name in names])
        differential_evolution(evaluate, [bounds[name] for name in names], seed=5300,
            popsize=4, maxiter=max_evaluations, polish=False, workers=1, updating='immediate', tol=.001)
    except SearchComplete:
        pass
    if best[0] is None:
        (workdir/'glm3.nml').write_text(baseline)
        raise RuntimeError('No usable measured GLM output; no reference is claimed')
    # Re-run selected parameters so final artifacts and metrics have one origin.
    try: evaluate([best[0]['parameters'][name] for name in names], final=True)
    except SearchComplete: raise RuntimeError('No time left for mandatory final GLM verification run')
    final = history[-1]
    if final.get('metrics') is None: raise RuntimeError('Final GLM verification failed')
    write_json(workdir/'metrics.json', final['metrics'])
    write_json(logs/'temperature-reference-status.json', {'measured':True,'executed_model':False,
        'thresholds_satisfied':final['satisfies_native_thresholds'],'final_evaluation':final,
        'native_grader_still_required':True})
    if not final['satisfies_native_thresholds']:
        raise RuntimeError('Measured best candidate remains outside native thresholds; not a known-good reference')
    return [workdir/'glm3.nml',workdir/'output/output.nc',workdir/'metrics.json']


def control(family, mode, workdir, logs, reference_state, manifest):
    state = json.loads(reference_state.read_text())
    if state.get('family') != family or state.get('generation_completed') is not True or state.get('fixture_manifest_sha256') != sha(manifest):
        raise ValueError('Controls require matching source-bound reference generation evidence')
    for record in state['outputs']:
        path = workdir/record['path']
        if sha(path) != record['sha256']: raise ValueError('Reference snapshot output changed before control')
    if mode == 'dependency_failure':
        # A deterministic pytest runtime/configuration fault, distinct from
        # missing task output. No dependency or native source is modified.
        write_json(logs/'grader_environment.json', {'PYTEST_ADDOPTS':'--capstudy-g2-deliberate-invalid-option'})
        return 'Run original test.sh with the recorded PYTEST_ADDOPTS override; require the unrecognized-argument error and infra_error. Native dependency provisioning must already work.'
    if family == 'schedule-planning':
        if mode == 'missing_required_output': (workdir/'results.json').unlink()
        else: write_json(workdir/'results.json', {'sent_results':[]})
    elif family == 'organize-messy-files':
        # Leave folder structure and parser setup healthy; deliberately omit one PDF.
        target = next(record for record in state['outputs'] if record['path'].endswith('.pdf'))
        path = workdir/target['path']
        if mode == 'missing_required_output':
            path.unlink()
            return 'One explicitly required PDF removed; native grader still runs against complete remaining tree.'
        other = next(record for record in state['outputs'] if Path(record['path']).parent != Path(target['path']).parent)
        shutil.move(str(path), str(workdir/Path(other['path']).parent/path.name))
    else:
        if mode == 'missing_required_output': (workdir/'metrics.json').unlink()
        else: write_json(workdir/'metrics.json', {})
    return 'Original native grader and reward are unchanged; record actual classification.'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',required=True,choices=['healthy_reference','missing_required_output','incomplete_output','dependency_failure'])
    parser.add_argument('--workdir',type=Path,default=Path('/root'))
    parser.add_argument('--tests',type=Path,default=Path('/tests'))
    parser.add_argument('--logs',type=Path,default=Path('/logs/reference'))
    parser.add_argument('--manifest',type=Path,default=Path(__file__).with_name('manifest.json'))
    parser.add_argument('--reference-state',type=Path)
    parser.add_argument('--max-evaluations',type=int,default=80)
    parser.add_argument('--max-seconds',type=int,default=720)
    parser.add_argument('--glm-timeout',type=int,default=90)
    args=parser.parse_args(argv)
    if min(args.max_evaluations,args.max_seconds,args.glm_timeout)<=0: raise ValueError('Positive bounded CPU limits required')
    manifest=json.loads(args.manifest.read_text()); family=manifest['family']
    if manifest.get('scope')!='calibration_evaluator_only' or manifest.get('split')!='train': raise ValueError('Evaluator-only calibration manifest required')
    if manifest['runtime_sha256']!=sha(__file__): raise ValueError('Fixture code hash changed')
    if args.logs.exists() and any(args.logs.iterdir()):
        raise ValueError('New empty reference log directory required; preserve prior fixture evidence')
    args.logs.mkdir(parents=True,exist_ok=True)
    verify_files(manifest['source_bindings'],args.workdir,args.tests,args.manifest.parent)
    result={'family':family,'mode':args.mode,'scope':'calibration_evaluator_only','executed_model':False,
        'fixture_manifest_sha256':sha(args.manifest),'native_grader_run':False,'generation_completed':False}
    try:
        if args.mode=='healthy_reference':
            verify_files(manifest['input_bindings'],args.workdir,args.tests,args.manifest.parent)
            if family=='schedule-planning': outputs=scheduling(args.workdir,args.tests)
            elif family=='organize-messy-files': outputs=organization(args.workdir,args.tests)
            elif family=='temperature-simulation': outputs=temperature(args.workdir,args.tests,args.logs,args.max_evaluations,args.max_seconds,args.glm_timeout)
            else: raise ValueError('Unsupported family')
            result.update(generation_completed=True,outputs=[{'path':str(p.relative_to(args.workdir)),
                'sha256':sha(p),'bytes':p.stat().st_size} for p in outputs])
        else:
            if args.reference_state is None: raise ValueError('Negative controls require a verified reference snapshot and --reference-state')
            result['control_note']=control(family,args.mode,args.workdir,args.logs,args.reference_state,args.manifest)
            result['control_applied']=True
        write_json(args.logs/'reference_manifest.json',result)
        return 0
    except Exception as exc:
        result.update(error_category=type(exc).__name__,error_reason=str(exc))
        write_json(args.logs/'reference_manifest.json',result)
        raise

if __name__=='__main__': raise SystemExit(main())
