"""Durable single-worker dispatch reservations and append-only state events.

SQLite transactions prevent two workers from dispatching the same scheduled job.
An uncertain dispatch remains reserved and requires explicit reconciliation.
This ledger is an admission control, not a guarantee about provider-side billing.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3
import uuid
from .contracts import canonical_hash

class AdmissionBlocked(RuntimeError):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def money(value):
    if isinstance(value,bool):raise ValueError('Boolean is not a monetary amount')
    try:d=Decimal(str(value))
    except InvalidOperation as e:raise ValueError('Invalid monetary amount') from e
    if not d.is_finite() or d<0:raise ValueError('Amount must be finite and nonnegative')
    return d

class Journal:
    def __init__(self,path,budget_usd,*,campaign_id,evidence_kind='experiment'):
        if evidence_kind not in ('experiment','fixture'):raise ValueError('Invalid evidence kind')
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.connection=sqlite3.connect(self.path,timeout=10,isolation_level=None)
        self.connection.row_factory=sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(
            job_id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
            reservation TEXT NOT NULL, charge TEXT, payment_mode TEXT NOT NULL,
            request_sha256 TEXT NOT NULL, worker TEXT, response_path TEXT,
            response_sha256 TEXT, provider_request_id TEXT, attempt_index INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS events(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            at TEXT NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL);
        ''')
        os.chmod(self.path,0o600)
        expected={'budget_usd':str(money(budget_usd)),'campaign_id':campaign_id,'evidence_kind':evidence_kind}
        with self.transaction():
            for key,value in expected.items():
                old=self.connection.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
                if old and old['value']!=value:raise AdmissionBlocked('Journal identity/budget differs; explicit migration required')
                self.connection.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',(key,value))
        self.budget=money(budget_usd)

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.connection.execute('ROLLBACK');raise
        else:self.connection.execute('COMMIT')

    def close(self):self.connection.close()

    def _event(self,job_id,event,payload):
        self.connection.execute('INSERT INTO events(job_id,at,event,payload) VALUES(?,?,?,?)',
                                (job_id,now(),event,json.dumps(payload,sort_keys=True,allow_nan=False)))

    def get(self,job_id):
        row=self.connection.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if row is None:raise KeyError(job_id)
        return dict(row)

    def totals(self):
        rows=self.connection.execute('SELECT state,reservation,charge FROM jobs').fetchall()
        spent=sum((money(r['charge']) for r in rows if r['charge'] is not None),Decimal(0))
        reserved=sum((money(r['reservation']) for r in rows if r['state'] in ('pending','running','needs_reconciliation','usage_unavailable')),Decimal(0))
        unresolved=sum(r['state'] in ('needs_reconciliation','usage_unavailable') for r in rows)
        return {'spent':spent,'reserved':reserved,'unresolved':unresolved,
                'warning':spent+reserved>=self.budget*Decimal('.8') if self.budget else False}

    def schedule(self,identity,request,upper_charge_usd,*,payment_mode='api',attempt_index=0,
                 included_usage_evidence=None,claim_worker=None,reuse_completed=False):
        if claim_worker is not None and (not isinstance(claim_worker,str) or not claim_worker.strip()):
            raise ValueError('Atomic dispatch requires a named worker')
        reservation=money(upper_charge_usd)
        if payment_mode not in ('api','subscription_included','local','fixture'):raise ValueError('Unknown payment mode')
        kind=self.connection.execute("SELECT value FROM metadata WHERE key='evidence_kind'").fetchone()['value']
        if payment_mode=='fixture' and kind!='fixture':raise AdmissionBlocked('Fixture dispatch in experiment ledger')
        if payment_mode in ('local','subscription_included','fixture') and reservation!=0:
            raise ValueError('Non-billable route cannot reserve an API charge')
        if payment_mode=='subscription_included':
            e=included_usage_evidence or {}
            if not (e.get('auth_method')=='claude.ai' and e.get('extra_usage_disabled') is True
                    and e.get('api_fallback_disabled') is True and e.get('verified_at')):
                raise AdmissionBlocked('Included-only billing evidence required')
        if payment_mode=='api' and (self.budget==0 or reservation==0):
            raise AdmissionBlocked('Paid API dispatch requires positive approved budget and reservation')
        identity_hash=canonical_hash(identity);request_hash=canonical_hash(request)
        with self.transaction():
            prior=self.connection.execute('SELECT * FROM jobs WHERE identity=?',(identity_hash,)).fetchone()
            if prior:
                if prior['request_sha256']!=request_hash:raise AdmissionBlocked('Scheduled request changed for same attempt identity')
                if claim_worker is not None:
                    if reuse_completed and prior['state']=='completed':return dict(prior)
                    self._claim_locked(prior['job_id'],claim_worker)
                    return self.get(prior['job_id'])
                return dict(prior)
            totals=self.totals()
            if totals['unresolved']:raise AdmissionBlocked('Unresolved dispatch/usage must be reconciled first')
            if totals['spent']+totals['reserved']+reservation>self.budget:
                raise AdmissionBlocked('Budget reservation exceeds ceiling')
            job='job-'+uuid.uuid4().hex
            self.connection.execute('INSERT INTO jobs(job_id,identity,state,reservation,payment_mode,request_sha256,attempt_index) VALUES(?,?,?,?,?,?,?)',
                                    (job,identity_hash,'pending',str(reservation),payment_mode,request_hash,attempt_index))
            self._event(job,'scheduled',{'identity':identity,'upper_charge_usd':str(reservation),
                                       'payment_mode':payment_mode,'included_usage_evidence':included_usage_evidence})
            if claim_worker is not None:self._claim_locked(job,claim_worker)
            return self.get(job)

    def schedule_and_claim(self,identity,request,upper_charge_usd,*,worker,**kwargs):
        """Atomically admit and claim; a failed claim creates no orphan reservation."""
        return self.schedule(identity,request,upper_charge_usd,claim_worker=worker,**kwargs)

    def claim(self,job_id,worker):
        with self.transaction():
            self._claim_locked(job_id,worker)

    def _claim_locked(self,job_id,worker):
        """Caller owns an immediate transaction, including any new reservation."""
        job=self.get(job_id)
        if job['state']!='pending':raise AdmissionBlocked('Job is not pending; never redispatch running/unknown jobs')
        totals=self.totals()
        if totals['unresolved']:raise AdmissionBlocked('Unresolved usage blocks dispatch')
        if totals['spent']+totals['reserved']>self.budget:
            raise AdmissionBlocked('Actual spend/reservations exceed ceiling; pending dispatch blocked')
        active=self.connection.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
        if active:raise AdmissionBlocked('Concurrency one: running job must finish or be reconciled first')
        self.connection.execute("UPDATE jobs SET state='running',worker=? WHERE job_id=?",(worker,job_id))
        self._event(job_id,'dispatch_started',{'worker':worker,'attempt_consumed_unless_confirmed_pre_dispatch_rejection':True})

    def event(self,job_id,event,payload):
        with self.transaction():self.get(job_id);self._event(job_id,event,payload)

    def incident(self,job_id,reason,*,confirmed_not_dispatched=False):
        with self.transaction():
            job=self.get(job_id)
            if job['state']!='running':raise AdmissionBlocked('Incident requires running job')
            state='not_dispatched' if confirmed_not_dispatched else 'needs_reconciliation'
            self.connection.execute('UPDATE jobs SET state=? WHERE job_id=?',(state,job_id))
            if confirmed_not_dispatched:self.connection.execute("UPDATE jobs SET charge='0' WHERE job_id=?",(job_id,))
            self._event(job_id,state,{'reason':reason,'attempt_consumed':not confirmed_not_dispatched})

    def complete(self,job_id,response_path,charge_usd,*,provider_request_id=None):
        path=Path(response_path).resolve()
        if not path.is_file():raise ValueError('Response bytes must be saved before completion')
        from .contracts import sha256_file
        with self.transaction():
            job=self.get(job_id)
            if job['state']!='running':raise AdmissionBlocked('Only running jobs can complete')
            charge=None if charge_usd is None else str(money(charge_usd))
            if job['payment_mode']=='subscription_included' and charge not in ('0','0.0'):
                if charge is not None and money(charge)!=0:raise AdmissionBlocked('Unexpected subscription charge requires reconciliation')
            state='completed' if charge is not None else 'usage_unavailable'
            self.connection.execute('UPDATE jobs SET state=?,charge=?,response_path=?,response_sha256=?,provider_request_id=? WHERE job_id=?',
                (state,charge,str(path),sha256_file(path),provider_request_id,job_id))
            self._event(job_id,state,{'charge_usd':charge,'response_path':str(path),
                                    'provider_request_id':provider_request_id})

    def completed_response(self,job_id):
        from .contracts import sha256_file
        job=self.get(job_id)
        if job['state']!='completed':raise AdmissionBlocked('No resolved completed response')
        if sha256_file(job['response_path'])!=job['response_sha256']:raise AdmissionBlocked('Saved response bytes changed')
        return json.loads(Path(job['response_path']).read_text())

    def reconcile_not_dispatched(self,job_id,*,evidence_path,expected_request_sha256,reason):
        """Reviewed requeue of the same job after proof no HTTP request was sent.

        This is an explicit operator action, never automatic retry. The review JSON
        must bind the job/request and hash its supporting incident/traceback files.
        It does not apply to lost responses, unknown TLS errors or provider failures.
        """
        from .contracts import sha256_file
        import hashlib
        if not isinstance(reason,str) or not reason.strip():
            raise ValueError('An explicit reviewed reconciliation reason is required')
        evidence=Path(evidence_path).resolve()
        raw=evidence.read_bytes()
        review=json.loads(raw)
        if not isinstance(review,dict) or review.get('job_id')!=job_id or review.get('request_sha256')!=expected_request_sha256:
            raise AdmissionBlocked('Review evidence must bind the exact job and request')
        if review.get('confirmed_not_dispatched') is not True or review.get('http_request_sent') is not False:
            raise AdmissionBlocked('Positive proof of no HTTP dispatch is required')
        if not isinstance(review.get('reviewer'),str) or not review['reviewer'].strip():
            raise AdmissionBlocked('Named review is required for explicit requeue')
        supporting=review.get('evidence_files')
        if not isinstance(supporting,list) or not supporting:
            raise AdmissionBlocked('Hashed supporting incident evidence is required')
        for record in supporting:
            if not isinstance(record,dict) or not record.get('path') or not record.get('sha256'):
                raise AdmissionBlocked('Malformed supporting evidence record')
            if sha256_file(record['path'])!=record['sha256']:
                raise AdmissionBlocked('Supporting incident evidence changed')
        with self.transaction():
            job=self.get(job_id)
            if job['state'] not in ('needs_reconciliation','not_dispatched'):
                raise AdmissionBlocked('Only an ended unresolved or known-undispatched job may be requeued')
            if job['request_sha256']!=expected_request_sha256:
                raise AdmissionBlocked('Request hash changed; never replay a different request')
            if job['response_path'] or job['response_sha256'] or job['provider_request_id']:
                raise AdmissionBlocked('A job with received-response evidence is not proven undispatched')
            if job['charge'] is not None and money(job['charge'])!=0:
                raise AdmissionBlocked('A charged job cannot be requeued as undispatched')
            totals=self.totals()
            own_unresolved=int(job['state']=='needs_reconciliation')
            if totals['unresolved']>own_unresolved:
                raise AdmissionBlocked('Resolve other uncertain dispatches before requeue')
            active=self.connection.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
            if active:raise AdmissionBlocked('A live running job prevents operator requeue')
            restored=money(job['reservation']) if job['state']=='not_dispatched' else Decimal(0)
            if totals['spent']+totals['reserved']+restored>self.budget:
                raise AdmissionBlocked('Restored reservation would exceed budget')
            self.connection.execute("UPDATE jobs SET state='pending',charge=NULL,worker=NULL WHERE job_id=?",(job_id,))
            self._event(job_id,'reviewed_not_dispatched_requeue',{
                'previous_state':job['state'],'reason':reason,'reviewer':review['reviewer'],
                'evidence_path':str(evidence),'evidence_sha256':hashlib.sha256(raw).hexdigest(),
                'request_sha256':expected_request_sha256,'attempt_consumed':False,
                'confirmed_incremental_charge_usd':'0','reservation_restored':job['reservation']})
            return self.get(job_id)

    def reconcile(self,job_id,*,evidence_path,charge_usd,response_path=None):
        """Explicit operator recovery; evidence must establish the dispatch outcome."""
        from .contracts import sha256_file
        evidence=Path(evidence_path).resolve()
        if not evidence.is_file():raise ValueError('Persistent reconciliation evidence required')
        charge=str(money(charge_usd))
        with self.transaction():
            job=self.get(job_id)
            if job['state'] not in ('needs_reconciliation','usage_unavailable','running'):
                raise AdmissionBlocked('Job does not require reconciliation')
            response=Path(response_path or job['response_path']).resolve() if response_path or job['response_path'] else None
            if response is not None and not response.is_file():raise ValueError('Missing recovered response')
            state='completed' if response else 'resolved_lost_response'
            self.connection.execute('UPDATE jobs SET state=?,charge=?,response_path=?,response_sha256=? WHERE job_id=?',
                (state,charge,str(response) if response else None,sha256_file(response) if response else None,job_id))
            self._event(job_id,'reconciled',{'evidence_path':str(evidence),'evidence_sha256':sha256_file(evidence),'state':state,'attempt_consumed':True})
