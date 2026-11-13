"""Server side: re-read a finished run's saved job record and print the same one-line result as remote_run.py,
applying the current classification rules (S4 amendment, November 12). No model call; the run is not repeated.

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
    record = json.loads((Path(config['artifact_root']) / 'jobs' / a.trial_id / 'completed.json').read_text())
    if record.get('trial_id') != a.trial_id or record.get('instance_id') != a.instance:
        raise SystemExit('Job record does not belong to this trial')
    print(json.dumps(summarize(record, config, a.instance, a.trial_id)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
