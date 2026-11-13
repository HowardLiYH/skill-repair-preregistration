"""Opt-in exact OCI retention; no runner integration, Docker, or pilot cleanup.

Raw archives stay by default. Duplicate removal is allowed only inside a NEW
owned formal snapshot directory, after exact reconstruction and image validation.
Trusted owner-controlled local directories only, as in oci_archive_store.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
from .contracts import canonical_hash, sha256_file
from .oci_archive_store import (HEX, TRIAL, StoreError, ingest_archive, reconstruct_archive,
    _open_regular, _bounded_manifest_bytes, _json_bytes, _publish, _locked_store,
    _capacity, _inventory, _sync_directory)
from .saved_image_identity import inspect_saved_identity

OWNER = 'capstudy-snapshot-retention-v1'
LIMIT = 1024 * 1024


def _safe_path(path):
    p=Path(os.path.abspath(path))
    if any(q.is_symlink() for q in (p,*p.parents)):
        raise StoreError('Symlink in retention path')
    return p


def _read(path):
    with _open_regular(_safe_path(path)) as f:
        size=os.fstat(f.fileno()).st_size
        if size>LIMIT:raise StoreError('Oversized retention record')
        raw=f.read(LIMIT+1)
        if len(raw)!=size:raise StoreError('Retention record changed')
    result=_json_bytes(raw)
    if not isinstance(result,dict):raise StoreError('Invalid retention record')
    return result


def _sealed(record):
    return {**record,'receipt_sha256':canonical_hash(record)}


def _check_seal(record):
    if record.get('receipt_sha256')!=canonical_hash({k:v for k,v in record.items() if k!='receipt_sha256'}):
        raise StoreError('Retention seal mismatch')


def _write_new(path,record):
    encoded=(json.dumps(record,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    if len(encoded)>LIMIT:raise StoreError('Oversized retention record')
    _publish(path,[encoded])


def _owned_directory(path,kind):
    root=_safe_path(path)
    if not root.exists():root.mkdir(parents=True,mode=0o700)
    if not root.is_dir() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:
        raise StoreError('Private current-user-owned directory required')
    marker=root/'retention-owner.json'
    expected={'format':OWNER,'kind':kind,'path':str(root),'uid':os.getuid()}
    if not marker.exists():
        if any(root.iterdir()):raise StoreError('Cannot adopt a nonempty unowned directory')
        _write_new(marker,expected)
    elif _read(marker)!=expected:raise StoreError('Retention directory ownership differs')
    return root


def create_owned_formal_directory(path, *, campaign_id, trial_id, scope, protocol_sha256):
    """Call BEFORE saving a new formal archive. Existing directories are refused."""
    root=_safe_path(path)
    if (not TRIAL.fullmatch(trial_id) or scope not in ('formal_calibration','formal_dev','formal_test')
            or not isinstance(campaign_id,str) or not campaign_id
            or not isinstance(protocol_sha256,str) or not HEX.fullmatch(protocol_sha256)):
        raise StoreError('Explicit new formal provenance required')
    root.mkdir(mode=0o700,exist_ok=False)
    owner={'format':OWNER,'kind':'new-formal-snapshot','path':str(root),'uid':os.getuid(),
           'ownership_id':uuid.uuid4().hex,'campaign_id':campaign_id,'trial_id':trial_id,
           'scope':scope,'protocol_sha256':protocol_sha256,'evidence_kind':'experiment',
           'archive_name':'evaluator-image.tar'}
    _write_new(root/'retention-owner.json',owner)
    return owner


class SnapshotRetention:
    def __init__(self, *, store_path, scratch_path, receipts_path, store_quota_bytes,
                 scratch_quota_bytes, minimum_free_bytes, receipt_quota_bytes=16*1024*1024):
        for key,value in [('store quota',store_quota_bytes),('scratch quota',scratch_quota_bytes),
                          ('free reserve',minimum_free_bytes),('receipt quota',receipt_quota_bytes)]:
            if type(value) is not int or value<0:raise StoreError('Nonnegative integer '+key+' required')
        self.store=_safe_path(store_path)
        self._store_owner()
        self.scratch=_owned_directory(scratch_path,'scratch')
        self.receipts=_owned_directory(receipts_path,'receipts')
        roots=[self.store,self.scratch,self.receipts]
        if any(a==b or a in b.parents or b in a.parents for i,a in enumerate(roots) for b in roots[i+1:]):
            raise StoreError('Store, scratch and receipts directories must be disjoint')
        self.store_quota=store_quota_bytes;self.scratch_quota=scratch_quota_bytes
        self.reserve=minimum_free_bytes;self.receipt_quota=receipt_quota_bytes

    def _store_owner(self):
        _safe_path(self.store)
        if self.store.exists() and (not self.store.is_dir() or self.store.stat().st_uid!=os.getuid()
                or self.store.stat().st_mode&0o077):
            raise StoreError('CAS must be a private current-user-owned directory')

    def _receipt(self,path,record):
        _owned_directory(self.receipts,'receipts')
        fd=os.open(self.receipts/'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+b') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):raise StoreError('Invalid receipt lock')
            fcntl.flock(lock,fcntl.LOCK_EX)
            size=len((json.dumps(record,sort_keys=True,indent=2,allow_nan=False)+'\n').encode())
            block=max(4096,os.statvfs(self.receipts).f_frsize)
            _capacity(self.receipts,((size+block-1)//block)*block,self.receipt_quota,self.reserve)
            _write_new(path,record)

    @contextmanager
    def _scratch(self,size):
        _owned_directory(self.scratch,'scratch')
        fd=os.open(self.scratch/'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+b') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):raise StoreError('Invalid scratch lock')
            fcntl.flock(lock,fcntl.LOCK_EX)
            # Account all retained/crashed files, including any unexpected ones.
            block=max(4096,os.statvfs(self.scratch).f_frsize)
            needed=((size+block-1)//block)*block
            if _inventory(self.scratch)['allocated_file_bytes']+needed>self.scratch_quota:
                raise StoreError('Scratch quota exceeded')
            if shutil.disk_usage(self.scratch).free<size+self.reserve:
                raise StoreError('Insufficient scratch free-space reserve')
            folder=Path(tempfile.mkdtemp(prefix='materialize-',dir=self.scratch));output=folder/'evaluator-image.tar'
            try:yield output
            finally:
                # Only remove the exact scratch file this invocation created.
                if output.exists() or output.is_symlink():
                    if output.is_symlink() or not output.is_file():raise StoreError('Scratch output ownership changed')
                    output.unlink()
                # No recursive removal or cleanup of unrelated/recovered files.
                folder.rmdir();_sync_directory(self.scratch)

    def _manifest(self,trial_id,expected_sha):
        self._store_owner()
        with _locked_store(self.store) as root:
            _capacity(root,0,self.store_quota,self.reserve)
            path=root/'manifests'/(trial_id+'.json')
            with _open_regular(path) as f:manifest=_json_bytes(_bounded_manifest_bytes(f))
            if sha256_file(path)!=expected_sha:raise StoreError('CAS manifest changed')
        return manifest

    def retain(self,snapshot,*,trial_id,remove_raw=False,owned_formal_directory=None):
        if not isinstance(trial_id,str) or not TRIAL.fullmatch(trial_id) or type(remove_raw) is not bool:
            raise StoreError('Explicit safe trial identity/removal flag required')
        raw=_safe_path(snapshot['path'])
        if raw.is_relative_to(self.store) or raw.is_relative_to(self.scratch) or raw.is_relative_to(self.receipts):
            raise StoreError('Raw archive must be outside retention working directories')
        if not isinstance(snapshot.get('sha256'),str) or not HEX.fullmatch(snapshot['sha256']):
            raise StoreError('Verified original archive hash required')
        with _open_regular(raw) as f:original=os.fstat(f.fileno())
        if sha256_file(raw)!=snapshot['sha256']:raise StoreError('Original archive hash mismatch')
        identity=inspect_saved_identity(snapshot)
        owner=None
        if remove_raw:
            if owned_formal_directory is None:raise StoreError('Removal requires NEW formal ownership')
            directory=_safe_path(owned_formal_directory);owner=_read(directory/'retention-owner.json')
            if (owner.get('format')!=OWNER or owner.get('kind')!='new-formal-snapshot'
                    or owner.get('scope') not in ('formal_calibration','formal_dev','formal_test')
                    or owner.get('evidence_kind')!='experiment' or owner.get('trial_id')!=trial_id
                    or owner.get('uid')!=os.getuid() or owner.get('path')!=str(directory)
                    or not owner.get('ownership_id') or raw!=directory/'evaluator-image.tar'
                    or original.st_uid!=os.getuid() or directory.stat().st_mode&0o077):
                raise StoreError('Only owned NEW formal archive duplicates may be removed')
        if remove_raw and (original.st_nlink!=1 or (self.receipts/(trial_id+'.removal.json')).exists()):
            raise StoreError('Removal requires single-link source and unused removal record')
        destination=self.receipts/(trial_id+'.json')
        if destination.exists() or destination.is_symlink():raise StoreError('Immutable retention receipt already exists')
        self._store_owner()
        ingest_archive(raw,self.store,trial_id,snapshot['image_id'],
                             quota_bytes=self.store_quota,min_free_bytes=self.reserve)
        manifest_path=self.store/'manifests'/(trial_id+'.json')
        manifest_hash=sha256_file(manifest_path)
        receipt={'schema_version':1,'format':OWNER,'trial_id':trial_id,'store_path':str(self.store),
                 'manifest_file_sha256':manifest_hash,'archive_sha256':snapshot['sha256'],
                 'archive_size':original.st_size,'image_id':snapshot['image_id'],
                 'image_identity':identity,'original_path':str(raw),'ownership':owner,
                 'retention_policy':{'store_quota_bytes':self.store_quota,'scratch_quota_bytes':self.scratch_quota,
                    'receipt_quota_bytes':self.receipt_quota,'minimum_free_bytes':self.reserve,
                    'implementation_sha256':sha256_file(Path(__file__))},
                 'raw_duplicate_removal_requested':remove_raw,'original_preserved_at_sealing':True,
                 'byte_exact_reconstruction_verified':True,'native_regrade_executed':False,
                 'full_study_scale_gate_passed':False}
        # Full reconstruction verifies EVERY recipe object, whole archive bytes,
        # and all OCI-reachable blobs before receipt publication or deletion.
        with self._scratch(original.st_size) as restored:
            restored_result=reconstruct_archive(self.store,trial_id,restored,min_free_bytes=self.reserve)
            restored_identity=inspect_saved_identity({'path':str(restored),'image_id':snapshot['image_id']})
            if (restored_result['archive_sha256']!=snapshot['sha256'] or restored.stat().st_size!=original.st_size
                    or restored_identity!=identity):raise StoreError('Exact reconstruction/image verification failed')
        # Detect replacement, mutation or hardlink tricks before changing source.
        now=raw.lstat()
        if (now.st_dev,now.st_ino,now.st_size,now.st_mtime_ns,now.st_ctime_ns)!=(
                original.st_dev,original.st_ino,original.st_size,original.st_mtime_ns,original.st_ctime_ns):
            raise StoreError('Original archive changed during retention')
        if sha256_file(raw)!=snapshot['sha256']:raise StoreError('Original archive changed')
        receipt=_sealed(receipt);self._receipt(destination,receipt)
        if remove_raw:
            latest=raw.lstat()
            if ((latest.st_dev,latest.st_ino,latest.st_size,latest.st_mtime_ns,latest.st_ctime_ns)!=(
                    original.st_dev,original.st_ino,original.st_size,original.st_mtime_ns,original.st_ctime_ns)
                    or sha256_file(raw)!=snapshot['sha256']):
                raise StoreError('Raw archive changed after receipt sealing; retained')
            if latest.st_nlink!=1:raise StoreError('Refusing duplicate removal of multiply linked archive')
            # A sealed intent exists before deletion. A crash may leave either raw
            # presence state; never claim that intent alone proves removal.
            raw.unlink();_sync_directory(raw.parent)
            self._receipt(self.receipts/(trial_id+'.removal.json'),_sealed({
                'format':OWNER,'trial_id':trial_id,'source_receipt_sha256':receipt['receipt_sha256'],
                'removed_path':str(raw),'removed_archive_sha256':snapshot['sha256']}))
        return {'receipt_path':str(destination),'receipt':receipt,'raw_original_present':raw.exists()}

    @contextmanager
    def materialize(self,receipt_path):
        _owned_directory(self.receipts,'receipts')
        receipt_path=_safe_path(receipt_path)
        if receipt_path.parent!=self.receipts:raise StoreError('Receipt outside owned receipt directory')
        receipt=_read(receipt_path);_check_seal(receipt)
        if (receipt.get('format')!=OWNER or receipt.get('schema_version')!=1
                or receipt.get('store_path')!=str(self.store) or not TRIAL.fullmatch(receipt.get('trial_id',''))
                or type(receipt.get('archive_size')) is not int or receipt['archive_size']<=0
                or receipt.get('byte_exact_reconstruction_verified') is not True):
            raise StoreError('Invalid retention receipt binding')
        manifest=self._manifest(receipt['trial_id'],receipt['manifest_file_sha256'])
        if any(manifest.get(k)!=receipt[k] for k in ('archive_sha256','archive_size','image_id')):
            raise StoreError('Receipt/manifest archive binding differs')
        with self._scratch(receipt['archive_size']) as path:
            result=reconstruct_archive(self.store,receipt['trial_id'],path,min_free_bytes=self.reserve)
            identity=inspect_saved_identity({'path':str(path),'image_id':receipt['image_id']})
            if result['archive_sha256']!=receipt['archive_sha256'] or identity!=receipt['image_identity']:
                raise StoreError('Materialized image differs from receipt')
            yield {'path':str(path),'sha256':receipt['archive_sha256'],'image_id':receipt['image_id'],
                   'retention_receipt_sha256':receipt['receipt_sha256']}

    @contextmanager
    def open_snapshot(self,snapshot_or_receipt):
        """Keep the raw-snapshot interface; CAS materialization is opt-in."""
        if isinstance(snapshot_or_receipt,dict):
            snapshot=dict(snapshot_or_receipt);path=_safe_path(snapshot['path'])
            if sha256_file(path)!=snapshot['sha256']:raise StoreError('Raw snapshot hash mismatch')
            inspect_saved_identity(snapshot)
            yield snapshot
        else:
            with self.materialize(snapshot_or_receipt) as snapshot:yield snapshot
