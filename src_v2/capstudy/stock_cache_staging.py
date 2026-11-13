"""Validated neutral npm cache injection into a new per-instance stage only."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import stat
from .contracts import canonical_hash
from .runner import ROOT, sha256_file
from .stock_dependency_cache import files_manifest, verify_lock_cache

NPMRC='cache=/opt/capstudy-dependencies/npm-cache\noffline=true\naudit=false\nfund=false\nupdate-notifier=false\n'
DIRECTORIES=('npm-cache','lock','vendor')


def bound_files(cache):
    rows=[]
    for directory in DIRECTORIES:
        root=cache/directory
        if root.is_symlink() or not root.is_dir():raise ValueError('Neutral cache directory missing or linked')
        for p in [root,*sorted(root.rglob('*'))]:
            if p.is_symlink() or not (p.is_dir() or p.is_file()):raise ValueError('Neutral cache special path rejected')
            row={'path':str(p.relative_to(cache)),'mode':stat.S_IMODE(p.stat().st_mode),'kind':'directory' if p.is_dir() else 'file'}
            if p.is_file():row.update(bytes=p.stat().st_size,sha256=sha256_file(p))
            rows.append(row)
    return rows


def descriptor(cache, offline_result):
    """Seal independently validated public dependencies; freeze returned hash in config."""
    cache=Path(cache).resolve();offline_result=Path(offline_result).resolve()
    manifest=json.loads((cache/'manifest.json').read_text());proof=json.loads(offline_result.read_text())
    if proof.get('passed') is not True or proof.get('external_network_disabled') is not True or proof.get('pre_oracle_output_absence_verified') is not True:
        raise ValueError('Successful unchanged offline oracle proof required')
    if proof['npm_globalconfig_path']!='/usr/local/etc/npmrc' or proof['source_dependency_manifest_sha256']!=sha256_file(cache/'manifest.json'):
        raise ValueError('Offline proof binding differs')
    actual=[r for r in files_manifest(cache) if r['path'].split('/')[0] in DIRECTORIES]
    if actual!=manifest['files']:raise ValueError('Captured dependency bytes changed')
    verify_lock_cache(json.loads((cache/'lock/package-lock.json').read_text()),cache/'npm-cache')
    return {'schema':'neutral-stock-cache-v1','cache_manifest_sha256':sha256_file(cache/'manifest.json'),
            'offline_proof_sha256':sha256_file(offline_result),'npm_globalconfig_path':proof['npm_globalconfig_path'],
            'npmrc':NPMRC,'files':bound_files(cache),'reference_outputs_included':False}


def apply_staged_cache(environment, cache, binding, expected_binding_sha256):
    """Return Dockerfile block to insert AFTER pinned Node, BEFORE selected skill COPY.

    The caller binds this descriptor/hash in runtime config for every condition.
    No shared solved/base image is substituted for the instance's Dockerfile.
    """
    environment=Path(environment).resolve();cache=Path(cache).resolve()
    if not environment.is_dir() or environment.name!='environment' or environment.is_relative_to(ROOT/'milestone 1'):
        raise ValueError('An existing staged environment outside historical data is required')
    if canonical_hash(binding)!=expected_binding_sha256 or binding.get('schema')!='neutral-stock-cache-v1':
        raise ValueError('Frozen neutral cache binding differs')
    if binding.get('npm_globalconfig_path')!='/usr/local/etc/npmrc' or binding.get('npmrc')!=NPMRC or binding.get('reference_outputs_included') is not False:
        raise ValueError('Unreviewed global npm configuration')
    if sha256_file(cache/'manifest.json')!=binding['cache_manifest_sha256'] or bound_files(cache)!=binding['files']:
        raise ValueError('Neutral cache bytes or modes changed')
    target=environment/'capstudy-neutral-dependencies'
    if target.exists() or target.is_symlink():raise ValueError('Fresh neutral cache staging path required')
    target.mkdir()
    for name in DIRECTORIES:shutil.copytree(cache/name,target/name,copy_function=shutil.copy2)
    if bound_files(target)!=binding['files'] or bound_files(cache)!=binding['files']:
        raise ValueError('Neutral cache changed during staging')
    (target/'npmrc').write_text(NPMRC);(target/'npmrc').chmod(0o644)
    return ('COPY capstudy-neutral-dependencies/npm-cache/ /opt/capstudy-dependencies/npm-cache/\n'
            'COPY capstudy-neutral-dependencies/lock/ /opt/capstudy-dependencies/d3-6.7.0/\n'
            'COPY capstudy-neutral-dependencies/vendor/ /opt/capstudy-dependencies/d3-6.7.0/vendor/\n'
            'COPY capstudy-neutral-dependencies/npmrc /usr/local/etc/npmrc\n')
