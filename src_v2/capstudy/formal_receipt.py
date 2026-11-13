"""Immutable local calibration receipts over raw remote evidence and Mac usage.

No dispatch, retry, provider access, registry mutation or raw-record rewriting.
Remote CAS bytes are independently checked by a separately captured verification
receipt; local bulk image blobs are deliberately not implied by a metadata hash.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from .contracts import canonical_hash, sha256_file, validate_trial
from .formal_calibration_gateway import validate_execution_binding
from .formal_verifier import classify_saved_formal
from .gateway_usage import build_attachment
from .runner import (extract_claude_evidence, classify_executor, execution_config_hash,
                     execution_signature, tree_hash)
from .trial_registry import validate_resolved_record


def regular(path):
    p=Path(path)
    if not p.is_absolute() or any(q.is_symlink() for q in [p,*p.parents]) or not p.is_file():
        raise ValueError('Absolute regular nonsymlink evidence required')
    return p


def load_bound(ref):
    if not isinstance(ref,dict) or set(ref)!={'path','sha256'}:raise ValueError('Explicit path/hash binding required')
    p=regular(ref['path'])
    data=p.read_bytes()
    if hashlib.sha256(data).hexdigest()!=ref['sha256']:raise ValueError('Evidence bytes changed')
    return json.loads(data)


def check_seal(value,key):
    if value.get(key)!=canonical_hash({k:v for k,v in value.items() if k!=key}):raise ValueError('Evidence seal differs')


def _usage_core(value):
    # Other completed physical trials may advance the global read-only ledger.
    # Every selected physical call, charge, binding and outcome remains exact.
    omit={'created_at','attachment_sha256','ledger_snapshot_sha256','ledger_event_sequence'}
    return {k:v for k,v in value.items() if k not in omit}


def verify_projection(raw,projection):
    files=projection.get('files');dirs=projection.get('directories')
    if not isinstance(files,list) or not isinstance(dirs,list):raise ValueError('Explicit file and directory projection required')
    mapping={};destinations=set()
    for row in files:
        if set(row)!={'original','local','sha256'} or not Path(row['original']).is_absolute():raise ValueError('Invalid artifact projection')
        p=regular(row['local'])
        if row['original'] in mapping or str(p) in destinations or sha256_file(p)!=row['sha256']:
            raise ValueError('Duplicate or changed projected artifact')
        mapping[row['original']]=row;destinations.add(str(p))
    required={r['path']:r['sha256'] for r in raw['output_artifacts']}
    if len(required)!=len(raw['output_artifacts']) or not required:raise ValueError('Unique raw artifact inventory required')
    required[raw['evaluator_snapshot']['path']]=raw['evaluator_snapshot']['sha256']
    for original,digest in required.items():
        if original not in mapping or mapping[original]['sha256']!=digest:raise ValueError('Raw artifact missing from explicit projection')
    directory_map={}
    for row in dirs:
        if set(row)!={'original','local'} or not Path(row['original']).is_absolute():raise ValueError('Invalid directory projection')
        local=Path(row['local'])
        if not local.is_absolute() or not local.is_dir() or any(p.is_symlink() for p in [local,*local.parents]):raise ValueError('Unsafe projected directory')
        if row['original'] in directory_map:raise ValueError('Duplicate directory projection')
        directory_map[row['original']]=local
        for original,item in mapping.items():
            source=Path(original)
            if source.is_relative_to(row['original']) and local/source.relative_to(row['original'])!=Path(item['local']):
                raise ValueError('File/directory projection disagrees')
    return mapping,directory_map


def verify_cas_receipt(raw,files,remote_ref,policy):
    remote=load_bound(remote_ref);check_seal(remote,'verification_sha256')
    snapshot=raw['evaluator_snapshot'];receipt=json.loads(regular(files[snapshot['path']]['local']).read_text());check_seal(receipt,'receipt_sha256')
    for key in ('trial_id','image_id'):
        expected=raw['trial_id'] if key=='trial_id' else snapshot['image_id']
        if receipt.get(key)!=expected or remote.get(key)!=expected:raise ValueError('CAS physical identity differs')
    if remote.get('image_identity')!=receipt.get('image_identity') or remote.get('kind')!='independent-remote-cas-verification-v1':
        raise ValueError('Independent OCI identity evidence differs')
    owner=receipt.get('ownership')
    if not isinstance(owner,dict) or any(owner.get(k)!=v for k,v in {'kind':'new-formal-snapshot','campaign_id':raw['campaign_id'],'trial_id':raw['trial_id'],'scope':'formal_calibration','evidence_kind':'experiment','protocol_sha256':policy['protocol_sha256']}.items()):
        raise ValueError('CAS ownership differs from physical study identity')
    wanted={'receipt_file_sha256':snapshot['sha256'],'receipt_sha256':receipt['receipt_sha256'],
            'manifest_file_sha256':receipt['manifest_file_sha256'],'archive_sha256':receipt['archive_sha256'],
            'archive_size':receipt['archive_size'],'byte_exact':True,'scratch_cleaned':True,
            'provider_requests':0,'store_path':receipt['store_path']}
    if any(remote.get(k)!=v for k,v in wanted.items()):raise ValueError('Independent remote CAS verification differs')
    implementations=remote.get('implementation_sha256',{})
    for name in ('formal_cas_verify.py','oci_archive_store.py','saved_image_identity.py'):
        if implementations.get(name)!=sha256_file(Path(__file__).with_name(name)):raise ValueError('CAS verification implementation differs')
    observed=datetime.fromisoformat(remote['observed_at'])
    if observed.tzinfo is None or observed>datetime.now(timezone.utc):raise ValueError('Invalid remote verification timestamp')
    return remote


def verify_inputs(spec):
    raw=load_bound(spec['raw_trial']);config=load_bound(spec['trial_config']);binding=load_bound(spec['execution_binding'])
    usage=load_bound(spec['usage_attachment']);validate_trial(raw);validate_resolved_record(raw)
    validate_execution_binding(binding);check_seal(usage,'attachment_sha256')
    if raw.get('reused_from_trial_id') is not None or raw.get('actual_trial_id')!=raw['trial_id']:
        raise ValueError('Unique physical calibration execution required')
    if binding['evidence_kind']!='experiment' or raw.get('evidence_kind','experiment')!='experiment':raise ValueError('Production receipt cannot promote fixture evidence')
    expected=spec['expected_cell'];candidate=spec['candidate']
    if expected['candidate_manifest_sha256']!=canonical_hash(candidate):raise ValueError('Candidate manifest changed')
    if candidate is not None and config.get('source_generation_adoption'):
        from .source_adoption import verify_candidate
        verify_candidate(config,candidate)
    for key in ('instance_id','family','condition','candidate_id'):
        if raw.get(key)!=expected[key] or binding.get(key)!=expected[key]:raise ValueError('Physical calibration cell differs')
    if (raw['trial_id']!=binding['physical_trial_id'] or raw['campaign_id']!=binding['scientific_campaign_id']
            or raw['split']!='train' or raw['repeat_id']!=1 or raw['config_sha256']!=canonical_hash(config)
            or spec['trial_config']['sha256']!=binding['trial_config_file_sha256']
            or execution_config_hash(config)!=binding['execution_config_sha256']
            or raw['actual_bundle_hash']!=binding['candidate_sha256']
            or raw['candidate_hash']!=binding['candidate_sha256']
            or raw['candidate_sha256']!=binding['candidate_sha256']):raise ValueError('Formal identity/config/bundle binding differs')
    base_hash=binding['base_config_sha256']
    # Prefer one byte-identical science config. A physical ID is supplied to the
    # runner as an argument; no unreviewed config projection is accepted here.
    if canonical_hash(config)!=base_hash:raise ValueError('Physical configuration must equal the frozen base configuration')
    files,dirs=verify_projection(raw,spec['projection'])
    task=dirs[raw['task_path']];source=dirs[raw['plan']['task_path']]
    bundle=None if raw['condition']=='no_skill' else dirs[raw['candidate_snapshot_path']]
    if (sha256_file(task/'instruction.md')!=raw['instruction_sha256'] or tree_hash(task/'tests')!=raw['reference_sha256']
            or tree_hash(task/'environment',exclude_skills=True)!=raw['staged_environment_sha256']
            or tree_hash(source/'environment',exclude_skills=True)!=raw['source_environment_sha256']
            or sha256_file(source/'instruction.md')!=raw['plan']['instruction_sha256']
            or (tree_hash(bundle) if bundle else None)!=binding['candidate_sha256']):raise ValueError('Task/source/candidate bytes or modes differ')
    instance=spec['instance']
    if any(instance[k]!=raw[k] for k in ('family','instance_id','split')):raise ValueError('Source instance identity differs')
    scope=load_bound({'path':binding['descriptor_path'],'sha256':binding['descriptor_sha256']})
    train_ref=dict(scope['train_manifest'])
    train_ref['path']=str(Path(binding['descriptor_path']).parent/Path(train_ref['path']))
    train_review=load_bound(train_ref)
    source_ref=spec['source_instance_manifest'];source_manifest=regular(source_ref['path'])
    assigned_ref=dict(train_review['train_jsonl'])
    assigned_ref['path']=str(Path(train_ref['path']).parent/Path(assigned_ref['path']))
    if source_manifest.resolve()!=regular(assigned_ref['path']).resolve() or source_ref['sha256']!=assigned_ref['sha256']:
        raise ValueError('Source manifest differs from approved scope')
    source_manifest=source_manifest.resolve()
    if sha256_file(source_manifest)!=source_ref['sha256'] or instance not in [json.loads(line) for line in source_manifest.read_text().splitlines() if line.strip()]:
        raise ValueError('Exact assigned source instance manifest differs')
    relative=Path(instance['path'])
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe assigned source task path')
    frozen_source=source_manifest.parent.parent/relative
    for name in ('instruction.md','task.toml'):
        if sha256_file(task/name)!=sha256_file(frozen_source/name):
            raise ValueError('Staged instruction/task configuration differs from frozen source')
    frozen_signature=execution_signature(instance,frozen_source,bundle,config,1)
    if (execution_signature(instance,source,bundle,config,1)!=frozen_signature
            or raw['plan'].get('source_execution_signature')!=frozen_signature
            or tree_hash(frozen_source/'tests')!=raw['reference_sha256']):
        raise ValueError('Original source differs from independent frozen local source')
    if raw['condition']=='full_skill':
        original_bundle=Path(instance['skill_bundle_path'])
        if original_bundle.is_absolute() or '..' in original_bundle.parts or tree_hash(source_manifest.parent.parent/original_bundle)!=binding['candidate_sha256']:
            raise ValueError('Full skill differs from independently frozen original bundle')
    snapshot_original=str(Path(raw['verifier_log_path']).parent/'evaluator_snapshot.json')
    if snapshot_original not in files or json.loads(regular(files[snapshot_original]['local']).read_text())!=raw['evaluator_snapshot']:
        raise ValueError('Raw snapshot metadata differs from completed record')
    if execution_signature(instance,task,bundle,config,1)!=raw['execution_signature']:raise ValueError('Frozen execution signature differs')
    if candidate is not None and (candidate.get('eligible') is not True or candidate.get('family')!=raw['family']
            or candidate.get('candidate_id')!=raw['candidate_id'] or candidate.get('strategy_id')!=binding['strategy_id']):
        raise ValueError('Selected eligible candidate identity differs')
    trace=regular(files[raw['agent_trace_path']]['local']);evidence=extract_claude_evidence(trace)
    health=classify_executor(raw['native_result'],evidence,max_turns=config['executor']['max_steps'],launcher_exit=raw['launcher_exit'])
    if health!=raw['executor_health'] or health.get('valid_execution') is not True:raise ValueError('Executor evidence unresolved or changed')
    remote=verify_cas_receipt(raw,files,spec['remote_cas_verification'],config['runtime']['formal_verifier_policy'])
    # A derived temporary view changes only snapshot.path, allowing the existing
    # saved-evidence classifier to operate without rewriting a mirrored artifact.
    with tempfile.TemporaryDirectory(prefix='capstudy-receipt-grade-') as temporary:
        trial=Path(temporary);logs=trial/'verifier';logs.mkdir()
        for original,item in files.items():
            path=Path(original)
            if path.is_relative_to(raw['verifier_log_path']):
                dest=logs/path.relative_to(raw['verifier_log_path']);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(item['local'],dest)
        snapshot=copy.deepcopy(raw['evaluator_snapshot']);snapshot['path']=files[snapshot['path']]['local']
        (trial/'evaluator_snapshot.json').write_text(json.dumps(snapshot))
        grade=classify_saved_formal(task,logs,raw['grader_exit'],config['runtime']['formal_verifier_policy'])
    if grade!=raw['verifier_health'] or grade.get('healthy') is not True or grade['status']!=raw['status']:
        raise ValueError('Native saved grading unresolved or changed')
    load_bound(spec['gateway_attestation'])
    fresh=build_attachment(ledger_path=spec['ledger_path'],trial_path=spec['raw_trial']['path'],
        config_path=spec['trial_config']['path'],attestation_path=spec['gateway_attestation']['path'],
        formal_execution_binding_path=spec['execution_binding']['path'])
    load_bound(spec['gateway_attestation'])
    if _usage_core(fresh)!=_usage_core(usage) or usage.get('accounting_evidence_resolved') is not True:
        raise ValueError('Sealed usage differs from the authoritative central ledger')
    record=copy.deepcopy(raw)
    record.update(evidence_kind='experiment',scope='formal_calibration',usage_settled=True,
        provider_usage_attachment_status='settled_authoritative_central_gateway',
        usage=usage['usage'],usage_source='central_gateway_formal_calibration_attachment',
        cost_usd=float(Decimal(usage['accounted_upper_usd'])),cost_available=True,
        execution_cost_basis=usage['accounting_basis'],actual_invoice_usd=None,
        call_ids=[c['job_id'] for c in usage['physical_calls']],raw_trial_sha256=spec['raw_trial']['sha256'],
        raw_trial_path=spec['raw_trial']['path'],physical_config_sha256=raw['config_sha256'],
        base_config_sha256=base_hash,path_projection=spec['projection'],
        remote_cas_verification_sha256=remote['verification_sha256'],
        usage_attachment_sha256=usage['attachment_sha256'])
    record['output_artifacts']=[dict(r,path=files[r['path']]['local'],original_path=r['path']) for r in raw['output_artifacts']]
    record['artifacts']=copy.deepcopy(record['output_artifacts'])
    for field in ('agent_trace_path','instruction_path'):
        if raw[field] in files:record[field]=files[raw[field]]['local']
    record['verifier_log_path']=str(dirs[raw['verifier_log_path']]);record['task_path']=str(task)
    record['evaluator_snapshot']=copy.deepcopy(raw['evaluator_snapshot']);record['evaluator_snapshot']['path']=files[raw['evaluator_snapshot']['path']]['local']
    validate_resolved_record(record)
    return record


def create_receipt(spec_ref,output):
    spec=load_bound(spec_ref);record=verify_inputs(spec)
    receipt={'schema_version':1,'kind':'formal-calibration-local-receipt-v1','specification':spec_ref,
             'record':record,'record_sha256':canonical_hash(record),'cell_sha256':canonical_hash(spec['expected_cell']),
             'config_sha256':record['base_config_sha256']}
    receipt['receipt_sha256']=canonical_hash(receipt)
    with Path(output).open('x') as f:
        os.chmod(output,0o600);json.dump(receipt,f,sort_keys=True,indent=2,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
    return record


def make_receipt_verifier(receipt_refs):
    """Return the exact callback required by calibration_controller."""
    refs=copy.deepcopy(receipt_refs)
    def verify_trial_receipt(record,expected,config_sha256):
        receipt=load_bound(refs[record['trial_id']]);check_seal(receipt,'receipt_sha256')
        spec=load_bound(receipt['specification']);verified=verify_inputs(spec)
        if (verified!=record or receipt['record']!=record or receipt['record_sha256']!=canonical_hash(record)
                or spec['expected_cell']!=expected or receipt['cell_sha256']!=canonical_hash(expected)
                or receipt['config_sha256']!=config_sha256):raise ValueError('Controller receipt/cell/config changed')
        return {'verified':True,'trial_sha256':canonical_hash(record),'cell_sha256':canonical_hash(expected),
                'config_sha256':config_sha256,'receipt_sha256':receipt['receipt_sha256']}
    return verify_trial_receipt
