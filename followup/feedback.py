"""Sanitized execution feedback for the repair arm.

Repair learns WHAT failed and HOW the agent went about the task, never the answer. For each failed verifier check it
receives a neutral id (check 1, check 2, ... from the task's frozen check inventory) and a failure category derived
from the verifier's report by fixed keyword rules (approximate). The verifier's check names and message text, which
can contain expected answers, are never passed on. The agent's own final message, errors and last commands are
passed on unchanged: they are the agent's output, not the answer key.

(The first study passed neither verifier messages nor traces. An intermediate design tried to hide values inside
verifier messages with patterns; that was dropped because it both removed useful diagnostics and could miss answers
written as plain words.)
"""
import json, re
from pathlib import Path
import xml.etree.ElementTree as ET

CATEGORIES = ('missing_output', 'timed_out', 'wrong_format', 'wrong_value', 'error_during_check', 'other_failure')
# First match wins. Approximate by design. Checked once, before the pilot, against a hand reading of 60 distinct
# failures from the first study's 560 saved reports (followup/audit_feedback.py); see docs/FOLLOWUP_IMPLEMENTATION.md.
_RULES = (
    ('timed_out', r'TimeoutError|timed out|Timeout \d|time limit'),
    ('missing_output', r'FileNotFoundError|No such file|not found|does not exist|missing (file|output)|is missing|not exist'
                       r'|isfile|isdir|must exist|must be copied|is empty|could not find|\bNo \w+( \w+)? found'),
    ('wrong_format', r'JSONDecodeError|KeyError|TypeError|invalid (json|format)|not a valid|unexpected (type|key|field)'
                     r'|missing (key|column|field)'),
    ('wrong_value', r'AssertionError|assert |!=|==|mismatch|differs|expected|within|tolerance|should'),
    ('wrong_format', r'ValueError|schema|column|header|parse'),
)


def categorize(tag, message):
    """A coarse category from the verifier's own report. Only the category leaves this function."""
    text = message or ''
    for name, pattern in _RULES:
        if re.search(pattern, text, re.I): return name
    return 'error_during_check' if tag == 'error' else 'other_failure'


def junit_cases(path):
    """Every test case in a verifier report: (native name, outcome, category or None, skip reason or None).
    outcome is one of pass, fail, skip. Native names stay on the Mac; they are never passed to the editor."""
    out = []
    for case in ET.parse(path).getroot().iter('testcase'):
        name = case.get('name') or ''
        bad = [c for c in case if c.tag in ('failure', 'error')]
        skip = [c for c in case if c.tag == 'skipped']
        if bad:
            # The report's one-line message when present; the traceback body only as a fallback (tracebacks quote
            # unrelated exception names from library code).
            out.append((name, 'fail', categorize(bad[0].tag, bad[0].get('message') or bad[0].text or ''), None))
        elif skip:
            out.append((name, 'skip', None, (skip[0].get('message') or skip[0].text or '')[:300]))
        else:
            out.append((name, 'pass', None, None))
    return out


def neutral_ids(names):
    """Stable neutral identifiers: 'check k' by position in the task's frozen check inventory."""
    return {name: f'check {i}' for i, name in enumerate(names, 1)}


def checks_from_junit(path, inventory=None):
    """Failed checks as {'check': neutral id, 'category'} and passed checks as neutral ids.

    The editor never sees a verifier's own check names: a name such as test_expected_city_is_Paris can carry an
    answer. With the task's frozen inventory the ids are stable across runs of the same task; without it they follow
    the report's order. A case missing from the inventory gets an 'extra check k' id."""
    cases = junit_cases(path)
    ids = neutral_ids(inventory if inventory is not None else [c[0] for c in cases])
    extra = 0
    failed, passed = [], []
    for name, outcome, category, _ in cases:
        if name not in ids:
            extra += 1; ids[name] = f'extra check {extra}'
        if outcome == 'fail':
            item = {'check': ids[name], 'category': category}
            if item not in failed: failed.append(item)
        elif outcome == 'pass':
            passed.append(ids[name])
    return failed, sorted(set(passed), key=_id_order)


def _id_order(check_id):
    head, _, number = check_id.rpartition(' ')
    return (head, int(number) if number.isdigit() else 0)


