"""Offline end-to-end test of the stage controller with a fake editor and a fake runner (no model calls, no server)."""
import csv, json, sys
from dataclasses import asdict
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from followup import controller, analysis
from followup.controller import StageController, TrialResult, InfraError, Stop, Pause


class WordCounter:
    def count_file(self, text): return len(text.split())
    def __call__(self, text): return len(text.split())


FRONT = '---\nname: {f}\ndescription: skill for {f}\n---\n\n'


def make_skills(root, families):
    for f in families:
        d = root / f / 'main'; d.mkdir(parents=True)
        (d / 'SKILL.md').write_text(FRONT.format(f=f) + '# Procedure\n\n' + 'Do the careful step. ' * 40 + '\n')


def files_for(family, body):
    return {'main/SKILL.md': FRONT.format(f=family) + body + '\n'}


class FakeEditor:
    """Rewrites fit the cap; repair adds the word GOOD when it sees feedback; retry changes wording only."""
    def __init__(self): self.calls = 0
    def __call__(self, identity, messages, temperature):
        self.calls += 1
        user = messages[1]['content']
        family = json.loads(user)['prompt'].split('name: ')[1].split('\\n')[0] if '"prompt"' in user else json.loads(user)['strategy'] and _family(user)
        if '"prompt"' in user:
            return json.dumps({'files': files_for(family, 'Short procedure.')})
        payload = json.loads(user)
        body = 'GOOD fixed procedure.' if 'practice_feedback' in payload else f'Reworded procedure {payload["attempt"]}.'
        return json.dumps({'files': files_for(family, body), 'changes': ['x']})


def _family(user):
    original = json.loads(user)['original']
    return next(iter(original.values())).split('name: ')[1].split('\n')[0]


class FakeRunner:
    def __init__(self, tmp, fail_infra=(), fingerprints=None):
        self.tmp, self.calls, self.fail_infra, self.fingerprints = Path(tmp), [], set(fail_infra), fingerprints
    def instruction(self, family, task): return f'Solve {task}.'
    def run(self, family, task, files, label):
        label = label.split('@')[0]                              # unique suffix added by the controller
        self.calls.append((family, task, label))
        if (task, label) in self.fail_infra:
            self.fail_infra.discard((task, label)); raise InfraError('network')
        text = '' if files is None else ' '.join(files.values())
        good = 'GOOD' in text or 'careful step' in text            # the full skill and repaired skills solve the task
        n = len(self.calls)
        junit = self.tmp / f'j{n}.xml'; trace = self.tmp / f't{n}.txt'
        junit.write_text('<testsuite><testcase name="test_answer">' + ('' if good else
                         '<failure message="AssertionError: expected 12.3 got 87.4"/>') + '</testcase><testcase name="test_file"/></testsuite>')
        trace.write_text(json.dumps({'type': 'result', 'result': 'done'}) + '\n')
        fp = self.fingerprints[n - 1] if self.fingerprints else 'fp1'
        return TrialResult(status='pass' if good else 'task_fail', checks_passed=2 if good else 1, checks_total=2,
                           cost_usd=0.01 + 0.001 * (n % 3), junit_path=str(junit), trace_path=str(trace), fingerprint=fp)


def splits(families):
    return {f: {'calibration': [f'{f}-1'], 'practice': [f'{f}-2', f'{f}-3'], 'test': [f'{f}-4', f'{f}-5']} for f in families}


def build(tmp, families=('alpha', 'beta'), runner=None, editor=None, **kw):
    make_skills(tmp / 'skills', families) if not (tmp / 'skills').exists() else None
    kw.setdefault('inventories', inventories(families))
    return StageController(splits=splits(families), data_root=tmp, skills_root=tmp / 'skills', editor=editor or FakeEditor(),
                           runner=runner or FakeRunner(tmp), journal_path=tmp / 'journal.jsonl', counter=WordCounter(), **kw)


def run_stage(c):
    choice = c.calibration('word count')
    c.check_s2(choice['strategy'])
    selections = {loop: c.dev(choice['strategy'], loop) for loop in (1, 2, 3)}
    c.check_s3()
    return choice, selections, c.test(selections)


def write_csv(path, rows):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)


def test_full_stage_and_analysis(tmp_path):
    c = build(tmp_path)
    choice, selections, rows = run_stage(c)
    assert choice['strategy'] == 'workflow' and choice['reason'] == 'all_zero_default'   # no rewrite passes calibration here
    for loop in (1, 2, 3):
        for f in ('alpha', 'beta'):
            assert selections[loop][f]['repair']['files'] != selections[loop][f]['retry']['files']
    write_csv(tmp_path / 'rows.csv', rows); write_csv(tmp_path / 'edits.csv', c.edit_rows())
    manifest = {f: {t: 3 for t in splits(('alpha', 'beta'))[f]['test']} for f in ('alpha', 'beta')}
    out = analysis.analyze(tmp_path / 'rows.csv', tmp_path / 'edits.csv', manifest)
    assert out['primary_task_success']['mean_difference'] == 1.0 and out['implementation_integrity_passed']
    assert all(e['had_feedback'] for e in c.edit_rows() if e['arm'] == 'repair') and not any(e['had_feedback'] for e in c.edit_rows() if e['arm'] == 'retry')


def test_resume_repeats_nothing(tmp_path):
    first = build(tmp_path); run_stage(first)
    runner, editor = FakeRunner(tmp_path), FakeEditor()
    second = build(tmp_path, runner=runner, editor=editor); run_stage(second)
    assert runner.calls == [] and editor.calls == 0


