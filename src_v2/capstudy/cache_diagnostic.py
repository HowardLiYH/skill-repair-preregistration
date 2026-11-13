"""Model-free cache diagnostic for the completed, retained HEAD pilot input.

Rebuilds exact staged inputs and selected skills, never a solver snapshot. This
is an observational diagnostic on shared Docker cache, not a randomized speed
comparison. It cannot skip instance-specific COPY validation.
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
from .runner import ROOT, tree_hash, sha256_file


def execute(output, explicit_cache=True):
    output=Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT/'private_artifacts') or not re.fullmatch(r'[a-z0-9-]+',output.name):
        raise ValueError('New private diagnostic directory required')
    reserve=30*1024**3
    if shutil.disk_usage(output.parent).free < reserve+10*1024**3:
        raise ValueError('Insufficient guarded build allowance')
    pilot=ROOT/'private_artifacts/gdp-solver-pilot'
    job=pilot/'jobs/gdp-head-loading-v2'
    trial=json.loads((job/'completed.json').read_text())
    pin=json.loads((pilot/'cache-pin-gdp-head-loading-v2.json').read_text())
    if trial['trial_id']!='gdp-head-loading-v2' or not trial['executed']:
        raise ValueError('Completed approved pilot required')
    selected=job/'selected_bundle'
    if tree_hash(selected)!=trial['actual_bundle_hash']:
        raise ValueError('Selected bundle changed after trial')
    source=job/'stage/tasks/weighted-gdp-calculation/weighted-gdp-calculation-1/environment'
    if sha256_file(source/'Dockerfile')!=pin['dockerfile_sha256']:
        raise ValueError('Staged recipe differs from retained original input image')
    if pin['image_kind']!='original built input image, not solver snapshot':
        raise ValueError('Only original build cache permitted')
    cached=json.loads(subprocess.check_output(['docker','image','inspect',pin['cache_tag']],timeout=30))[0]
    if cached['Id']!=pin['image_id']:
        raise ValueError('Retained cache image identity changed')
    output.mkdir(parents=True)
    context=output/'context'
    shutil.copytree(source,context,ignore=shutil.ignore_patterns('skills'))
    shutil.copytree(selected,context/'skills')
    if tree_hash(context/'skills')!=trial['actual_bundle_hash']:
        raise ValueError('Context selected skills differ')
    record={'evidence_kind':'model_free_cache_diagnostic','executed_model':False,'provider_requests':0,
            'trial_input':'gdp-head-loading-v2','cache_source':pin,'explicit_cache_from':explicit_cache,
            'controller_sha256':sha256_file(Path(__file__)),'context_sha256':tree_hash(context),
            'selected_bundle_sha256':trial['actual_bundle_hash'],'started_unix':time.time(),
            'shared_mutable_cache':True,'causal_speed_comparison':False,
            'note':'Any later build may benefit from cache warmed by this run; instance COPY steps still execute or validate cache keys.'}
    tag='capstudy-cache-diagnostic:'+output.name
    command=['docker','build','--memory','4g','--memory-swap','4g','--cpu-period','100000','--cpu-quota','100000']
    if explicit_cache:command+=['--cache-from',pin['cache_tag']]
    command+=['-t',tag,str(context)]
    write_json_atomic(output/'pending.json',record)
    try:
        result=monitored_process(command,output/'build',reserve_bytes=reserve,timeout=900)
        text=(output/'build/process-stdout.txt').read_text(errors='replace')
        record.update(exit_code=result.returncode,cache_hit_lines=len(re.findall(r'---> Using cache',text)),
                      step_count=len(re.findall(r'^Step \d+/\d+ :',text,re.M)),image_tag=tag)
        if result.returncode==0:
            record['image_id']=json.loads(subprocess.check_output(['docker','image','inspect',tag],timeout=30))[0]['Id']
        record['stdout_sha256']=sha256_file(output/'build/process-stdout.txt')
    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
        record.update(error_category=type(exc).__name__,error_reason=str(exc))
    record['finished_unix']=time.time();write_json_atomic(output/'result.json',record)
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--default-cache',action='store_true')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute:
        print(json.dumps({'executed':False,'executed_model':False,'explicit_cache_from':not args.default_cache}));return
    print(json.dumps(execute(args.output,not args.default_cache)))


if __name__=='__main__':main()
