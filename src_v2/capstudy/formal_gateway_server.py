"""Explicit, bounded Mac-only server for one admitted formal calibration trial.

Default is read-only validation. No new ledger, credentials in evidence, or
automatic restart/retry. Historical pilot serving remains unchanged.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
from .contracts import canonical_hash, sha256_file, write_json_atomic
from .formal_calibration_gateway import (ACCOUNTING_CAMPAIGN,
    validate_execution_binding, validate_scope, create_formal_gateway,
    formal_server_state, formal_dispatch_storage)
from .deepseek_gateway import OfficialTransport, make_server
from .connection_pilot import read_private_key
from .pilot_gateway_server import read_relay_token, ROOT
from .journal import Journal, money, AdmissionBlocked


def open_existing_journal(path):
    """Open existing central storage without CREATE or metadata repair semantics."""
    path=Path(path)
    if (any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file()
            or path.stat().st_uid!=os.getuid() or path.stat().st_mode&0o077):
        raise ValueError('Existing private owner-controlled journal required')
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=10,isolation_level=None)
    try:
        connection.row_factory=sqlite3.Row
        metadata=dict(connection.execute('SELECT key,value FROM metadata'))
        if (metadata.get('campaign_id')!=ACCOUNTING_CAMPAIGN or metadata.get('evidence_kind')!='experiment'
                or money(metadata.get('budget_usd'))!=5):
            raise ValueError('Existing central accounting identity differs')
        # Missing schema is an error, never an invitation to initialize storage.
        for table in ('jobs','events'):connection.execute('SELECT * FROM '+table+' LIMIT 0')
        if connection.execute('PRAGMA journal_mode').fetchone()[0].lower()!='wal':
            raise ValueError('Existing durable WAL journal required')
        connection.execute('PRAGMA synchronous=FULL')
        journal=Journal.__new__(Journal)
        journal.path=path;journal.connection=connection;journal.budget=money(5)
        return journal
    except BaseException:
        connection.close();raise


def _claim_lifecycle(path,binding):
    """One immutable launch intent survives crashes and concurrent starts."""
    path=Path(path)
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Linked lifecycle paths prohibited')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.exists() or (path.parent/'remote-route-attestation.json').exists():
        raise ValueError('Existing server lifecycle requires explicit reconciliation')
    claim=path.parent/'server-launch-claim.json'
    fd=os.open(claim,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump({'execution_binding_sha256':canonical_hash(binding),'pid':os.getpid(),
            'claimed_at':datetime.now(timezone.utc).isoformat(),'automatic_restart':False},stream,sort_keys=True)
        stream.flush();os.fsync(stream.fileno())
    directory_fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)


def _bounded_connections(server):
    """Bound header parsing too: do_POST runs only AFTER all headers arrive."""
    original=server.get_request
    def get_request():
        connection,address=original()
        connection.settimeout(30)
        return connection,address
    server.get_request=get_request


def inspect_binding(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Regular immutable execution binding required')
    binding=json.loads(path.read_text())
    validate_execution_binding(binding)
    if binding['evidence_kind']!='experiment':raise ValueError('Serving entry point requires experiment admission')
    scope,_=validate_scope(binding['descriptor_path'],binding['base_config_path'])
    ledger=Path(binding['accounting_journal_path'])
    expected=ROOT/'private_artifacts/deepseek-pilot/ledger.sqlite'
    if ledger.resolve()!=expected.resolve() or ledger.is_symlink() or not ledger.is_file():
        raise ValueError('Only the existing central accounting journal may be opened')
    root=Path(scope['dispatch_storage_policy']['root'])/'gateway'/binding['physical_trial_id']
    formal_dispatch_storage(binding,root/'requests')
    return binding,root


def _expected_source_proof(binding):
    """Recompute source identities on the key host; remote hex strings are not proof."""
    from .runner import ROOT as repo, runner_implementation_hash, tree_hash, canonical_hash as runner_hash
    scope,_=validate_scope(binding['descriptor_path'],binding['base_config_path'])
    projection_ref=scope['train_manifest'];base=Path(binding['descriptor_path']).parent
    projection_path=Path(projection_ref['path'])
    if not projection_path.is_absolute():projection_path=base/projection_path
    projection_raw=projection_path.read_bytes()
    import hashlib
    if hashlib.sha256(projection_raw).hexdigest()!=projection_ref['sha256']:
        raise ValueError('Frozen train projection changed')
    projection=json.loads(projection_raw)
    train_path=Path(projection['train_jsonl']['path'])
    if not train_path.is_absolute():train_path=projection_path.parent/train_path
    train_raw=train_path.read_bytes()
    train_hash=hashlib.sha256(train_raw).hexdigest()
    if train_hash!=projection['train_jsonl']['sha256']:raise ValueError('Frozen train source changed')
    rows=[json.loads(line) for line in train_raw.splitlines() if line.strip()]
    selected=[row for row in rows if row['instance_id']==binding['instance_id']]
    if len(selected)!=1:raise ValueError('Unique frozen train instance required')
    row=selected[0]
    config=json.loads(Path(binding['trial_config_path']).read_text())
    if runner_hash(config)!=binding['trial_config_sha256']:raise ValueError('Trial configuration changed')
    data_root=Path(config['data_root'])
    if not data_root.is_absolute():data_root=repo/data_root
    task=(data_root/row['path']).resolve()
    if not task.is_relative_to(data_root.resolve()):raise ValueError('Task path escapes source root')
    implementation=runner_implementation_hash()
    signature=runner_hash({'split':'train','instance_id':row['instance_id'],
        'instruction_sha256':sha256_file(task/'instruction.md'),
        'environment_sha256':tree_hash(task/'environment',exclude_skills=True),
        'task_config_sha256':sha256_file(task/'task.toml'),
        'runner_implementation_sha256':implementation,'bundle_sha256':binding['candidate_sha256'],
        'execution_config_sha256':binding['execution_config_sha256'],'repeat_id':1})
    return {'train_manifest_sha256':train_hash,'runner_implementation_sha256':implementation,
        'source_execution_signature':signature}


def validate_remote_plan(binding,plan):
    required={'physical_trial_id':binding['physical_trial_id'],'instance_id':binding['instance_id'],
        'condition':binding['condition'],'candidate_id':binding['candidate_id'],
        'actual_bundle_hash':binding['candidate_sha256'],'config_sha256':binding['trial_config_sha256'],
        'execution_config_sha256':binding['execution_config_sha256']}
    if any(plan.get(k)!=v for k,v in required.items()):raise ValueError('Remote plan differs from admitted execution binding')
    if plan.get('repeat_id')!=1 or type(plan.get('repeat_id')) is not int:
        raise ValueError('Formal calibration requires repeat 1')
    expected=_expected_source_proof(binding)
    if any(plan.get(k)!=v for k,v in expected.items()):
        raise ValueError('Remote source/runner proof differs from locally recomputed frozen source')
    import re
    keys=['train_manifest_sha256','runner_implementation_sha256','source_execution_signature']
    if binding['condition']=='unchanged_rewrite':keys.append('original_candidate_record_sha256')
    elif plan.get('original_candidate_record_sha256') is not None:raise ValueError('Reference controls cannot bind generated candidate metadata')
    if any(not re.fullmatch(r'[0-9a-f]{64}',plan.get(k) or '') for k in keys):
        raise ValueError('Remote plan lacks immutable source and implementation evidence')
    return {k:plan.get(k) for k in (*keys,'original_candidate_record_sha256')}


def remote_attestation(binding,state,state_path,plan):
    if state.get('listening') is not True or state.get('execution_binding')!=binding:
        raise ValueError('Matching listening gateway state required')
    proof=validate_remote_plan(binding,plan)
    state_raw=Path(state_path).read_bytes()
    if json.loads(state_raw)!=state:raise ValueError('Persisted live gateway state differs')
    expiry=state.get('listener_expires_at',state['expires_at'])
    if datetime.fromisoformat(expiry)<=datetime.now(timezone.utc):raise ValueError('Listener attestation expired')
    import hashlib
    return {'schema_version':1,'kind':'formal_calibration_remote_route',**proof,
        'execution_binding':binding,'execution_binding_sha256':canonical_hash(binding),
        'gateway_state_sha256':hashlib.sha256(state_raw).hexdigest(),
        'gateway_policy_sha256':sha256_file(Path(__file__).with_name('deepseek_gateway.py')),
        'central_ledger_only':True,'gateway_enforces_per_request_budget':True,
        'approved_ceiling_usd':5,'provider_key_location':'mac_only',
        'expires_at':state.get('listener_expires_at',state['expires_at']),'remote_config_sha256':binding['trial_config_sha256'],
        'reviewer':'Howard + Audit Agent','ready':True}


def serve_loop(gateway,state_path,*,port,lifetime_seconds,remote_plan,server_factory=make_server):
    """Close the listener at the earlier caller/registered deadline, even in a request.

    A request already dispatched can finish accounting after listener closure;
    provider transport has a separate finite timeout. No new request is accepted.
    """
    validate_remote_plan(gateway.execution_binding,remote_plan)
    if type(lifetime_seconds) is not int or not 1<=lifetime_seconds<=7200:
        raise ValueError('Listener lifetime must be within registered maximum')
    remaining=(datetime.fromisoformat(gateway.formal_registration['expires_at'])-
               datetime.now(timezone.utc)).total_seconds()
    if remaining<=0:raise ValueError('Registered trial has expired')
    duration=min(remaining,lifetime_seconds)
    state_path=Path(state_path)
    if state_path.exists():raise ValueError('Existing server lifecycle requires reconciliation, not automatic restart')
    _claim_lifecycle(state_path,gateway.execution_binding)
    started=datetime.now(timezone.utc).isoformat()
    state=formal_server_state(gateway,port=port,lifetime_seconds=lifetime_seconds,started_at=started)
    state.update(pid=os.getpid(),actual_listener_limit_seconds=duration,
        listener_expires_at=min(datetime.fromisoformat(gateway.formal_registration['expires_at']),
            datetime.fromisoformat(started)+timedelta(seconds=duration)).isoformat())
    stopping=threading.Event();closed_at=[];server=None;timer=None;old_handlers={}
    lifecycle_lock=threading.RLock()
    original_handle=gateway.handle
    def bounded_handle(*args,**kwargs):
        if stopping.is_set() or datetime.now(timezone.utc)>=datetime.fromisoformat(state['listener_expires_at']):
            raise AdmissionBlocked('Listener lifetime ended before request admission')
        return original_handle(*args,**kwargs)
    gateway.handle=bounded_handle
    def close_listener(*_):
        with lifecycle_lock:
            stopping.set()
            if server is not None:
                server.server_close()
                if not closed_at:closed_at.append(datetime.now(timezone.utc).isoformat())
    try:
        server=server_factory(gateway,('127.0.0.1',port));server.timeout=1
        _bounded_connections(server)
        for signum in (signal.SIGTERM,signal.SIGINT):
            old_handlers[signum]=signal.signal(signum,close_listener)
        remaining=(datetime.fromisoformat(state['listener_expires_at'])-datetime.now(timezone.utc)).total_seconds()
        if remaining<=0:raise ValueError('Listener deadline expired during startup')
        timer=threading.Timer(remaining,close_listener);timer.daemon=True;timer.start()
        with lifecycle_lock:
            if stopping.is_set():raise ValueError('Listener closed before attestation')
            write_json_atomic(state_path,state)
            write_json_atomic(state_path.parent/'remote-route-attestation.json',
                remote_attestation(gateway.execution_binding,state,state_path,remote_plan))
        print(json.dumps({'listening':True,'trial_id':gateway.trial_id,'port':port}),flush=True)
        while not stopping.is_set():
            try:server.handle_request()
            except (OSError,ValueError):
                if not stopping.is_set():raise
    finally:
        close_listener()
        gateway.handle=original_handle
        if timer is not None:timer.cancel()
        for signum,handler in old_handlers.items():signal.signal(signum,handler)
        write_json_atomic(state_path,{**state,'listening':False,
            'listener_closed_at':closed_at[0] if closed_at else None,
            'stopped_at':datetime.now(timezone.utc).isoformat()})


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binding',type=Path,required=True)
    p.add_argument('--token-file',type=Path)
    p.add_argument('--remote-plan',type=Path)
    p.add_argument('--port',type=int,default=18081)
    p.add_argument('--lifetime-seconds',type=int,default=7200)
    p.add_argument('--serve',action='store_true')
    a=p.parse_args(argv)
    if not 1024<=a.port<=65535 or not 1<=a.lifetime_seconds<=7200:
        raise ValueError('Bounded nonprivileged loopback settings required')
    binding,root=inspect_binding(a.binding)
    if not a.serve:
        print(json.dumps({'listening':False,'model_calls':0,
            'binding_sha256':canonical_hash(binding),'trial_id':binding['physical_trial_id']}));return 0
    if a.token_file is None or a.remote_plan is None:p.error('--serve requires --token-file and --remote-plan')
    if a.remote_plan.is_symlink() or not a.remote_plan.is_file():raise ValueError('Regular transferred remote plan required')
    remote_plan=json.loads(a.remote_plan.read_text());validate_remote_plan(binding,remote_plan)
    if any((root/name).exists() or (root/name).is_symlink() for name in
           ('server.json','server-launch-claim.json','remote-route-attestation.json')):
        raise ValueError('Existing lifecycle requires reconciliation')
    # Nothing secret is read before all read-only scope/config/file checks pass.
    token=read_relay_token(a.token_file)
    key=read_private_key(ROOT/'private_artifacts/secrets/deepseek.env')
    journal=open_existing_journal(binding['accounting_journal_path'])
    try:
        gateway=create_formal_gateway(journal=journal,artifact_root=root/'requests',
            transport=OfficialTransport(key,timeout_seconds=300),relay_token=token,execution_binding=binding)
        serve_loop(gateway,root/'server.json',port=a.port,lifetime_seconds=a.lifetime_seconds,remote_plan=remote_plan)
    finally:journal.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
