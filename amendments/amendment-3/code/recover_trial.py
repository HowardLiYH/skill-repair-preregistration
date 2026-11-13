"""Server side: re-read a finished run's saved job record and print the same one-line result as remote_run.py,
applying the current classification rules (S4 amendment, November 12; Amendments 2 and 3, November 13). No model call; the run is not repeated.

  python3 -B followup/recover_trial.py --config /opt/followup/runtime_config.json --trial-id fu-... --instance ID --split dev
"""
import argparse, json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src_v2')]
sys.path.append(str(REPO / 'followup'))
from remote_run import summarize                                   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    for name in ('--config', '--trial-id', '--instance', '--split'): ap.add_argument(name, required=True)
    a = ap.parse_args(argv)
    config = json.loads(Path(a.config).read_text())
    if a.split in config.get('formal_policies_by_split', {}):
        config['runtime']['formal_verifier_policy'] = config['formal_policies_by_split'][a.split]
    job = Path(config['artifact_root']) / 'jobs' / a.trial_id
    record = json.loads((job / 'completed.json').read_text())
    if record.get('trial_id') != a.trial_id or record.get('instance_id') != a.instance:
        raise SystemExit('Job record does not belong to this trial')
    reclassified = None
    health = record.get('verifier_health') or {}
    rules = {'unresolved_native_setup_error': 'Amendment 2 (missing-output records), owner approval 2026-11-13T03:53Z',
             'native_sidecar_not_verified': 'Amendment 3 (consistent skipped checks), owner approval 2026-11-13T12:10Z'}
    if health.get('status') == 'infra_error' and health.get('reason') in rules and record.get('agent_trace_path'):
        # Re-read the saved verifier evidence under the current (amended) native policy. The original record is kept
        # unchanged; the re-reading is written beside it (one file per amendment). Missing or corrupt reports,
        # unrelated setup errors and anything else inconsistent still classify as infrastructure.
        from capstudy.formal_verifier import classify_saved_formal
        trial = Path(record['agent_trace_path']).parent.parent
        try:
            new = classify_saved_formal(Path(record['task_path']), trial / 'verifier', record.get('grader_exit'),
                                        config['runtime']['formal_verifier_policy'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            new = dict(health, reread_error=type(exc).__name__)
        reclassified = {'original_verifier_health': health, 'amended_verifier_health': new, 'rule': rules[health['reason']]}
        suffix = '' if health['reason'] == 'unresolved_native_setup_error' else '.amendment3'
        (job / f'completed.reclassified{suffix}.json').write_text(json.dumps(reclassified, indent=1, default=str))
        record = dict(record, verifier_health=new)
        # As the runner decides a status: the verifier's grade when the agent's execution was valid; otherwise the
        # run stays infrastructure (an image refusal is then decided on the Mac with the relay's own record).
        if (record.get('executor_health') or {}).get('valid_execution') and new.get('status') in ('pass', 'task_fail'):
            record['status'] = new['status']; record['native_reward'] = new.get('native_reward')
    summary = summarize(record, config, a.instance, a.trial_id)
    if reclassified: summary['verifier_reclassification'] = {k: (v.get('status'), v.get('reason')) if isinstance(v, dict) else v for k, v in reclassified.items()}
    print(json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
