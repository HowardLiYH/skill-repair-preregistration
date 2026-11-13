"""Durable logical receipts over independently verified physical trials.

No runner, network, model, retry, or budget dispatch. A claim is not spending
permission. Integration must stage/hash inputs and obtain budget admission first.
"""
from __future__ import annotations
import copy
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import uuid
from .contracts import canonical_hash, sha256_file, CONDITIONS


class ReconciliationRequired(RuntimeError):
    pass


_BINDING = ('campaign_id', 'protocol_sha256', 'evidence_kind', 'scope', 'split',
            'family', 'instance_id', 'condition', 'candidate_id', 'candidate_sha256',
            'repeat_id', 'logical_slot', 'execution_signature', 'reference_sha256',
            'execution_config_sha256', 'config_sha256', 'reuse_conditions')
_SHARED = {'unchanged_rewrite', 'feedback_repair', 'retry_no_feedback'}


def _binding(value):
    b = copy.deepcopy(value)
    if set(b) != set(_BINDING):
        raise ValueError('Exact logical binding fields required')
    for key in set(_BINDING) - {'candidate_id', 'candidate_sha256', 'repeat_id', 'reuse_conditions'}:
        if not isinstance(b[key], str) or not b[key]:
            raise ValueError('Empty binding: ' + key)
    if b['evidence_kind'] not in ('experiment', 'fixture') or b['split'] not in ('train', 'dev', 'test'):
        raise ValueError('Invalid evidence kind or split')
    if b['scope'] not in ('formal_calibration', 'formal_dev', 'formal_test') and not b['scope'].startswith('pilot:'):
        raise ValueError('Explicit formal phase or named pilot scope required')
    if b['condition'] not in CONDITIONS or type(b['repeat_id']) is not int or b['repeat_id'] < 1:
        raise ValueError('Invalid condition/repeat')
    if b['scope'].startswith('formal_') and b['split'] != {'formal_calibration':'train','formal_dev':'dev','formal_test':'test'}[b['scope']]:
        raise ValueError('Formal scope/split mismatch')
    if b['condition'] in ('full_skill', 'no_skill'):
        if b['candidate_id'] is not None:
            raise ValueError('Reference does not have a candidate ID')
        if b['condition'] == 'no_skill' and b['candidate_sha256'] is not None:
            raise ValueError('No-skill does not have a bundle')
    elif not isinstance(b['candidate_id'], str) or not b['candidate_id']:
        raise ValueError('Candidate identity required')
    if b['condition'] != 'no_skill' and (not isinstance(b['candidate_sha256'], str) or not b['candidate_sha256']):
        raise ValueError('Selected bundle hash required')
    allowed = b['reuse_conditions']
    if (not isinstance(allowed, list) or not allowed or any(not isinstance(x,str) for x in allowed)
            or allowed != sorted(set(allowed)) or b['condition'] not in allowed):
        raise ValueError('Sorted unique explicit reuse conditions required')
    if len(allowed) > 1 and (not set(allowed) <= _SHARED or not b['candidate_id']):
        raise ValueError('Only protocol-bound common initial candidate conditions can share')
    return b


def logical_id(binding):
    return 'logical-' + canonical_hash(_binding(binding))


def _compatible(a, b):
    # All provenance/config/candidate/repeat fields remain equal. Condition labels
    # can differ only within the exact same prospectively bound allowed mapping.
    return ({k:v for k,v in a.items() if k not in ('condition','logical_slot')} ==
            {k:v for k,v in b.items() if k not in ('condition','logical_slot')}
            and b['condition'] in a['reuse_conditions'])