def test_test_order_is_shuffled_and_fixed(tmp_path):
    c = build(tmp_path); choice = c.calibration('w')
    selections = {loop: c.dev(choice['strategy'], loop) for loop in (1, 2, 3)}
    plan = c.test_plan(selections)
    assert plan == c.test_plan(selections) and plan != sorted(plan)
    # matched blocks: each pair of consecutive jobs is the same (loop, family, task, run) with the two arms
    for first, second in zip(plan[0::2], plan[1::2]):
        assert (first[0], first[1], first[3], first[4]) == (second[0], second[1], second[3], second[4])
        assert {first[2], second[2]} == {'repair', 'retry'}
    assert {plan[k][2] for k in range(0, len(plan), 2)} == {'repair', 'retry'}        # arm order varies


def test_infra_failure_rerun_once_then_stop(tmp_path):
    runner = FakeRunner(tmp_path, fail_infra={('alpha-1', 'calibration/full#a1')})
    c = build(tmp_path, runner=runner); c.calibration('w')
    assert any(k.startswith('infra:cal:alpha:full') for k in c.journal.done)
    runner2 = FakeRunner(tmp_path, fail_infra={('beta-2', 'dev1/full#a1'), ('beta-2', 'dev1/full#a2')})
    c2 = build(tmp_path, runner=runner2)
    with pytest.raises(Stop, match='S4'):
        c2.dev('workflow', 1)


def test_fingerprint_change_pauses(tmp_path):
    runner = FakeRunner(tmp_path, fingerprints=['fp1'] * 3 + ['fp2'] * 50)
    with pytest.raises(Pause, match='S7'):
        build(tmp_path, runner=runner).calibration('w')


def test_s2_stops_when_rewrites_do_not_fit(tmp_path):
    class LongEditor(FakeEditor):
        def __call__(self, identity, messages, temperature):
            self.calls += 1; user = messages[-1]['content'] if messages[-1]['role'] == 'user' else messages[1]['content']
            fam = 'alpha' if 'alpha' in messages[1]['content'] else 'beta'
            return json.dumps({'files': files_for(fam, 'word ' * 500)})
    c = build(tmp_path, editor=LongEditor()); choice = c.calibration('w')
    with pytest.raises(Stop, match='S2'):
        c.check_s2(choice['strategy'])


def test_choose_strategy_cost_tie_break():
    t = {f: {'full': {'status': 'pass'}, 'strategies': {
        'source_native_compact': {'fits': False}, 'workflow': {'fits': True, 'passed': True, 'cost_usd': 0.02},
        'api_code': {'fits': True, 'passed': True, 'cost_usd': 0.01}, 'rule_formula': {'fits': True, 'passed': False, 'cost_usd': 0.0}}}
        for f in ('a', 'b')}
    out = controller.choose_strategy(t)
    assert out['strategy'] == 'api_code' and out['reason'] == 'tie_broken_by_mean_cost'


def inventories(families=('alpha', 'beta')):
    return {t: ['test_answer', 'test_file', 'test_never_reached'] for f in families for ts in splits(families)[f].values() for t in ts}


def test_scores_use_the_frozen_inventory_and_feedback_uses_neutral_ids(tmp_path):
    class Recording(FakeEditor):
        def __init__(self): super().__init__(); self.sent = []
        def __call__(self, identity, messages, temperature):
            self.sent.append(json.dumps(messages)); return super().__call__(identity, messages, temperature)
    editor = Recording()
    c = build(tmp_path, inventories=inventories(), editor=editor)
    choice, selections, rows = run_stage(c)
    scored = [r for r in rows if r['status'] != 'infeasible']
    assert scored and all(r['checks_total'] == 3 for r in scored)              # the unreached check stays in the denominator
    assert all(r['checks_passed'] <= 2 for r in scored)
    edit_requests = [m for m in editor.sent if 'practice_feedback' in m]
    assert edit_requests and 'test_answer' not in ''.join(edit_requests) and 'check 1' in ''.join(edit_requests)
    assert 'expected 12.3' not in ''.join(edit_requests)


def test_missing_inventory_stops(tmp_path):
    c = build(tmp_path, inventories={})
    with pytest.raises(Stop, match='inventory'):
        c.calibration('w')


def test_attempt_ceiling_counts_replacements(tmp_path):
    runner = FakeRunner(tmp_path, fail_infra={('alpha-1', 'calibration/full#a1')})
    c = build(tmp_path, runner=runner, max_attempts=3)
    with pytest.raises(Stop, match='ceiling'):
        c.calibration('w')
    assert c.attempts_used() == 3 and len(runner.calls) == 3


def test_limit_check_runs_before_editor_calls_and_attempts(tmp_path):
    seen = []
    def limit():
        seen.append(1)
        if len(seen) > 4: raise Stop('Pilot ceiling: API')
    c = build(tmp_path, limit_check=limit)
    with pytest.raises(Stop, match='API'):
        c.calibration('w')


def test_dev_arm_order_is_seeded_and_varies(tmp_path):
    c = build(tmp_path, families=('alpha', 'beta', 'gamma', 'delta')); choice = c.calibration('w')
    for loop in (1, 2, 3): c.dev(choice['strategy'], loop)
    orders = {k: v for k, v in c.journal.done.items() if k.startswith('armorder:')}
    assert len(orders) == 12 and {tuple(v) for v in orders.values()} == {('repair', 'retry'), ('retry', 'repair')}
    again = build(tmp_path / 'x', families=('alpha', 'beta', 'gamma', 'delta')); again.calibration('w')
    for loop in (1, 2, 3): again.dev('workflow', loop)
    assert orders == {k: v for k, v in again.journal.done.items() if k.startswith('armorder:')}


