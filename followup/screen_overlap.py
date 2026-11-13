"""Screening check for task overlap (Notion 0.4 / C3): within each new family, does any test task duplicate or reword a
calibration or practice task? Compares instruction text (normalized similarity), the verifier folder (tests/) and the
environment folder by content hash. Offline; prints every pair with its evidence."""
import difflib, hashlib, json, re, sys
from pathlib import Path

DATA = Path(sys.argv[1] if len(sys.argv) > 1 else 'followup-data')


def tree_hash(path):
    h = hashlib.sha256()
    for f in sorted(p for p in Path(path).rglob('*') if p.is_file() and 'skills' not in p.parts):
        h.update(str(f.relative_to(path)).encode()); h.update(f.read_bytes())
    return h.hexdigest()[:16]


def norm(text): return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', text.lower())).strip()


rows = []
for family in sorted(p.name for p in (DATA / 'test/tasks').iterdir()):
    tasks = {}
    for split in ('train', 'dev', 'test'):
        for t in sorted((DATA / split / 'tasks' / family).iterdir()):
            tasks[t.name] = (split, t)
    for test_name, (s, tp) in tasks.items():
        if s != 'test': continue
        for other, (s2, op) in tasks.items():
            if s2 == 'test': continue
            sim = difflib.SequenceMatcher(None, norm((tp / 'instruction.md').read_text()), norm((op / 'instruction.md').read_text())).ratio()
            rows.append({'family': family, 'test': test_name, 'other': other, 'other_split': s2,
                         'instruction_similarity': round(sim, 3),
                         'same_instruction': norm((tp / 'instruction.md').read_text()) == norm((op / 'instruction.md').read_text()),
                         'same_tests': tree_hash(tp / 'tests') == tree_hash(op / 'tests'),
                         'same_environment': tree_hash(tp / 'environment') == tree_hash(op / 'environment')})
flag = [r for r in rows if r['same_instruction'] or r['same_tests'] or (r['same_environment'] and r['instruction_similarity'] > 0.8)
        or r['instruction_similarity'] > 0.9]
print(json.dumps({'pairs': len(rows), 'flagged_for_reading': flag}, indent=1))
print('max similarity per family:', {f: max(r['instruction_similarity'] for r in rows if r['family'] == f) for f in sorted({r['family'] for r in rows})})
Path('private_artifacts/followup-v1/screening').mkdir(parents=True, exist_ok=True)
Path('private_artifacts/followup-v1/screening/overlap.json').write_text(json.dumps(rows, indent=1))
