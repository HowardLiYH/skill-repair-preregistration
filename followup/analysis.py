"""Pre-registered analysis for the follow-up study (docs/FOLLOWUP_IMPLEMENTATION.md, section 3 and decision rule).

Model-free. Reads the stage's test rows and edit log, writes one JSON result.

  python3 followup/analysis.py --rows TEST_ROWS.csv --edits EDITS.csv --manifest MANIFEST.json --output RESULT.json

MANIFEST.json is the frozen manifest: {family: {test task: number of applicable checks}}, written before any run.

TEST_ROWS.csv: one row per test run, with columns
  family, task, arm (repair|retry), loop (1-3), run (1-3), status (pass|task_fail|infeasible),
  checks_passed, checks_total, sensitivity14 (1|0)
EDITS.csv: one row per edit attempt in dev, with columns
  family, loop, arm, attempt, valid (1|0), changed (1|0), within_cap (1|0), had_feedback (1|0), feedback_opportunity (1|0),
  edit_distance (normalized token edit distance to the text it edited; a diagnostic, never a gate)
"""
import argparse, csv, json, random
from math import comb

ARMS = ('repair', 'retry')
SEED, RESAMPLES = 5300, 10000


def _bool(v): return str(v).strip().lower() in ('1', 'true', 'yes')


def load_rows(path, loops, runs, manifest):
    """manifest: the frozen {family: {test task: number of applicable checks}}. Required: completeness and every
    row's check denominator are verified against what was frozen, not against the rows received."""
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('The frozen task manifest is required')
    checks = {}
    for family, tasks in manifest.items():
        if not isinstance(tasks, dict) or not tasks:
            raise ValueError(f'Manifest entry for {family} must map each test task to its number of applicable checks')
        for task, n in tasks.items():
            if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
                raise ValueError(f'Manifest check count for {family}/{task} must be a positive integer')
            checks[family, task] = n
    rows = list(csv.DictReader(open(path, newline='')))
    seen = set()
    for r in rows:
        if r['arm'] not in ARMS: raise ValueError(f"Unknown arm {r['arm']!r}")
        if r['status'] not in ('pass', 'task_fail', 'infeasible'):
            raise ValueError(f"Unresolved status {r['status']!r} in {r['family']}/{r['task']}: every row must be resolved before analysis")
        key = (r['family'], r['task'], r['arm'], int(r['loop']), int(r['run']))
        if key in seen: raise ValueError(f'Duplicate row {key}')
        seen.add(key)
    tasks = {(r['family'], r['task']) for r in rows}
    if tasks != set(checks):
        raise ValueError(f'Rows do not match the frozen task manifest: missing {sorted(set(checks) - tasks)[:5]}, unexpected {sorted(tasks - set(checks))[:5]}')
    expected = {(f, t, a, l, n) for f, t in checks for a in ARMS for l in range(1, loops + 1) for n in range(1, runs + 1)}
    if seen != expected:
        missing = sorted(expected - seen)[:5]
        raise ValueError(f'Incomplete matrix: {len(expected - seen)} rows missing, e.g. {missing}')
    for r in rows:
        validate_checks(r, checks[r['family'], r['task']])
    return rows


def validate_checks(r, inventory_size):
    """Passed checks must lie between 0 and the total, and the total must be the task's frozen number of applicable
    checks (skipped or unreached checks count as not passed, so they stay in the denominator)."""
    if r['status'] == 'infeasible': return
    passed, total = int(r['checks_passed']), int(r['checks_total'])
    if total != inventory_size:
        raise ValueError(f"{r['family']}/{r['task']}: {total} checks recorded, frozen inventory has {inventory_size}")
    if not 0 <= passed <= total:
        raise ValueError(f"{r['family']}/{r['task']}: {passed} passed checks out of {total}")


