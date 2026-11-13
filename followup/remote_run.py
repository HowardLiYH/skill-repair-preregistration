"""Server side of one follow-up agent run (runs on the experiment server, never on the Mac).

Uses the first study's runner unchanged (capstudy.runner.run_trial): the same staging, pinned Claude Code CLI,
isolated solver network, native verifier and evaluator snapshot. The relay URL and the per-run relay token are the
only route to the model; the provider key never reaches this machine.

  python3 remote_run.py --config RUNTIME_CONFIG.json --trial-id ID --instance INSTANCE_ID --split dev|test|train
                        --condition CONDITION [--bundle DIR] --token-file FILE --bundle-sha256 HASH
Prints one JSON line with the graded result.
"""
import argparse, hashlib, json, os, subprocess, sys, tarfile, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src_v2')]
from capstudy import runner                                       # noqa: E402
from capstudy.deepseek_runtime import URL_ENV, TOKEN_ENV           # noqa: E402
sys.path.append(str(REPO / 'followup'))
import feedback                                                    # noqa: E402

RELAY_URL = 'http://172.30.53.1:18080'
CONDITIONS = {'full_skill', 'unchanged_rewrite', 'feedback_repair', 'retry_no_feedback'}


ABSENT = ('Could not find the file', 'No such file or directory')   # docker cp's own wording for a missing path


class ExportFailed(RuntimeError):
    """An output could not be copied for a reason other than its absence: never recorded as 'absent'."""


def copy_out(cid, path, dest):
    """True if copied, False if the agent never produced the path; raises ExportFailed for any other failure."""
    r = subprocess.run(['docker', 'cp', f'{cid}:{path}', str(dest)], capture_output=True, text=True, timeout=900)
    if r.returncode == 0: return True
    if any(marker in (r.stderr or '') for marker in ABSENT): return False
    raise ExportFailed(f'docker cp {path} failed: {(r.stderr or r.stdout or "").strip()[:200]}')