class EvidenceError(RuntimeError):
    """A verifier report is missing, unreadable, invalid or does not belong to the run. An infrastructure issue to
    reconcile (recover the evidence), never a score of zero and never a reason to rerun a graded run."""


def score_junit(path, inventory, not_applicable=()):
    """Check-level score against the task's frozen inventory of applicable checks.

    Three quantities are kept apart: the frozen number of applicable checks (the denominator), the number the report
    actually executed, and the number passed. A check that is skipped, failed, or absent from a valid report (not
    reached because the agent's output was missing or unusable) counts as not passed. A missing or unreadable report,
    a report with duplicate check names, or one with checks outside the inventory raises EvidenceError."""
    if len(set(inventory)) != len(inventory): raise ValueError('Check inventory has duplicate names')
    applicable = [n for n in inventory if n not in set(not_applicable)]
    if not applicable: raise ValueError('Empty check inventory')
    if not path or not Path(path).is_file() or Path(path).stat().st_size == 0:
        raise EvidenceError(f'Verifier report missing: {path}')
    try:
        cases = junit_cases(path)
    except ET.ParseError as exc:
        raise EvidenceError(f'Verifier report unreadable: {exc}') from exc
    names = [c[0] for c in cases]
    if not names: raise EvidenceError('Verifier report contains no checks')
    if len(set(names)) != len(names): raise EvidenceError('Verifier report has duplicate check names')
    unexpected = sorted(set(names) - set(inventory))
    if unexpected: raise EvidenceError(f'Verifier report has checks outside the frozen inventory: {unexpected[:3]}')
    found = {name: (outcome, reason) for name, outcome, _, reason in cases}
    outcomes = {n: found.get(n, ('not_reached', None)) for n in applicable}
    return {'checks_passed': sum(o == 'pass' for o, _ in outcomes.values()), 'checks_total': len(applicable),
            'checks_executed': sum(n in found and found[n][0] in ('pass', 'fail') for n in applicable),
            'outcomes': {n: {'outcome': o, 'skip_reason': r} for n, (o, r) in outcomes.items()}}


def describe(checks, inventory, descriptions):
    """Static descriptions keyed by neutral id, identical for both arms (frozen, AI-written, practice checks only)."""
    ids = neutral_ids(inventory)
    def text(name):  # parametrized checks (name[param]) share their function's description
        return descriptions.get(name) or descriptions.get(name.split('[')[0])
    return {ids[name]: text(name) for name in inventory if text(name)}


def packet_fields(packet):
    """Every string in a feedback packet with its provenance, for audits: verifier-derived fields may only be neutral
    check ids and categories."""
    out = []
    for run in packet['runs']:
        for c in run['failed_checks']: out += [('verifier:check_id', c['check']), ('verifier:category', c['category'])]
        out += [('verifier:check_id', n) for n in run['passed_checks']]
        out += [('agent:summary', run['agent_summary'])] + [('agent:error', e) for e in run['agent_errors']]
        out += [('agent:step', e) for e in run['agent_last_steps']]
    return out


