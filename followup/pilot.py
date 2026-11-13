"""The bounded development pilot (docs/FOLLOWUP_IMPLEMENTATION.md, section 0.2). Runs on the Mac.

  SSL_CERT_FILE=/etc/ssl/cert.pem python3.11 followup/pilot.py --check            # offline: inputs, inventories, ceilings
  SSL_CERT_FILE=/etc/ssl/cert.pem python3.11 followup/pilot.py --run --host HOST   # one pass (resumable)

One pass: `workflow` rewrite per family (with the length re-ask), one dev loop of repair and retry, each selected
skill once on each test task. Ceilings across all pilot revisions: 150 physical agent attempts (replacements count),
$2.50 of API since the pilot started (every editor call and re-ask counts), $5 of hosting (setup and idle count).
"""
import argparse, json, sys, time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from followup.controller import StageController, Stop                     # noqa: E402

FAMILIES = ('earthquake-plate-calculation', 'financial-analysis', 'schedule-planning', 'weighted-gdp-calculation')
DATA = ROOT / 'milestone 1/data'
SKILLS = DATA / 'shared/skills/human_authored'
PILOT = ROOT / 'private_artifacts/followup-v1/pilot'
VERSION = 'v1'                                   # revision label; a documented fix starts v2 and keeps v1
MAX_ATTEMPTS, API_CEILING, HOSTING_CEILING, HOURLY = 150, Decimal('2.50'), Decimal('5'), Decimal('0.112')
PER_STEP_MARGIN = Decimal('0.15')                # the most one agent run or editor call is expected to cost


def splits(families=FAMILIES):
    out = {}
    for family in families:
        tasks = {split: sorted(p.name for p in (DATA / split / 'tasks' / family).iterdir() if p.is_dir()) for split in ('train', 'dev', 'test')}
        out[family] = {'calibration': tasks['train'], 'practice': tasks['dev'], 'test': tasks['test']}
    return out


def inventories(split_map):
    """Frozen check inventories: every check name seen in the first study's saved reports for the task, in report
    order, cross-checked against the test functions defined in the task's own test files."""
    import re
    saved = json.loads((ROOT / 'private_artifacts/followup-v1/feedback-audit.json').read_text())['inventories']
    out = {}
    for family, parts in split_map.items():
        for split_name, tasks in (('dev', parts['practice']), ('test', parts['test'])):
            for task in tasks:
                names = saved.get(task)
                if not names: raise SystemExit(f'No saved check inventory for {task}')
                defined = set()
                for f in (DATA / split_name / 'tasks' / family / task / 'tests').glob('test*.py'):
                    defined |= set(re.findall(r'^\s*def (test_\w+)', f.read_text(), re.M))
                if {n.split('[')[0] for n in names} != defined:
                    raise SystemExit(f'Inventory for {task} does not match its test functions')
                out[task] = names
    return out


def hosting_spent(log):
    """Hosting since the pilot's server was created, from the hosting log (one ISO time per line: 'created <time>')."""
    total = Decimal(0); now = datetime.now(timezone.utc)
    lines = [l.split() for l in Path(log).read_text().splitlines() if l.startswith(('created ', 'deleted '))] if Path(log).exists() else []
    start = None
    for kind, stamp in lines:
        t = datetime.fromisoformat(stamp)
        if kind == 'created': start = t
        elif start is not None: total += Decimal((t - start).total_seconds() / 3600); start = None
    if start is not None: total += Decimal((now - start).total_seconds() / 3600)
    return total * HOURLY


def attempts_in_other_versions():
    """The attempt ceiling is one running total across pilot versions."""
    total = 0
    for journal in PILOT.glob('v*/journal.jsonl'):
        if journal.parent.name == VERSION: continue
        total += sum(line.startswith('{"key": "attempt:') for line in journal.read_text().splitlines())
    return total


def descriptions(split_map):
    """Frozen AI-written practice-check descriptions; every practice check must have one."""
    record = json.loads((ROOT / 'followup/check_descriptions.json').read_text())
    for family, parts in split_map.items():
        names = {n for t in parts['practice'] for n in inventories({family: parts})[t]}
        missing = {n for n in names if n not in record.get(family, {}) and n.split('[')[0] not in record.get(family, {})}
        if missing: raise SystemExit(f'No frozen description for {family}: {sorted(missing)}')
    return {f: record[f] for f in split_map}


