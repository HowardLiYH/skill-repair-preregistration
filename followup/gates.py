"""Step 0: editor-only gates, run from the Mac before any agent run (docs/FOLLOWUP_IMPLEMENTATION.md).

  Gate A (budget):     the 32 calibration rewrites end valid and within the cap (pass: at least 30).
  Gate B (editing):    edits differ from their input and stay within the cap (pass: at least 90%).
  Gate C (separation): each repair request equals its retry request plus the feedback block only.

The gates look only at text and token counts, never at task outcomes.
  python3.11 followup/gates.py --plan        # build every first request, count tokens, no model call
  SSL_CERT_FILE=/etc/ssl/cert.pem python3.11 followup/gates.py --execute     # run with DeepSeek Flash through the spending ledger
(the system certificate store is named explicitly, as in the first study, so TLS verification works)
"""
import argparse, glob, json, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from capstudy.bundles import load_bundle                                    # noqa: E402
from capstudy.tokenization import bundle_count                              # noqa: E402
from capstudy.contracts import write_json_atomic                            # noqa: E402
from capstudy.deepseek_tokenization import (DeepSeekOfflineCounter, OFFICIAL_MODEL, OFFICIAL_REVISION,   # noqa: E402
                                            OFFICIAL_TOKENIZER_SHA256)
from followup import editing, feedback                                      # noqa: E402

DATA = ROOT / 'milestone 1/data'
SKILLS = DATA / 'shared/skills/human_authored'
GENERATION = ROOT / 'private_artifacts/formal-calibration-v1/generation'
CONTROLLER = ROOT / 'private_artifacts/formal-study-v1/central/controller-amendment2'
OPERATIONS = ROOT / 'private_artifacts/formal-study-v1/central/operations'
OUT = ROOT / 'private_artifacts/followup-v1/step0'
STRATEGIES = ('source_native_compact', 'workflow', 'api_code', 'rule_formula')
EDIT_TEMPERATURE = 0.7


def counter():
    return DeepSeekOfflineCounter(ROOT / 'private_artifacts/external/deepseek-tokenizer/tokenizer.json', model_id=OFFICIAL_MODEL,
                                  revision=OFFICIAL_REVISION, expected_sha256=OFFICIAL_TOKENIZER_SHA256,
                                  expected_runtime_version='0.22.2', cache_dir=ROOT / 'private_artifacts/deepseek-pilot/count_cache')


def families():
    return sorted(p.name for p in SKILLS.iterdir() if p.is_dir())


def dev_instructions(family):
    tasks = sorted(p for p in (DATA / 'dev/tasks' / family).iterdir() if p.is_dir())
    if len(tasks) != 2: raise ValueError('Expected two dev tasks for ' + family)
    return [{'instance_id': p.name, 'instruction': (p / 'instruction.md').read_text()} for p in tasks]


def saved_first_answer(family, strategy, messages):
    """The first study's own first rewrite answer, used as round 0 only if its request was byte-identical."""
    folders = glob.glob(str(GENERATION / family / strategy / 'editor_calls' / '*'))
    if len(folders) != 1: return None
    request = json.loads(Path(folders[0], 'request.json').read_text())['editor_request']
    if request.get('system') != messages[0]['content'] or \
            json.dumps({'prompt': request.get('prompt')}, sort_keys=True, ensure_ascii=False) != messages[1]['content']:
        return None
    return json.loads(Path(folders[0], 'response.json').read_text()).get('response_text')