def _encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_resolved_record(record):
    if (not isinstance(record.get('trial_id'), str) or not record['trial_id']
            or record.get('job_status') != 'completed' or record.get('executed') is not True
            or record.get('status') not in ('pass', 'task_fail')
            or record.get('executor_health', {}).get('valid_execution') is not True
            or record.get('verifier_health', {}).get('healthy') is not True
            or record.get('verifier_health', {}).get('status') != record.get('status')
            or record.get('verifier_health', {}).get('native_reward') != record.get('native_reward')
            or record.get('grader_exit') != 0
            or record.get('native_reward') != int(record['status'] == 'pass')):
        raise ValueError('Resolved trial requires identity and healthy executor/native evidence')


class TrialRegistry:
    def __init__(self, path, *, verify_evidence):
        if not callable(verify_evidence):
            raise ValueError('Independent physical evidence verifier required')
        self.path, self.verify_evidence = Path(path), verify_evidence
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('Registry cannot be a symlink')
        with self._connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS registry_metadata(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cells(id TEXT PRIMARY KEY,binding TEXT NOT NULL,
                    token TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT);
                CREATE TABLE IF NOT EXISTS physical(id TEXT PRIMARY KEY,owner TEXT UNIQUE NOT NULL,
                    binding TEXT NOT NULL,record TEXT NOT NULL,usage TEXT NOT NULL,seal TEXT NOT NULL);
            ''')
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute('SELECT version FROM registry_metadata').fetchall()
            if not rows:c.execute('INSERT INTO registry_metadata VALUES(1)')
            elif rows != [(1,)]:raise ValueError('Unsupported registry version')
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        if self.path.is_symlink():raise ValueError('Registry cannot be a symlink')
        c=sqlite3.connect(self.path, timeout=5)
        c.execute('PRAGMA synchronous=FULL')
        try:
            with c:yield c
        finally:c.close()

    def claim_cell(self, binding):
        b=_binding(binding); key=logical_id(b)
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT binding,token,state,receipt FROM cells WHERE id=?',(key,)).fetchone()
            if row:
                if row[0] != _encode(b):raise ValueError('Logical binding corrupted')
                if row[2]=='completed':return {'state':'completed','receipt':self._verify_receipt(c,json.loads(row[3]),b)}
                physical=c.execute('SELECT id FROM physical WHERE owner=?',(key,)).fetchone()
                if physical:
                    return {'state':'physical_completed','logical_trial_id':key,'physical_trial_id':physical[0]}
                raise ReconciliationRequired('Existing claim may have dispatched; reconcile before retry')
            token=uuid.uuid4().hex
            c.execute('INSERT INTO cells VALUES(?,?,?,?,NULL)',(key,_encode(b),token,'claimed'))
            return {'state':'claimed','logical_trial_id':key,'claim_token':token}

    def begin_execution(self, binding, claim_token):
        """Persist before any external launch; this grants no budget permission."""
        b=_binding(binding);key=logical_id(b)
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute('UPDATE cells SET state=? WHERE id=? AND binding=? AND token=? AND state=?',
                ('running',key,_encode(b),claim_token,'claimed')).rowcount
            if changed!=1:raise ReconciliationRequired('Execution was already started or claim differs')

    def _verify_physical(self, b, record, usage):
        validate_resolved_record(record)
        expected={k:b[k] for k in ('campaign_id','evidence_kind','scope','split','family','instance_id',
                  'condition','candidate_id','candidate_sha256','repeat_id','execution_signature',
                  'reference_sha256','execution_config_sha256','config_sha256','protocol_sha256')}
        if any(record.get(k)!=v for k,v in expected.items()):raise ValueError('Physical trial binding mismatch')
        artifacts=record.get('output_artifacts')
        if not isinstance(artifacts,list) or not artifacts:raise ValueError('Immutable physical artifacts required')
        for item in artifacts:
            p=Path(item['path'])
            if p.is_symlink() or not p.is_file() or sha256_file(p)!=item['sha256']:
                raise ValueError('Physical artifact missing or corrupted')
        if (usage.get('settled') is not True or usage.get('physical_trial_id')!=record['trial_id']
                or usage.get('execution_signature')!=b['execution_signature']
                or usage.get('campaign_id')!=b['campaign_id'] or usage.get('scope')!=b['scope']):
            raise ValueError('Settled bound usage required')
        calls=usage.get('physical_calls')
        if not isinstance(calls,list) or not calls:raise ValueError('Physical usage calls required')
        ids=set()
        for call in calls:
            if not isinstance(call.get('call_id'),str) or not call['call_id'] or call['call_id'] in ids:
                raise ValueError('Unique physical call IDs required')
            ids.add(call['call_id'])
            try:cost=Decimal(str(call['accounted_upper_usd']))
            except (KeyError,InvalidOperation):raise ValueError('Measured physical cost required')
            if not cost.is_finite() or cost<0:raise ValueError('Invalid physical cost')
            if not isinstance(call.get('accounting_basis'),str) or not call['accounting_basis']:
                raise ValueError('Explicit cost basis required')
        proof=self.verify_evidence(copy.deepcopy(record),copy.deepcopy(usage),copy.deepcopy(b))
        if (not isinstance(proof,dict) or proof.get('verified') is not True
                or proof.get('record_sha256')!=canonical_hash(record)
                or proof.get('usage_sha256')!=canonical_hash(usage)
                or proof.get('binding_sha256')!=canonical_hash(b)):
            raise ValueError('Independent evidence verification failed')

    def record_completed(self, binding, claim_token, record, usage):
        b=_binding(binding);record=copy.deepcopy(record);usage=copy.deepcopy(usage);key=logical_id(b)
        self._verify_physical(b,record,usage)
        seal=canonical_hash({'binding':b,'record':record,'usage':usage})
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT binding,token,state FROM cells WHERE id=?',(key,)).fetchone()
            if row!=(_encode(b),claim_token,'running'):raise ValueError('Live matching claim required')
            existing=c.execute('SELECT seal FROM physical WHERE id=? OR owner=?',(record['trial_id'],key)).fetchall()
            if existing:
                if existing!=[(seal,)]:raise ValueError('Physical completion already differs')
                return record['trial_id']
            c.execute('INSERT INTO physical VALUES(?,?,?,?,?,?)',
                      (record['trial_id'],key,_encode(b),_encode(record),_encode(usage),seal))
        return record['trial_id']

    def _physical(self,c,physical_id):
        row=c.execute('SELECT binding,record,usage,seal FROM physical WHERE id=?',(physical_id,)).fetchone()
        if not row:raise ValueError('Unknown completed physical trial')
        b,r,u=map(json.loads,row[:3])
        if canonical_hash({'binding':b,'record':r,'usage':u})!=row[3]:raise ValueError('Physical seal corrupted')
        self._verify_physical(_binding(b),r,u)
        return b,r,u,row[3]

    def seal_receipt(self,binding,physical_id):
        b=_binding(binding);key=logical_id(b)
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT binding,state,receipt FROM cells WHERE id=?',(key,)).fetchone()
            if not row or row[0]!=_encode(b):raise ValueError('Existing matching logical claim required')
            if row[1]=='completed':
                receipt=self._verify_receipt(c,json.loads(row[2]),b)
                if receipt['actual_trial_id']!=physical_id:raise ValueError('Logical result already resolved differently')
                return receipt
            pb,r,u,seal=self._physical(c,physical_id)
            if not _compatible(pb,b):raise ValueError('Physical scope/config/candidate/reuse policy differs')
            own=c.execute('SELECT id FROM physical WHERE owner=?',(key,)).fetchone()
            if own and own[0]!=physical_id:raise ValueError('Cannot replace own completed physical result')
            if not own and row[1]!='claimed':raise ReconciliationRequired('Cannot reuse over a potentially dispatched attempt')
            receipt={'schema_version':1,'trial_id':key,'binding':b,'actual_trial_id':physical_id,
                     'reused_from_trial_id':physical_id if logical_id(pb)!=key else None,
                     'physical_seal':seal,'status':r['status'],'native_reward':r['native_reward'],
                     'usage_settled':True,'physical_call_ids':[x['call_id'] for x in u['physical_calls']]}
            receipt['receipt_sha256']=canonical_hash(receipt)
            c.execute('UPDATE cells SET state=?,receipt=? WHERE id=?',('completed',_encode(receipt),key))
            return receipt

    def _verify_receipt(self,c,receipt,b):
        if receipt.get('receipt_sha256')!=canonical_hash({k:v for k,v in receipt.items() if k!='receipt_sha256'}):
            raise ValueError('Receipt seal corrupted')
        pb,r,u,seal=self._physical(c,receipt['actual_trial_id'])
        expected={'schema_version':1,'trial_id':logical_id(b),'binding':b,'actual_trial_id':r['trial_id'],
                  'reused_from_trial_id':r['trial_id'] if logical_id(pb)!=logical_id(b) else None,
                  'physical_seal':seal,'status':r['status'],'native_reward':r['native_reward'],
                  'usage_settled':True,'physical_call_ids':[x['call_id'] for x in u['physical_calls']]}
        if not _compatible(pb,b) or {k:v for k,v in receipt.items() if k!='receipt_sha256'}!=expected:
            raise ValueError('Receipt projection differs from immutable physical evidence')
        return copy.deepcopy(receipt)

    def verified_receipt(self,binding):
        b=_binding(binding)
        with self._connect() as c:
            row=c.execute('SELECT receipt FROM cells WHERE id=? AND state=?',(logical_id(b),'completed')).fetchone()
            if not row:raise ReconciliationRequired('Logical cell has no sealed completed receipt')
            return self._verify_receipt(c,json.loads(row[0]),b)

    def project_result(self, binding):
        """Logical view; physical evidence remains unchanged in the registry."""
        b=_binding(binding)
        with self._connect() as c:
            row=c.execute('SELECT receipt FROM cells WHERE id=? AND state=?',(logical_id(b),'completed')).fetchone()
            if not row:raise ReconciliationRequired('Logical cell has no sealed completed receipt')
            receipt=self._verify_receipt(c,json.loads(row[0]),b)
            _,record,usage,_=self._physical(c,receipt['actual_trial_id'])
            result=copy.deepcopy(record)
            result.update({k:b[k] for k in ('condition','candidate_id','candidate_sha256','config_sha256')})
            result.update(trial_id=receipt['trial_id'],actual_trial_id=receipt['actual_trial_id'],
                reused_from_trial_id=receipt['reused_from_trial_id'],usage_settled=True,
                receipt_sha256=receipt['receipt_sha256'],physical_call_ids=receipt['physical_call_ids'],
                logical_binding_sha256=canonical_hash(b))
            bases={x['accounting_basis'] for x in usage['physical_calls']}
            total=sum((Decimal(str(x['accounted_upper_usd'])) for x in usage['physical_calls']),Decimal(0))
            # Attributed execution cost is retained on each logical view. Campaign
            # spend must use accounted_cost(), not a sum of projected rows.
            result.update(cost_usd=float(total),execution_cost_basis=next(iter(bases)) if len(bases)==1 else None,
                marginal_dispatch_cost_usd=0.0 if receipt['reused_from_trial_id'] else float(total))
            return result

    def accounted_cost(self):
        """Count each verified physical request once, never logical receipt copies."""
        calls={}
        with self._connect() as c:
            for (physical_id,) in c.execute('SELECT id FROM physical').fetchall():
                _,_,usage,_=self._physical(c,physical_id)
                for call in usage['physical_calls']:
                    if call['call_id'] in calls and calls[call['call_id']]!=call:
                        raise ValueError('Conflicting repeated physical-call accounting')
                    calls[call['call_id']]=call
        return {'physical_call_count':len(calls),'accounted_upper_usd':str(sum(
            (Decimal(str(x['accounted_upper_usd'])) for x in calls.values()),Decimal(0))),
            'actual_invoice_usd':None}
