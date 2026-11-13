"""Offline tests for the pre-registered analysis (synthetic rows, no model calls)."""
import csv, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from followup import analysis

FIELDS = ['family', 'task', 'arm', 'loop', 'run', 'status', 'checks_passed', 'checks_total', 'sensitivity14']


def manifest_for(families=8, tasks=2, checks=4):
    return {f'f{fam}': {f'f{fam}-t{t}': checks for t in range(tasks)} for fam in range(families)}


def write_rows(path, outcome, families=8, tasks=2, loops=3, runs=3, total=4):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, FIELDS); w.writeheader()
        for fam in range(families):
            for t in range(tasks):
                for arm in analysis.ARMS:
                    for l in range(1, loops + 1):
                        for n in range(1, runs + 1):
                            status, passed = outcome(fam, t, arm, l, n)
                            w.writerow({'family': f'f{fam}', 'task': f'f{fam}-t{t}', 'arm': arm, 'loop': l, 'run': n,
                                        'status': status, 'checks_passed': passed, 'checks_total': total,
                                        'sensitivity14': int(t != 1 or fam > 2)})


def run(path, manifest=None):
    return analysis.analyze(path, None, manifest or manifest_for())


def test_clear_repair_win(tmp_path):
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('pass', 4) if a == 'repair' else ('task_fail', 2))
    r = run(tmp_path / 'r.csv')['primary_task_success']
    assert r['mean_difference'] == 1 and r['descriptive']['family_wins'] == 8
    assert r['flip_test']['p_two_sided'] < 0.01 and r['flip_test']['verdict'] == 'rejected_mean_positive'
    assert set(r['per_family']) == {f'f{i}' for i in range(8)} and 'null_hypothesis' in r['flip_test']


def test_tie_is_not_rejected(tmp_path):
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('pass', 4) if t == 0 else ('task_fail', 3))
    out = run(tmp_path / 'r.csv')
    p = out['primary_task_success']
    assert p['flip_test']['verdict'] == 'not_rejected' and p['descriptive']['family_ties'] == 8
    assert p['flip_test']['nonzero_families'] == 0 and p['flip_test']['smallest_possible_p'] == 1.0
    assert out['secondary_check_level']['mean_difference'] == 0
    assert 'sensitivity_excluding_rephrased' in out


def test_check_level_sees_what_task_level_misses(tmp_path):
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('task_fail', 3 if a == 'repair' else 1))
    out = run(tmp_path / 'r.csv')
    assert out['primary_task_success']['mean_difference'] == 0
    assert out['secondary_check_level']['flip_test']['verdict'] == 'rejected_mean_positive'


def test_infeasible_scores_zero_on_both_measures(tmp_path):
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('infeasible', 0) if a == 'retry' else ('task_fail', 2))
    out = run(tmp_path / 'r.csv')
    assert out['secondary_check_level']['mean_difference'] == 0.5


def test_incomplete_or_unresolved_matrix_is_refused(tmp_path):
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('pass', 4), runs=2)
    with pytest.raises(ValueError, match='Incomplete'):
        run(tmp_path / 'r.csv')
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('infra_error', 0) if f == 0 else ('pass', 4))
    with pytest.raises(ValueError, match='Unresolved'):
        run(tmp_path / 'r.csv')


def test_manifest_is_mandatory_on_every_path(tmp_path):
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('pass', 4))
    for bad in (None, {}, {'f0': ['f0-t0', 'f0-t1']}, {'f0': {'f0-t0': 0}}):
        with pytest.raises((ValueError, TypeError)):
            analysis.analyze(tmp_path / 'r.csv', None, bad)
    with pytest.raises(TypeError):
        analysis.analyze(tmp_path / 'r.csv', None)          # no default: a caller cannot skip it


def test_rows_must_match_frozen_manifest(tmp_path):
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('pass', 4), families=1)
    with pytest.raises(ValueError, match='frozen task manifest'):
        run(tmp_path / 'r.csv')


