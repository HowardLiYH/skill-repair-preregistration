"""Amendment 5 (owner approval, November 13, 2026): the image trace check reads every entry of the rejected request's
final tool-result batch and matches the image to the agent's own tool call in the last assistant turn. Offline."""
import json, sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2'), str(ROOT / 'followup' / 'tests')]
from followup import feedback
from followup import runner as R
from followup.controller import TrialResult
from test_controller import build, FakeRunner

IMG = [{'type': 'image', 'source': {'type': 'base64', 'data': 'iVBOR'}}]
NOTE = {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '[Image: original 1800x2400, displayed at 1500x2000.]'}]}}


def call(tid, name='Read'):
    return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': {}}]}}


def result(tid, content):
    return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': content}]}}


def trace(path, entries, error='API Error: 400 Error response'):
    entries = entries + [{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': error}]}},
                         {'type': 'result', 'is_error': True, 'result': error}]
    Path(path).write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
    return feedback.relay_image_rejection(path)


def test_image_followed_by_a_cli_note_is_confirmed(tmp_path):
    out = trace(tmp_path / 't', [call('t1', 'Bash'), result('t1', 'ok'), call('t2'), result('t2', IMG), NOTE])
    assert out == {'confirmed': True, 'reason': 'agent_image_refused_by_text_only_relay', 'tools': ['Read']}


def test_no_image_in_the_final_batch_is_refused(tmp_path):
    out = trace(tmp_path / 't', [call('t2'), result('t2', 'plain text'), NOTE])
    assert out['reason'] == 'no_image_in_the_rejected_request'


def test_image_from_an_earlier_turn_is_refused(tmp_path):
    out = trace(tmp_path / 't', [call('t1'), result('t1', IMG), call('t2', 'Bash'), result('t2', 'ok'), NOTE])
    assert out['reason'] == 'no_image_in_the_rejected_request'


def test_unmatched_tool_call_id_is_refused(tmp_path):
    assert trace(tmp_path / 't', [call('t2'), result('zz', IMG), NOTE])['reason'] == 'image_not_from_an_agent_tool_call'
    # An image answering an earlier turn's call (not the last assistant turn) is not matched either.
    out = trace(tmp_path / 'u', [call('t1'), result('t1', 'ok'), call('t2', 'Bash'), result('t1', IMG), NOTE])
    assert out['reason'] == 'image_not_from_an_agent_tool_call'


def test_no_assistant_tool_call_or_other_errors_are_refused(tmp_path):
    assert not trace(tmp_path / 't', [result('t2', IMG), NOTE])['confirmed']
    assert trace(tmp_path / 'u', [call('t2'), result('t2', IMG), NOTE], error='API Error: Connection dropped')['reason'] == \
        'final_result_is_not_a_400_rejection'


def test_image_request_rejected_for_another_reason_stays_infrastructure(tmp_path):
    candidate = {'image_refusal_candidate': {'trace': {'confirmed': True}, 'verifier_status': 'pass'}}
    for reason, types, expect in ((R.TEXT_ONLY_REASON, ['image'], True), ('Unsupported request fields', ['image'], False),
                                  (R.TEXT_ONLY_REASON, ['image', 'document'], False)):
        d = tmp_path / f'{len(types)}{reason[:5]}'; (d / 'gateway').mkdir(parents=True)
        (d / 'gateway' / 'rejections.jsonl').write_text(json.dumps({'at': 1e10, 'reason': reason, 'unsupported_block_types': types}) + '\n')
        assert bool(R.image_refusal_confirmed(d, candidate)) is expect


class ImageRunner(FakeRunner):
    """Both attempts of one cell recorded as infrastructure; the earlier one qualifies (and scored better)."""
    def __init__(self, tmp):
        super().__init__(tmp, fail_infra={('alpha-1', 'calibration/full#a1'), ('alpha-1', 'calibration/full#a2')})
        self.reread = []
    def amend_recover(self, label, task):
        self.reread.append(label)
        junit = self.tmp / f'{len(self.reread)}.xml'
        junit.write_text('<testsuite><testcase name="test_answer"/><testcase name="test_file"/><testcase name="test_never_reached"/></testsuite>')
        status = 'pass' if '#a1' in label else 'task_fail'
        return ({'status': status, 'amendment': {'rule': 'S4'}},
                TrialResult(status=status, checks_passed=3, checks_total=3, cost_usd=0.02, junit_path=str(junit)))


def test_earliest_qualifying_attempt_adopted_later_kept_as_diagnostic_no_third_run(tmp_path):
    from followup.controller import Stop
    runner = ImageRunner(tmp_path)
    c = build(tmp_path, runner=runner)
    with pytest.raises(Stop, match='second infrastructure failure'):
        c.run('cal:alpha:full', 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full')
    before, calls = dict(c.journal.done), len(runner.calls)
    report = c.apply_amendment3(runner.amend_recover, 'owner', 'ok', tag='a5', rule='Amendment 5')
    assert report == [('cal:alpha:full#a1', 'adopted_as_planned_run', None), ('cal:alpha:full#a2', 'kept_as_diagnostic')]
    assert c.run('cal:alpha:full', 'alpha', 'alpha-1', c.source('alpha').files, 'calibration/full').status == 'pass'
    assert len(runner.calls) == calls                                     # no third run
    for k, v in before.items(): assert c.journal.get(k) == v
    assert c.infra_counts() == (0, 2)
    assert c.apply_amendment3(runner.amend_recover, 'owner', 'ok', tag='a5', rule='Amendment 5') == []
