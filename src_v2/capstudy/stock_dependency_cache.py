"""Prospective neutral D3 cache and unchanged offline-oracle validation.

Only public npm dependency artifacts enter the overlay; reference outputs and
solutions never enter it. All conditions would receive identical dependency
bytes. This is model-free preparation, not a formal runtime freeze.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

from .contracts import write_json_atomic, canonical_hash
from .g2_preflight import RESERVE
from .grader_preflight import monitored_process
from .runner import ROOT, sha256_file

CLEAN_ENV=['-i','HOME=/tmp/capstudy-solver-home',
           'PATH=/usr/local/bin:/usr/bin:/bin','LANG=C.UTF-8','LC_ALL=C.UTF-8']


def inspect(reference, expected):
    value=json.loads(subprocess.check_output(['docker','image','inspect',reference],timeout=30))[0]
    if value['Id']!=expected:raise ValueError('Bound image identity changed')
    return value


def verify_lock_cache(lock, cache):
    rows=[]
    for name,entry in sorted(lock['packages'].items()):
        if not name:continue
        integrity=entry.get('integrity','')
        if not integrity.startswith('sha512-') or not entry.get('resolved','').startswith('https://registry.npmjs.org/'):
            raise ValueError('Explicit public registry SHA-512 package integrity required')
        digest=base64.b64decode(integrity.removeprefix('sha512-'),validate=True)
        if len(digest)!=64:raise ValueError('SHA-512 integrity length mismatch')
        hexed=digest.hex();path=cache/'_cacache/content-v2/sha512'/hexed[:2]/hexed[2:4]/hexed[4:]
        if path.is_symlink() or not path.is_file() or hashlib.sha512(path.read_bytes()).digest()!=digest:
            raise ValueError('npm cached tarball integrity mismatch: '+name)
        rows.append({'package_path':name,'version':entry['version'],'integrity':integrity,
                     'resolved':entry['resolved'],'cached_tarball_sha256':sha256_file(path)})
    if not rows or lock['packages']['node_modules/d3']['version']!='6.7.0':
        raise ValueError('Native D3 6.7.0 dependency resolution required')
    return rows


def files_manifest(root):
    rows=[]
    for p in sorted(root.rglob('*')):
        if p.is_symlink():raise ValueError('Dependency cache contains a symlink')
        if p.is_file():rows.append({'path':str(p.relative_to(root)),'bytes':p.stat().st_size,'sha256':sha256_file(p)})
    return rows


def capture(native_result, output):
    native_result=Path(native_result).resolve();output=Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT/'private_artifacts'):
        raise ValueError('New private capture directory required')
    prior=json.loads(native_result.read_text())
    if prior['family']!='stock-data-visualization' or not prior['native_reference_and_incomplete_verified']:
        raise ValueError('Verified native stock reference required')
    source=inspect(prior['checkpoint_image_tag'],prior['checkpoint_image']['Id'])
    output.mkdir(parents=True);(output/'npm-cache').mkdir();(output/'lock').mkdir();(output/'vendor').mkdir()
    name='capstudy-stock-dependency-capture'
    subprocess.run(['docker','create','--name',name,'--network','none','--entrypoint','true',source['Id']],
                   check=True,capture_output=True,timeout=30)
    allowed=[('/root/.npm/_cacache','npm-cache/_cacache'),('/tmp/package.json','lock/package.json'),
             ('/tmp/package-lock.json','lock/package-lock.json'),
             ('/tmp/node_modules/d3/dist/d3.min.js','vendor/d3.min.js'),
             ('/tmp/node_modules/d3/LICENSE','vendor/LICENSE'),
             ('/tmp/node_modules/d3/package.json','vendor/package.json')]
    try:
        for path,relative in allowed:
            subprocess.run(['docker','cp',name+':'+path,str(output/relative)],check=True,capture_output=True,timeout=90)
    finally:subprocess.run(['docker','rm',name],check=True,capture_output=True,timeout=30)
    lock=json.loads((output/'lock/package-lock.json').read_text())
    packages=verify_lock_cache(lock,output/'npm-cache')
    vendor=json.loads((output/'vendor/package.json').read_text())
    if vendor['name']!='d3' or vendor['version']!='6.7.0':raise ValueError('D3 package identity differs')
    files=files_manifest(output)
    record={'kind':'neutral_public_dependency_cache','executed_model':False,'provider_requests':0,
            'native_result_sha256':sha256_file(native_result),'source_checkpoint_image_id':source['Id'],
            'native_base_image_id':prior['base_image']['Id'],'package':'d3','version':'6.7.0',
            'native_oracle_sha256':prior['source_hashes']['solution/solve.sh'],
            'lock_sha256':sha256_file(output/'lock/package-lock.json'),
            'bundled_d3_sha256':sha256_file(output/'vendor/d3.min.js'),'packages':packages,'files':files,
            'reference_outputs_copied':False,'solution_or_grader_copied':False,
            'source_allowlist':[path for path,_ in allowed]}
    write_json_atomic(output/'manifest.json',record);return record


def offline(runtime_result, capture_path, output):
    runtime_result=Path(runtime_result).resolve();capture_path=Path(capture_path).resolve();output=Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT/'private_artifacts') or not output.name.replace('-','').isalnum():
        raise ValueError('New private offline validation directory required')
    runtime=json.loads(runtime_result.read_text());cache=json.loads((capture_path/'manifest.json').read_text())
    if not runtime['passed'] or runtime['base_image_id']!=cache['native_base_image_id']:
        raise ValueError('Pinned CLI overlay must derive from the original native input base, not reference output')
    inspect(runtime['image_tag'],runtime['image_id'])
    for row in cache['files']:
        if sha256_file(capture_path/row['path'])!=row['sha256']:raise ValueError('Dependency capture changed')
    verify_lock_cache(json.loads((capture_path/'lock/package-lock.json').read_text()),capture_path/'npm-cache')
    output.mkdir(parents=True);context=output/'context';context.mkdir()
    for directory in ('npm-cache','lock','vendor'):shutil.copytree(capture_path/directory,context/directory)
    if files_manifest(context)!=cache['files']:
        raise ValueError('Post-copy context differs from captured dependency manifest')
    for row in cache['files']:
        if sha256_file(capture_path/row['path'])!=row['sha256']:raise ValueError('Dependency source changed during copy')
    config_probe=monitored_process(['docker','run','--rm','--network','none','--cpus','1','--memory','256m',
        '--read-only','--tmpfs','/tmp:rw,nosuid,size=64m','--entrypoint','env',runtime['image_id'],*CLEAN_ENV,
        'sh','-c','mkdir -p "$HOME" && npm config get globalconfig'],output/'globalconfig-probe',reserve_bytes=RESERVE,timeout=60)
    globalconfig=(output/'globalconfig-probe/process-stdout.txt').read_text().strip()
    if config_probe.returncode or globalconfig!='/usr/local/etc/npmrc':
        raise ValueError('Unexpected pinned npm global configuration location')
    (context/'npmrc').write_text('cache=/opt/capstudy-dependencies/npm-cache\noffline=true\naudit=false\nfund=false\nupdate-notifier=false\n')
    (context/'Dockerfile').write_text('FROM '+runtime['image_id']+'\n'
        'COPY npm-cache/ /opt/capstudy-dependencies/npm-cache/\n'
        'COPY lock/ /opt/capstudy-dependencies/d3-6.7.0/\n'
        'COPY vendor/ /opt/capstudy-dependencies/d3-6.7.0/vendor/\n'
        'COPY npmrc '+globalconfig+'\n')
    tag='capstudy-stock-offline:'+output.name
    record={'kind':'prospective_shared_dependency_overlay','executed_model':False,'provider_requests':0,
            'source_runtime_result_sha256':sha256_file(runtime_result),
            'source_dependency_manifest_sha256':sha256_file(capture_path/'manifest.json'),
            'runtime_image_id':runtime['image_id'],'image_tag':tag,'passed':False,
            'npm_globalconfig_path':globalconfig,'npm_globalconfig_sha256':sha256_file(context/'npmrc'),
            'postcopy_dependency_manifest_sha256':canonical_hash(cache['files']),
            'clean_environment':CLEAN_ENV,'dockerfile_sha256':sha256_file(context/'Dockerfile'),
            'neutral_dependency_path':'/opt/capstudy-dependencies','output_prepopulation_prohibited':True,
            'pre_oracle_output_absence_verified':False,'formal_runtime_freeze_claimed':False,
            'native_offline_grader_pass_claimed':False,'started_unix':time.time()}
    write_json_atomic(output/'pending.json',record);container=None
    try:
        build=monitored_process(['docker','build','--memory','4g','--memory-swap','4g',
            '--cpu-period','100000','--cpu-quota','100000','-t',tag,str(context)],output/'build',reserve_bytes=RESERVE,timeout=180)
        if build.returncode:raise RuntimeError('Neutral dependency overlay build failed')
        image=json.loads(subprocess.check_output(['docker','image','inspect',tag],timeout=30))[0]
        record['image_id']=image['Id'];task=ROOT/'milestone 1/data/train/tasks/stock-data-visualization/stock-data-visualization-1'
        record['unchanged_native_oracle_sha256']=sha256_file(task/'solution/solve.sh')
        if record['unchanged_native_oracle_sha256']!=cache['native_oracle_sha256']:
            raise RuntimeError('Native oracle changed after dependency capture')
        shell='mkdir -p "$HOME" && test ! -e /root/output && bash /solution/solve.sh && npm list --prefix /tmp --all --json && sha256sum /root/output/js/d3.v6.min.js'
        container='capstudy-'+output.name+'-oracle'
        process=monitored_process(['docker','run','--name',container,'--network','none','--cpus','1','--memory','4g',
            '--security-opt','no-new-privileges','--entrypoint','env','-v',str(task/'solution')+':/solution:ro',
            image['Id'],*CLEAN_ENV,'bash','-c',shell],output/'oracle',reserve_bytes=RESERVE,timeout=180,container_name=container)
        record['oracle_exit']=process.returncode
        if process.returncode:raise RuntimeError('Unchanged native oracle failed with external network disabled')
        subprocess.run(['docker','cp',container+':/root/output/js/d3.v6.min.js',str(output/'generated-d3.min.js')],
                       check=True,capture_output=True,timeout=30)
        record['generated_d3_sha256']=sha256_file(output/'generated-d3.min.js')
        if record['generated_d3_sha256']!=cache['bundled_d3_sha256']:
            raise RuntimeError('Offline generated library differs from native dependency bytes')
        checkpoint='capstudy-stock-offline-reference:'+output.name
        subprocess.run(['docker','commit',container,checkpoint],check=True,capture_output=True,timeout=120)
        record['evaluator_only_reference_image_id']=json.loads(subprocess.check_output(['docker','image','inspect',checkpoint],timeout=30))[0]['Id']
        record['evaluator_only_reference_tag']=checkpoint
        record.update(passed=True,external_network_disabled=True,unchanged_oracle_executed=True,
                      pre_oracle_output_absence_verified=True)
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        record.update(error_category=type(exc).__name__,error_reason=str(exc))
    finally:
        if container:subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=30)
        record['finished_unix']=time.time();write_json_atomic(output/'result.json',record)
    return record


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['capture','offline'])
    p.add_argument('--native-result',type=Path);p.add_argument('--runtime-result',type=Path)
    p.add_argument('--capture',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--execute',action='store_true');a=p.parse_args()
    if not a.execute:print(json.dumps({'executed':False,'executed_model':False,'action':a.action}));return
    if a.action=='capture':
        r=capture(a.native_result,a.output);print(json.dumps({'captured_packages':len(r['packages']),'reference_outputs_copied':False}))
    else:
        r=offline(a.runtime_result,a.capture,a.output);print(json.dumps({'passed':r['passed'],'error':r.get('error_reason')}))


if __name__=='__main__':main()