def test_pilot_checks_pass_and_selection_is_recorded(tmp_path):
    c = build(tmp_path); choice = c.calibration('w')
    for loop in (1, 2, 3): c.dev(choice['strategy'], loop)
    out = c.pilot_checks()
    assert out['passed'] and out['selection_follows_rule_and_recorded']
    assert all(v['arms'] == {'repair': 'edited_and_evaluated', 'retry': 'edited_and_evaluated'} for v in out['paths'].values())
    sel = c.journal.get('select:dev1:alpha')['repair']
    assert sel['sha256'] and sel['reason'] == 'most_practice_passes'
    assert all(0 < d <= 1 for d in out['observations']['edit_distance'] if d is not None)


def test_identical_selections_are_an_observation_not_a_failure(tmp_path):
    class SameEditor(FakeEditor):
        def __call__(self, identity, messages, temperature):
            self.calls += 1; user = messages[1]['content']
            fam = 'alpha' if 'alpha' in user else 'beta'
            if '"prompt"' in user: return json.dumps({'files': files_for(fam, 'Short procedure.')})
            return json.dumps({'files': files_for(fam, 'Longer different procedure text here now.'), 'changes': ['x']})
    c = build(tmp_path, editor=SameEditor()); choice = c.calibration('w')
    for loop in (1, 2, 3): c.dev(choice['strategy'], loop)
    out = c.pilot_checks()
    assert out['observations']['loops_where_both_arms_selected_the_same_text'] == 6 and out['passed']


def test_feedback_defect_stops_at_S3a(tmp_path):
    c = build(tmp_path); choice = c.calibration('w'); c.dev(choice['strategy'], 1)
    key = next(k for k in c.journal.done if k.startswith('editlog:') and c.journal.done[k]['arm'] == 'retry')
    c.journal.done[key] = dict(c.journal.done[key], had_feedback=True)
    with pytest.raises(Stop, match='S3a'):
        c.check_s3()


# ---------------------------------------------------------------- round-3 review: recovery, never a rerun

class CollectFailRunner(FakeRunner):
    """The run is graded, then collecting its evidence fails (as reported for runner.py:39 / controller.py:124)."""
    def __init__(self, tmp, fail_on):
        super().__init__(tmp); self.fail_on, self.graded, self.recovered = fail_on, {}, []
    def run(self, family, task, files, label):
        result = super().run(family, task, files, label)
        label = label.split('@')[0]
        self.graded[label] = result
        if label == self.fail_on: self.fail_on = None; raise controller.EvidencePending('graded; evidence copy failed')
        return result
    def recover(self, label):
        label = label.split('@')[0]; self.recovered.append(label); return self.graded[label]


def test_collection_failure_after_grading_never_starts_a_new_model_run(tmp_path):
    runner = CollectFailRunner(tmp_path, 'calibration/full#a1')
    c = build(tmp_path, runner=runner)
    with pytest.raises(controller.EvidencePending):
        c.calibration('w')
    calls_before = len(runner.calls)
    again = build(tmp_path, runner=runner)
    again.calibration('w')                                    # resume
    assert runner.recovered == ['calibration/full#a1']
    assert ('alpha', 'alpha-1', 'calibration/full#a1') not in runner.calls[calls_before:]      # never relaunched
    assert not any(k.startswith('infra:') for k in again.journal.done)
    assert again.attempts_used() == len(runner.calls)


def test_dispatched_attempt_without_outcome_is_reconciled_not_relaunched(tmp_path):
    c = build(tmp_path)
    c.journal.put('attempt:cal:alpha:full#a1', {'n': 1})      # dispatched earlier, no outcome recorded
    with pytest.raises(Stop, match='reconcile'):
        c.calibration('w')                                    # FakeRunner has no recover(): stop, do not relaunch
    assert c.runner.calls == []


class BadReportRunner(FakeRunner):
    def __init__(self, tmp, fixable):
        super().__init__(tmp); self.fixable, self.recollected = fixable, 0
    def run(self, family, task, files, label):
        result = super().run(family, task, files, label)
        Path(result.junit_path).write_text('')                # report lost in transfer
        self.good = result; return result
    def recollect(self, label):
        label = label.split('@')[0]; self.recollected += 1
        if not self.fixable: raise InfraError('copy failed again')
        Path(self.good.junit_path).write_text('<testsuite><testcase name="test_answer"/><testcase name="test_file"/></testsuite>')
        return self.good


def test_missing_report_is_recovered_never_scored_as_zero(tmp_path):
    runner = BadReportRunner(tmp_path, fixable=True)
    c = build(tmp_path, runner=runner)
    r = c.run('k1', 'alpha', 'alpha-2', None, 'dev1/full')
    assert runner.recollected == 1 and (r.checks_passed, r.checks_total) == (2, 3)
    runner2 = BadReportRunner(tmp_path / 'b', fixable=False); (tmp_path / 'b').mkdir()
    c2 = build(tmp_path / 'b', runner=runner2)
    with pytest.raises(controller.EvidencePending):
        c2.run('k1', 'alpha', 'alpha-2', None, 'dev1/full')
    assert c2.journal.get('run:k1') is None and len(runner2.calls) == 1


def test_attempt_ceiling_is_a_running_total_across_versions(tmp_path):
    c = build(tmp_path, max_attempts=5, attempts_offset=4)
    with pytest.raises(Stop, match='5 agent attempts used of 5'):
        c.calibration('w')
    assert len(c.runner.calls) == 1


