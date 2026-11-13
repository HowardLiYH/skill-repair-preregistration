"""Build the confirmatory study's frozen per-task setup from the screening results (Notion 0.4). Runs on the Mac.

Writes, for the families that passed screening:
  * check inventories {task: [verifier check names, as the JUnit report names them]} (frozen check lists),
  * the analysis manifest {family: {test task: number of applicable checks}},
  * the server runtime config for the confirmatory study (same executor, tools, network and limits as the pilot;
    data root = the collected new families; per-split native verifier policies with grader hashes, expected check
    counts and required output paths; output-retention lists, which may contain file patterns),
  * family splits.
No model call. The protocol hash in the verifier policies is filled in at the freeze.

  python3.11 followup/confirmatory_config.py --screening private_artifacts/followup-v1/screening/server --out private_artifacts/followup-v1/confirmatory
"""
import argparse, copy, glob, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src_v2')]
from capstudy.native_evidence import grader_hashes                 # noqa: E402

DATA = ROOT / 'followup-data'
REMOTE = '/opt/followup'
EXCLUDED = {
    'github-repo-analytics': 'technical: every task requires a GitHub token and live GitHub access (task.toml required_env GH_TOKEN); the solver has no internet access and no credentials',
    'dependency-vulnerability-check': 'overlap: both test tasks (4, 5) reword the calibration task (1): same package-lock.json, same verifier, same requested output; no test task remains',
}
# Output files the verifier reads, beyond what its constants name (reviewed from each verifier).
OUTPUTS = {'travel-planning': ['/app/output/itinerary.json'],
           'video-object-counting': ['/root/counting_results.csv']}
RETAIN_EXTRA = {'video-object-counting': ['/root/keyframes_*.png']}   # read by the verifier; kept by pattern
SCOPE = {'train': 'formal_calibration', 'dev': 'formal_dev', 'test': 'formal_test'}


def junit_name(ctrf_name):
    return ctrf_name.split('::')[-1]


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('--screening', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--inventories', help='JUnit-based check lists (followup/screen_inventory.py); required for the freeze')
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    results = {json.load(open(f))['instance']: json.load(open(f)) for f in sorted(glob.glob(f'{a.screening}/*.json'))}
    junit = {json.load(open(f))['instance']: json.load(open(f)) for f in sorted(glob.glob(f'{a.inventories}/*.json'))} if a.inventories else {}
    failed = {t: r for t, r in results.items() if not (r['build_ok'] and r['verifier_accepts_reference'] and r['untouched_fails_cleanly'])}
    if failed: raise SystemExit(f'Screening failures must be resolved or recorded first: {sorted(failed)}')
    families = sorted({r['family'] for r in results.values()} - set(EXCLUDED))
    splits, inventories, manifest, retain = {}, {}, {}, {}
    pilot = json.loads((ROOT / 'private_artifacts/followup-v1/pilot/runtime_config.json').read_text())
    policies = {}
    for split in ('train', 'dev', 'test'):
        base = copy.deepcopy(pilot['formal_policies_by_split']['dev'])
        base.update(campaign_id='followup-confirmatory-v1', scope=SCOPE[split], protocol_sha256='0' * 64, tasks={})
        policies[split] = base
    for family in families:
        tasks = {s: sorted(p.name for p in (DATA / s / 'tasks' / family).iterdir()) for s in ('train', 'dev', 'test')}
        splits[family] = {'calibration': tasks['train'], 'practice': tasks['dev'], 'test': tasks['test']}
        for split, names in tasks.items():
            for name in names:
                r = results[name]
                if a.inventories and name not in junit: raise SystemExit(f'No JUnit check list for {name}')
                inv = junit[name]['inventory'] if a.inventories else [junit_name(c) for c in r['inventory']]
                if len(set(inv)) != len(inv): raise SystemExit(f'Duplicate check names in {name}')
                inventories[name] = inv
                if split == 'test': manifest.setdefault(family, {})[name] = len(inv)
                outputs = OUTPUTS.get(family) or [p for p in r['root_paths_in_verifier'] if '*' not in p and p not in _inputs(split, family, name)]
                policies[split]['tasks'][f'{family}/{name}'] = {'expected_tests': len(inv), 'grader_hashes': grader_hashes(DATA / split / 'tasks' / family / name),
                                                               'required_output_paths': outputs}
                retain[name] = outputs + RETAIN_EXTRA.get(family, [])
    config = copy.deepcopy(pilot)
    config.update(campaign_id='followup-confirmatory-v1', budget_usd=19, data_root=f'{REMOTE}/followup-data', artifact_root=f'{REMOTE}/artifacts-confirmatory',
                  selected_before_test_manifest_id='followup-confirmatory-v1-selection', formal_policies_by_split=policies,
                  followup_required_outputs=retain)
    for name, value in (('splits.json', splits), ('check_inventories.json', inventories), ('test_manifest.json', manifest),
                        ('runtime_config.json', config), ('exclusions.json', EXCLUDED)):
        (out / name).write_text(json.dumps(value, indent=1, sort_keys=True) + '\n')
    print(json.dumps({'families': families, 'test_tasks': sum(len(v) for v in manifest.values()),
                      'tasks': len(inventories), 'excluded': sorted(EXCLUDED)}, indent=1))


def _inputs(split, family, name):
    import re
    env = DATA / split / 'tasks' / family / name / 'environment'
    copied = set(re.findall(r'COPY\s+\S+\s+(/root/\S+)', (env / 'Dockerfile').read_text()))
    return copied | {'/root/' + p.name for p in env.iterdir()}


if __name__ == '__main__':
    raise SystemExit(main())