def test_scores_are_bounded_and_match_the_inventory(tmp_path):
    """Reviewer's case: 5 passed checks of 4 must be refused, and so must a total that differs from the inventory."""
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('pass', 5 if f == 0 else 4))
    with pytest.raises(ValueError, match='5 passed checks out of 4'):
        run(tmp_path / 'r.csv')
    write_rows(tmp_path / 'r.csv', lambda f, t, a, l, n: ('pass', 1), total=1)
    with pytest.raises(ValueError, match='frozen inventory has 4'):
        run(tmp_path / 'r.csv')
    with pytest.raises(ValueError):
        analysis.row_scores({'family': 'f', 'task': 't', 'status': 'pass', 'checks_passed': '5', 'checks_total': '4'})


def test_verdict_follows_the_mean_never_the_family_count():
    """First review's case: many small family gains and a few large losses must not be called a repair win."""
    means = {f'f{i}': {'repair': 0.55, 'retry': 0.5} for i in range(13)}
    means.update({f'g{i}': {'repair': 0.0, 'retry': 1.0} for i in range(4)})
    r = analysis.contrast(means)
    assert r['mean_difference'] < 0 and r['descriptive']['family_wins'] == 13
    assert r['flip_test']['verdict'] != 'rejected_mean_positive'


def test_flip_test_matches_known_values():
    assert analysis.flip_test([1] * 8) == 2 / 256
    assert abs(analysis.flip_test([1] * 6 + [0, 0]) - 0.03125) < 1e-9
    assert analysis.flip_test([1, -1, 1, -1]) == 1.0


def test_smallest_possible_p_is_reported():
    means = {f'f{i}': {'repair': 1.0, 'retry': 0.0} for i in range(5)}
    means.update({f'g{i}': {'repair': 0.5, 'retry': 0.5} for i in range(4)})
    t = analysis.contrast(means)['flip_test']
    assert t['nonzero_families'] == 5 and t['smallest_possible_p'] == 0.0625 and t['verdict'] == 'not_rejected'


EDIT_FIELDS = ['family', 'loop', 'arm', 'attempt', 'changed', 'within_cap', 'had_feedback', 'valid', 'feedback_opportunity', 'edit_distance']


def test_delivery_S3a_integrity_and_S3b_opportunity(tmp_path):
    with open(tmp_path / 'e.csv', 'w', newline='') as f:
        w = csv.writer(f); w.writerow(EDIT_FIELDS)
        for i in range(10):
            w.writerow(['f0', 1, 'repair', i, 1, 1, 1, 1, 1, 0.2]); w.writerow(['f0', 1, 'retry', i, int(i > 2), 1, 0, 1, 1, 0.1])
    d = analysis.delivery(tmp_path / 'e.csv')
    assert d['S3a_integrity']['passed'] and d['S3b_opportunity']['share_valid_and_changed'] == 0.85
    assert abs(d['S3b_opportunity']['mean_edit_distance_by_arm']['repair'] - 0.2) < 1e-9


def test_feedback_defects_fail_S3a_but_low_opportunity_does_not(tmp_path):
    (tmp_path / 'e.csv').write_text(','.join(EDIT_FIELDS) + '\nf0,1,repair,1,1,1,0,1,1,\nf0,1,retry,1,1,1,1,1,1,\n')
    d = analysis.delivery(tmp_path / 'e.csv')['S3a_integrity']
    assert not d['passed'] and d['repair_calls_missing_feedback_despite_failed_run'] == 1 and d['retry_calls_with_feedback'] == 1
    (tmp_path / 'e.csv').write_text(','.join(EDIT_FIELDS) + '\nf0,1,repair,1,0,0,0,0,0,\nf0,1,retry,1,0,0,0,0,0,\n')
    d = analysis.delivery(tmp_path / 'e.csv')
    assert d['S3a_integrity']['passed'] and d['S3b_opportunity']['invalid'] == 2