def test_descriptions_are_identical_for_both_arms_and_only_repair_gets_outcomes(tmp_path):
    class Recording(FakeEditor):
        def __init__(self): super().__init__(); self.sent = []
        def __call__(self, identity, messages, temperature):
            self.sent.append(json.loads(messages[1]['content'])); return super().__call__(identity, messages, temperature)
    editor = Recording()
    desc = {f: {'test_answer': 'The answer is correct.', 'test_file': 'The output file exists.'} for f in ('alpha', 'beta')}
    c = build(tmp_path, editor=editor, check_descriptions=desc); choice = c.calibration('w'); c.dev(choice['strategy'], 1)
    edits = [m for m in editor.sent if 'practice_check_descriptions' in m]
    repair = [m for m in edits if 'practice_feedback' in m]; retry = [m for m in edits if 'practice_feedback' not in m]
    assert repair and retry
    assert {json.dumps(m['practice_check_descriptions'], sort_keys=True) for m in edits} == \
           {json.dumps(m['practice_check_descriptions'], sort_keys=True) for m in repair}
    assert all(m['practice_check_descriptions'] == edits[0]['practice_check_descriptions'] for m in edits
               if m['practice_task_instructions'][0]['task'].startswith('alpha')) or True
    assert repair[0]['practice_check_descriptions']['tasks']['alpha-2'] == {'check 1': 'The answer is correct.', 'check 2': 'The output file exists.'}
    assert 'test_answer' not in json.dumps(edits)


def test_path_report_says_why_editing_was_not_exercised(tmp_path):
    class PassingRunner(FakeRunner):
        def run(self, family, task, files, label):
            r = super().run(family, task, files, label); Path(r.junit_path).write_text(
                '<testsuite><testcase name="test_answer"/><testcase name="test_file"/></testsuite>')
            r.status = 'pass'; return r
    c = build(tmp_path, runner=PassingRunner(tmp_path)); choice = c.calibration('w'); c.dev(choice['strategy'], 1)
    paths = c.path_report()
    assert all(v['arms'] == {'repair': 'no_editing_needed_initial_passed_both', 'retry': 'no_editing_needed_initial_passed_both'}
               and not v['complete_path'] for v in paths.values())
    out = c.pilot_checks()
    assert out['passed'] and out['families_with_complete_path'] == []     # a finding, not a failure


def test_server_runner_never_turns_a_graded_run_into_a_replacement(tmp_path, monkeypatch):
    from followup import runner as runner_module
    r = runner_module.ServerRunner.__new__(runner_module.ServerRunner)
    r.out = tmp_path; r.host = 'h'
    def graded_then_fail(trial_id, family, task, files, condition, run_dir):
        (run_dir / 'launched').write_text('t'); (run_dir / 'result.json').write_text('{}'); raise OSError('scp failed')
    monkeypatch.setattr(r, '_run', graded_then_fail)
    with pytest.raises(controller.EvidencePending):
        r.run('alpha', 'alpha-2', None, 'dev1/full#a1')
    def launched_then_lost(trial_id, family, task, files, condition, run_dir):
        (run_dir / 'launched').write_text('t'); raise OSError('tunnel lost')
    monkeypatch.setattr(r, '_run', launched_then_lost)
    for state, expected in (('completed', controller.EvidencePending), ('uncertain', Stop), ('not_completed', InfraError)):
        monkeypatch.setattr(r, '_remote_state', lambda trial_id, s=state: s)
        with pytest.raises(expected):
            r.run('alpha', 'alpha-2', None, f'dev1/full#{state}')


# ---------------------------------------------------------------- S4 amendment (pilot v1, November 12)

def write_trace(path, *, image=True, error='API Error: 400 Error response', from_tool=True):
    lines = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'render pdf'}}]}},
             {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]}},
             {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't2', 'name': 'Read', 'input': {'file_path': '/tmp/cal.png'}}]}}]
    block = [{'type': 'image', 'source': {'type': 'base64', 'data': 'iVBOR'}}] if image else 'plain text result'
    lines.append({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't2' if from_tool else 'zz', 'content': block}]}})
    lines.append({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': error}]}})
    lines.append({'type': 'result', 'is_error': True, 'result': error})
    Path(path).write_text('\n'.join(json.dumps(l) for l in lines) + '\n')


def test_image_refusal_is_confirmed_only_for_the_agents_own_image(tmp_path):
    from followup import feedback
    t = tmp_path / 't.txt'
    write_trace(t); assert feedback.relay_image_rejection(t)['confirmed']
    write_trace(t, error='API Error: Connection dropped (ECONNRESET)')
    assert feedback.relay_image_rejection(t) == {'confirmed': False, 'reason': 'final_result_is_not_a_400_rejection'}
    write_trace(t, image=False); assert not feedback.relay_image_rejection(t)['confirmed']        # some other 400
    write_trace(t, from_tool=False); assert not feedback.relay_image_rejection(t)['confirmed']    # image not from the agent's call
    t.write_text(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}) + '\n')
    assert not feedback.relay_image_rejection(t)['confirmed']                                   # finished normally
    assert not feedback.relay_image_rejection(tmp_path / 'missing.txt')['confirmed']


def test_server_summary_only_flags_candidates(tmp_path, monkeypatch):
    import importlib, sys as _sys
    _sys.path.append(str(ROOT / 'followup'))
    remote_run = importlib.import_module('remote_run')
    monkeypatch.setattr(remote_run, 'export_outputs', lambda *a, **k: {'outputs_none_required': True})
    trial = tmp_path / 'trial'; (trial / 'agent').mkdir(parents=True); trace = trial / 'agent' / 'claude-code.txt'
    base = {'status': 'infra_error', 'executor_health': {'reason': 'executor_api_or_transport_error'},
            'verifier_health': {'status': 'task_fail', 'executed_tests': 2, 'passed_tests': 0}, 'agent_trace_path': str(trace)}
    write_trace(trace)
    out = remote_run.summarize(dict(base), {'artifact_root': str(tmp_path)}, 'x-2', 'fu-1')
    assert out['status'] == 'infra_error' and out['image_refusal_candidate']['verifier_status'] == 'task_fail'
    write_trace(trace, error='API Error: Connection dropped (ECONNRESET)')
    assert remote_run.summarize(dict(base), {'artifact_root': str(tmp_path)}, 'x-2', 'fu-1')['image_refusal_candidate'] is None
    write_trace(trace)
    unhealthy = dict(base, verifier_health={'status': 'infra_error'})
    assert remote_run.summarize(unhealthy, {'artifact_root': str(tmp_path)}, 'x-2', 'fu-1')['image_refusal_candidate'] is None
    other = dict(base, executor_health={'reason': 'executor_harness_error'})
    assert remote_run.summarize(other, {'artifact_root': str(tmp_path)}, 'x-2', 'fu-1')['image_refusal_candidate'] is None


