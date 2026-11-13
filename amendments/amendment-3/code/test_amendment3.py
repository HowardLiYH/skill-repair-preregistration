"""Amendment 3 (owner approval, November 13, 2026): consistent skipped checks are graded natively and count as not
passed; the earliest qualifying attempt of a cell is adopted through a separate record. Offline, synthetic evidence."""
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2'), str(ROOT / 'followup' / 'tests')]
from capstudy.native_evidence import classify_junit, grader_hashes, SKIP_RULE, REPORT
from followup.controller import TrialResult, InfraError, Pause
from test_controller import build, FakeRunner


# ---------------------------------------------------------------- the native classifier

def task_and_logs(tmp, *, skipped=1, failed=0, terminal=None, reward=None, extra=''):
    task = tmp / 'family' / 'task-1'; (task / 'tests').mkdir(parents=True)
    (task / 'tests' / 'test.sh').write_text('pytest /tests/test_outputs.py\n')
    (task / 'tests' / 'test_outputs.py').write_text('def test_a(): pass\n')
    logs = tmp / 'logs'; logs.mkdir()
    cases = ['<testcase classname="t" name="test_pass"/>']
    cases += [f'<testcase classname="t" name="test_fail{i}"><failure message="wrong">x</failure></testcase>' for i in range(failed)]
    cases += [f'<testcase classname="t" name="test_skip{i}"><skipped message="needs page image {i}">s</skipped></testcase>' for i in range(skipped)]
    n = len(cases)
    (logs / REPORT).write_text(f'<testsuites><testsuite tests="{n}" failures="{failed}" errors="0" skipped="{skipped}">'
                               + ''.join(cases) + '</testsuite></testsuites>')
    parts = ['1 passed'] + ([f'{failed} failed'] if failed else []) + ([f'{skipped} skipped'] if skipped else [])
    (logs / 'test-stdout.txt').write_text('==== ' + (terminal or ', '.join(parts)) + f'{extra} in 1.20s ====\n')
    (logs / 'reward.txt').write_text(str(reward if reward is not None else int(failed == 0)))
    return task, logs, n


def classify(task, logs, n, rule=SKIP_RULE):
    return classify_junit(task, logs, 0, expected_tests=n, expected_grader_hashes=grader_hashes(task), skip_rule=rule)


def test_consistent_skips_are_graded_with_the_native_grade_and_their_reasons(tmp_path):
    task, logs, n = task_and_logs(tmp_path, skipped=2, failed=1)
    out = classify(task, logs, n)
    assert out['status'] == 'task_fail' and out['native_reward'] == 0 and out['healthy']
    assert out['skipped_tests'] == 2 and out['passed_tests'] == 1 and out['executed_tests'] == 2
    assert [c['reason'] for c in out['skipped_checks']] == ['needs page image 0', 'needs page image 1']


def test_native_pass_with_skips_stays_a_native_pass(tmp_path):
    task, logs, n = task_and_logs(tmp_path, skipped=1, failed=0)
    assert classify(task, logs, n)['status'] == 'pass'           # the verifier's own grade is unchanged


def test_first_study_behaviour_unchanged_without_the_rule(tmp_path):
    task, logs, n = task_and_logs(tmp_path, skipped=1)
    for rule in (None, 'some-other-rule'):
        out = classify(task, logs, n, rule)
        assert out['status'] == 'infra_error' and out['reason'] == 'native_sidecar_not_verified'


@pytest.mark.parametrize('kw', [
    {'terminal': '1 passed, 2 skipped'},                         # terminal totals disagree with the report
    {'reward': 0},                                               # native reward disagrees with the outcomes
    {'extra': ', 1 deselected'},                                 # other incomplete coverage still refused
])
def test_inconsistent_skip_reports_stay_infrastructure(tmp_path, kw):
    task, logs, n = task_and_logs(tmp_path, skipped=1, **kw)
    assert classify(task, logs, n)['status'] == 'infra_error'


def test_wrong_check_count_or_missing_report_stays_infrastructure(tmp_path):
    task, logs, n = task_and_logs(tmp_path, skipped=1)
    assert classify(task, logs, n + 1)['status'] == 'infra_error'
    (logs / REPORT).unlink()
    assert classify(task, logs, n)['status'] == 'infra_error'


