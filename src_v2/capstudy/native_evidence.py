"""Prospective native pytest evidence sidecar; original tests/rewards stay intact.

The old classifier and raw reports remain historical evidence. This policy adds
JUnit output to future native invocations and cross-checks its individual cases
against the terminal summary. It never treats a generic setup error as a zero.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from .contracts import canonical_hash, sha256_file

POLICY = 'native-pytest-junit-v1'
REPORT = 'capstudy-junit.xml'
ADDOPTS = '--junitxml=/logs/verifier/' + REPORT
LIMIT = 16 * 1024 * 1024


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('Missing, unsafe or oversized native report')
    with path.open('rb') as handle:
        value = handle.read(LIMIT + 1)
    if len(value) > LIMIT:
        raise ValueError('Native report changed beyond limit')
    return value


def grader_hashes(task):
    root = Path(task) / 'tests'
    files = sorted(p for p in root.rglob('*') if p.is_file()
                   and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo'))
    if not files or any(p.is_symlink() for p in files):
        raise ValueError('Regular immutable native grader files required')
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in files}


def parse_junit(path):
    raw = _read(path)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('XML declarations/entities prohibited')
    root = ET.fromstring(raw)
    suites = list(root) if root.tag == 'testsuites' else [root]
    if len(suites) != 1 or suites[0].tag != 'testsuite':
        raise ValueError('One native pytest suite required')
    suite = suites[0]
    cases = []
    for node in suite.findall('testcase'):
        case_id = (node.get('classname'), node.get('name'))
        if not all(case_id):
            raise ValueError('JUnit case identity missing')
        outcomes = [n for n in node if n.tag in ('failure', 'error', 'skipped')]
        if len(outcomes) > 1:
            raise ValueError('Ambiguous testcase outcome')
        outcome = outcomes[0] if outcomes else None
        cases.append({'id': list(case_id),
            'status': outcome.tag if outcome is not None else 'passed',
            'message': outcome.get('message', '') if outcome is not None else '',
            'trace': outcome.text or '' if outcome is not None else ''})
    if not cases or len({tuple(c['id']) for c in cases}) != len(cases):
        raise ValueError('Empty or duplicated native cases')
    counts = {k: sum(c['status'] == k for c in cases)
              for k in ('passed', 'failure', 'error', 'skipped')}
    expected = {'tests': len(cases), 'failures': counts['failure'],
                'errors': counts['error'], 'skipped': counts['skipped']}
    if any(suite.get(k) != str(v) for k, v in expected.items()):
        raise ValueError('JUnit summary differs from testcase records')
    return cases, counts


def _terminal_counts(logs, task):
    script = (Path(task) / 'tests/test.sh').read_text()
    report = 'process-stdout.txt' if 'ctrf' in script else 'test-stdout.txt'
    text = re.sub(r'\x1b\[[0-9;]*m', '', _read(Path(logs) / report).decode('utf-8'))
    lines = [line for line in text.splitlines()
             if re.search(r'\b\d+ (?:passed|failed|errors?|skipped)\b.*\bin \d', line)]
    if len(lines) != 1:
        raise ValueError('Exactly one completed native pytest summary required')
    counts = {key: int(value) for value, key in re.findall(
        r'\b(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b', lines[0])}
    if any(counts.get(k, 0) for k in ('skipped', 'deselected', 'xfailed', 'xpassed')):
        raise ValueError('Incomplete native test coverage')
    return {'passed': counts.get('passed', 0), 'failure': counts.get('failed', 0),
            'error': counts.get('error', 0) + counts.get('errors', 0), 'skipped': 0}


def classify_junit(task, logs, returncode, *, expected_tests, expected_grader_hashes,
                   absence_evidence=None, missing_contract=None, expected_image_id=None):
    """Fail closed on unknown errors; preserve actual completed-check count.

    expected_grader_hashes and optional missing_contract must come from the
    prospectively frozen, independently reviewed configuration. Absence probes
    are evaluator-owned, before grading, bound to the actual saved image.
    """
    result = {'status': 'infra_error', 'healthy': False, 'native_reward': None,
        'reason': 'native_sidecar_not_verified', 'evidence_format': POLICY,
        'executed_tests': 0, 'setup_error_tests': 0, 'policy': POLICY}
    try:
        if type(expected_tests) is not int or expected_tests <= 0:
            raise ValueError('Explicit calibrated test count required')
        if grader_hashes(task) != expected_grader_hashes:
            raise ValueError('Native grader source binding changed')
        reward = _read(Path(logs) / 'reward.txt').decode().strip()
        if reward not in ('0', '1') or type(returncode) is not int or returncode != 0:
            raise ValueError('Invalid native reward/process exit')
        result['native_reward'] = int(reward)
        cases, counts = parse_junit(Path(logs) / REPORT)
        if len(cases) != expected_tests or counts != _terminal_counts(logs, task) or counts['skipped']:
            raise ValueError('Native testcase/terminal/expected counts disagree')
        result['executed_tests'] = counts['passed'] + counts['failure']
        result['setup_error_tests'] = counts['error']
        if int(reward) != int(counts['failure'] == 0 and counts['error'] == 0):
            raise ValueError('Native reward differs from sidecar outcomes')
        if grader_hashes(task) != expected_grader_hashes:
            raise ValueError('Native grader source changed while reading reports')
        if counts['error']:
            if not _missing_matches(cases, missing_contract, absence_evidence,
                                    expected_grader_hashes, expected_image_id):
                result['reason'] = 'unresolved_native_setup_error'
                return result
            result['reason'] = 'reviewed_missing_required_output'
            result['normalization_contract_sha256'] = canonical_hash(missing_contract)
            result['absence_evidence_sha256'] = canonical_hash(absence_evidence)
        else:
            result['reason'] = 'completed_native_grading'
        result.update(healthy=True, status='pass' if int(reward) else 'task_fail',
            passed_tests=counts['passed'], failed_tests=counts['failure'],
            junit_sha256=sha256_file(Path(logs) / REPORT))
        return result
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, ET.ParseError):
        return result


def _missing_matches(cases, contract, absence, hashes, image_id):
    if not isinstance(contract, dict) or not isinstance(absence, dict):
        return False
    if (contract.get('policy') != POLICY or contract.get('grader_hashes') != hashes
            or contract.get('review_status') != 'approved_source_bound_missing_output'
            or not re.fullmatch(r'[0-9a-f]{64}', contract.get('calibration_evidence_sha256') or '')
            or not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id)
            or absence.get('image_id') != image_id
            or absence.get('capture_phase') != 'before_grading'
            or type(absence.get('probe_exit')) is not int or absence.get('probe_exit') != 0):
        return False
    paths = contract.get('required_absent_paths')
    observations = absence.get('paths')
    if (not isinstance(paths, list) or not paths or len(paths) != len(set(paths))
            or not all(isinstance(p, str) and p.startswith('/') for p in paths)
            or not isinstance(observations, dict)
            or any(observations.get(p) != {'exists': False} for p in paths)):
        return False
    approved = contract.get('error_signatures')
    if not isinstance(approved, list) or not approved:
        return False
    # Each entire diagnostic is calibrated, rather than searching for a benign
    # substring in a trace which could also contain an unrelated runtime error.
    for case in cases:
        if case['status'] == 'error':
            signature = error_signature(case,contract.get('diagnostic_normalization','exact'))
            if signature not in approved:
                return False
    return True


def error_signature(case,normalization='exact'):
    """Only discard process-specific addresses in native pytest object reprs.

    Paths, line numbers, exception text, classes and all substantive values stay
    exact. Other normalization policies require a prospective code change.
    """
    if normalization not in ('exact','python-test-object-address-v1'):
        raise ValueError('Unknown native diagnostic normalization')
    values={key:case[key] for key in ('message','trace')}
    if normalization!='exact':
        values={key:re.sub(r'(<test_outputs\.[A-Za-z_][\w.]* object at )0x[0-9a-fA-F]+(?=>)',
                           r'\1<address>',text) for key,text in values.items()}
    return canonical_hash(values)


def reconcile_record(raw_health, sidecar_health):
    """A separate record, never a rewrite of historical native reports."""
    return {'raw_native_health': copy.deepcopy(raw_health),
            'reviewed_health': copy.deepcopy(sidecar_health),
            'native_tests_or_reward_modified': False}
