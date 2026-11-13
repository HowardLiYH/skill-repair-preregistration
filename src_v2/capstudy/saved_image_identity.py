"""Read-only saved-image identity check; not wired into the active runner yet.

Modern Docker may identify an OCI manifest instead of its configuration. Both
are accepted only within one completely verified OCI image graph. Compressed
OCI backups must first be materialized under an explicit disk admission policy.
No Docker load, filesystem extraction, archive conversion, or network operation.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from .oci_archive_store import inspect_archive, StoreError


def inspect_saved_identity(snapshot):
    image_id = snapshot.get('image_id', '')
    if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise ValueError('Expected content-addressed Docker image ID')
    path = Path(snapshot['path'])
    if path.is_symlink() or not path.is_file():
        raise ValueError('Saved image must be a regular nonsymlink file')
    try:
        with tarfile.open(path, 'r:*') as archive:
            members=archive.getmembers()
            names=[m.name.rstrip('/') if m.isdir() else m.name for m in members]
            if len(names)!=len(set(names)):
                raise ValueError('Duplicate saved-image archive member')
            for m,name in zip(members,names):
                p=PurePosixPath(name)
                if (not name or p.is_absolute() or '..' in p.parts or '\\' in name
                        or str(p)!=name or not (m.isfile() or m.isdir())):
                    raise ValueError('Unsafe saved-image archive member')
            if 'oci-layout' in names or 'index.json' in names:
                oci=True
            else:
                oci=False
                member=archive.getmember('manifest.json')
                if not member.isfile() or member.size>1024*1024:
                    raise ValueError('Invalid saved-image manifest')
                manifest=json.load(archive.extractfile(member))
                if not isinstance(manifest,list) or len(manifest)!=1 or not isinstance(manifest[0],dict):
                    raise ValueError('Evaluator archive must contain exactly one image')
                config=archive.getmember(manifest[0]['Config'])
                if not config.isfile() or config.size>16*1024*1024:
                    raise ValueError('Invalid saved-image configuration')
                config_id='sha256:'+hashlib.sha256(archive.extractfile(config).read()).hexdigest()
                if image_id!=config_id:
                    raise ValueError('Saved archive does not match claimed image identity')
                return {'image_id':image_id,'image_identity_kind':'config_digest',
                        'image_config_digests':[config_id],'format':'legacy_docker_archive'}
        if oci:
            with path.open('rb') as stream:
                proof=inspect_archive(stream,image_id)
            if len(proof['image_config_digests'])!=1:
                raise ValueError('Evaluator archive must contain exactly one image configuration')
            return {k:proof[k] for k in ('image_id','image_identity_kind','image_config_digests','archive_sha256')}
    except (tarfile.TarError,KeyError,TypeError,OSError,StoreError) as exc:
        raise ValueError('Unreadable or invalid saved image identity') from exc
