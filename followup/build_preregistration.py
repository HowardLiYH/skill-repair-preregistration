"""Assemble the public pre-registration folder for the confirmatory study from the private repository.

  python3.11 followup/build_preregistration.py --notion EXPORT.md --out ../skill-repair-preregistration

Copies (never edits) the frozen inputs, computes the protocol hash, writes it into the server runtime config's
verifier policies, and lists every file with its SHA-256 in MANIFEST.json. Refuses to write files that contain local
paths, e-mail addresses, IP addresses or key-like strings.
"""
import argparse, hashlib, json, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / 'private_artifacts/followup-v1/confirmatory'
CODE = ['analysis.py', 'controller.py', 'editing.py', 'feedback.py', 'client.py', 'budget.py', 'runner.py', 'remote_run.py',
        'recover_trial.py', 'probe_solver.py', 'power_sim.py', 'screen_overlap.py', 'screen_server.py', 'screen_inventory.py',
        'confirmatory_config.py', 'collect_new_families.py', 'replay_selector.py', 'check_descriptions.json', '__init__.py',
        'confirmatory.py', 'gates.py', 'pilot.py', 'build_preregistration.py', 'preregistration_README.md']
CAPSTUDY = ['compress/xing.py', 'bundles.py', 'tokenization.py', 'deepseek_tokenization.py', 'native_evidence.py']
UNSAFE = [(r'/U' r'sers/[^/\s]+', 'local path'), (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}', 'e-mail address'),
          (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', 'IP address'), (r'sk-[A-Za-z0-9]{20,}', 'key'),
          (r'BEGIN [A-Z ]*PRIVATE KEY', 'private key')]
ALLOWED_IPS = {'1.1.1.1'}   # a fixed public resolver used as a network-policy test constant; never our server


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_safe(path):
    text = Path(path).read_text(errors='replace')
    for pattern, kind in UNSAFE:
        for m in re.finditer(pattern, text):
            hit = m.group(0)
            if kind == 'IP address':
                import ipaddress
                try: ip = ipaddress.ip_address(hit)
                except ValueError: continue                     # a version number, not an address
                if ip.is_private or ip.is_loopback or ip.is_unspecified or hit in ALLOWED_IPS: continue
            if kind == 'e-mail address' and hit.endswith(('example.com', 'anthropic.com')): continue
            raise SystemExit(f'{path}: contains a {kind}: {hit[:40]}')


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument('--notion', required=True); ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    for child in out.iterdir():
        if child.name != '.git': shutil.rmtree(child) if child.is_dir() else child.unlink()
    put = lambda src, dst: (Path(out / dst).parent.mkdir(parents=True, exist_ok=True), shutil.copy2(src, out / dst))
    put(a.notion, 'procedure/PROCEDURE-frozen-' + Path(a.notion).name.split('export-')[-1])
    put(ROOT / 'docs/FOLLOWUP_IMPLEMENTATION.md', 'plan/IMPLEMENTATION_PLAN.md')
    put(ROOT / 'docs/PILOT_REPORT.md', 'history/PILOT_REPORT.md')
    put(ROOT / 'docs/SCREENING_REPORT.md', 'history/SCREENING_REPORT.md')
    for name in ('splits.json', 'check_inventories.json', 'test_manifest.json', 'exclusions.json'):
        put(CONF / name, 'design/' + name)
    put(ROOT / 'results/followup/power_sim_confirmatory.json', 'design/power_simulation.json')
    # Same layout as the source repository, so the code and its offline tests run from this folder as published.
    for name in CODE: put(ROOT / 'followup' / name, 'followup/' + name)
    for name in ('test_analysis.py', 'test_controller.py', 'test_editing.py', 'test_budget.py'):
        put(ROOT / 'followup/tests' / name, 'followup/tests/' + name)
    for f in sorted((ROOT / 'followup/fixtures').glob('*.py')): put(f, 'followup/fixtures/' + f.name)
    for f in sorted((ROOT / 'src_v2').rglob('*')):
        if f.is_file() and '__pycache__' not in f.parts and f.suffix in ('.py', '.json', '.txt'):
            put(f, str(f.relative_to(ROOT)))
    for f in sorted((ROOT / 'prompts').glob('*.txt')): put(f, 'prompts/' + f.name)
    for f in sorted((ROOT / 'final/output/candidates/earthquake-plate-calculation').rglob('*')):   # test fixture (public skill + released rewrite)
        if f.is_file(): put(f, str(f.relative_to(ROOT)))
    seeds = {'test_order_and_dev_arm_order_seed': 20261111, 'bootstrap_seed': 5300, 'bootstrap_resamples': 10000,
             'power_simulation_seed': 20261112, 'selector_version': 'n21-2026-11-12'}
    (out / 'design/seeds.json').write_text(json.dumps(seeds, indent=1) + '\n')
    budget = {'api_cap_usd_cumulative': 19, 'hosting_cap_usd_cumulative': 31, 'approved': '2026-11-12, by the owner',
              'planned_agent_runs_max': {'calibration': 35, 'dev': 252, 'test': 306, 'total': 593},
              'note': 'Infrastructure replacements and editor calls are counted separately; caps are cumulative, including the first study and the pilot.'}
    (out / 'design/budget.json').write_text(json.dumps(budget, indent=1) + '\n')
    # Protocol hash: the procedure, the plan, the design files, the code and the seeds (everything except the runtime
    # config, which records the hash).
    frozen = sorted(p for p in out.rglob('*') if p.is_file() and '.git' not in p.parts)
    protocol = hashlib.sha256(''.join(f'{p.relative_to(out)}\0{sha(p)}\n' for p in frozen).encode()).hexdigest()
    config = json.loads((CONF / 'runtime_config.json').read_text())
    for policy in config['formal_policies_by_split'].values(): policy['protocol_sha256'] = protocol
    config['frozen_protocol_sha256'] = protocol          # one protocol identity everywhere
    (out / 'design/runtime_config.json').write_text(json.dumps(config, indent=1, sort_keys=True) + '\n')
    (CONF / 'runtime_config.frozen.json').write_text(json.dumps(config, indent=1, sort_keys=True) + '\n')   # the copy that is deployed
    shutil.copy2(ROOT / 'followup/preregistration_README.md', out / 'README.md')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    files = sorted(p for p in out.rglob('*') if p.is_file() and '.git' not in p.parts)
    for f in files: check_safe(f)
    manifest = {'protocol_sha256': protocol, 'source_commit_private_repository': commit,
                'files': {str(p.relative_to(out)): sha(p) for p in files}}
    (out / 'MANIFEST.json').write_text(json.dumps(manifest, indent=1) + '\n')
    print(json.dumps({'files': len(files) + 1, 'protocol_sha256': protocol}))


if __name__ == '__main__':
    raise SystemExit(main())
