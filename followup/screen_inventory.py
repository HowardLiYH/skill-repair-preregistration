"""Frozen check inventory from the JUnit report the formal verifier itself produces (PYTEST_ADDOPTS
--junitxml), run on the untouched input. CTRF reports merge parametrized checks, so the inventory is taken from JUnit.
No model call.

  python3 -B followup/screen_inventory.py --data /opt/followup/followup-data --split dev --instance X --out DIR
"""
import argparse, json, shutil, subprocess, tempfile, xml.etree.ElementTree as ET
from pathlib import Path

REPORT_ADDOPTS = '--junitxml=/logs/verifier/capstudy-junit.xml'


def main(argv=None):
    ap = argparse.ArgumentParser()
    for n in ('--data', '--split', '--instance', '--out'): ap.add_argument(n, required=True)
    a = ap.parse_args(argv)
    family = a.instance.rsplit('-', 1)[0]
    source = Path(a.data) / a.split / 'tasks' / family / a.instance
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='inv-', dir=out)); task = work / a.instance
    shutil.copytree(source, task); (task / 'environment' / 'skills').mkdir(exist_ok=True)
    image = f'followup-inv-{a.instance}'.lower(); logs = work / 'logs'; logs.mkdir()
    try:
        subprocess.run(['docker', 'build', '-t', image, str(task / 'environment')], check=True, capture_output=True, timeout=3600)
        subprocess.run(['docker', 'run', '--rm', '-e', 'PYTEST_ADDOPTS=' + REPORT_ADDOPTS, '-v', f'{task / "tests"}:/tests:ro',
                        '-v', f'{logs}:/logs/verifier', '--entrypoint', 'bash', image, '/tests/test.sh'], capture_output=True, timeout=1800)
    finally:
        subprocess.run(['docker', 'image', 'rm', '-f', image], capture_output=True)
    names = [c.get('name') for c in ET.parse(logs / 'capstudy-junit.xml').getroot().iter('testcase')]
    result = {'instance': a.instance, 'split': a.split, 'family': family, 'inventory': names,
              'duplicates': sorted({n for n in names if names.count(n) > 1})}
    (out / f'{a.instance}.json').write_text(json.dumps(result, indent=1))
    print(json.dumps({'instance': a.instance, 'checks': len(names), 'duplicates': result['duplicates']}))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
