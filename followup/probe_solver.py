"""N16 hidden-answer probe, run on the experiment server before any paid run. No model calls, no relay token.

For one task instance: stage it exactly as the runner does (same staging code, same runtime pinning), check the staged
harness's solver launch, build the solver image from the staged environment, start the solver container with the
same mounts and limits as a real run (before any verifier injection), and look for the verifier and the answers:

  * the solver `docker run` in the staged harness mounts only /logs/agent (no /tests, no /logs, no /solution);
  * `docker inspect` of the live solver container lists only that mount;
  * inside the container: no test_outputs.py / test.sh / tests or solution folders / oracle or expected-answer files,
    and /proc/mounts shows nothing from the task's tests/ or solution/ folders;
  * the staged task folder contains no solution/ and the image build context holds only environment/ files.

  python3 -B followup/probe_solver.py --config /opt/followup/runtime_config.json --instance earthquake-plate-calculation-2 \
      --split dev --output /opt/followup/probes/earthquake-plate-calculation-2.json

The result lists every finding; `separated` is true only if all checks pass. Containers and images it creates are
removed; nothing else is touched.
"""
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src_v2')]

SUSPECT = r'(^|/)(test_outputs\.py|test\.sh|solve\.sh|expected[^/]*|oracle[^/]*|answer_key[^/]*|ground_truth[^/]*)$'
SUSPECT_DIRS = ('/tests', '/solution', '/oracle', '/reference', '/logs/verifier')


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=kw.pop('timeout', 1800), **kw)