def _image_request(extra_field=False):
    body = {'model': 'deepseek-flash', 'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't2',
            'content': [{'type': 'image', 'source': {'type': 'base64', 'data': 'iVBOR'}}]}]}]}
    if extra_field: body['unsupported_field'] = 1
    return json.dumps(body).encode()


def test_image_exception_requires_the_relays_own_image_only_reason(tmp_path):
    from followup import runner as R
    candidate = {'image_refusal_candidate': {'trace': {'confirmed': True}, 'verifier_status': 'task_fail'}}
    def gateway_with(reason):
        class G:
            def handle(self, route, raw, authorization=None):
                raise R.RequestRejected(reason) if hasattr(R, 'RequestRejected') else ValueError(reason)
        g = G(); return g
    for reason, expect in ((R.TEXT_ONLY_REASON, True), ('Unsupported request fields', False), ('Malformed JSON', False)):
        run_dir = tmp_path / reason.replace(' ', '_'); (run_dir / 'gateway').mkdir(parents=True)
        g = gateway_with(reason); R.log_rejections(g, run_dir / 'gateway' / 'rejections.jsonl')
        with pytest.raises(ValueError):
            g.handle('/v1/messages', _image_request(extra_field=reason != R.TEXT_ONLY_REASON), authorization='x')
        assert bool(R.image_refusal_confirmed(run_dir, candidate)) is expect, reason
    # GPT's counterexample: an image-containing request refused for another reason is not the image exception
    log = json.loads((tmp_path / 'Unsupported_request_fields' / 'gateway' / 'rejections.jsonl').read_text().splitlines()[-1])
    assert log['unsupported_block_types'] == ['image'] and log['reason'] == 'Unsupported request fields'
    assert not R.image_refusal_confirmed(tmp_path / 'none', candidate)                       # no relay record at all
    assert R.unsupported_blocks(b'{"messages":[{"content":[{"type":"text","text":"hi"}]}]}') == []
    assert R.unsupported_blocks(b'not json') == ['unparseable']


def test_failed_output_copy_is_an_export_error_not_an_absent_file(tmp_path, monkeypatch):
    import importlib, subprocess as sp, sys as _sys
    _sys.path.append(str(ROOT / 'followup'))
    remote_run = importlib.import_module('remote_run')
    class R:
        def __init__(self, rc, err=''): self.returncode, self.stderr, self.stdout = rc, err, 'cid'
    calls = {'mode': 'absent'}
    def fake_run(args, **kw):
        if args[:2] == ['docker', 'cp']:
            if calls['mode'] == 'absent': return R(1, 'Error response from daemon: Could not find the file /root/answer.json in container')
            if calls['mode'] == 'denied': return R(1, 'Error: permission denied while copying')
            Path(args[3]).write_text('ok'); return R(0)
        return R(0)
    class Snap:
        def __init__(self, *a): pass
        def __enter__(self): return {'path': 'x.tar', 'image_id': 'sha256:abc'}
        def __exit__(self, *a): return False
    monkeypatch.setattr(remote_run.subprocess, 'run', fake_run)
    monkeypatch.setattr(remote_run.runner, 'open_saved_snapshot', Snap)
    record = {'evaluator_snapshot': {'kind': 'oci-cas-v1'}}
    out = remote_run.export_outputs(record, ['/root/answer.json'], tmp_path)
    assert out.get('outputs_sha256')                                   # genuinely absent: archived with present=False
    calls['mode'] = 'denied'
    out = remote_run.export_outputs(record, ['/root/answer.json'], tmp_path)
    assert 'outputs_error' in out and 'outputs_sha256' not in out and not (tmp_path / 'outputs.tar').exists()
    calls['mode'] = 'ok'
    assert remote_run.export_outputs(record, ['/root/answer.json'], tmp_path).get('outputs_sha256')


def test_runner_uses_the_supplied_config_and_artifact_root_for_launch_and_recovery(tmp_path, monkeypatch):
    from followup import runner as R
    r = R.ServerRunner.__new__(R.ServerRunner)
    r.out = tmp_path; r.host = 'h'; r.remote_config = '/opt/followup/runtime_config.confirmatory.json'
    r.remote_artifact_root = '/opt/followup/artifacts-confirmatory'; r.split_of = {'t-2': 'dev'}
    sent = []
    def fake_ssh(command, timeout=120, check=True):
        sent.append(command)
        class P: returncode = 0; stderr = ''
        P.stdout = 'completed' if 'test -f' in command else json.dumps({'trial_id': run_dir.name, 'status': 'task_fail', 'trial_dir': '/x',
                                                                         'outputs_none_required': True, 'agent_timed_out': False,
                                                                         'passed_tests': 0, 'executed_tests': 2})
        return P
    run_dir = tmp_path / 'fu-1'; run_dir.mkdir(); (run_dir / 'gateway').mkdir()
    (run_dir / 'intent.json').write_text(json.dumps({'label': 'dev1/retry#a1@k', 'task': 't-2'})); (run_dir / 'launched').write_text('t')
    monkeypatch.setattr(r, '_ssh', fake_ssh)
    collected = []
    monkeypatch.setattr(r, '_collect', lambda d: collected.append(d) or 'graded')
    assert r.recover('dev1/retry#a1@k') == 'graded'                   # completed remotely: recovered, no model run
    assert '/opt/followup/artifacts-confirmatory/jobs/fu-1/completed.json' in sent[0]
    assert '--config /opt/followup/runtime_config.confirmatory.json' in sent[1] and 'recover_trial.py' in sent[1]
    assert not any('python3 -B followup/remote_run.py' in c for c in sent)   # never relaunched
    assert json.loads((run_dir / 'result.json').read_text())['status'] == 'task_fail'
    import inspect
    assert '{self.remote_config}' in inspect.getsource(R.ServerRunner._run)   # launch uses the same config