def row_scores(r):
    """(task success, check-level score). An infeasible condition scores 0 on both."""
    if r['status'] == 'infeasible': return 0.0, 0.0
    passed, total = int(r['checks_passed']), int(r['checks_total'])
    if total <= 0: raise ValueError(f"No checks recorded for {r['family']}/{r['task']}")
    if not 0 <= passed <= total: raise ValueError(f"{r['family']}/{r['task']}: {passed} passed checks out of {total}")
    return float(r['status'] == 'pass'), passed / total


def family_means(rows, which, task_filter=None):
    """Per family and arm: mean over loops, runs and tasks (equal weight to every run of every task)."""
    acc = {}
    for r in rows:
        if task_filter is not None and not task_filter(r): continue
        acc.setdefault((r['family'], r['arm']), []).append(row_scores(r)[which])
    families = sorted({f for f, _ in acc})
    return {f: {a: sum(acc[f, a]) / len(acc[f, a]) for a in ARMS} for f in families if all((f, a) in acc for a in ARMS)}


def sign_test(diffs):
    wins = sum(d > 0 for d in diffs); losses = sum(d < 0 for d in diffs); n = wins + losses
    p = 1.0 if n == 0 else min(1.0, 2 * sum(comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n)
    return wins, losses, len(diffs) - n, p


def flip_test(diffs):
    """Exact two-sided paired randomization test for the mean: every sign assignment of the family differences."""
    n = len(diffs); observed = abs(sum(diffs))
    if n > 20: raise ValueError('Exact enumeration is intended for at most 20 families')
    hits = 0
    for mask in range(1 << n):
        total = sum(d if mask >> i & 1 else -d for i, d in enumerate(diffs))
        hits += abs(total) >= observed - 1e-12
    return hits / (1 << n)


FLIP_NULL = ('Two-sided family-level sign-flip test under the assumption that, under the null, the joint distribution '
             'of family differences is invariant to independently reversing their signs. The estimated average '
             'difference and all family differences are reported separately. The test is not an assumption-free test '
             'of zero average effect.')


def contrast(means):
    """Three things reported separately: the mean family difference, its uncertainty (family bootstrap), and every
    family's own difference. The two-sided family-level sign-flip test is reported with its stated assumption
    (FLIP_NULL). A rejection is worded "the sign-flip test rejected its null, and the estimated average difference
    favored repair" (or retry). The sign test is descriptive only."""
    families = sorted(means)
    diffs = [means[f]['repair'] - means[f]['retry'] for f in families]
    rng = random.Random(SEED)
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(RESAMPLES))
    lo, hi = boots[int(0.025 * RESAMPLES) - 1], boots[int(0.975 * RESAMPLES) - 1]
    wins, losses, ties, sign_p = sign_test(diffs)
    mean = sum(diffs) / len(diffs)
    p = flip_test(diffs)
    nonzero = sum(d != 0 for d in diffs)
    if p >= 0.05 or mean == 0: verdict = 'not_rejected'
    else: verdict = 'rejected_mean_positive' if mean > 0 else 'rejected_mean_negative'
    return {'families': len(families),
            'mean_difference': mean,
            'uncertainty': {'ci95_family_bootstrap': [lo, hi], 'resamples': RESAMPLES, 'seed': SEED,
                            'note': f'Resamples families; rests on {len(families)} families.'},
            'per_family': {f: {'repair': means[f]['repair'], 'retry': means[f]['retry'],
                               'difference': means[f]['repair'] - means[f]['retry']} for f in families},
            'flip_test': {'p_two_sided': p, 'alpha': 0.05, 'null_hypothesis': FLIP_NULL, 'verdict': verdict,
                          'nonzero_families': nonzero, 'smallest_possible_p': min(1.0, 2 / 2 ** nonzero) if nonzero else 1.0},
            'descriptive': {'family_wins': wins, 'family_losses': losses, 'family_ties': ties, 'sign_test_p': sign_p}}


