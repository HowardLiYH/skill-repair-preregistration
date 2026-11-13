"""The confirmatory study (Notion phases 1 to 3) on the 7 screened families. Runs on the Mac; resumable.

  SSL_CERT_FILE=/etc/ssl/cert.pem python3.11 followup/confirmatory.py --check
  SSL_CERT_FILE=/etc/ssl/cert.pem python3.11 followup/confirmatory.py --phase calibration --host HOST
  SSL_CERT_FILE=/etc/ssl/cert.pem python3.11 followup/confirmatory.py --phase dev --host HOST
  SSL_CERT_FILE=/etc/ssl/cert.pem python3.11 followup/confirmatory.py --phase test --host HOST
  python3.11 followup/confirmatory.py --phase analyze

Each phase refuses to start until the previous phase's gate passed (S2 after calibration, S3 before test). Spending
is admitted against the owner's cumulative caps (API $19, hosting $31; rule S1, warning at 80%). Test outcomes are not
printed or summarised until every test run has finished (S5); the analysis uses the frozen script and manifest.
"""
import argparse, csv, json, shutil, sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from followup.controller import StageController, Stop, Pause          # noqa: E402

DATA = ROOT / 'followup-data'
SKILLS = DATA / 'shared/skills/human_authored'
CONF = ROOT / 'private_artifacts/followup-v1/confirmatory'
STATE = CONF / 'run'
APPROVAL = ROOT / 'private_artifacts/followup-v1/budget-approval-stage2-confirmatory.json'
HOSTING_LOG = ROOT / 'private_artifacts/followup-v1/pilot/hosting-log.txt'   # the same server since the pilot
HOSTING_BEFORE = Decimal('13.79') + Decimal('0.01')                           # first study + six-minute server (Nov 11)
HOSTING_CAP, HOURLY, MARGIN = Decimal('31'), Decimal('0.112'), Decimal('0.15')
MIN_FREE_BYTES = 20 * 1024 ** 3                                               # the Mac's free-space floor (S7)
REMOTE_CONFIG = '/opt/followup/runtime_config.confirmatory.json'            # the frozen design/runtime_config.json, deployed


def remote_artifact_root():
    """The artifact root written in the frozen runtime config (launch and recovery both use it)."""
    return json.loads((CONF / 'runtime_config.json').read_text())['artifact_root']


def hosting_total():
    from followup.pilot import hosting_spent
    return HOSTING_BEFORE + hosting_spent(HOSTING_LOG)


def build(host=None):
    from followup.budget import FollowupJournal
    from followup.client import LedgerClient
    from followup.gates import counter
    STATE.mkdir(parents=True, exist_ok=True)
    journal = FollowupJournal(APPROVAL)
    splits = json.loads((CONF / 'splits.json').read_text())
    inventories = json.loads((CONF / 'check_inventories.json').read_text())
    descriptions = {f: v for f, v in json.loads((ROOT / 'followup/check_descriptions.json').read_text()).items() if f in splits}
    count = counter()
    warnings = STATE / 'warnings.jsonl'

    def limit_check():
        t = journal.totals()
        if journal.headroom() < MARGIN: raise Stop(f"Rule S1: cumulative API cap ${journal.budget} reached (spent ${t['spent']})")
        hosting = hosting_total()
        if hosting + Decimal('0.05') > HOSTING_CAP: raise Stop(f'Rule S1: cumulative hosting cap ${HOSTING_CAP} reached (${hosting:.2f})')
        if shutil.disk_usage(ROOT).free < MIN_FREE_BYTES: raise Pause('Rule S7: the Mac is at its 20 GB free-space floor')
        if (t['spent'] + t['reserved']) >= journal.budget * Decimal('0.8') or hosting >= HOSTING_CAP * Decimal('0.8'):
            with open(warnings, 'a') as f:
                f.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'api': str(t['spent']), 'hosting': str(hosting)}) + '\n')

    editor = LedgerClient(count, STATE / 'editor', journal=journal, worker='followup-confirmatory')
    runner = None
    if host:
        from followup.runner import ServerRunner
        split_of = {t: s for f in splits for s, key in (('train', 'calibration'), ('dev', 'practice'), ('test', 'test')) for t in splits[f][key]}
        runner = ServerRunner(host=host, known_hosts=ROOT / 'private_artifacts/followup-v1/followup_known_hosts', journal=journal,
                              out_dir=STATE / 'runs', remote_config=REMOTE_CONFIG,
                              splits_by_task=split_of, data_root=DATA, remote_artifact_root=remote_artifact_root())
    controller = StageController(splits=splits, data_root=DATA, skills_root=SKILLS,
                                 editor=lambda identity, messages, temperature: editor.call({'stage': 'confirmatory', **identity}, messages, temperature),
                                 runner=runner, journal_path=STATE / 'journal.jsonl', counter=count, loops=3, runs=3,
                                 inventories=inventories, check_descriptions=descriptions, limit_check=limit_check)
    description = f'{count.tokenizer_id}@{count.revision}; complete-file sum including frozen wrappers'
    return controller, journal, description


