"""Offline tests (no model calls): python3.11 -m pytest followup/tests -q"""
import json, sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from capstudy.bundles import load_bundle
from followup import editing, feedback

SKILLS = ROOT / 'final/output/candidates'


class WordCounter:
    """Stand-in counter for tests: whitespace tokens."""
    def count_file(self, text): return len(text.split())
    def __call__(self, text): return len(text.split())


def bundle(tmp_path):
    root = tmp_path / 'skill' / 'demo'; root.mkdir(parents=True)
    (root / 'SKILL.md').write_text('---\nname: demo\ndescription: demo skill\n---\n\n# Demo\n\n' + 'Step one do the thing. ' * 60 + '\n')
    return load_bundle(tmp_path / 'skill')


def answer(source, body):
    path = next(iter(source.files))
    front = source.files[path].split('---\n', 2)
    return json.dumps({'files': {path: '---\n' + front[1] + '---\n\n' + body + '\n'}})


def test_budget_loop_reasks_until_within_cap(tmp_path):
    source = bundle(tmp_path); counter = WordCounter(); cap = 60
    answers = [answer(source, 'word ' * 200), answer(source, 'word ' * 90), answer(source, 'word ' * 30)]
    seen = []
    def call(messages, round_index):
        seen.append(messages); return answers[round_index]
    out = editing.run_loop(call, [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}], source, 'workflow', counter, cap)
    assert out['eligible'] and out['accepted'] and [r['eligible'] for r in out['rounds']] == [False, False, True]
    assert 'Remove at least' in seen[1][-1]['content'] and seen[1][-2]['role'] == 'assistant'


def test_budget_loop_stops_after_three_reasks_and_never_cuts(tmp_path):
    source = bundle(tmp_path); calls = []
    def call(messages, round_index):
        calls.append(round_index); return answer(source, 'word ' * 500)
    out = editing.run_loop(call, [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}], source, 'workflow', WordCounter(), 60)
    assert not out['eligible'] and calls == [0, 1, 2, 3] and 'over_cap' in out['ineligibility_reasons']
    assert out['candidate_tokens'] > 500      # the over-cap text is kept as is, not truncated


def test_copy_is_rejected_once(tmp_path):
    source = bundle(tmp_path); path = next(iter(source.files))
    current = json.loads(answer(source, 'short body here'))['files']
    replies = [answer(source, 'short body here'), answer(source, 'a different short body')]
    out = editing.run_loop(lambda m, i: replies[i], [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}],
                           source, 'workflow', WordCounter(), 60, current_files=current)
    assert out['rounds'][0]['copy_of_current'] and out['accepted'] and out['files'][path] != current[path]
    stuck = editing.run_loop(lambda m, i: replies[0], [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}],
                             source, 'workflow', WordCounter(), 60, current_files=current)
    assert len(stuck['rounds']) == 2 and not stuck['accepted']


def test_invalid_json_is_reasked(tmp_path):
    source = bundle(tmp_path)
    replies = ['not json', answer(source, 'fine body')]
    out = editing.run_loop(lambda m, i: replies[i], [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}], source, 'workflow', WordCounter(), 60)
    assert out['eligible'] and out['rounds'][0]['reasons'] == ['invalid_json_or_file_mapping']


def test_retry_equals_repair_minus_feedback():
    files = {'a/SKILL.md': '---\nname: a\n---\ntext\n'}
    dev = [{'instance_id': 't-2', 'instruction': 'do x'}, {'instance_id': 't-3', 'instruction': 'do y'}]
    fb = {'runs': [{'task': 't-2', 'failed_checks': [{'check': 'test_a', 'message': 'm'}]}]}
    repair = editing.edit_messages(files, files, files, dev, 100, 1, 'workflow', fb)
    retry = editing.edit_messages(files, files, files, dev, 100, 1, 'workflow', None)
    assert editing.differs_only_by_feedback(repair, retry)
    assert 'practice_feedback' not in retry[1]['content'] and 'test_a' not in retry[1]['content']
    other = editing.edit_messages(files, files, files, dev, 100, 2, 'workflow', None)
    assert not editing.differs_only_by_feedback(repair, other)


@pytest.mark.parametrize('message,expected', [
    ("AssertionError: -47.5421 != -36.9181 within 0.0001 delta : Expected latitude -36.9181, got -47.5421", 'wrong_value'),
    ("Expected city is Paris", 'wrong_value'),
    ("FileNotFoundError: [Errno 2] No such file or directory: '/root/metrics.json'", 'missing_output'),
    ("AssertionError: metrics.json not found", 'missing_output'),
    ("playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.", 'timed_out'),
    ("KeyError: 'distance_km'", 'wrong_format'),
    ("something odd happened", 'other_failure')])
def test_failure_category(message, expected):
    assert feedback.categorize('failure', message) == expected


