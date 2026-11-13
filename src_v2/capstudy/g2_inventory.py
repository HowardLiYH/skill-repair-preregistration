"""Static evaluator-only G2 readiness inventory. No Docker, imports of native tests,
model calls, credential reads, or reference-answer export. Train split only.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import tomllib
from .contracts import write_json_atomic

ROOT = Path(__file__).resolve().parents[2]
SPECS = {
 'dbscan-parameter-tuning': dict(outputs=['/root/pareto_frontier.csv'],
   incomplete='Keep the valid CSV header but remove all result rows.',
   missing_setup_risk=True, notes=['Reference solve.sh requires its sibling at /solution/solve.py.',
   'Native tests load output CSV in a pytest fixture: deleting the CSV causes setup errors.',
   'CPU clustering/grid search can be expensive; 4 CPUs and 3600-second agent limit are declared.']),
 'earthquake-plate-calculation': dict(outputs=['/root/answer.json'],
   incomplete='Replace reference JSON with an empty valid object; preserve parseable setup.',
   missing_setup_risk=True, notes=['Native unittest setUpClass opens answer.json: absence or malformed JSON causes setup errors.']),
 'financial-analysis': dict(outputs=['/root/answers.json'],
   incomplete='Replace reference JSON with an empty valid object.',
   missing_setup_risk=False, notes=['Local SEC ZIP inputs are expanded during build; measure decompressed size.',
   'Dockerfile comment mentions LibreOffice/gnumeric, but its install command does not install them.']),
 'organize-messy-files': dict(outputs=['/root/papers/<subject folders>/*'],
   incomplete='Remove one output PDF from an otherwise complete reference tree; preserve all subject directories.',
   missing_setup_risk=False, notes=['Use the checked project PDF cache override; never download again merely because native Dockerfile contains URLs.',
   'Native reference filename set differs from calibration inputs; reconcile evaluator-only fixture before healthy-reference claim.']),
 'schedule-planning': dict(outputs=['/root/results.json','/root/<reply filenames referenced by results.json>'],
   incomplete='Use a valid results.json with an empty sent_results list; no email is sent by the grader.',
   missing_setup_risk=False, notes=['No native reference solution is present. Prepare a separately labeled evaluator-only reference fixture.',
   'solution.env names HUGGINGFACE_API_TOKEN, but no reference script exists and grader reads local files only; do not pass a credential on this basis.',
   'pdfplumber is unpinned in native Dockerfile.']),
 'stock-data-visualization': dict(outputs=['/root/output/index.html','/root/output/js/d3.v6.min.js',
   '/root/output/js/visualization.js','/root/output/css/style.css','/root/output/data/stock-descriptions.csv',
   '/root/output/data/indiv-stock/*.csv'],
   incomplete='Preserve a working reference page and JS/data, then remove only css/style.css; verify browser setup still completes.',
   missing_setup_risk=True, notes=['Missing index.html causes browser fixture setup errors, not a clean scientific task failure.',
   'Reference requires npm d3@6.7.0; cache/package it before isolated reference execution.',
   'Playwright, Chromium build, pytest and pytest-json-ctrf are unpinned in native install commands.',
   'Grader launches CPU headless Chromium and localhost:8765 HTTP server; loopback is required even with external egress blocked.']),
 'temperature-simulation': dict(outputs=['/root/glm3.nml','/root/output/output.nc','/root/metrics.json'],
   incomplete='Keep a working reference configuration and omit metrics.json. Do not use output.nc deletion alone: the first test regenerates it.',
   missing_setup_risk=False, notes=['No native reference solution is present. A real passing calibration fixture requires measured GLM results, not invented metrics.',
   'Native Dockerfile explicitly targets linux/amd64; bundled glm ELF is x86-64 and uses CPU NetCDF/HDF5/Fortran libraries.',
   'Grader reruns glm; native subprocess has no inner timeout, so enforce the outer verifier timeout.',
   'Native grader emits pytest text, not CTRF; pytest is unpinned.']),
 'weighted-gdp-calculation': dict(outputs=['/root/gdp.xlsx'],
   incomplete='Clear output cells H35:L40 in a valid reference workbook, preserving workbook structure (existing GDP preflight control).',
   missing_setup_risk=False, notes=['In-place workbook output: an untouched-input control differs from deleting the input workbook.',
   'Grader uses ssconvert to write sheet.csv.* without overwriting the workbook.',
   'Column-width tests can skip on conversion artifacts; strict health classification must review skips.']),
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def static_tests(path):
    """Count static parametrized cases without evaluating or importing test code."""
    tree = ast.parse(Path(path).read_text())
    bindings = {target.id: node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}
    def cardinality(node):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)): return len(node.elts)
        if isinstance(node, ast.Name) and node.id in bindings: return cardinality(bindings[node.id])
        return None
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith('test_'): continue
        count = 1
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == 'parametrize':
                size = cardinality(decorator.args[1]) if len(decorator.args) > 1 else None
                count = count * size if count is not None and size is not None else None
        rows.append({'name': node.name, 'line': node.lineno, 'static_case_count': count})
    return {'method': 'AST only; not pytest collection or execution', 'test_function_count': len(rows),
            'static_case_count': sum(x['static_case_count'] for x in rows) if all(x['static_case_count'] is not None for x in rows) else None,
            'tests': rows}


def pdf_names(path):
    return {n.value for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Constant)
        and isinstance(n.value, str) and re.fullmatch(r'[0-9.]+(?:v\d+)?\.pdf', n.value)}


def plan(data_root=None):
    data = Path(data_root) if data_root else ROOT / 'milestone 1/data'
    instances = [json.loads(line) for line in (data / 'train/instances.jsonl').read_text().splitlines() if line.strip()]
    if len(instances) != 8 or {row['family'] for row in instances} != set(SPECS):
        raise ValueError('Expected exactly the eight pinned calibration families')
    rows = []
    for instance in sorted(instances, key=lambda row: row['family']):
        if instance['split'] != 'train': raise ValueError('Only calibration/train may be inventoried')
        task = data / instance['path']; family = instance['family']; spec = SPECS[family]
        cfg = tomllib.loads((task / 'task.toml').read_text())
        dockerfile = (task / 'environment/Dockerfile').read_text()
        script = (task / 'tests/test.sh').read_text()
        reference = sorted(p for p in (task / 'solution').rglob('*') if p.is_file())
        inputs = [p for p in sorted((task / 'environment').rglob('*')) if p.is_file()
                  and p.name != 'Dockerfile' and 'skills' not in p.relative_to(task / 'environment').parts]
        environment = cfg.get('environment', {})
        native_files = [task/'task.toml',task/'environment/Dockerfile',task/'tests/test.sh',task/'tests/test_outputs.py']
        grading_network = bool(re.search(r'\b(?:apt-get|pip3?|uvx|curl)\b', script))
        item = {'family': family, 'instance_id': instance['instance_id'], 'split': 'train',
            'task_path': instance['path'], 'source_files': {str(p.relative_to(data)): digest(p) for p in native_files},
            'declared_resources': {k: environment.get(k) for k in ('cpus','memory_mb','storage_mb','gpus','allow_internet')},
            'declared_limits_seconds': {'build': environment.get('build_timeout_sec'),
                'agent': cfg.get('agent',{}).get('timeout_sec'), 'grader': cfg.get('verifier',{}).get('timeout_sec')},
            'cpu_gpu_assessment': {'gpu_explicitly_required': bool(environment.get('gpus',0)),
                'gpu_dependency_found_in_reviewed_dockerfile': bool(re.search(r'cuda|cudnn|nvidia|--gpus',dockerfile,re.I)),
                'inference': 'CPU native task/grader dependencies; absence of GPU declaration is not an explicit zero.'},
            'environment_names_only': {'task_required_env': environment.get('required_env',[]),
                'verifier_env': sorted(cfg.get('verifier',{}).get('env',{})),
                'solution_env': sorted(cfg.get('solution',{}).get('env',{})),
                'grader_optional_path_overrides': ['WORKING_DIR','RESULTS_PATH'] if family=='schedule-planning' else []},
            'native_base': next(line for line in dockerfile.splitlines() if line.startswith('FROM ')),
            'working_directory': '/root',
            'input_files': [{'path': str(p.relative_to(task/'environment')), 'bytes':p.stat().st_size,'sha256':digest(p)} for p in inputs],
            'input_bytes_before_build_expansion': sum(p.stat().st_size for p in inputs),
            'native_build_dependencies': [line.strip() for line in dockerfile.splitlines()
                if re.search(r'\b(?:apt-get|pip3?|playwright install)\b',line)],
            'pinned_python_packages_in_native_sources': sorted(set(re.findall(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.-]+',dockerfile+'\n'+script))),
            'network_dependencies': {'native_build_package_fetch': True, 'native_grader_dependency_fetch': grading_network,
                'native_reference_dependency_fetch': family=='stock-data-visualization',
                'external_task_data_fetch_during_solve_found': False,
                'solver_external_egress_needed_beyond_model': 'not found by static review; validate in isolated run',
                'build_data_cache': None, 'package_cache_prepared_and_verified': False},
            'grader': {'entrypoint':'bash /tests/test.sh','native_script':script,
                'evaluator_assets': [{'path': str(p.relative_to(task)), 'bytes': p.stat().st_size, 'sha256': digest(p)}
                    for p in sorted((task/'tests').rglob('*')) if p.is_file()],
                'reports':['/logs/verifier/reward.txt','/logs/verifier/ctrf.json','/logs/verifier/process-stdout.txt']
                    if 'ctrf' in script else ['/logs/verifier/reward.txt','/logs/verifier/test-stdout.txt'],
                'collection':static_tests(task/'tests/test_outputs.py')},
            'reference': {'present': bool(reference), 'entrypoint':'bash /solution/solve.sh' if reference else None,
                'mount_path':'/solution:ro','evaluator_only':True,
                'files':[{'path':str(p.relative_to(task)), 'sha256':digest(p)} for p in reference],
                'healthy_reference_verified_by_this_inventory': False},
            'output_artifacts':spec['outputs'],
            'controls': {'untouched_input':'Run separately, record actual health classification; never assume it is a healthy zero.',
                'literal_missing_output_may_cause_setup_error':spec['missing_setup_risk'],
                'parseable_incomplete_output_proposal':spec['incomplete'],
                'proposal_executed':False,'reference_expected_status':'pass',
                'incomplete_expected_status':'task_fail only after all tests complete without setup/collection errors'},
            'notes':spec['notes'], 'g2_status':'not_established_by_static_inventory'}
        if family == 'organize-messy-files':
            manifest_path=data/'external/pdf_manifest.json'; manifest=json.loads(manifest_path.read_text())
            selected=[r for r in manifest['files'] if instance['instance_id'] in r['instances']]
            validated=all(r['status']=='validated' and (data/r['path']).is_file() and digest(data/r['path'])==r['sha256'] for r in selected)
            expected=pdf_names(task/'tests/test_outputs.py'); oracle=pdf_names(task/'solution/solution.py')
            cached={Path(r['path']).name for r in selected}
            item['network_dependencies']['build_data_cache']={'manifest_path':str(manifest_path.relative_to(data)),
                'manifest_sha256':digest(manifest_path),'selected_count':len(selected),'selected_hashes_verified':validated,
                'selected_bytes':sum((data/r['path']).stat().st_size for r in selected),
                'selected_names_match_native_tests':cached==expected,
                'stage_override':'stage_skilllearnbench.py replaces the download RUN block with COPY __project_pdfs/',
                'arxiv_network_needed_with_validated_stage':False}
            item['reference']['filename_mismatch']={'expected_pdf_count':len(expected),'reference_pdf_count':len(oracle),
                'reference_pdfs_absent_from_selected_inputs':len(oracle-cached),
                'expected_pdfs_absent_from_reference':len(expected-oracle),
                'prediction':'unmodified native reference raises FileNotFoundError; requires evaluator-only reconciliation'}
        rows.append(item)
    return {'schema_version':1,'evidence_kind':'static_g2_evaluator_readiness_inventory',
        'executed':False,'executed_model':False,'docker_invoked':False,'credentials_read':False,
        'reference_answers_exported':False,'scope':'eight train/calibration instances only',
        'g2_pass_claimed':False,'formal_calibration_started':False,
        'health_contract_proposal': {'implemented':False,'native_tests_or_rewards_modified':False,
            'problem':'DBSCAN/earthquake/stock can raise setup errors solely because required solver output is absent; blanket infra_error classification can mislabel genuine task failure.',
            'required_controls':['healthy_reference','literal_missing_required_output','parseable_incomplete_output','dependency_or_runtime_failure'],
            'conditional_normalization':'Only a future calibrated source-bound contract may label known missing required output task_fail; current classifier remains unchanged.',
            'admission_conditions':['Missing path is an explicit required output in this inventory, not an input, dependency, evaluator asset or arbitrary path.',
                'Absent output is independently observed in the final solver snapshot before grader execution.',
                'Native reward is zero; collected test identities and every error traceback match the same pinned model-free missing-output control.',
                'Healthy reference passes and separate dependency/runtime controls remain infra_error.',
                'No additional errors, unexpected skips, timeouts, report truncation or unknown traceback are present.'],
            'evidence_preservation':'Retain original native errors/reward, unavailable dependent checks, output-presence manifest and calibration-contract hash; never describe setup-blocked checks as executed/passed.'},
        'global_notes':['Static case counts require confirmation from completed native grading.',
            'Keep reference solutions, expected outputs and diagnostic controls evaluator-only; never provide them to editors/solvers.',
            'Prepared caches/runtime overlays must be versioned and applied identically to all conditions.',
            'Image sizes, expanded inputs and peak memory/disk remain unmeasured here; admit sequential builds with monitored headroom.'],
        'recommended_preflight_order':['weighted-gdp-calculation','earthquake-plate-calculation','financial-analysis',
            'dbscan-parameter-tuning','stock-data-visualization','organize-messy-files','schedule-planning','temperature-simulation'],
        'families':rows}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    result=plan();write_json_atomic(args.output,result)
    print(json.dumps({'executed':False,'families':len(result['families']),'output':str(args.output)},indent=2))

if __name__=='__main__': main()