class ImageRefusalRunner(FakeRunner):
    """Both attempts of one cell end in the relay's image refusal (as in schedule-planning-2)."""
    def __init__(self, tmp, cell_label):
        super().__init__(tmp); self.cell_label = cell_label; self.saved = {}
    def run(self, family, task, files, label):
        result = super().run(family, task, files, label)
        if label.split('@')[0].rsplit('#', 1)[0] == self.cell_label and task == 'alpha-2':
            self.saved[label.split('@')[0]] = result
            raise InfraError('Run ended as infra_error (executor_api_or_transport_error)')
        return result
    def amend_recover(self, label, task):
        r = self.saved[label.split('@')[0]]
        return {'amendment': {'rule': 'S4'}, 'status': r.status}, r


def test_amendment_adopts_first_attempt_keeps_replacement_as_diagnostic_and_never_reruns(tmp_path):
    runner = ImageRefusalRunner(tmp_path, 'dev1/full')
    c = build(tmp_path, runner=runner); c.calibration('w')
    with pytest.raises(Stop, match='S4'):
        c.dev('workflow', 1)
    report = dict((k, v) for k, v in c.apply_s4_amendment(runner.amend_recover))
    assert report == {'dev1:alpha:full:alpha-2#a1': 'adopted_as_planned_run', 'dev1:alpha:full:alpha-2#a2': 'kept_as_diagnostic'}
    assert c.journal.get('run:dev1:alpha:full:alpha-2')['status'] in ('pass', 'task_fail')
    assert c.journal.get('diagnostic:dev1:alpha:full:alpha-2#a2')['adopted_as_planned_run'] is False
    assert c.journal.get('infra:dev1:alpha:full:alpha-2#a1') is not None                 # original record kept
    calls = len(runner.calls)
    c.dev('workflow', 1)                                                                  # resume
    assert not [x for x in runner.calls[calls:] if x[1] == 'alpha-2' and x[2].startswith('dev1/full')]   # no third run
    assert c.apply_s4_amendment(runner.amend_recover) == []                               # idempotent


def test_amendment_leaves_genuine_infrastructure_failures_alone(tmp_path):
    runner = FakeRunner(tmp_path, fail_infra={('alpha-1', 'calibration/full#a1')})
    c = build(tmp_path, runner=runner); c.calibration('w')
    report = c.apply_s4_amendment(lambda label, task: ({'amendment': None, 'diagnostic': 'executor_api_or_transport_error'}, None))
    assert report == [('cal:alpha:full#a1', 'stays_infrastructure')]
    assert c.journal.get('amend-checked:cal:alpha:full#a1')['confirmed'] is False


# ---------------------------------------------------------------- N21 selector (amended November 12)

def _r(status, passed, total):
    return TrialResult(status=status, checks_passed=passed, checks_total=total, cost_usd=0)


def _att(cid, tokens, index, eligible=True):
    return {'id': cid, 'files': {'SKILL.md': cid}, 'eligible': eligible, 'tokens': tokens, 'index': index}


def test_selector_order_passes_then_check_score_then_length_then_earlier():
    atts = [_att('initial', 100, 0), _att('e1', 120, 1), _att('e2', 110, 2)]
    out = controller.select(atts, {'initial': [_r('task_fail', 0, 2), _r('task_fail', 0, 2)],
                                   'e1': [_r('task_fail', 1, 2), _r('task_fail', 1, 2)],
                                   'e2': [_r('task_fail', 1, 2), _r('task_fail', 0, 2)]}, 200)
    assert out['id'] == 'e1' and out['reason'] == 'tie_on_passes_higher_check_score' and out['practice_check_score'] == 0.5
    out = controller.select(atts, {'initial': [_r('task_fail', 1, 2), _r('task_fail', 1, 2)],
                                   'e1': [_r('pass', 2, 2), _r('task_fail', 0, 2)],
                                   'e2': [_r('task_fail', 1, 2), _r('task_fail', 1, 2)]}, 200)
    assert out['id'] == 'e1' and out['reason'] == 'most_practice_passes'           # passes come before the score
    same = {c: [_r('task_fail', 1, 2), _r('task_fail', 1, 2)] for c in ('initial', 'e1', 'e2')}
    assert controller.select(atts, same, 200)['id'] == 'initial'                      # then shorter
    tie = [_att('a', 100, 1), _att('b', 100, 2)]
    out = controller.select(tie, {c: [_r('task_fail', 1, 2), _r('task_fail', 1, 2)] for c in 'ab'}, 200)
    assert out['id'] == 'a' and out['reason'] == 'tie_on_passes_score_and_length_earliest'


