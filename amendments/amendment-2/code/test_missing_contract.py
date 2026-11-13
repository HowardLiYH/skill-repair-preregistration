"""Amendment 2: missing-output records, both directions (offline, synthetic reports; the server checks the same on
each task's real model-free control)."""
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from capstudy.native_evidence import parse_junit, error_signature, _missing_matches, POLICY

IMAGE = 'sha256:' + 'a' * 64
HASHES = {'test.sh': 'b' * 64, 'test_outputs.py': 'c' * 64}
SETUP = 'FileNotFoundError: [Errno 2] No such file or directory: /root/sc100-filled.pdf'


def report(tmp, message=SETUP, extra_failure=False):
    cases = [f'<testcase classname="test_outputs.TestPDF" name="test_{i}"><error message="{message}">trace {i}</error></testcase>' for i in range(3)]
    if extra_failure: cases.append('<testcase classname="test_outputs.TestPDF" name="test_x"><failure message="wrong">t</failure></testcase>')
    n_err, n_fail = 3, int(extra_failure)
    path = tmp / 'capstudy-junit.xml'
    path.write_text(f'<testsuites><testsuite tests="{n_err + n_fail}" failures="{n_fail}" errors="{n_err}" skipped="0">' + ''.join(cases) + '</testsuite></testsuites>')
    return parse_junit(path)


def contract(cases):
    signatures = sorted({error_signature(c, 'python-test-object-address-v1') for c in cases if c['status'] == 'error'})
    return {'policy': POLICY, 'grader_hashes': HASHES, 'review_status': 'approved_source_bound_missing_output',
            'calibration_evidence_sha256': 'd' * 64, 'required_absent_paths': ['/root/sc100-filled.pdf'],
            'error_signatures': signatures, 'diagnostic_normalization': 'python-test-object-address-v1'}


def absence(exists=False):
    return {'image_id': IMAGE, 'capture_phase': 'before_grading', 'probe_exit': 0, 'paths': {'/root/sc100-filled.pdf': {'exists': exists}}}


def test_verified_missing_output_matches_its_record(tmp_path):
    cases, _ = report(tmp_path)
    assert _missing_matches(cases, contract(cases), absence(), HASHES, IMAGE)


def test_unrelated_setup_error_stays_infrastructure(tmp_path):
    control, _ = report(tmp_path)
    other_dir = tmp_path / 'o'; other_dir.mkdir()
    cases, _ = report(other_dir, message='ModuleNotFoundError: No module named pypdf')
    assert not _missing_matches(cases, contract(control), absence(), HASHES, IMAGE)


def test_output_present_or_other_image_or_changed_grader_stays_infrastructure(tmp_path):
    cases, _ = report(tmp_path); rec = contract(cases)
    assert not _missing_matches(cases, rec, absence(exists=True), HASHES, IMAGE)
    assert not _missing_matches(cases, rec, absence(), HASHES, 'sha256:' + 'e' * 64)
    assert not _missing_matches(cases, rec, absence(), dict(HASHES, **{'test.sh': 'f' * 64}), IMAGE)
    assert not _missing_matches(cases, None, absence(), HASHES, IMAGE)                 # no record at all


def test_corrupt_or_missing_report_is_refused(tmp_path):
    bad = tmp_path / 'capstudy-junit.xml'; bad.write_text('<testsuites><testsuite tests="1"')
    with pytest.raises(Exception):
        parse_junit(bad)
    with pytest.raises(ValueError):
        parse_junit(tmp_path / 'absent.xml')
    lying = tmp_path / 'l'; lying.mkdir()
    (lying / 'capstudy-junit.xml').write_text('<testsuites><testsuite tests="5" failures="0" errors="0" skipped="0">'
                                              '<testcase classname="a" name="b"/></testsuite></testsuites>')
    with pytest.raises(ValueError, match='summary differs'):
        parse_junit(lying / 'capstudy-junit.xml')
