"""Read-only central-gateway usage attachment for the scoped GDP pilot.

No model calls, secret discovery, journal mutation, or raw-trial rewriting.
The seal detects edits; the explicit reviewed binding is operator attestation,
not a cryptographic proof of the remote process's network identity.
"""
from __future__ import annotations
import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from .contracts import canonical_hash, sha256_file, validate_trial
from .deepseek_gateway import MODEL, UpstreamResponse, prepare_request, response_accounting
from .runner import canonical_hash as runner_hash, execution_config_hash


def _load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Evidence must be an existing regular file')
    raw = path.read_bytes()
    import hashlib
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def build_attachment(*, ledger_path, trial_path, config_path, attestation_path,
                     formal_execution_binding_path=None):
    trial, trial_sha = _load(trial_path)
    config, config_sha = _load(config_path)
    attestation, attestation_sha = _load(attestation_path)
    validate_trial(trial)
    kind = trial.get('evidence_kind', 'experiment')
    formal=None;formal_sha=None
    if formal_execution_binding_path is not None:
        from .formal_calibration_gateway import validate_execution_binding,ACCOUNTING_CAMPAIGN,PHASE
        formal,formal_sha=_load(formal_execution_binding_path)
        validate_execution_binding(formal)
        resolved=(trial.get('status') in ('pass','task_fail') and trial.get('job_status')=='completed'
                  and trial.get('executed') is True)
        failed_attempt=(trial.get('status')=='infra_error' and trial.get('job_status')=='infra_error'
                        and type(trial.get('executed')) is bool)
        if (not (resolved or failed_attempt)
            or any(trial.get(key,kind if key=='evidence_kind' else None)!=formal[field] for key,field in (
                ('campaign_id','scientific_campaign_id'),('trial_id','physical_trial_id'),
                ('instance_id','instance_id'),('family','family'),('split','split'),('condition','condition'),
                ('candidate_id','candidate_id'),('candidate_hash','candidate_sha256'),
                ('actual_bundle_hash','candidate_sha256'),('evidence_kind','evidence_kind')))
            or trial_sha is None or config_sha!=formal['trial_config_file_sha256']):
            raise ValueError('Formal physical trial/cell/candidate/config binding differs')
    elif (trial.get('executed') is not True or trial.get('job_status') != 'completed'
            or trial['status'] not in ('pass', 'task_fail')
            or trial['family'] != 'weighted-gdp-calculation' or trial['split'] != 'train'
            or trial['condition'] not in ('full_skill', 'head_truncation')):
        raise ValueError('Only completed full/head GDP calibration pilot trials are supported')
    if (config.get('campaign_id') != trial['campaign_id']
            or (formal is None and config.get('pilot_trial_id') != trial['trial_id'])
            or config.get('executor', {}).get('provider') != 'deepseek'
            or config['executor'].get('model_id') != MODEL or trial['model_id'] != MODEL
            or trial['config_sha256'] != runner_hash(config)
            or trial.get('execution_config_sha256') != execution_config_hash(config)):
        raise ValueError('Trial/config/model binding mismatch')
    expected = {'schema_version': 1, 'campaign_id': trial['campaign_id'],
        'trial_id': trial['trial_id'], 'evidence_kind': kind,
        'trial_sha256': trial_sha, 'config_file_sha256': config_sha,
        'config_sha256': trial['config_sha256'],
        'execution_config_sha256': trial['execution_config_sha256'],
        'exclusive_trial_relay_confirmed': True}
    if formal is not None:
        expected.update(formal_execution_binding_sha256=formal_sha,
            scientific_campaign_id=formal['scientific_campaign_id'],accounting_campaign_id=ACCOUNTING_CAMPAIGN,
            phase=PHASE,base_config_sha256=formal['base_config_sha256'])
    if any(attestation.get(k) != v for k, v in expected.items()):
        raise ValueError('Explicit reviewed trial-to-gateway binding required')
    if any(not isinstance(attestation.get(k), str) or not attestation[k].strip()
           for k in ('reviewer', 'reviewed_at', 'reason')):
        raise ValueError('Named, dated review and reason required')
    server_path = attestation.get('gateway_server_state_path')
    if not server_path:
        raise ValueError('Stopped gateway state evidence required')
    server, server_sha = _load(server_path)
    if (server_sha != attestation.get('gateway_server_state_sha256')
            or server.get('trial_id') != trial['trial_id']
            or server.get('listening') is not False or not server.get('stopped_at')
            or server.get('bind') != '127.0.0.1' or server.get('budget_usd') != 5):
        raise ValueError('Gateway must be stopped and bound to this trial')
    if formal is not None and (server.get('execution_binding')!=formal
        or server.get('accounting_campaign_id')!=ACCOUNTING_CAMPAIGN
        or server.get('provider_key_location')!='mac_only' or type(server.get('lifetime_seconds')) is not int
        or not 1<=server['lifetime_seconds']<=7200):
        raise ValueError('Formal stopped gateway state lacks the exact approved execution binding')
    mapping = config.get('runtime', {}).get('deepseek_mapping', {})
    if (mapping.get('route') != 'private-deepseek-anthropic-relay'
            or mapping.get('model_id') != MODEL
            or not isinstance(mapping.get('allowed_tools'), list)
            or type(mapping.get('max_output_tokens')) is not int
            or not 0 < mapping['max_output_tokens'] <= 16384
            or attestation.get('mapping_sha256') != canonical_hash(mapping)
            or attestation.get('relay_url_sha256') != mapping.get('relay_url_sha256')
            or not mapping.get('relay_url_sha256')):
        raise ValueError('Reviewed runtime/relay mapping differs')
    ledger_path = Path(ledger_path)
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise ValueError('Existing regular ledger required')
    if formal is not None and str(ledger_path.resolve())!=formal['accounting_journal_path']:
        raise ValueError('Formal usage must use the exact existing central accounting ledger path')
    # Use URI read-only mode, not Journal (whose constructor writes metadata).
    with closing(sqlite3.connect(ledger_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        metadata = dict(conn.execute('SELECT key,value FROM metadata'))
        jobs = [dict(r) for r in conn.execute('SELECT * FROM jobs ORDER BY job_id')]
        events = [dict(r) for r in conn.execute('SELECT * FROM events ORDER BY sequence')]
    expected_accounting_campaign=trial['campaign_id'] if formal is None else ACCOUNTING_CAMPAIGN
    if (metadata.get('campaign_id') != expected_accounting_campaign
            or metadata.get('evidence_kind') != kind or Decimal(metadata['budget_usd']) != 5):
        raise ValueError('Ledger campaign, evidence kind or pilot budget differs')
    if any(j['state'] not in ('completed', 'not_dispatched') for j in jobs):
        raise ValueError('Campaign has active or unresolved jobs; stop and reconcile first')
    formal_registration=None
    if formal is not None:
        key='formal_calibration_physical:'+canonical_hash({'trial_id':formal['physical_trial_id']})
        formal_registration=json.loads(metadata.get(key,'null'))
        if (not isinstance(formal_registration,dict) or formal_registration.get('binding')!=formal
            or formal_registration.get('relay_token_sha256')!=server.get('relay_token_sha256')
            or any(formal_registration.get(key)!=server.get(key) for key in ('registered_at','expires_at'))):
            raise ValueError('Central physical-trial/token registration differs from formal server evidence')
    scheduled = {}
    for event in events:
        if event['event'] == 'scheduled':
            if event['job_id'] in scheduled:
                raise ValueError('Duplicate schedule evidence')
            scheduled[event['job_id']] = json.loads(event['payload'])['identity']
    selected, undispatched, provider_ids = [], [], set()
    totals = {k: 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')}
    total_charge = Decimal(0)
    for job in jobs:
        identity = scheduled.get(job['job_id'])
        if identity is None or canonical_hash(identity) != job['identity']:
            raise ValueError('Journal schedule identity is missing or changed')
        expected_phase='executor_pilot_gateway' if formal is None else PHASE
        if identity.get('phase') != expected_phase or identity.get('trial_id') != trial['trial_id']:
            continue
        if formal is not None and identity.get('execution_binding')!=formal:
            raise ValueError('Formal gateway job was bound to a different scientific/physical cell')
        if job['state'] == 'not_dispatched':
            if job['charge'] not in (None, '0') or job['response_path']:
                raise ValueError('Undispatched job contains contradictory charge/response')
            undispatched.append(job['job_id'])
            continue
        if job['state'] != 'completed':
            raise ValueError('This trial has lost response evidence')
        response, response_sha = _load(job['response_path'])
        request_path = Path(job['response_path']).parent / 'request.json'
        if formal is not None and str(request_path.parent.parent.resolve())!=formal_registration['artifact_root']:
            raise ValueError('Formal response is outside its registered immutable artifact root')
        request, request_sha = _load(request_path)
        normalized, _ = prepare_request(json.dumps(request).encode())
        if (request['max_tokens'] > mapping['max_output_tokens']
                or not {t['name'] for t in request.get('tools', [])} <= set(mapping['allowed_tools'])):
            raise ValueError('Provider request exceeds trial tool/output policy')
        request_hash = canonical_hash(request)
        if (normalized != request or identity.get('request_sha256') != request_hash
                or job['request_sha256'] != request_hash or response.get('request_sha256') != request_hash
                or response.get('job_id') != job['job_id'] or response.get('trial_id') != trial['trial_id']
                or response_sha != job['response_sha256']):
            raise ValueError('Gateway request/response identity or hash differs')
        if formal is not None and response.get('execution_binding')!=formal:
            raise ValueError('Formal response execution binding differs')
        observation_refs=[]
        observation_policy=config.get('runtime',{}).get('tool_observation')
        if observation_policy is not None:
            from .tool_observation import verify as verify_observation
            if identity.get('tool_observation_policy')!=observation_policy:
                raise ValueError('Accounting identity observation policy differs')
            first=response.get('tool_observation')
            relevant=[e for e in events if e['job_id']==job['job_id']]
            observed=[json.loads(e['payload']) for e in relevant if e['event']=='gateway_tool_observation']
            if observed != [first]:
                raise ValueError('Authoritative observation event/response differs')
            refs=[first]+[json.loads(e['payload']).get('tool_observation') for e in relevant if e['event']=='gateway_response_reused']
            for reference in refs:
                verify_observation(reference,root=request_path.parent.parent/'observations',policy=observation_policy,
                                   provider_body=request,allowed_tools=mapping['allowed_tools'])
                observation_refs.append(reference)
        elif response.get('tool_observation') is not None or identity.get('tool_observation_policy') is not None:
            raise ValueError('Unconfigured observation adapter evidence')
        charge, provider_id, usage = response_accounting(
            UpstreamResponse(response['status'], response['headers'],
                base64.b64decode(response['body_base64'], validate=True), response['truncated']),
            streaming=request.get('stream', False), output_cap=request['max_tokens'])
        if (charge != Decimal(job['charge']) or charge != Decimal(response['accounted_upper_charge_usd'])
                or response.get('usage') != usage or response.get('actual_invoice_charge_usd') is not None
                or not provider_id or provider_id != job['provider_request_id']):
            raise ValueError('Provider usage/accounting/request ID differs')
        if provider_id in provider_ids:
            raise ValueError('Duplicate provider ID across physical jobs')
        provider_ids.add(provider_id)
        for key in totals:
            totals[key] += usage.get(key, 0)
        total_charge += charge
        selected.append({'job_id': job['job_id'], 'provider_request_id': provider_id,
            'request_sha256': request_hash, 'request_file_sha256': request_sha,
            'response_sha256': response_sha, 'usage': usage,
            'accounted_upper_usd': str(charge)})
        if observation_policy is not None:
            selected[-1]['tool_observation_references']=observation_refs
    if not selected:
        raise ValueError('No completed physical gateway calls for this trial')
    result = {'schema_version': 1, 'attachment_type': 'central_gateway_pilot_usage',
        'evidence_kind': kind, 'created_at': datetime.now(timezone.utc).isoformat(),
        'campaign_id': trial['campaign_id'], 'trial_id': trial['trial_id'],
        'trial_sha256': trial_sha, 'config_file_sha256': config_sha,
        'config_sha256': trial['config_sha256'], 'execution_config_sha256': trial['execution_config_sha256'],
        'attestation_sha256': attestation_sha, 'gateway_server_state_sha256': server_sha,
        'mapping_sha256': canonical_hash(mapping), 'binding_basis': 'explicit_reviewed_operator_attestation',
        'ledger_snapshot_sha256': canonical_hash({'metadata': metadata, 'jobs': jobs, 'events': events}),
        'ledger_event_sequence': max((e['sequence'] for e in events), default=0),
        'physical_calls': selected, 'physical_call_count': len(selected),
        'confirmed_undispatched_job_ids': undispatched,
        'replay_events_excluded': sum(e['event'] == 'gateway_response_reused' and e['job_id'] in {j['job_id'] for j in selected} for e in events),
        'usage': totals, 'accounted_upper_usd': str(total_charge), 'actual_invoice_usd': None,
        'accounting_basis': 'observed_usage_at_published_peak_tariff_upper_bound',
        'scientific_study_complete': False}
    if formal is not None:
        result.update(attachment_type='central_gateway_formal_calibration_usage',
            scientific_campaign_id=formal['scientific_campaign_id'],accounting_campaign_id=ACCOUNTING_CAMPAIGN,
            phase=PHASE,formal_execution_binding_sha256=formal_sha,
            base_config_sha256=formal['base_config_sha256'],execution_binding=formal,
            raw_trial_outcome={key:trial.get(key) for key in ('status','job_status','executed','launcher_started','diagnostic_category')},
            scientific_result_resolved=trial['status'] in ('pass','task_fail'),
            native_result_uncertainty=('unresolved_infrastructure' if trial['status']=='infra_error' else None),
            accounting_evidence_resolved=True)
    result['attachment_sha256'] = canonical_hash(result)
    return result


def save_attachment(path, attachment):
    if attachment.get('attachment_sha256') != canonical_hash({k:v for k,v in attachment.items() if k != 'attachment_sha256'}):
        raise ValueError('Attachment seal mismatch')
    # Exclusive creation prevents overwriting either the raw trial or prior seal.
    with Path(path).open('x') as f:
        os.chmod(path, 0o600)
        json.dump(attachment, f, indent=2, sort_keys=True, allow_nan=False)
        f.write('\n'); f.flush(); os.fsync(f.fileno())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('ledger', 'trial', 'config', 'attestation', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args(argv)
    result = build_attachment(ledger_path=a.ledger, trial_path=a.trial,
        config_path=a.config, attestation_path=a.attestation)
    save_attachment(a.output, result)
    print(json.dumps({'trial_id': result['trial_id'], 'physical_call_count': result['physical_call_count'],
        'accounted_upper_usd': result['accounted_upper_usd'], 'output': str(a.output)}))


if __name__ == '__main__':
    main()