def solver_run_block(eval_runner_text):
    """The solver container's `docker run [...]` argument list in the staged harness."""
    start = eval_runner_text.index('# Run container (detached')
    start = eval_runner_text.index('"run",', start)
    block = eval_runner_text[start:start + 3000]
    return block[:block.index('capture_output=True')]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('--config', '--instance', '--split', '--output'): ap.add_argument(name, required=True)
    a = ap.parse_args(argv)
    config = json.loads(Path(a.config).read_text())
    if a.split in config.get('formal_policies_by_split', {}):          # per-split native verifier policy
        config['runtime']['formal_verifier_policy'] = config['formal_policies_by_split'][a.split]
    data_root = Path(config['data_root'])
    rows = [json.loads(l) for l in (data_root / a.split / 'instances.jsonl').read_text().splitlines() if l.strip()]
    record = next(r for r in rows if r['instance_id'] == a.instance)
    findings, checks = [], {}
    with tempfile.TemporaryDirectory(dir=config.get('artifact_root')) as tmp:
        stage = Path(tmp) / 'stage'
        from capstudy.single_stage import stage_instance
        from capstudy.runtime import pin_staged_runtime
        stage_instance(record, data_root.resolve(), stage)
        pin_staged_runtime(stage, config)
        task = stage / 'tasks' / record['family'] / record['instance_id']

        # 1. the staged harness's solver launch
        block = solver_run_block((stage / 'core/eval_runner.py').read_text())
        mounts = re.findall(r'"-v",\s*\n?\s*f?"([^"]+)"', block)
        checks['harness_solver_mounts'] = mounts
        if any(':/tests' in m or m.endswith(':/logs') or 'solution' in m for m in mounts) or len(mounts) != 1 or not mounts[0].endswith(':/logs/agent'):
            findings.append('staged harness mounts more than /logs/agent into the solver container')

        # 2. staged task folder and build context
        checks['staged_task_entries'] = sorted(p.name for p in task.iterdir())
        if (task / 'solution').exists(): findings.append('solution/ is staged with the task')
        context = sorted(str(p.relative_to(task / 'environment')) for p in (task / 'environment').rglob('*') if p.is_file())
        bad = [p for p in context if re.search(SUSPECT, p) or p.split('/')[0] in ('tests', 'solution')]
        checks['build_context_files'] = len(context)
        if bad: findings.append(f'build context contains verifier/answer-like files: {bad[:5]}')

        # 3. live solver container, before any verifier injection
        tag = f'followup-probe-{a.instance}'.lower()
        name = tag + '-solver'
        try:
            b = sh(['docker', 'build', '-t', tag, str(task / 'environment')], timeout=3600)
            if b.returncode: raise RuntimeError('image build failed: ' + b.stderr[-400:])
            logs = Path(tmp) / 'agent'; logs.mkdir()
            r = sh(['docker', 'run', '-d', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--network', config['runtime']['network'], '--name', name, '-v', f'{logs}:/logs/agent', tag, 'sleep', 'infinity'])
            if r.returncode: raise RuntimeError('container start failed: ' + r.stderr[-400:])
            inspect = json.loads(sh(['docker', 'inspect', name]).stdout)[0]
            checks['container_mounts'] = [m['Destination'] for m in inspect.get('Mounts', [])]
            if checks['container_mounts'] != ['/logs/agent']: findings.append(f"live container mounts: {checks['container_mounts']}")
            found = sh(['docker', 'exec', name, 'sh', '-c',
                        'find / -xdev \\( -path /proc -o -path /sys \\) -prune -o -print 2>/dev/null']).stdout.splitlines()
            # Name check outside system/library trees (e.g. the C++ standard header /usr/include/c++/13/expected
            # is not an answer file), plus a content check everywhere: any file in the container whose bytes equal a
            # file of the task's tests/ or solution/ folders.
            system = ('/usr/include/', '/usr/lib/', '/usr/share/', '/usr/local/lib/', '/opt/conda/', '/root/.cache/')
            hits = [p for p in found if (re.search(SUSPECT, p) or p in SUSPECT_DIRS) and not p.startswith(system)]
            checks['suspect_paths_in_container'] = hits[:50]
            if hits: findings.append(f'verifier/answer-like paths visible to the solver: {hits[:5]}')
            import hashlib
            source = Path(config['data_root']) / a.split / 'tasks' / record['family'] / record['instance_id']
            hidden = {}
            for folder in ('tests', 'solution'):
                for f in (source / folder).rglob('*') if (source / folder).exists() else []:
                    if f.is_file() and f.stat().st_size >= 64:
                        hidden.setdefault(f.stat().st_size, set()).add(hashlib.sha256(f.read_bytes()).hexdigest())
            sizes = ' '.join(str(n) for n in hidden)
            listing = sh(['docker', 'exec', name, 'sh', '-c',
                          'find / -xdev \\( -path /proc -o -path /sys \\) -prune -o -type f -printf "%s %p\\n" 2>/dev/null']).stdout
            candidates = [line.split(' ', 1)[1] for line in listing.splitlines() if line.split(' ', 1)[0] in sizes.split()]
            matches = []
            for path in candidates[:500]:
                digest = sh(['docker', 'exec', name, 'sha256sum', path]).stdout.split(' ')[0]
                if any(digest in v for v in hidden.values()): matches.append(path)
            checks['hidden_files_checked'] = sum(len(v) for v in hidden.values())
            checks['container_files_with_hidden_content'] = matches
            if matches: findings.append(f'files identical to tests/solution content visible to the solver: {matches[:5]}')
            proc_mounts = sh(['docker', 'exec', name, 'cat', '/proc/mounts']).stdout
            checks['proc_mounts_mentioning_tests_or_solution'] = [l for l in proc_mounts.splitlines() if '/tests' in l or 'solution' in l]
            if checks['proc_mounts_mentioning_tests_or_solution']: findings.append('/proc/mounts shows tests or solution')
        except Exception as exc:
            findings.append(f'probe could not complete: {type(exc).__name__}: {str(exc)[:300]}')
        finally:
            sh(['docker', 'rm', '-f', name]); sh(['docker', 'image', 'rm', '-f', tag])
    out = {'instance': a.instance, 'split': a.split, 'family': record['family'], 'separated': not findings,
           'findings': findings, 'checks': checks, 'model_calls': 0}
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ('instance', 'separated', 'findings')}))
    return 0 if not findings else 1


if __name__ == '__main__':
    raise SystemExit(main())
