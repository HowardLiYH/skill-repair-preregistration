"""Persist editor requests before transport; bounded confirmed pre-dispatch retries."""
import json
from pathlib import Path
import time
import uuid
from .contracts import write_json_atomic
from .journal import AdmissionBlocked

class ConfirmedPreDispatchRejection(RuntimeError):
    """Transport has positive provider evidence that generation never began."""

def dispatch_editor(journal,identity,request,transport,*,artifact_root,upper_charge_usd,
                    payment_mode='api',included_usage_evidence=None,sleep=time.sleep,cost_resolver=None):
    job=journal.schedule(identity,request,upper_charge_usd,payment_mode=payment_mode,
                         attempt_index=identity['attempt_index'],included_usage_evidence=included_usage_evidence)
    if job['state']=='completed':return journal.completed_response(job['job_id'])
    if job['state']!='pending':raise AdmissionBlocked('Existing editor attempt requires reconciliation, not redispatch')
    folder=Path(artifact_root)/'editor_calls'/job['job_id'];folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    write_json_atomic(folder/'request.json',request)
    journal.claim(job['job_id'],'worker-'+uuid.uuid4().hex)
    for index in range(3):
        try:
            result=transport(request)
        except ConfirmedPreDispatchRejection as exc:
            journal.event(job['job_id'],'confirmed_pre_dispatch_rejection',{'dispatch_index':index,'error_type':type(exc).__name__})
            if index==2:
                journal.incident(job['job_id'],'pre_dispatch_retries_exhausted',confirmed_not_dispatched=True)
                raise
            sleep((10,30)[index])
            continue
        except BaseException as exc:
            if hasattr(exc,'evidence'):
                write_json_atomic(folder/'failed_response.json',exc.evidence)
            journal.incident(job['job_id'],'transport_'+type(exc).__name__,
                             confirmed_not_dispatched=getattr(exc,'dispatched',None) is False)
            raise
        # Preserve even malformed/model-refusal responses before downstream parsing.
        try:
            write_json_atomic(folder/'response.json',result)
            if cost_resolver is not None:
                result=cost_resolver(result)
                write_json_atomic(folder/'accounted_response.json',result)
            # An explicitly labelled conservative accounting charge can bound a
            # provider bill without pretending the provider returned USD usage.
            charge=result.get('accounted_charge_usd',result.get('actual_incremental_charge_usd'))
            path=folder/('accounted_response.json' if cost_resolver is not None else 'response.json')
            journal.complete(job['job_id'],path,charge,
                             provider_request_id=result.get('request_id'))
        except BaseException as exc:
            journal.incident(job['job_id'],'persistence_or_accounting_'+type(exc).__name__)
            raise
        if journal.get(job['job_id'])['state']!='completed':
            raise AdmissionBlocked('Response saved; actual charge unavailable, pause scheduling')
        return result
