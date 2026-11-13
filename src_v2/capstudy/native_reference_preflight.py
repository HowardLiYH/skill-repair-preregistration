"""Model-free calibration grader controls for four native-reference families.

Expected CSV/JSON artifacts are evaluator-only positive fixtures, never solver
inputs. Literal missing-output errors remain raw infrastructure classifications
until a separately reviewed source-bound normalization contract exists.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import tomllib
from .contracts import write_json_atomic, sha256_file
from .grader_preflight import monitored_process
from .g2_preflight import RESERVE, control_expectation
from .runner import ROOT, classify_native

FAMILIES = ('dbscan-parameter-tuning', 'earthquake-plate-calculation',
            'financial-analysis', 'stock-data-visualization')
OUTPUT = dict(zip(FAMILIES, ('/root/pareto_frontier.csv', '/root/answer.json',
                            '/root/answers.json', '/root/output/index.html')))
MODES = ('healthy_reference', 'incomplete_output', 'missing_required_output',
         'dependency_failure', 'untouched_input')


def generation_command(family):
    if family not in FAMILIES: raise ValueError('Unsupported native reference family')
    if family == 'dbscan-parameter-tuning':
        return 'cp /tests/expected_frontier.csv /root/pareto_frontier.csv'
    if family == 'financial-analysis':
        return 'cp /tests/expected_output.json /root/answers.json'
    return 'bash /solution/solve.sh'


def fault_command(family, mode):
    if family not in FAMILIES or mode not in MODES: raise ValueError('Unknown control')
    if mode == 'missing_required_output':
        code = 'from pathlib import Path; Path(' + repr(OUTPUT[family]) + ').unlink()'
    elif mode == 'incomplete_output':
        if family == 'dbscan-parameter-tuning':
            code = "from pathlib import Path; p=Path('/root/pareto_frontier.csv'); p.write_text(p.read_text().splitlines()[0]+'\\n')"
        elif family in ('earthquake-plate-calculation', 'financial-analysis'):
            code = 'from pathlib import Path; Path(' + repr(OUTPUT[family]) + ").write_text('{}\\n')"
        else:
            code = "from pathlib import Path; p=Path('/root/output/js/visualization.js'); p.write_text(p.read_text()+\"\\ndocument.getElementById('legend').remove();\\n\")"
    else: return 'true'
    return 'python3 -c ' + shlex.quote(code)


def output_probe(family):
    return 'python3 -c ' + shlex.quote(
        "from pathlib import Path; import json,hashlib; p=Path(" + repr(OUTPUT[family]) + "); "
        "print('CAPSTUDY_OUTPUT_STATE='+json.dumps({'path':str(p),'exists':p.is_file(),"
        "'sha256':hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None}))")


def stable_hashes(root):
    return {str(p.relative_to(root)):sha256_file(p) for p in sorted(root.rglob('*'))
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo')}


def execute(family, output):
    if family not in FAMILIES: raise ValueError('Unsupported family')
    output=Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT/'private_artifacts'):
        raise ValueError('New private evaluator-only output directory required')
    if shutil.disk_usage(output.parent).free < RESERVE+10*1024**3:
        raise ValueError('30GiB reserve plus10GiB working allowance required')
    task=ROOT/'milestone 1/data/train/tasks'/family/(family+'-1')
    cfg=tomllib.loads((task/'task.toml').read_text())
    suffix=output.name
    if not suffix.replace('-','').isalnum(): raise ValueError('Safe output basename required')
    output.mkdir(parents=True)
    tag='capstudy-native-reference:'+suffix; checkpoint='capstudy-native-checkpoint:'+suffix
    record={'evidence_kind':'native_grader_calibration_controls','family':family,
        'instance_id':family+'-1','split':'train','executed_model':False,
        'scientific_performance_result':False,'started_unix':time.time(),
        'source_hashes':stable_hashes(task),'base_image_tag':tag,'checkpoint_image_tag':checkpoint,
        'positive_fixture_origin':'native evaluator expected artifact' if family in
            ('dbscan-parameter-tuning','financial-analysis') else 'native reference solution',
        'controls':[],'native_reference_and_incomplete_verified':False,'all_controls_passed':False,
        'missing_output_normalization_implemented':False,'g2_passed':False}
    write_json_atomic(output/'pending.json',record)
    context=output/'build_context'
    shutil.copytree(task/'environment',context,ignore=shutil.ignore_patterns('skills','__pycache__','*.pyc'))
    (context/'skills').mkdir()
    current=None
    def command(image,name,logs,shell):
        logs.mkdir(parents=True)
        return ['docker','run','--name',name,'--cpus','1','--memory','4g',
            '--security-opt','no-new-privileges','--entrypoint','bash',
            '-v',str(task/'tests')+':/tests:ro','-v',str(task/'solution')+':/solution:ro',
            '-v',str(logs)+':/logs/verifier',image,'-lc',shell]
    def run(args,logs,timeout,name=None):
        return monitored_process(args,logs,reserve_bytes=RESERVE,timeout=timeout,container_name=name)
    try:
        built=run(['docker','build','--memory','4g','--memory-swap','4g',
            '--cpu-period','100000','--cpu-quota','100000','-t',tag,str(context)],output/'build',
            min(900,cfg['environment'].get('build_timeout_sec',900)))
        if built.returncode: raise RuntimeError('Native build failed')
        record['base_image']=json.loads(subprocess.check_output(['docker','image','inspect',tag],timeout=30))[0]
        current='capstudy-native-'+suffix+'-generation'
        logs=output/'generation'; shell='set -e; python3 -m pip freeze; '+generation_command(family)+'; '+output_probe(family)
        generated=run(command(tag,current,logs,shell),logs/'process',
            min(900,cfg['agent'].get('timeout_sec',900)),current)
        if generated.returncode: raise RuntimeError('Positive reference generation failed')
        subprocess.run(['docker','commit',current,checkpoint],check=True,capture_output=True,timeout=120)
        record['checkpoint_image']=json.loads(subprocess.check_output(['docker','image','inspect',checkpoint],timeout=30))[0]
        subprocess.run(['docker','rm',current],check=True,capture_output=True,timeout=30);current=None
        for mode in MODES:
            if mode!='healthy_reference' and not record['controls'][0]['expectation_met']:
                raise RuntimeError('Positive native grader did not pass; negative controls not admitted')
            logs=output/mode;current='capstudy-native-'+suffix+'-'+mode.replace('_','-')
            shell='set -e; '+fault_command(family,mode)+'; '+output_probe(family)+'; '
            if mode=='dependency_failure': shell+='env PYTEST_ADDOPTS=--capstudy-g2-deliberate-invalid-option '
            shell+='bash /tests/test.sh'
            process=run(command(tag if mode=='untouched_input' else checkpoint,current,logs,shell),
                logs/'process',min(1800,cfg['verifier'].get('timeout_sec',1800)),current)
            shutil.copyfile(logs/'process/process-stdout.txt',logs/'process-stdout.txt')
            shutil.copyfile(logs/'process/process-stderr.txt',logs/'process-stderr.txt')
            # Keep process files separate from native output; health discovery
            # reads the native CTRF/reward files in this exact control directory.
            health=classify_native(task,logs,process.returncode)
            text=(logs/'process/process-stdout.txt').read_text(errors='replace')+'\n'+(logs/'process/process-stderr.txt').read_text(errors='replace')
            probes=[json.loads(line.split('=',1)[1]) for line in text.splitlines() if line.startswith('CAPSTUDY_OUTPUT_STATE=')]
            row={'mode':mode,'process_exit':process.returncode,'native_health':health,'output_presence':probes,
                **control_expectation(mode,health,text)}
            record['controls'].append(row);write_json_atomic(output/'progress.json',record)
            subprocess.run(['docker','rm',current],check=True,capture_output=True,timeout=30);current=None
        record['native_reference_and_incomplete_verified']=all(r['expectation_met'] for r in record['controls'][:2])
        record['all_controls_passed']=all(r['expectation_met'] for r in record['controls'])
        record['final_source_hashes']=stable_hashes(task)
        if record['source_hashes']!=record['final_source_hashes']:
            raise RuntimeError('Native source changed during reference controls')
        record['completed_unix']=time.time();write_json_atomic(output/'completed.json',record)
        return record
    except BaseException as exc:
        record['error_type']=type(exc).__name__;record['error']=str(exc)
        write_json_atomic(output/'incident.json',record);raise
    finally:
        if current:subprocess.run(['docker','rm','-f',current],capture_output=True,timeout=30)
        # Preserve original native images and pre-grader checkpoint for audited
        # regrading/storage validation; no global or implicit parent pruning.


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--family',choices=FAMILIES,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--execute',action='store_true')
    a=p.parse_args(argv)
    if not a.execute:print(json.dumps({'executed':False,'family':a.family,'model_calls':0}));return 0
    r=execute(a.family,a.output)
    print(json.dumps({k:r[k] for k in ('family','native_reference_and_incomplete_verified','all_controls_passed','g2_passed')}))
    return 0

if __name__=='__main__':raise SystemExit(main())