def test_feedback_never_contains_verifier_message_text(tmp_path):
    """The reviewer's case: an answer written as plain words must not reach repair."""
    junit = tmp_path / 'j.xml'
    junit.write_text('<testsuite><testcase name="test_city[Paris-expected]"><failure message="Expected city is Paris">'
                     'assert answer == "Paris"; reference id us7000myfa; value 419.32</failure></testcase>'
                     '<testcase name="test_file_exists"/></testsuite>')
    trace = tmp_path / 't.txt'; trace.write_text(json.dumps({'type': 'result', 'result': 'I wrote London to answer.json'}) + '\n')
    packet = feedback.assemble([feedback.run_feedback('task-2', junit, trace, False, ['test_city[Paris-expected]', 'test_file_exists'])])
    text = json.dumps(packet)
    for secret in ('Paris', 'us7000myfa', '419.32'):
        assert secret not in text
    assert packet['runs'][0]['failed_checks'] == [{'check': 'check 1', 'category': 'wrong_value'}]
    assert 'test_city' not in text and 'test_file_exists' not in text
    assert 'London' in text                                   # the agent's own output is kept
    verifier_fields = [v for k, v in feedback.packet_fields(packet) if k.startswith('verifier:')]
    assert set(verifier_fields) == {'check 1', 'wrong_value', 'check 2'}


def test_released_skills_survive_first_study_postprocessing():
    """finish() on a saved eligible first-study skill reports it eligible with the same file set."""
    fam = SKILLS / 'earthquake-plate-calculation'
    source = load_bundle(fam / 'full_skill')
    files = {p: (fam / 'unchanged_rewrite' / p).read_text() for p in source.files}
    out = editing.finish(source, json.dumps({'files': files}), 'workflow', WordCounter(), 10 ** 6)
    assert out['eligible'] and set(out['files']) == set(source.files)


def test_answer_bearing_check_names_never_reach_repair(tmp_path):
    """Second review's case: an unparametrised name can carry the answer; passed names too."""
    junit = tmp_path / 'j.xml'
    junit.write_text('<testsuite><testcase name="test_expected_city_is_Paris"><failure message="AssertionError: wrong"/></testcase>'
                     '<testcase name="test_total_is_419_32"/><testcase name="test_skipped_one"><skipped message="needs output"/></testcase></testsuite>')
    trace = tmp_path / 't.txt'; trace.write_text(json.dumps({'type': 'result', 'result': 'done'}) + '\n')
    inventory = ['test_total_is_419_32', 'test_expected_city_is_Paris', 'test_skipped_one', 'test_never_reached']
    packet = feedback.assemble([feedback.run_feedback('task-2', junit, trace, False, inventory)])
    text = json.dumps(packet)
    assert 'Paris' not in text and '419' not in text and 'test_' not in text
    assert packet['runs'][0]['failed_checks'] == [{'check': 'check 2', 'category': 'wrong_value'}]
    assert packet['runs'][0]['passed_checks'] == ['check 1']
    assert all(kind in ('verifier:check_id', 'verifier:category') or kind.startswith('agent:') for kind, _ in feedback.packet_fields(packet))


def test_score_counts_skipped_and_unreached_checks_as_not_passed(tmp_path):
    """Second review's case: 1 pass and 9 skips is 1 of 10, not 100%."""
    junit = tmp_path / 'j.xml'
    junit.write_text('<testsuite><testcase name="c0"/>' + ''.join(f'<testcase name="c{i}"><skipped message="prerequisite"/></testcase>' for i in range(1, 9)) + '</testsuite>')
    inventory = [f'c{i}' for i in range(10)]
    score = feedback.score_junit(junit, inventory)
    assert (score['checks_passed'], score['checks_total']) == (1, 10)
    assert score['outcomes']['c9']['outcome'] == 'not_reached' and score['outcomes']['c3']['skip_reason'] == 'prerequisite'
    assert feedback.score_junit(junit, inventory, not_applicable=['c9'])['checks_total'] == 9
    assert score['checks_executed'] == 1                    # executed differs from the inventory: accepted
    for bad in (tmp_path / 'missing.xml', tmp_path / 'empty.xml', tmp_path / 'broken.xml'):
        if bad.name == 'empty.xml': bad.write_text('')
        if bad.name == 'broken.xml': bad.write_text('<testsuite><testcase name="c0"')
        with pytest.raises(feedback.EvidenceError):          # never a score of zero
            feedback.score_junit(bad, inventory)
    (tmp_path / 'odd.xml').write_text('<testsuite><testcase name="c0"/><testcase name="surprise"/></testsuite>')
    with pytest.raises(feedback.EvidenceError, match='outside the frozen inventory'):
        feedback.score_junit(tmp_path / 'odd.xml', inventory)
    (tmp_path / 'dup.xml').write_text('<testsuite><testcase name="c0"/><testcase name="c0"/></testsuite>')
    with pytest.raises(feedback.EvidenceError, match='duplicate'):
        feedback.score_junit(tmp_path / 'dup.xml', inventory)


def test_categories_on_hand_read_failures():
    cases = {'missing_output': ["AssertionError: assert False  +  where False = <function isfile at 0x7>('/root/answers.json')",
                                'AssertionError: style.css must exist at /root/output/css/style.css assert False',
                                'Failed: Could not find all subject folders', 'AssertionError: No SVG element found'],
             'wrong_format': ["AssertionError: metrics.json missing key: overall_rmse assert 'overall_rmse' in {}"],
             'wrong_value': ["AssertionError: GDP value mismatches:   H26: expected 418.0, got 2019.0",
                             'AssertionError: Missing papers in /root/papers/LLM: [x]'],
             'timed_out': ['playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.']}
    for category, messages in cases.items():
        for m in messages: assert feedback.categorize('failure', m) == category, m
