"""Amendment 2 (owner approval, November 13, 2026): model-free missing-output records for one task, built the first
study's way and derived only from controls (never from any agent run). Runs on the server; no model call.

For the task: stage it as a real run does, start its solver container with nothing produced (untouched input), and
run the unchanged formal native verifier (JUnit sidecar, CAS snapshot, absence probe). If the verifier reports setup
errors, record each one's exact signature together with the grader hashes, the required-absent output paths and the
report's hash. Then check both directions on this same evidence:
  * with the record, the untouched control is graded as a native failure (reward 0, task_fail);
  * with one signature removed (an unrelated error), the same evidence stays infrastructure;
  * with the report missing, classification refuses (infrastructure).
A task whose verifier reports no setup errors needs no record.

  python3 -B followup/build_missing_contract.py --config /opt/followup/runtime_config.confirmatory.json --instance X --split dev --out DIR
"""
import argparse, copy, hashlib, json, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src_v2')]


def main(argv=None):
    ap = argparse.ArgumentParser()
    for n in ('--config', '--instance', '--split', '--out'): ap.add_argument(n, required=True)
    a = ap.parse_args(argv)
    config = json.loads(Path(a.config).read_text())
    policy = copy.deepcopy(config['formal_policies_by_split'][a.split])
    # Controls keep their snapshots in a separate store, never in the study's evidence store.
    control_store = Path(a.out) / 'control-cas'
    policy['storage'] = dict(policy['storage'], store_path=str(control_store / 'cas'), scratch_path=str(control_store / 'scratch'),
                             receipts_path=str(control_store / 'receipts'))
    config['runtime']['formal_verifier_policy'] = policy
    data_root = Path(config['data_root'])
    record = next(json.loads(l) for l in (data_root / a.split / 'instances.jsonl').read_text().splitlines()
                  if l.strip() and json.loads(l)['instance_id'] == a.instance)
    task_id = f"{record['family']}/{a.instance}"
    task_policy = policy['tasks'][task_id]
    from capstudy.single_stage import stage_instance
    from capstudy.runtime import pin_staged_runtime
    from capstudy.formal_verifier import run_formal_verifier, classify_saved_formal
    from capstudy.native_evidence import parse_junit, error_signature, POLICY, REPORT
    from capstudy.runner import classify_native
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='contract-', dir=a.out))
    stage = work / 'stage'; stage_instance(record, data_root.resolve(), stage); pin_staged_runtime(stage, config)
    task = stage / 'tasks' / record['family'] / a.instance
    trial = work / f'control-{a.instance}'; trial.mkdir()
    tag = f'followup-contract-{a.instance}'.lower(); name = 'evaluation_' + tag
    result = {'instance': a.instance, 'split': a.split, 'family': record['family'], 'model_calls': 0,
              'evidence_kind': 'model_free_assigned_instance_native_negative_preflight'}
    try:
        subprocess.run(['docker', 'build', '-t', tag, str(task / 'environment')], check=True, capture_output=True, timeout=3600)
        subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'none', tag, 'sleep', 'infinity'], check=True, capture_output=True)
        completed = run_formal_verifier(name, task, trial, timeout=1800, policy=policy, native_classifier=classify_native)
        logs = trial / 'verifier'
        cases, counts = parse_junit(logs / REPORT)
        absence = json.loads((logs / 'absence.json').read_text())
        result.update(returncode=completed.returncode, counts=counts, absence=absence.get('paths'))
        errors = [c for c in cases if c['status'] == 'error']
        if not errors:
            result['contract'] = None
            result['note'] = 'no setup errors with the output absent: graded natively without a record'
        else:
            missing = [p for p in task_policy['required_output_paths'] if (absence.get('paths') or {}).get(p) != {'exists': False}]
            if missing: raise SystemExit(f'Required outputs were not absent in the control: {missing}')
            normalization = 'python-test-object-address-v1'
            signatures = sorted({error_signature(c, normalization) for c in errors})
            contract = {'policy': POLICY, 'review_status': 'approved_source_bound_missing_output',
                        'grader_hashes': task_policy['grader_hashes'],
                        'calibration_evidence_sha256': hashlib.sha256((logs / REPORT).read_bytes()).hexdigest(),
                        'required_absent_paths': task_policy['required_output_paths'], 'error_signatures': signatures,
                        'diagnostic_normalization': normalization, 'family': record['family'], 'instance_id': a.instance,
                        'evidence_kind': result['evidence_kind'],
                        'interpretation': 'Exact output-absence setup diagnostics from a model-free control; unrelated setup errors remain infrastructure.',
                        'amendment': 'Amendment 2, owner approval 2026-11-13T03:53Z'}
            result['contract'] = contract
            # Both directions, on this same control evidence.
            graded = copy.deepcopy(policy); graded['tasks'][task_id]['missing_output_contract'] = contract
            health = classify_saved_formal(task, logs, completed.returncode, graded)
            result['check_with_record'] = {k: health.get(k) for k in ('status', 'native_reward', 'reason')}
            other = copy.deepcopy(contract); other['error_signatures'] = signatures[1:] or ['0' * 64]
            unrelated = copy.deepcopy(policy); unrelated['tasks'][task_id]['missing_output_contract'] = other
            health2 = classify_saved_formal(task, logs, completed.returncode, unrelated)
            result['check_unrelated_error'] = {k: health2.get(k) for k in ('status', 'reason')}
            hidden = logs / REPORT; moved = hidden.with_suffix('.hidden')
            hidden.rename(moved)
            try:
                h3 = classify_saved_formal(task, logs, completed.returncode, graded)
                result['check_missing_report'] = ('refused: ' + str(h3.get('status')) + ' ' + str(h3.get('reason'))
                                                  if h3.get('status') not in ('pass', 'task_fail') else 'accepted: ' + str(h3.get('status')))
            except Exception as exc:
                result['check_missing_report'] = f'refused: {type(exc).__name__}'
            finally:
                moved.rename(hidden)
            ok = (result['check_with_record']['status'] == 'task_fail' and result['check_with_record']['native_reward'] in (0, 0.0)
                  and result['check_unrelated_error']['status'] != 'task_fail' and result['check_missing_report'].startswith('refused'))
            result['directions_verified'] = ok
            shutil.copy2(logs / REPORT, out / f'{a.instance}-control-junit.xml')
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        subprocess.run(['docker', 'image', 'rm', '-f', tag], capture_output=True)
    (out / f'{a.instance}.json').write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result.get(k) for k in ('instance', 'counts', 'directions_verified')} | {'has_record': bool(result.get('contract'))}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