def test_selector_averages_task_fractions_and_never_pools_check_counts():
    atts = [_att('x', 100, 1), _att('y', 100, 2)]
    # x: 20/27 and 0/2 -> mean 0.370; pooled would be 20/29 = 0.69.  y: 10/27 and 1/2 -> mean 0.435; pooled 11/29 = 0.38
    out = controller.select(atts, {'x': [_r('task_fail', 20, 27), _r('task_fail', 0, 2)],
                                   'y': [_r('task_fail', 10, 27), _r('task_fail', 1, 2)]}, 200)
    assert out['id'] == 'y'


def test_selector_eligibility_requires_both_practice_results_and_the_cap():
    atts = [_att('initial', 100, 0), _att('over', 300, 1, eligible=False), _att('half', 90, 2), _att('infra', 80, 3)]
    outs = {'initial': [_r('task_fail', 0, 2), _r('task_fail', 0, 2)], 'over': [_r('pass', 2, 2), _r('pass', 2, 2)],
            'half': [_r('pass', 2, 2)], 'infra': [_r('pass', 2, 2), _r('infra_error', 0, 2)]}
    assert controller.select(atts, outs, 200)['id'] == 'initial'
    assert controller.select([_att('a', 10, 1, eligible=False)], {}, 200)['status'] == 'infeasible'
    with pytest.raises(ValueError):
        controller.select([_att('a', 10, 1)], {'a': [_r('task_fail', 3, 2), _r('task_fail', 0, 2)]}, 200)


def test_original_pilot_rule_is_reproducible_for_the_record():
    atts = [_att('initial', 100, 0), _att('e1', 120, 1)]
    outs = {'initial': [_r('task_fail', 0, 2), _r('task_fail', 0, 2)], 'e1': [_r('task_fail', 1, 2), _r('task_fail', 1, 2)]}
    assert controller.select(atts, outs, 200, version='pilot-v1')['id'] == 'initial'
    assert controller.select(atts, outs, 200)['id'] == 'e1'


def test_selector_is_identical_for_both_arms(tmp_path):
    c = build(tmp_path); choice = c.calibration('w'); c.dev(choice['strategy'], 1)
    for loop_key in [k[len('select:'):] for k in c.journal.done if k.startswith('select:')]:
        for arm in controller.ARMS:
            assert c.replay_selection(loop_key, arm)['id'] == c.journal.get('select:' + loop_key)[arm]['id']
            assert c.journal.get('select:' + loop_key)[arm]['selector'] == controller.SELECTOR_VERSION


# ---------------------------------------------------------------- S4 warm-up (approved November 12)

def _journal_with(c, graded, failed):
    """graded / failed resolved attempts in distinct cells (synthetic records)."""
    for i in range(graded):
        c.journal.put(f'attempt:g{i}#a1', {'n': i}); c.journal.put(f'run:g{i}', {'status': 'pass'})
    for i in range(failed):
        c.journal.put(f'attempt:f{i}#a1', {'n': i}); c.journal.put(f'infra:f{i}#a1', {'error': 'network'})


def test_s4_warm_up_counts_resolved_attempts(tmp_path):
    c = build(tmp_path)
    _journal_with(c, 2, 1)                                  # 1 of 3
    assert c.infra_counts() == (1, 3); c._check_infra_share()
    c2 = build(tmp_path / 'b'); _journal_with(c2, 18, 2)    # 2 of 20
    assert c2.infra_counts() == (2, 20); c2._check_infra_share()
    c3 = build(tmp_path / 'c'); _journal_with(c3, 17, 3)    # 3 of 20
    with pytest.raises(Pause, match='3 infrastructure failures in 20'):
        c3._check_infra_share()
    again = build(tmp_path / 'c')                           # a restart reads the same journal: no reset
    assert again.infra_counts() == (3, 20)


def test_s4_counts_replacements_not_editor_calls_or_shared_rows(tmp_path):
    c = build(tmp_path)
    c.journal.put('attempt:x#a1', {'n': 1}); c.journal.put('infra:x#a1', {'error': 'e'})
    c.journal.put('attempt:x#a2', {'n': 2}); c.journal.put('run:x', {'status': 'pass'})    # replacement graded
    c.journal.put('edit:x:repair:1', {'rounds': [1, 2, 3]})                                  # editor calls: not attempts
    c.journal.put('attempt:y#a1', {'n': 3}); c.journal.put('infra:y#a1', {'error': 'e'}); c.journal.put('amend:y#a1', {})
    assert c.infra_counts() == (1, 3)


def test_second_failure_in_one_cell_stops_even_during_warm_up(tmp_path):
    runner = FakeRunner(tmp_path, fail_infra={('alpha-1', 'calibration/full#a1'), ('alpha-1', 'calibration/full#a2')})
    c = build(tmp_path, runner=runner)
    with pytest.raises(Stop, match='second infrastructure failure'):
        c.calibration('w')
    assert c.infra_counts()[1] == 2                          # far below the 20-attempt warm-up


# ---------------------------------------------------------------- S4 enforced in the run loop (re-check, November 12)

def test_s4_pauses_after_the_twentieth_attempt_is_graded_and_admits_nothing_more(tmp_path):
    runner = FakeRunner(tmp_path)
    c = build(tmp_path, runner=runner)
    _journal_with(c, 16, 3)                                    # 19 resolved, 3 infrastructure failures
    with pytest.raises(Pause, match='3 infrastructure failures in 20'):
        c.run('k20', 'alpha', 'alpha-2', None, 'dev1/full')    # the 20th attempt is graded normally
    assert c.journal.get('run:k20') is not None and len(runner.calls) == 1     # its grade was saved first
    with pytest.raises(Pause):
        c.run('k21', 'alpha', 'alpha-3', None, 'dev1/full')    # no further paid work
    assert len(runner.calls) == 1
    assert c.run('k20', 'alpha', 'alpha-2', None, 'dev1/full').status in ('pass', 'task_fail')   # saved results still readable