# ---------------------------------------------------------------- adoption of the earliest qualifying attempt

class SkipRunner(FakeRunner):
    """calibration/full on alpha-1: attempt 1 was recorded as infrastructure (skipped checks); its saved evidence is a
    native task failure with a skipped check (worse); the replacement passed (better)."""
    def __init__(self, tmp, qualifies=True):
        super().__init__(tmp, fail_infra={('alpha-1', 'calibration/full#a1')}); self.qualifies, self.reread = qualifies, 0
    def amend_recover(self, label, task):
        self.reread += 1
        if not self.qualifies:
            return {'status': 'infra_error', 'diagnostic': 'executor_api_or_transport_error'}, None
        junit = self.tmp / 'a1.xml'
        junit.write_text('<testsuite><testcase name="test_answer"><failure message="wrong"/></testcase>'
                         '<testcase name="test_file"><skipped message="needs page image"/></testcase></testsuite>')
        return ({'status': 'task_fail', 'verifier_reclassification': {'rule': 'Amendment 3'}},
                TrialResult(status='task_fail', checks_passed=0, checks_total=3, cost_usd=0.02, junit_path=str(junit),
                            run_id='a1'))


CELL = 'cal:alpha:full'


def test_earlier_valid_attempt_is_adopted_even_when_it_scores_worse(tmp_path):
    runner = SkipRunner(tmp_path)
    c = build(tmp_path, runner=runner)
    replacement = c.run(CELL, 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full')
    assert replacement.status == 'pass' and replacement.checks_passed == 2           # the graded replacement
    before = {k: v for k, v in c.journal.done.items()}
    calls = len(runner.calls)
    report = c.apply_amendment3(runner.amend_recover, 'owner', 'test approval')
    assert report == [(CELL + '#a1', 'adopted_as_planned_run', CELL + '#a2')]
    adopted = c.run(CELL, 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full')
    assert adopted.status == 'task_fail' and adopted.checks_passed == 0              # the earlier, worse attempt governs
    assert c.journal.get('adopt:' + CELL)['previous_planned_run']['status'] == 'pass'
    assert c.journal.get('superseded:' + CELL + '#a2')['adopted_attempt'] == CELL + '#a1'
    assert c.journal.get('a3:' + CELL + '#a1')['checks']['outcomes']['test_file']['skip_reason'] == 'needs page image'
    for k, v in before.items(): assert c.journal.get(k) == v                          # nothing overwritten
    assert len(runner.calls) == calls                                                 # no model execution
    assert c.planned_attempt(CELL) == CELL + '#a1'
    # One cell, one observation: the calibration table holds one result for it.
    assert c.apply_amendment3(runner.amend_recover, 'owner', 'again') == [] and runner.reread == 1   # idempotent


def test_s4_counts_after_reconciliation_keep_the_attempt_history(tmp_path):
    runner = SkipRunner(tmp_path)
    c = build(tmp_path, runner=runner)
    c.run(CELL, 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full')
    assert c.infra_counts() == (1, 2)
    c.apply_amendment3(runner.amend_recover, 'owner', 'ok')
    assert c.infra_counts() == (0, 2)                     # the re-read attempt is no longer a failure; still resolved


def test_attempt_that_does_not_qualify_stays_infrastructure(tmp_path):
    runner = SkipRunner(tmp_path, qualifies=False)
    c = build(tmp_path, runner=runner)
    c.run(CELL, 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full')
    assert c.apply_amendment3(runner.amend_recover, 'owner', 'ok') == [(CELL + '#a1', 'stays_infrastructure')]
    assert c.journal.get('adopt:' + CELL) is None and c.run(CELL, 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full').status == 'pass'
    assert c.infra_counts() == (1, 2)


def test_attempts_already_resolved_by_an_earlier_amendment_are_left_unchanged(tmp_path):
    runner = SkipRunner(tmp_path)
    c = build(tmp_path, runner=runner)
    c.run(CELL, 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full')
    c.journal.put('diagnostic:' + CELL + '#a1', {'adopted_as_planned_run': False})
    assert c.apply_amendment3(runner.amend_recover, 'owner', 'ok') == [(CELL + '#a1', 'already_resolved')]
    assert runner.reread == 0 and c.journal.get('adopt:' + CELL) is None