def failed_initial_dev_runs(family):
    """Feedback inputs from the first study's saved dev runs of the initial rewrite (verifier report + agent trace)."""
    db = sqlite3.connect(f'file:{(CONTROLLER / (family + "-dev.sqlite")).resolve()}?mode=ro', uri=True); runs = []
    for (raw,) in db.execute("select result from events where kind='trial' order by seq"):
        trial = json.loads(raw)
        if trial.get('condition') != 'unchanged_rewrite' or trial.get('status') != 'task_fail': continue
        base = glob.glob(str(OPERATIONS / trial['actual_trial_id'] / 'collected/mirror/job/stage/output/evaluation_log/*/*/*/*'))
        if len(base) != 1: raise ValueError('Evidence folder not found for ' + trial['actual_trial_id'])
        runs.append(feedback.run_feedback(trial['instance_id'], Path(base[0], 'verifier/capstudy-junit.xml'),
                                          Path(base[0], 'agent/claude-code.txt'), trial.get('agent_timed_out')))
    return runs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true'); mode.add_argument('--execute', action='store_true')
    ap.add_argument('--only-gate-a-failures-of', type=Path, help='a previous gate_a folder: rerun only its cells that ended over the cap, keep the rest, and skip gates B and C')
    ap.add_argument('--reask-temperature', type=float, default=0.0)
    ap.add_argument('--launch', type=int, default=1, help='launch number; part of each call identity, so a launch that failed before any request was sent can be repeated under a new number')
    args = ap.parse_args(argv)
    count = counter(); OUT.mkdir(parents=True, exist_ok=True)
    description = f'{count.tokenizer_id}@{count.revision}; complete-file sum including frozen wrappers'
    client = None
    if args.execute:
        from followup.client import LedgerClient, backup_ledger
        backup = backup_ledger(ROOT / 'private_artifacts/followup-v1/ledger-before-followup.sqlite')
        client = LedgerClient(count, OUT)
        print('ledger backup sha256', backup, '| ledger before:', client.totals())
    plan_tokens = []

    def runner(identity, temperature):
        def call(messages, round_index):
            if client is None: raise RuntimeError('plan mode makes no model calls')
            return client.call({**identity, 'round': round_index, 'launch': args.launch}, messages, temperature)
        return call

    report = {'gate_a': [], 'gate_b': [], 'gate_c': []}
    try:
        # ---------------- Gate A: 32 rewrites with the budget loop
        accepted_workflow = {}
        for family in families():
            source = load_bundle(SKILLS / family); original = bundle_count(source.files, count); cap = original // 2
            for strategy in STRATEGIES:
                messages = editing.rewrite_messages(source, strategy, original, cap, description)
                first = saved_first_answer(family, strategy, messages)
                plan_tokens.append(sum(count.count_file(m['content']) for m in messages))
                row = {'family': family, 'strategy': strategy, 'cap': cap, 'saved_first_answer': first is not None}
                if first is not None:
                    row['first_study'] = editing.finish(source, first, strategy, count, cap)
                    row['first_study'] = {k: row['first_study'][k] for k in ('eligible', 'candidate_tokens', 'ineligibility_reasons')}
                previous = args.only_gate_a_failures_of and json.loads((args.only_gate_a_failures_of / f'{family}--{strategy}.json').read_text())
                if previous and previous['eligible']:
                    row.update(eligible=True, tokens=previous['tokens'], rounds=previous['rounds'], kept_from_previous_launch=True)
                    report['gate_a'].append(row); continue
                if args.execute:
                    out = editing.run_loop(runner({'gate': 'A', 'family': family, 'strategy': strategy}, args.reask_temperature), messages, source,
                                           strategy, count, cap, first_raw=first)
                    row.update(eligible=out['eligible'], tokens=out['candidate_tokens'], rounds=out['rounds'])
                    write_json_atomic(OUT / 'gate_a' / f'{family}--{strategy}.json', {**row, 'files': out['files']})
                    if strategy == 'workflow' and out['eligible']: accepted_workflow[family] = out['files']
                    print('A', family, strategy, [r['tokens'] for r in out['rounds']], 'cap', cap, 'OK' if out['eligible'] else 'FAIL', flush=True)
                report['gate_a'].append(row)

        # ---------------- Gate B and C: edits
        def edit(label, family, initial, current, fb, attempt):
            source = load_bundle(SKILLS / family); cap = bundle_count(source.files, count) // 2
            messages = editing.edit_messages(source.files, initial, current, dev_instructions(family), cap, attempt, 'workflow', fb)
            plan_tokens.append(sum(count.count_file(m['content']) for m in messages))
            row = {'case': label, 'family': family, 'arm': 'repair' if fb is not None else 'retry', 'cap': cap,
                   'request_tokens': plan_tokens[-1]}
            if args.execute:
                out = editing.run_loop(runner({'gate': 'B', 'case': label, 'family': family, 'arm': row['arm']}, EDIT_TEMPERATURE),
                                       messages, source, 'workflow', count, cap, current_files=current)
                row.update(accepted=out['accepted'], eligible=out['eligible'], tokens=out['candidate_tokens'], rounds=out['rounds'])
                write_json_atomic(OUT / 'gate_b' / f'{label}--{family}--{row["arm"]}.json', {**row, 'files': out['files'], 'raw_text': out['raw_text']})
                print('B', label, family, row['arm'], [(r['tokens'], r['copy_of_current']) for r in out['rounds']], 'OK' if out['accepted'] else 'FAIL', flush=True)
            report['gate_b'].append(row)
            return messages

        # B1: real feedback on the first study's starting rewrites that failed a dev task
        for family in ([] if args.only_gate_a_failures_of else families()):
            start_dir = ROOT / 'final/output/candidates' / family / 'unchanged_rewrite'
            if not start_dir.is_dir(): continue
            runs = failed_initial_dev_runs(family)
            fb = feedback.assemble(runs)
            if fb is None: continue
            source = load_bundle(SKILLS / family); start = {p: (start_dir / p).read_text() for p in source.files}
            # Verifier-derived fields may only be check names and categories (no message text).
            leak = [v for k, v in feedback.packet_fields(fb) if k == 'verifier:category' and v not in feedback.CATEGORIES]
            leak += [c for r in fb['runs'] for c in r['failed_checks'] if set(c) != {'check', 'category'}]
            repair = edit('old-start', family, start, start, fb, 1)
            retry = edit('old-start', family, start, start, None, 1)
            report['gate_c'].append({'family': family, 'differs_only_by_feedback': editing.differs_only_by_feedback(repair, retry),
                                     'retry_has_no_feedback_text': 'practice_feedback' not in retry[1]['content'],
                                     'verifier_text_fields': len(leak), 'failed_runs': len(fb['runs']),
                                     'feedback_chars': len(json.dumps(fb))})
            write_json_atomic(OUT / 'gate_c' / f'{family}--feedback.json', fb)
        # B2: a retry-style edit of each new within-cap workflow rewrite from gate A
        for family, files in ([] if args.only_gate_a_failures_of else sorted(accepted_workflow.items())):
            edit('new-start', family, files, files, None, 1)
    finally:
        if client is not None:
            report['ledger_after'] = client.totals(); report['calls'] = client.calls; client.close()

    a = report['gate_a']; b = report['gate_b']; c = report['gate_c']
    summary = {'mode': 'execute' if args.execute else 'plan', 'requests_planned': len(plan_tokens), 'max_request_tokens': max(plan_tokens),
               'gate_c_pass': None if args.only_gate_a_failures_of else bool(c) and all(x['differs_only_by_feedback'] and x['retry_has_no_feedback_text'] and not x['verifier_text_fields'] for x in c),
               'first_study_eligible_reproduced': sum(bool(x.get('first_study', {}).get('eligible')) for x in a)}
    if args.execute:
        summary.update(gate_a_within_cap=sum(x['eligible'] for x in a), gate_a_cells=len(a), gate_a_pass=sum(x['eligible'] for x in a) >= 30,
                       gate_b_accepted=sum(x['accepted'] for x in b), gate_b_edits=len(b),
                       gate_b_pass=None if args.only_gate_a_failures_of else bool(b) and sum(x['accepted'] for x in b) / len(b) >= 0.9,
                       new_calls=sum(not x['reused'] for x in report['calls']),
                       new_spend_usd=str(sum(__import__('decimal').Decimal(x['charge_usd']) for x in report['calls'] if not x['reused'])))
    report['summary'] = summary
    name = 'gates-plan.json' if not args.execute else f'gates-result-launch{args.launch}.json'
    write_json_atomic(OUT / name, report)
    print(json.dumps(summary, indent=1)); print(json.dumps(c, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