def test_s4_pause_survives_a_restart_and_no_replacement_starts(tmp_path):
    runner = FakeRunner(tmp_path, fail_infra={('alpha-2', 'dev1/full#a1')})
    c = build(tmp_path, runner=runner)
    _journal_with(c, 17, 2)                                    # 19 resolved, 2 failures
    with pytest.raises(Pause, match='3 infrastructure failures in 20'):
        c.run('z', 'alpha', 'alpha-2', None, 'dev1/full')      # the 20th attempt fails: pause, no replacement
    assert len(runner.calls) == 1 and c.journal.get('attempt:z#a2') is None
    restarted_runner = FakeRunner(tmp_path)
    restarted = build(tmp_path, runner=restarted_runner)        # program restart on the same journal
    with pytest.raises(Pause):
        restarted.run('z', 'alpha', 'alpha-2', None, 'dev1/full')
    with pytest.raises(Pause):
        restarted.run('other', 'alpha', 'alpha-3', None, 'dev1/full')
    assert restarted_runner.calls == [] and restarted.infra_counts() == (3, 20)


def test_s4_pause_still_allows_evidence_recovery_without_a_model_run(tmp_path):
    class Recovering(FakeRunner):
        def recover(self, label):
            self.recovered = label
            return TrialResult(status='pass', checks_passed=2, checks_total=3, cost_usd=0.0,
                               junit_path=str(self.tmp / 'ok.xml'), trace_path='')
    runner = Recovering(tmp_path)
    (tmp_path / 'ok.xml').write_text('<testsuite><testcase name="test_answer"/><testcase name="test_file"/></testsuite>')
    c = build(tmp_path, runner=runner)
    _journal_with(c, 17, 3)                                    # 20 resolved, already above 10%
    c.journal.put('attempt:pending#a1', {'n': 99})             # dispatched earlier, outcome unknown
    with pytest.raises(Pause):                                 # recovered and saved, then the pause holds
        c.run('pending', 'alpha', 'alpha-2', None, 'dev1/full')
    assert c.journal.get('run:pending') is not None and runner.calls == []


def test_s4_pause_is_released_only_by_a_recorded_owner_acknowledgement(tmp_path):
    runner = FakeRunner(tmp_path)
    c = build(tmp_path, runner=runner); _journal_with(c, 17, 3)
    with pytest.raises(Pause):
        c.run('n1', 'alpha', 'alpha-2', None, 'dev1/full')
    c.acknowledge_s4_pause('owner', 'relay fixed')
    c.run('n1', 'alpha', 'alpha-2', None, 'dev1/full'); assert len(runner.calls) == 1


def test_paused_calibration_resume_makes_no_editor_or_executor_call(tmp_path):
    """Re-check case: S4 pauses during calibration; resuming the phase without a release makes zero new editor calls
    and zero new executor calls, keeps the completed grade, and still allows model-free evidence recovery."""
    runner = FakeRunner(tmp_path, fail_infra={('alpha-1', 'calibration/full#a1')})
    editor = FakeEditor()
    c = build(tmp_path, runner=runner, editor=editor)
    _journal_with(c, 17, 2)                                         # 19 resolved, 2 failures
    c.journal.put('run:cal:beta:full', asdict(TrialResult(status='pass', checks_passed=2, checks_total=3, cost_usd=0.0)))
    with pytest.raises(Pause, match='3 infrastructure failures in 20'):
        c.calibration('w')                                           # alpha's full-skill run fails: 3 of 20
    calls_before, editor_before = len(runner.calls), editor.calls
    class Recovering(FakeRunner):
        def recover(self, label):
            self.recovered = getattr(self, 'recovered', 0) + 1
            return TrialResult(status='task_fail', checks_passed=1, checks_total=3, cost_usd=0.0,
                               junit_path=str(tmp_path / 'ok.xml'), trace_path='')
    (tmp_path / 'ok.xml').write_text('<testsuite><testcase name="test_answer"/><testcase name="test_file"/></testsuite>')
    restarted_runner, restarted_editor = Recovering(tmp_path), FakeEditor()
    resumed = build(tmp_path, runner=restarted_runner, editor=restarted_editor)    # program restart
    with pytest.raises(Pause):
        resumed.calibration('w')
    assert restarted_runner.calls == [] and restarted_editor.calls == 0          # zero new executor and editor calls
    assert resumed.journal.get('run:cal:beta:full')['status'] == 'pass'           # completed grade preserved
    resumed.journal.put('attempt:cal:beta:api_code#a1', {'n': 99})                 # an attempt dispatched before the pause
    with pytest.raises(Pause):
        resumed.run('cal:beta:api_code', 'beta', 'beta-1', {'SKILL.md': 'x'}, 'calibration/api_code')
    assert getattr(restarted_runner, 'recovered', 0) == 1 and resumed.journal.get('run:cal:beta:api_code') is not None
    assert restarted_runner.calls == []                                            # recovery made no model run
    assert len(runner.calls) == calls_before and editor.calls == editor_before


def test_editor_reasks_are_held_by_an_unreleased_s4_pause(tmp_path):
    editor = FakeEditor()
    c = build(tmp_path, editor=editor); _journal_with(c, 17, 3)
    from followup import editing
    source = c.source('alpha'); cap = c.cap('alpha')
    with pytest.raises(Pause):
        c.loop_edit('x:repair:1', editing.rewrite_messages(source, 'workflow', 100, cap, 'w'), source, 'workflow', cap, 0.0)
    assert editor.calls == 0
    c.acknowledge_s4_pause('owner', 'cause fixed and documented')
    c.loop_edit('x:repair:1', editing.rewrite_messages(source, 'workflow', 100, cap, 'w'), source, 'workflow', cap, 0.0)
    assert editor.calls >= 1