def export_outputs(record_out, paths, trial):
    """Essential outputs (the files the verifier reads) from the post-run solver image, as one tar with its hash.
    Nothing is dropped for size: if they cannot be exported, the Mac stops before any cleanup."""
    if paths is None: return {'outputs_error': 'no required-output list for this task in the runtime config'}
    if not paths: return {'outputs_none_required': True}
    snapshot = record_out.get('evaluator_snapshot')
    if not snapshot: return {'outputs_error': 'no retained post-run snapshot recorded'}
    target = Path(trial) / 'outputs.tar'
    try:
        # The post-run image is retained in the verified CAS store (the tag is removed after saving); load it back.
        with runner.open_saved_snapshot(snapshot) as actual:
            subprocess.run(['docker', 'image', 'load', '-i', actual['path']], check=True, capture_output=True, timeout=900)
            image = actual['image_id']
        cid = subprocess.run(['docker', 'create', '--network', 'none', image, 'true'], check=True, capture_output=True, text=True, timeout=120).stdout.strip()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                listing = []
                for path in paths:
                    if '*' in path:                      # a file pattern the verifier reads (e.g. /root/keyframes_*.png)
                        import fnmatch, shutil
                        parent = str(Path(path).parent)
                        staging = Path(tmp) / '_pattern'
                        copied = copy_out(cid, parent + '/.', staging)
                        found = sorted(f.name for f in staging.iterdir() if f.is_file() and fnmatch.fnmatch(f.name, Path(path).name)) if copied else []
                        for name in found:
                            dest = Path(tmp) / parent.lstrip('/') / name; dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(staging / name), dest)
                        shutil.rmtree(staging, ignore_errors=True)
                        listing.append({'path': path, 'present': bool(found), 'matched': found})
                        continue
                    dest = Path(tmp) / path.lstrip('/')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    listing.append({'path': path, 'present': copy_out(cid, path, dest)})
                (Path(tmp) / 'outputs-listing.json').write_text(json.dumps(listing, indent=1))
                with tarfile.open(target, 'w:gz') as tar: tar.add(tmp, arcname='outputs')
        finally:
            subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, timeout=120)
            subprocess.run(['docker', 'image', 'rm', '-f', '--no-prune', image], capture_output=True, timeout=120)
    except Exception as exc:
        target.unlink(missing_ok=True)                    # never leave an archive that looks complete
        return {'outputs_error': f'{type(exc).__name__}: {str(exc)[:200]}'}
    return {'outputs_tar': str(target), 'outputs_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'outputs_bytes': target.stat().st_size}


def summarize(record_out, config, instance, trial_id):
    """The one-line result the Mac reads. For the S4 amendment it only flags a candidate (trace pattern plus a graded
    verifier); the Mac decides, using the relay's own recorded rejection reason. Nothing else changes classification."""
    trial = Path(record_out['agent_trace_path']).parent.parent if record_out.get('agent_trace_path') else None
    health = record_out.get('verifier_health') or {}
    status, candidate = record_out['status'], None
    if (status == 'infra_error' and (record_out.get('executor_health') or {}).get('reason') == 'executor_api_or_transport_error'
            and health.get('status') in ('pass', 'task_fail') and trial is not None):
        check = feedback.relay_image_rejection(record_out['agent_trace_path'])
        if check['confirmed']:
            # Only a candidate: the Mac accepts it only with the relay's own recorded image-only refusal.
            candidate = {'trace': check, 'verifier_status': health['status']}
    graded = status in ('pass', 'task_fail') or candidate is not None
    outputs = export_outputs(record_out, config.get('followup_required_outputs', {}).get(instance), trial) if trial and graded else {}
    return {'trial_id': trial_id, 'status': status, 'native_reward': record_out.get('native_reward'),
            'executed_tests': health.get('executed_tests'), 'passed_tests': health.get('passed_tests'),
            'failed_tests': health.get('failed_tests'), 'agent_timed_out': record_out.get('agent_timed_out'),
            'diagnostic': record_out.get('diagnostic_category'), 'duration_seconds': record_out.get('duration_seconds'),
            'trial_dir': str(trial) if trial else None, 'image_refusal_candidate': candidate,
            'completed': str(Path(config['artifact_root']) / 'jobs' / trial_id / 'completed.json'), **outputs}


def main(argv=None):
    ap = argparse.ArgumentParser()
    for name in ('--config', '--trial-id', '--instance', '--split', '--condition', '--token-file'):
        ap.add_argument(name, required=True)
    ap.add_argument('--bundle'); ap.add_argument('--bundle-sha256')
    a = ap.parse_args(argv)
    if a.condition not in CONDITIONS: raise SystemExit('Unknown condition')
    config = json.loads(Path(a.config).read_text())
    if a.split in config.get('formal_policies_by_split', {}):          # per-split native verifier policy
        config['runtime']['formal_verifier_policy'] = config['formal_policies_by_split'][a.split]
    data_root = Path(config['data_root'])
    rows = [json.loads(l) for l in (data_root / a.split / 'instances.jsonl').read_text().splitlines() if l.strip()]
    record = next(r for r in rows if r['instance_id'] == a.instance)
    candidate = None
    if a.condition != 'full_skill':
        candidate = {'bundle_path': a.bundle, 'family': record['family'], 'candidate_id': a.trial_id}
    token = Path(a.token_file).read_text().strip()
    os.environ[URL_ENV], os.environ[TOKEN_ENV] = RELAY_URL, token

    def frozen_check(cfg, row, cand):
        # The selected bytes must be the ones the Mac sent (hash recorded in the Mac journal before dispatch).
        ok = cand is None or runner.tree_hash(Path(cand['bundle_path'])) == a.bundle_sha256
        return {'verified': ok}

    def eligibility_check(cand, row, cfg):
        return {'eligible': cand is not None and runner.tree_hash(Path(cand['bundle_path'])) == a.bundle_sha256}

    def admission(signature):
        # Spending is admitted per request by the gateway on the Mac against the approved cap.
        return {'reservation_id': 'gateway-per-request:' + a.trial_id, 'execution_signature': signature}

    record_out = runner.run_trial(record, a.condition, candidate, config, execute=True, admission=admission,
                                  frozen_check=frozen_check, eligibility_check=eligibility_check,
                                  physical_trial_id=a.trial_id)
    print(json.dumps(summarize(record_out, config, a.instance, a.trial_id)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