def delivery(path):
    """Edit accounting, split as in rule S3. S3a implementation integrity (a defect): repair carried no feedback
    although a failed practice run existed, or retry carried feedback. S3b opportunity (reported, never a defect):
    how many edits were valid and changed, how often feedback was available, how much edits changed the text."""
    edits = list(csv.DictReader(open(path, newline='')))
    valid = [e for e in edits if _bool(e.get('valid', '1'))]
    changed = [e for e in valid if _bool(e['changed'])]
    repair = [e for e in edits if e['arm'] == 'repair']
    opportunities = [e for e in repair if _bool(e.get('feedback_opportunity', e['had_feedback']))]
    missing = [e for e in opportunities if not _bool(e['had_feedback'])]
    leaked = [e for e in edits if e['arm'] == 'retry' and _bool(e['had_feedback'])]
    sizes = [float(e['edit_distance']) for e in valid if e.get('edit_distance') not in (None, '')]
    by_arm = {a: [float(e['edit_distance']) for e in valid if e['arm'] == a and e.get('edit_distance') not in (None, '')] for a in ARMS}
    return {'S3a_integrity': {'passed': not missing and not leaked,
                              'repair_calls_missing_feedback_despite_failed_run': len(missing),
                              'retry_calls_with_feedback': len(leaked)},
            'S3b_opportunity': {'edit_attempts': len(edits), 'valid_changed': len(changed),
                                'valid_unchanged': len(valid) - len(changed), 'invalid': len(edits) - len(valid),
                                'share_valid_and_changed': len(changed) / len(edits) if edits else None,
                                'share_within_cap': sum(_bool(e['within_cap']) for e in edits) / len(edits) if edits else None,
                                'repair_calls': len(repair), 'repair_calls_with_failed_run_available': len(opportunities),
                                'repair_calls_without_any_run_to_report': len(repair) - len(opportunities),
                                'mean_edit_distance': sum(sizes) / len(sizes) if sizes else None,
                                'mean_edit_distance_by_arm': {a: (sum(v) / len(v) if v else None) for a, v in by_arm.items()}}}


def analyze(rows_path, edits_path, manifest, loops=3, runs=3):
    rows = load_rows(rows_path, loops, runs, manifest)
    result = {'design': {'loops': loops, 'runs': runs, 'bootstrap_resamples': RESAMPLES, 'seed': SEED},
              'primary_task_success': contrast(family_means(rows, 0)),
              'secondary_check_level': contrast(family_means(rows, 1)),
              'delivery': delivery(edits_path) if edits_path else None}
    if any(_bool(r.get('sensitivity14', '1')) is False for r in rows):
        result['sensitivity_excluding_rephrased'] = contrast(family_means(rows, 0, lambda r: _bool(r.get('sensitivity14', '1'))))
    result['implementation_integrity_passed'] = bool(result['delivery'] and result['delivery']['S3a_integrity']['passed'])
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--rows', required=True); ap.add_argument('--edits'); ap.add_argument('--output', required=True)
    ap.add_argument('--manifest', required=True, help='Frozen JSON {family: {test task: applicable checks}}')
    ap.add_argument('--loops', type=int, default=3); ap.add_argument('--runs', type=int, default=3)
    args = ap.parse_args(argv)
    result = analyze(args.rows, args.edits, json.load(open(args.manifest)), args.loops, args.runs)
    with open(args.output, 'w') as f: json.dump(result, f, indent=1, sort_keys=True)
    p = result['primary_task_success']; ci = p['uncertainty']['ci95_family_bootstrap']; t = p['flip_test']
    print(f"mean repair - retry = {p['mean_difference']:+.3f}  family bootstrap 95% [{ci[0]:+.3f}, {ci[1]:+.3f}]  "
          f"sign-flip p={t['p_two_sided']:.4f} ({t['verdict']}, {t['nonzero_families']} nonzero families)")
    for f, d in p['per_family'].items(): print(f"  {f}: {d['difference']:+.3f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