def gate(controller, key):
    value = controller.journal.get(key)
    if value is None: raise SystemExit(f'Gate not passed yet: {key}')
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--phase', choices=('calibration', 'dev', 'test', 'analyze')); ap.add_argument('--check', action='store_true')
    ap.add_argument('--host')
    ap.add_argument('--acknowledge-s4', metavar='REASON', help="owner's recorded release of an S4 pause after the cause was fixed")
    a = ap.parse_args(argv)
    controller, journal, description = build(a.host if a.phase in ('calibration', 'dev', 'test') else None)
    t = journal.totals()
    print(json.dumps({'families': sorted(controller.splits), 'api_spent_cumulative': str(t['spent']), 'api_cap': str(journal.budget),
                      'hosting_cumulative': f'{hosting_total():.2f}', 'hosting_cap': str(HOSTING_CAP),
                      'attempts_used': controller.attempts_used()}, indent=1))
    if a.acknowledge_s4:
        print(json.dumps(controller.acknowledge_s4_pause('owner', a.acknowledge_s4))); return 0
    if a.check: return 0
    if a.phase in ('calibration', 'dev', 'test') and not a.host: raise SystemExit('--host is required')
    try:
        if a.phase == 'calibration':
            choice = controller.calibration(description)
            fits = controller.check_s2(choice['strategy'])
            controller.journal.get('gate:S2') or controller.journal.put('gate:S2', {'strategy': choice['strategy'], 'fits': fits, 'choice': choice})
            print(json.dumps(controller.journal.get('gate:S2'), indent=1))
        elif a.phase == 'dev':
            strategy = gate(controller, 'gate:S2')['strategy']
            selections = {loop: controller.dev(strategy, loop) for loop in (1, 2, 3)}
            s3 = controller.check_s3()
            report = {'S3': s3, 'paths': controller.path_report(),
                      'selections': {loop: {f: {arm: {k: v for k, v in sel[arm].items() if k != 'files'} for arm in sel} for f, sel in s.items()}
                                     for loop, s in selections.items()}}
            controller.journal.get('gate:S3') or controller.journal.put('gate:S3', report)
            (STATE / 'dev-report.json').write_text(json.dumps(report, indent=1, default=str))
            print(json.dumps({'S3': s3}, indent=1))          # opportunity pattern, before any test result (S5)
        elif a.phase == 'test':
            gate(controller, 'gate:S3')
            strategy = gate(controller, 'gate:S2')['strategy']
            selections = {loop: controller.dev(strategy, loop) for loop in (1, 2, 3)}     # journaled: no new run
            frozen = {str(loop): {f: {arm: sel[arm].get('sha256') for arm in sel} for f, sel in s.items()} for loop, s in selections.items()}
            controller.journal.get('gate:selections-frozen') or controller.journal.put('gate:selections-frozen', frozen)
            rows = controller.test(selections)
            with open(STATE / 'test_rows.csv', 'w', newline='') as f:
                w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)
            edits = controller.edit_rows()
            with open(STATE / 'edits.csv', 'w', newline='') as f:
                w = csv.DictWriter(f, sorted({k for e in edits for k in e})); w.writeheader(); w.writerows(edits)
            controller.journal.get('gate:test-complete') or controller.journal.put('gate:test-complete', {'rows': len(rows)})
            print(json.dumps({'test_rows': len(rows), 'note': 'all test runs finished; run --phase analyze'}))
        elif a.phase == 'analyze':
            gate(controller, 'gate:test-complete')
            from followup import analysis
            result = analysis.analyze(STATE / 'test_rows.csv', STATE / 'edits.csv', json.loads((CONF / 'test_manifest.json').read_text()))
            result['fingerprints'] = controller.fingerprint_report()
            (STATE / 'analysis.json').write_text(json.dumps(result, indent=1, sort_keys=True))
            print(json.dumps(result['primary_task_success'], indent=1))
    except (Stop, Pause) as exc:
        print(json.dumps({'stopped' if isinstance(exc, Stop) else 'paused': str(exc)}))
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
