"""Independent bounded remote CAS verification; no models, Docker, or CAS edits.

Reconstructs one owned temporary archive, validates its full bytes and OCI image
identity, removes only that scratch directory, then publishes a small receipt.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from .contracts import canonical_hash,sha256_file
from .oci_archive_store import reconstruct_archive
from .saved_image_identity import inspect_saved_identity


def verify(receipt_path,expected_sha256,output,*,scratch_root,minimum_free_bytes):
    receipt_path=Path(receipt_path);output=Path(output);scratch_root=Path(scratch_root)
    if (receipt_path.is_symlink() or not receipt_path.is_file() or sha256_file(receipt_path)!=expected_sha256
            or output.exists() or output.is_symlink() or not scratch_root.is_dir() or scratch_root.is_symlink()
            or type(minimum_free_bytes) is not int or minimum_free_bytes<20*1024**3):
        raise ValueError('Bound receipt, fresh output and guarded scratch required')
    data=receipt_path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=expected_sha256:raise ValueError('Retention receipt changed')
    receipt=json.loads(data)
    if receipt.get('receipt_sha256')!=canonical_hash({k:v for k,v in receipt.items() if k!='receipt_sha256'}):
        raise ValueError('Retention receipt seal differs')
    store=Path(receipt['store_path']);manifest=store/'manifests'/(receipt['trial_id']+'.json')
    if manifest.is_symlink() or sha256_file(manifest)!=receipt['manifest_file_sha256']:raise ValueError('CAS manifest changed')
    if scratch_root.resolve().is_relative_to(store.resolve()):raise ValueError('Scratch must be outside CAS')
    if shutil.disk_usage(scratch_root).free<minimum_free_bytes+receipt['archive_size']:raise ValueError('Insufficient scratch reserve')
    with tempfile.TemporaryDirectory(prefix='capstudy-cas-check-',dir=scratch_root) as temporary:
        archive=Path(temporary)/'verification.tar'
        verified=reconstruct_archive(store,receipt['trial_id'],archive,min_free_bytes=minimum_free_bytes)
        identity=inspect_saved_identity({'path':str(archive),'image_id':receipt['image_id']})
        if (verified['archive_sha256']!=receipt['archive_sha256'] or verified['archive_size']!=receipt['archive_size']
                or identity!=receipt['image_identity']):raise ValueError('Reconstructed evaluator identity differs')
    if sha256_file(receipt_path)!=expected_sha256 or sha256_file(manifest)!=receipt['manifest_file_sha256']:
        raise ValueError('CAS receipt/manifest changed during verification')
    result={'schema_version':1,'kind':'independent-remote-cas-verification-v1',
            'trial_id':receipt['trial_id'],'image_id':receipt['image_id'],'receipt_file_sha256':expected_sha256,
            'receipt_sha256':receipt['receipt_sha256'],'manifest_file_sha256':receipt['manifest_file_sha256'],
            'archive_sha256':receipt['archive_sha256'],'archive_size':receipt['archive_size'],
            'store_path':receipt['store_path'],'image_identity':identity,'byte_exact':True,'scratch_cleaned':True,
            'provider_requests':0,'observed_at':datetime.now(timezone.utc).isoformat(),
            'implementation_sha256':{name:sha256_file(Path(__file__).with_name(name)) for name in
                ('formal_cas_verify.py','oci_archive_store.py','saved_image_identity.py')}}
    result['verification_sha256']=canonical_hash(result)
    with output.open('x') as f:
        os.chmod(output,0o600);json.dump(result,f,indent=2,sort_keys=True,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--receipt',type=Path,required=True);p.add_argument('--receipt-sha256',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--scratch',type=Path,required=True);p.add_argument('--minimum-free-bytes',type=int,default=20*1024**3);a=p.parse_args()
    r=verify(a.receipt,a.receipt_sha256,a.output,scratch_root=a.scratch,minimum_free_bytes=a.minimum_free_bytes)
    print(json.dumps({'verified':True,'trial_id':r['trial_id'],'verification_sha256':r['verification_sha256']}))
