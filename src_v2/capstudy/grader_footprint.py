"""Measure native GDP APT footprint on the actual Docker architecture.

Default prints the no-model plan. --execute pulls Ubuntu and metadata only;
APT explicitly declines package installation. It never receives credentials.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import uuid
from .contracts import write_json_atomic
from .grader_preflight import plan, monitored_process, normalize_architecture
from .runner import sha256_file

PACKAGES = ['python3', 'python3-pip', 'python3-venv', 'libreoffice', 'gnumeric']


def parse_sizes(text):
    def size(pattern):
        match = re.search(pattern, text)
        if not match:
            raise ValueError('APT no-install output lacks a complete size estimate')
        amount = float(match.group(1).replace(',', ''))
        return int(amount * {'B':1, 'kB':1000, 'MB':1000**2, 'GB':1000**3}[match.group(2)])
    if not re.search(r'\[Y/n\]\s*N\s*\nAbort\.', text):
        raise ValueError('APT output does not confirm declined installation')
    return {'apt_archive_bytes': size(r'Need to get ([0-9,.]+) (B|kB|MB|GB) of archives'),
            'apt_additional_installed_bytes': size(r'After this operation, ([0-9,.]+) (B|kB|MB|GB) of additional disk space')}


def measure(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    reserve = 6 * 1024**3
    metadata = plan()
    pull = monitored_process(['docker','pull','ubuntu:24.04'], output/'pull', reserve_bytes=reserve, timeout=300)
    if pull.returncode:
        raise RuntimeError('Ubuntu base pull failed')
    raw = subprocess.run(['docker','image','inspect','ubuntu:24.04'], capture_output=True, text=True, check=True)
    image = json.loads(raw.stdout)[0]
    info = json.loads(subprocess.run(['docker','info','--format','{{json .}}'],capture_output=True,text=True,check=True).stdout)
    architecture = normalize_architecture(image['Architecture'])
    if architecture != normalize_architecture(info['Architecture']) or info.get('OSType') != 'linux':
        raise ValueError('Native Linux architecture mismatch')
    name = 'capstudy-gdp-footprint-' + uuid.uuid4().hex[:12]
    command = ['docker','run','--rm','--name',name,'--cpus','1','--memory','1g','ubuntu:24.04',
               'bash','-c','export LC_ALL=C; apt-get update && apt-get --assume-no install ' + ' '.join(PACKAGES)]
    try:
        result = monitored_process(command,output/'apt',reserve_bytes=reserve,timeout=600,container_name=name)
        # APT exits 1 after the explicit no answer; parse its affirmative abort evidence.
        if result.returncode != 1:
            raise ValueError('Unexpected APT no-install return code')
        log = output/'apt/process-stdout.txt'
        record = {'evidence_kind':'apt_no_install_size_measurement',
                  'native_dockerfile_sha256':metadata['native_dockerfile_sha256'],
                  'architecture':architecture,'base_image_id':image['Id'],
                  'base_repo_digests':image['RepoDigests'],'base_size_bytes':image['Size'],
                  'apt_log_path':str(log),'apt_log_sha256':sha256_file(log),
                  'packages':PACKAGES, 'command':command,
                  'office_packages_installed':False,'executed_model':False,**parse_sizes(log.read_text())}
        write_json_atomic(output/'measurement.json',record)
        return record
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    if args.execute and args.output is None:parser.error('--execute requires a new --output directory')
    result=measure(args.output) if args.execute else {'executed':False,'executed_model':False,
        'base_image':'ubuntu:24.04','packages':PACKAGES,'apt_install_mode':'--assume-no',
        'required_architecture':'match actual native Linux Docker server','reserve_bytes':6*1024**3}
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
