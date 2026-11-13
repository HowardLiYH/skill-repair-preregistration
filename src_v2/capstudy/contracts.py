"""Versioned evidence contracts. Fixture records are never experimental evidence."""
from __future__ import annotations
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

CONDITIONS = ('feedback_repair', 'retry_no_feedback', 'unchanged_rewrite', 'head_truncation', 'sentence_extraction', 'llmlingua2', 'full_skill', 'no_skill', 'full_skill_fallback')
STATUSES = ('pass', 'task_fail', 'infeasible', 'infra_error')
CANDIDATE_FIELDS = 'schema_version campaign_id candidate_id family method strategy_id parent_candidate_id initial_candidate_id attempt_index source_bundle_sha256 bundle_path instruction_sha256 support_sha256 file_sha256 original_tokens candidate_tokens token_cap tokenizer_id tokenizer_revision eligible ineligibility_reasons prompt_sha256 response_path call_ids config_sha256 created_at'.split()
TRIAL_FIELDS = 'schema_version campaign_id split family instance_id condition candidate_id candidate_hash repeat_id trial_id git_commit config_sha256 source_environment_sha256 staged_environment_sha256 model_id cli_version image_digest architecture effective_tools effective_limits status native_reward grader_exit agent_exit agent_timed_out diagnostic_category instruction_path instruction_sha256 agent_trace_path verifier_log_path output_artifacts usage usage_source duration_seconds call_ids cost_available selection_manifest_id actual_bundle_hash fallback_reason'.split()
CALL_FIELDS = 'schema_version call_id request_id campaign_id phase family condition model_id started_at completed_at usage currency pricing_version known_charge unavailable_fields'.split()

def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]

def write_json_atomic(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True, allow_nan=False); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def _base(record, fields):
    missing = set(fields) - record.keys()
    if missing: raise ValueError('missing required fields: ' + ', '.join(sorted(missing)))
    if record['schema_version'] != 1: raise ValueError('unsupported schema_version')
    if record.get('evidence_kind', 'experiment') not in ('fixture', 'experiment'): raise ValueError('invalid evidence_kind')

def _nonnegative(value, name, nullable=False):
    if nullable and value is None: return
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0: raise ValueError('invalid ' + name)

def validate_candidate(record):
    _base(record, CANDIDATE_FIELDS)
    if type(record['eligible']) is not bool: raise ValueError('eligible must be boolean')
    if not isinstance(record['ineligibility_reasons'], list): raise ValueError('reasons must be list')
    for key in ('original_tokens','candidate_tokens','token_cap'): _nonnegative(record[key], key, nullable=True)
    if record['eligible']:
        if record['ineligibility_reasons']: raise ValueError('eligible candidate has rejection reasons')
        for key in ('bundle_path','instruction_sha256','source_bundle_sha256','support_sha256','candidate_tokens','token_cap','original_tokens','tokenizer_id'):
            if record[key] is None: raise ValueError('eligible candidate missing ' + key)
        if record['candidate_tokens'] > record['token_cap']: raise ValueError('over-cap eligible candidate')
    elif not record['ineligibility_reasons']: raise ValueError('ineligible candidate needs reason')
    return record

def validate_trial(record):
    _base(record, TRIAL_FIELDS)
    if record['condition'] not in CONDITIONS or record['status'] not in STATUSES: raise ValueError('unknown condition/status')
    if record['split'] not in ('train','dev','test'): raise ValueError('invalid split')
    if not isinstance(record['repeat_id'], int) or isinstance(record['repeat_id'], bool) or record['repeat_id'] < 1: raise ValueError('invalid repeat_id')
    if record['status'] in ('pass','task_fail'):
        if record['native_reward'] != int(record['status']=='pass'): raise ValueError('reward/status mismatch')
        if record['grader_exit'] != 0 or not record['verifier_log_path']: raise ValueError('resolved trial needs grading evidence')
    if record['status']=='infeasible' and record['native_reward'] is not None: raise ValueError('infeasible is not native execution')
    if record['condition']=='no_skill' and any(record[k] is not None for k in ('candidate_id','candidate_hash','actual_bundle_hash')): raise ValueError('no-skill has no bundle')
    _nonnegative(record['duration_seconds'], 'duration_seconds', nullable=True)
    if type(record['agent_timed_out']) is not bool or type(record['cost_available']) is not bool: raise ValueError('invalid diagnostic boolean')
    return record

def validate_call(record):
    _base(record, CALL_FIELDS)
    if record['phase'] not in ('calibration','compression','dev_execution','repair_edit','retry_edit','test_execution'): raise ValueError('unknown phase')
    if not record['call_id']: raise ValueError('empty call id')
    _nonnegative(record['known_charge'], 'known_charge', nullable=True)
    if record['known_charge'] is None and not record['unavailable_fields']: raise ValueError('unavailable cost needs reason')
    return record
