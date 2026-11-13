"""Shared bounded local-artifact admission for explicit formal dispatch only.

The lock spans request/generation AND persistence. Existing pilot routes do not
use this policy. No pruning, budget creation or automatic quota expansion.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
from .contracts import canonical_hash
from .journal import AdmissionBlocked
from .oci_archive_store import _inventory,_open_regular,_publish

REQUEST_LIMIT=1024*1024
RESPONSE_LIMIT=16*1024*1024
JOURNAL_HEADROOM=8*1024*1024
FREE_FLOOR=20*1024**3
FIELDS={'id','root','quota_bytes','minimum_free_bytes','request_max_bytes','response_max_bytes',
        'journal_headroom_bytes','maximum_growth_bytes'}


def validate_storage_policy(policy,*,evidence_kind,artifact_root):
    if not isinstance(policy,dict) or set(policy)!=FIELDS or policy.get('id')!='formal-dispatch-storage-v1':
        raise AdmissionBlocked('Explicit frozen shared dispatch storage policy required')
    if not isinstance(policy['root'],str) or not Path(policy['root']).is_absolute():
        raise AdmissionBlocked('Absolute shared artifact root required')
    root=Path(policy['root']);artifact=Path(artifact_root)
    for path in (root,artifact):
        if any(p.is_symlink() for p in (path,*path.parents)):
            raise AdmissionBlocked('Symlink in dispatch artifact path')
    if artifact.resolve()==root.resolve() or not artifact.resolve().is_relative_to(root.resolve()):
        raise AdmissionBlocked('Formal artifacts must be below shared quota root')
    if any(type(policy[k]) is not int or policy[k]<0 for k in FIELDS-{'id','root'}):
        raise AdmissionBlocked('Integer storage limits required')
    growth=8*REQUEST_LIMIT+8*RESPONSE_LIMIT+policy['journal_headroom_bytes']
    if (policy['request_max_bytes']!=REQUEST_LIMIT or policy['response_max_bytes']!=RESPONSE_LIMIT
            or policy['journal_headroom_bytes']<JOURNAL_HEADROOM or policy['maximum_growth_bytes']!=growth
            or policy['quota_bytes']<growth or (evidence_kind=='experiment' and policy['minimum_free_bytes']<FREE_FLOOR)):
        raise AdmissionBlocked('Storage bounds weaker than request/response/copy/journal/free-space policy')
    if evidence_kind not in ('experiment','fixture'):raise AdmissionBlocked('Explicit evidence kind required')
    return dict(policy)


def prepare_storage_root(policy):
    root=Path(policy['root'])
    if root.is_symlink():raise AdmissionBlocked('Shared storage root is a symlink')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=root.stat()
    if not root.is_dir() or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise AdmissionBlocked('Private current-user-owned shared storage root required')
    marker=root/'dispatch-storage-owner.json'
    wanted={'format':'formal-dispatch-storage-v1','policy':policy,'policy_sha256':canonical_hash(policy)}
    if marker.exists():
        with _open_regular(marker) as f:
            if os.fstat(f.fileno()).st_size>65536 or json.load(f)!=wanted:
                raise AdmissionBlocked('Shared storage policy/ownership changed')
    else:
        if any(root.iterdir()):raise AdmissionBlocked('Cannot adopt nonempty unowned dispatch storage')
        _publish(marker,[(json.dumps(wanted,sort_keys=True)+'\n').encode()])
    return root


@contextmanager
def dispatch_storage_guard(policy,*,evidence_kind,artifact_root):
    policy=validate_storage_policy(policy,evidence_kind=evidence_kind,artifact_root=artifact_root)
    root=prepare_storage_root(policy)
    fd=os.open(root/'.dispatch-lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+b') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):raise AdmissionBlocked('Invalid shared storage lock')
        fcntl.flock(lock,fcntl.LOCK_EX)
        used=_inventory(root)['allocated_file_bytes'];growth=policy['maximum_growth_bytes']
        if used+growth>policy['quota_bytes']:raise AdmissionBlocked('Shared formal artifact quota cannot admit next request')
        if shutil.disk_usage(root).free<policy['minimum_free_bytes']+growth:
            raise AdmissionBlocked('Local free-space floor cannot admit next bounded request')
        yield {'policy_sha256':canonical_hash(policy),'used_before':used,'reserved_growth_bytes':growth}
        if _inventory(root)['allocated_file_bytes']>policy['quota_bytes'] or shutil.disk_usage(root).free<policy['minimum_free_bytes']:
            raise AdmissionBlocked('Post-dispatch storage bound exceeded; preserve evidence and stop')
