"""Model-free pinned Node/CLI compatibility check on an existing native image.

Builds a separate image using the same pinned runtime layer as GDP. No native
input image is modified. The smoke container has no network or credentials.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from .contracts import write_json_atomic
from .grader_preflight import monitored_process
from .runner import ROOT, sha256_file

RESERVE = 30 * 1024**3


def smoke_command(image, name):
    return ['docker','run','--rm','--name',name,'--network','none','--cpus','1','--memory','1g',
            '--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=128m','--cap-drop','ALL',
            '--security-opt','no-new-privileges','--entrypoint','env',image,'-i',
            'PATH=/usr/local/bin:/usr/bin:/bin','HOME=/tmp/capstudy-version-smoke',
            'DISABLE_AUTOUPDATER=1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1',
            'bash','-c','node --version && /usr/local/bin/claude --version && /usr/local/bin/claude --help']


def execute(base_image, output):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:/@-]*',base_image):
        raise ValueError('Explicit native image reference required')
    output = Path(output).resolve()
    if output.exists() or not re.fullmatch(r'[a-z0-9-]+', output.name) or not output.is_relative_to(ROOT/'private_artifacts'):
        raise ValueError('New alphanumeric output directory required')
    base = json.loads(subprocess.check_output(['docker','image','inspect',base_image],timeout=30))[0]
    if base['Architecture'] != 'amd64' or base['Os'] != 'linux':
        raise ValueError('Pinned runtime is Linux amd64 only')
    if shutil.disk_usage(output.parent).free < RESERVE + 5*1024**3:
        raise ValueError('Insufficient native overlay disk allowance')
    output.mkdir(parents=True)
    layer = ROOT/'configs/runtime/node24-claude2-amd64.layer'
    dockerfile = output/'Dockerfile'
    dockerfile.write_text('FROM '+base_image+'\n'+layer.read_text()+'\n')
    (output/'.dockerignore').write_text('*\n!Dockerfile\n')
    tag = 'capstudy-g2-runtime:'+output.name
    record = {'executed_model':False,'provider_requests':0,'kind':'native_runtime_binary_compatibility',
              'controller_sha256':sha256_file(Path(__file__)),
              'base_image_id':base['Id'],'base_image_reference':base_image,
              'runtime_layer_sha256':sha256_file(layer),'dockerfile_sha256':sha256_file(dockerfile),
              'started_unix':time.time(),'image_tag':tag,'passed':False,
              'limits':'Version/help only; model-free tool/skill protocol mock is separate.'}
    write_json_atomic(output/'pending.json',record)
    try:
        built = monitored_process(['docker','build','--memory','4g','--memory-swap','4g',
                                   '--cpu-period','100000','--cpu-quota','100000','-t',tag,str(output)],output/'build',
                                  reserve_bytes=RESERVE,timeout=600)
        if built.returncode: raise RuntimeError('Pinned runtime overlay build failed')
        actual = json.loads(subprocess.check_output(['docker','image','inspect',base_image],timeout=30))[0]
        if actual['Id'] != base['Id']: raise RuntimeError('Native base changed during overlay build')
        overlay = json.loads(subprocess.check_output(['docker','image','inspect',tag],timeout=30))[0]
        record['image_id'] = overlay['Id']
        name = 'capstudy-'+output.name
        tested = monitored_process(smoke_command(tag,name),output/'smoke',reserve_bytes=RESERVE,
                                   timeout=60,container_name=name)
        text = (output/'smoke/process-stdout.txt').read_text()
        record['exit_code'] = tested.returncode
        record['passed'] = tested.returncode == 0 and text.splitlines()[:2] == ['v24.11.1','2.1.296 (Claude Code)']
        record['stdout_sha256'] = sha256_file(output/'smoke/process-stdout.txt')
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        record.update(error_category=type(exc).__name__,error_reason=str(exc))
    record['finished_unix'] = time.time()
    write_json_atomic(output/'result.json',record)
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute:
        print(json.dumps({'executed':False,'executed_model':False,'base_image':args.image}));return
    print(json.dumps(execute(args.image,args.output)))


if __name__=='__main__':main()
