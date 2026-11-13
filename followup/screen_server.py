"""Technical screening of one new task on the server (Notion 0.4). No model call.

For the task: build its environment image exactly as written (skills folder empty), then in fresh containers
(1) untouched input: run the native verifier on the unchanged environment, expected to fail cleanly;
(2) reference: produce the reference output (the task's own solution/solve.sh, or for tasks without one an
    evaluator-only fixture derived from the verifier's own expected values, as in the first study's G2 fixtures), then
    run the native verifier, expected to pass.
Records the verifier's check list (from its CTRF report), outcomes, and the /root paths the verifier reads.

  python3 -B followup/screen_server.py --data /opt/followup/followup-data --split dev --instance court-form-filling-2 --out DIR
"""
import argparse, json, re, shutil, subprocess, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def sh(args, timeout=3600, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)


def verifier(image, task, logs, prepare=None, fixture=None, timeout=1800):
    logs.mkdir(parents=True, exist_ok=True)
    name = 'screen-' + logs.name + '-' + task.name
    mounts = ['-v', f'{task / "tests"}:/tests:ro', '-v', f'{logs}:/logs/verifier']
    if (task / 'solution').exists(): mounts += ['-v', f'{task / "solution"}:/solution:ro']
    if fixture: mounts += ['-v', f'{fixture}:/fixture.py:ro']
    script = (prepare + ' && ' if prepare else '') + 'bash /tests/test.sh'
    sh(['docker', 'rm', '-f', name])
    r = sh(['docker', 'run', '--name', name, '--cpus', '4', '--memory', '4g', *mounts, '--entrypoint', 'bash', image, '-lc', script], timeout=timeout)
    (logs / 'stdout.txt').write_text(r.stdout[-200000:]); (logs / 'stderr.txt').write_text(r.stderr[-200000:])
    sh(['docker', 'rm', '-f', name])
    ctrf = logs / 'ctrf.json'
    tests = json.loads(ctrf.read_text())['results']['tests'] if ctrf.exists() else []
    reward = (logs / 'reward.txt').read_text().strip() if (logs / 'reward.txt').exists() else None
    return {'returncode': r.returncode, 'reward': reward, 'checks': [{'name': t['name'], 'status': t['status']} for t in tests]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    for n in ('--data', '--split', '--instance', '--out'): ap.add_argument(n, required=True)
    a = ap.parse_args(argv)
    data = Path(a.data); family = a.instance.rsplit('-', 1)[0]
    source = data / a.split / 'tasks' / family / a.instance
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='screen-', dir=out))
    task = work / a.instance; shutil.copytree(source, task)
    # The family's human-authored skill is installed as in a full-skill run (some reference solutions call its
    # scripts); the untouched-input control uses the same image and never runs the solution.
    skills = task / 'environment' / 'skills'
    if skills.exists(): shutil.rmtree(skills)
    bundle = data / 'shared' / 'skills' / 'human_authored' / family
    shutil.copytree(bundle, skills) if bundle.exists() else skills.mkdir()
    image = f'followup-screen-{a.instance}'.lower()
    result = {'instance': a.instance, 'split': a.split, 'family': family, 'model_calls': 0,
              'has_solution': (task / 'solution' / 'solve.sh').exists()}
    b = sh(['docker', 'build', '-t', image, str(task / 'environment')], timeout=3600)
    result['build_ok'] = b.returncode == 0
    if not result['build_ok']:
        (out / f'{a.instance}-build.log').write_text(b.stdout[-50000:] + b.stderr[-50000:])
    else:
        fixture = REPO / 'followup' / 'fixtures' / f'{family}.py'
        prepare = 'bash /solution/solve.sh' if result['has_solution'] else (
            'python3 /fixture.py' if fixture.exists() else None)
        result['reference_source'] = 'solution/solve.sh' if result['has_solution'] else (
            f'evaluator-only fixture {fixture.name}' if fixture.exists() else 'none')
        result['untouched'] = verifier(image, task, work / 'untouched')
        result['reference'] = verifier(image, task, work / 'reference', prepare=prepare,
                                       fixture=fixture if fixture.exists() else None) if prepare else None
        sh(['docker', 'image', 'rm', '-f', image])
    tests_text = ''.join(p.read_text(errors='replace') for p in (task / 'tests').glob('*.py'))
    result['root_paths_in_verifier'] = sorted(set(re.findall(r'["\'](/root/[^"\']+)["\']', tests_text)))
    ref = result.get('reference') or {}
    result['inventory'] = [c['name'] for c in ref.get('checks', [])] or [c['name'] for c in (result.get('untouched') or {}).get('checks', [])]
    result['verifier_accepts_reference'] = bool(ref) and ref.get('reward') == '1' and all(c['status'] == 'passed' for c in ref['checks']) and bool(ref['checks'])
    result['untouched_fails_cleanly'] = bool(result.get('untouched')) and result['untouched'].get('reward') == '0'
    (out / f'{a.instance}.json').write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result.get(k) for k in ('instance', 'build_ok', 'reference_source', 'verifier_accepts_reference', 'untouched_fails_cleanly')}
                     | {'checks': len(result['inventory'])}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