def build(host=None, families=FAMILIES):
    from followup.budget import FollowupJournal
    from followup.client import LedgerClient
    from followup.gates import counter
    PILOT.mkdir(parents=True, exist_ok=True)
    journal = FollowupJournal(ROOT / 'private_artifacts/followup-v1/budget-approval-stage1.json')
    state = PILOT / 'pilot-state.json'
    if not state.exists():
        state.write_text(json.dumps({'api_spent_at_start': str(journal.totals()['spent']), 'started': datetime.now(timezone.utc).isoformat()}))
    start = Decimal(json.loads(state.read_text())['api_spent_at_start'])
    count = counter(); split_map = splits(families); inv = inventories(split_map); desc = descriptions(split_map)
    hosting_log = PILOT / 'hosting-log.txt'

    def limit_check():
        t = journal.totals()
        api = t['spent'] + t['reserved'] - start
        if api + PER_STEP_MARGIN > API_CEILING: raise Stop(f'Pilot ceiling: API ${api:.4f} used of ${API_CEILING}')
        hosting = hosting_spent(hosting_log)
        if hosting + Decimal('0.05') > HOSTING_CEILING: raise Stop(f'Pilot ceiling: hosting ${hosting:.3f} used of ${HOSTING_CEILING}')
        if journal.headroom() < PER_STEP_MARGIN: raise Stop('Rule S1: cumulative API cap reached')

    editor = LedgerClient(count, PILOT / VERSION / 'editor', journal=journal, worker='followup-pilot-' + VERSION)
    runner = None
    if host:
        from followup.runner import ServerRunner
        runner = ServerRunner(host=host, known_hosts=ROOT / 'private_artifacts/followup-v1/followup_known_hosts', journal=journal,
                              out_dir=PILOT / VERSION / 'runs', remote_config='/opt/followup/runtime_config.json',
                              splits_by_task={t: s for f in split_map for s, key in (('train', 'calibration'), ('dev', 'practice'), ('test', 'test'))
                                              for t in split_map[f][key]}, data_root=DATA,
                              remote_artifact_root='/opt/followup/artifacts')
    controller = StageController(splits=split_map, data_root=DATA, skills_root=SKILLS,
                                 editor=lambda identity, messages, temperature: editor.call({'pilot': VERSION, **identity}, messages, temperature),
                                 runner=runner, journal_path=PILOT / VERSION / 'journal.jsonl', counter=count, loops=1, runs=1,
                                 inventories=inv, max_attempts=MAX_ATTEMPTS, limit_check=limit_check,
                                 attempts_offset=attempts_in_other_versions(), check_descriptions=desc)
    description = f'{count.tokenizer_id}@{count.revision}; complete-file sum including frozen wrappers'
    return controller, journal, editor, inv, description, limit_check, start


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true'); mode.add_argument('--run', action='store_true')
    ap.add_argument('--host')
    ap.add_argument('--families', nargs='+', default=list(FAMILIES), choices=FAMILIES,
                    help='N13: first one family\'s complete path, then the others (same journal, same running totals)')
    a = ap.parse_args(argv)
    controller, journal, editor, inv, description, limit_check, start = build(a.host if a.run else None, tuple(sorted(a.families)))
    t = journal.totals()
    print(json.dumps({'families': FAMILIES, 'tasks_with_inventory': len(inv), 'api_spent_total': str(t['spent']),
                      'api_cap': str(journal.budget), 'pilot_api_used': str(t['spent'] - start),
                      'attempts_used': controller.attempts_used()}, indent=1))
    limit_check()
    if a.check: return 0
    if not a.host: raise SystemExit('--run needs --host')
    report = {'version': VERSION, 'families': sorted(a.families)}
    try:
        report['rewrites'] = controller.fixed_strategy_rewrites('workflow', description)
        report['selections'] = {1: controller.dev('workflow', 1)}
        report['pilot_checks'] = controller.pilot_checks()
        rows = controller.test(report['selections'])
        report['test_rows'] = rows
    except Stop as exc:
        report['stopped'] = str(exc)
    report['attempts_used'] = controller.attempts_used()
    t = journal.totals(); report['api_used'] = str(t['spent'] - start)
    report['fingerprints'] = controller.fingerprint_report()
    for sel in (report.get('selections') or {}).values():
        for fam in sel.values():
            for arm in fam.values(): arm.pop('files', None)
    (PILOT / VERSION / f"pilot-report-{'-'.join(sorted(a.families)) if len(a.families) < len(FAMILIES) else 'all'}.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: v for k, v in report.items() if k != 'test_rows'}, indent=1, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