def _clip(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + ' …[cut]'


def trace_excerpt(path, *, summary_chars=1200, step_chars=220, steps=8, errors=3, error_chars=400):
    """The agent's own final message, last error outputs and last commands, from its stream-json transcript."""
    lines = []
    with open(path, encoding='utf-8', errors='replace') as handle:
        for line in handle:
            line = line.strip()
            if line.startswith('{'):
                try: lines.append(json.loads(line))
                except ValueError: pass
    summary, commands, problems = '', [], []
    for item in lines:
        kind = item.get('type')
        content = (item.get('message') or {}).get('content') if isinstance(item.get('message'), dict) else None
        if kind == 'result' and isinstance(item.get('result'), str):
            summary = item['result']
        elif kind == 'assistant' and isinstance(content, list):
            for part in content:
                if part.get('type') == 'tool_use':
                    data = part.get('input') or {}
                    commands.append(_clip(data.get('command') or data.get('file_path') or data, step_chars))
                elif part.get('type') == 'text' and part.get('text'):
                    summary = summary or part['text']
        elif kind == 'user' and isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'tool_result' and part.get('is_error'):
                    problems.append(_clip(part.get('content'), error_chars))
    return {'agent_summary': _clip(summary, summary_chars), 'agent_errors': problems[-errors:],
            'agent_last_steps': commands[-steps:]}


def run_feedback(instance_id, junit_path, trace_path, timed_out, inventory=None):
    if inventory is None: raise ValueError('Neutral ids require the task\'s frozen check inventory')
    failed, passed = checks_from_junit(junit_path, inventory)
    return {'task': instance_id, 'failed_checks': failed, 'passed_checks': passed, 'timed_out': bool(timed_out),
            **trace_excerpt(trace_path)}


def assemble(runs, max_chars=16000):
    """Feedback block for the failed runs, with the size limit split equally between them."""
    runs = [r for r in runs if r['failed_checks'] or r['timed_out']]
    if not runs:
        return None
    share = max_chars // len(runs)
    out = []
    for run in sorted(runs, key=lambda r: r['task']):
        run = json.loads(json.dumps(run))
        while len(json.dumps(run, ensure_ascii=False)) > share:
            if run['agent_last_steps']: run['agent_last_steps'].pop(0)
            elif run['agent_errors']: run['agent_errors'].pop(0)
            elif len(run['agent_summary']) > 200: run['agent_summary'] = run['agent_summary'][:len(run['agent_summary']) // 2] + ' …[cut]'
            elif len(run['failed_checks']) > 1: run['failed_checks'].pop()
            else: break
        out.append(run)
    return {'runs': out, 'note': 'Sanitized execution feedback: checks are given by neutral id (check 1, check 2, ...) and, when failed, an approximate failure category; the verifier\'s check names and message text are not included. Agent fields are the agent\'s own output.'}


def relay_image_rejection(trace_path):
    """S4 amendment (pilot v1, November 12): confirm from the agent's own trace that a run ended because the agent
    itself attached an image (a tool result it requested, e.g. reading a rendered PDF page) and the text-only relay
    refused that request. Every other ending, including any other 400, is not confirmed."""
    items = []
    try:
        with open(trace_path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if line.startswith('{'):
                    try: items.append(json.loads(line))
                    except ValueError: pass
    except OSError:
        return {'confirmed': False, 'reason': 'trace_unreadable'}
    results = [x for x in items if x.get('type') == 'result']
    if not results: return {'confirmed': False, 'reason': 'no_final_result'}
    final = results[-1]
    if not (final.get('is_error') and str(final.get('result', '')).startswith('API Error: 400')):
        return {'confirmed': False, 'reason': 'final_result_is_not_a_400_rejection'}
    conversation = [x for x in items if x.get('type') in ('user', 'assistant')]
    # The final assistant item is the CLI's error text; the request it reports was built from the turns before it.
    while conversation and conversation[-1].get('type') == 'assistant':
        content = (conversation[-1].get('message') or {}).get('content') or []
        if any(isinstance(b, dict) and b.get('type') == 'text' and str(b.get('text', '')).startswith('API Error: 400') for b in content):
            conversation.pop(); break
        return {'confirmed': False, 'reason': 'no_api_error_turn'}
    if not conversation or conversation[-1].get('type') != 'user':
        return {'confirmed': False, 'reason': 'rejected_request_did_not_end_with_a_tool_result'}
    last_user = (conversation[-1].get('message') or {}).get('content') or []
    images = [b for b in last_user if isinstance(b, dict) and b.get('type') == 'tool_result'
              and isinstance(b.get('content'), list) and any(isinstance(c, dict) and c.get('type') == 'image' for c in b['content'])]
    if not images: return {'confirmed': False, 'reason': 'no_image_in_the_rejected_request'}
    requested = {}
    for x in conversation:
        if x.get('type') == 'assistant':
            for b in (x.get('message') or {}).get('content') or []:
                if isinstance(b, dict) and b.get('type') == 'tool_use': requested[b.get('id')] = b.get('name')
    tools = [requested.get(b.get('tool_use_id')) for b in images]
    if not all(tools): return {'confirmed': False, 'reason': 'image_not_from_an_agent_tool_call'}
    return {'confirmed': True, 'reason': 'agent_image_refused_by_text_only_relay', 'tools': tools}
