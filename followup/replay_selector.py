"""N22: replay the pilot's practice records under the original rule and the amended selector (no model call).
Writes private_artifacts/followup-v1/pilot/selector-replay.json. The replay is not evidence of test performance."""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from followup import pilot, controller                          # noqa: E402

c, *_ = pilot.build(None)
rows = []
for key in sorted(k[len('select:'):] for k in c.journal.done if k.startswith('select:')):
    recorded = c.journal.get('select:' + key)
    row = {'loop': key}
    for version in ('pilot-v1', controller.SELECTOR_VERSION):
        picks = {arm: c.replay_selection(key, arm, version) for arm in controller.ARMS}
        row[version] = {arm: {'id': p['id'], 'reason': p['reason'], 'check_score': p.get('practice_check_score')} for arm, p in picks.items()}
        row[version]['same_text'] = picks['repair']['id'] == picks['retry']['id'] == 'initial'   # both kept the shared starting rewrite
    row['original_matches_record'] = all(row['pilot-v1'][arm]['id'] == recorded[arm]['id'] for arm in controller.ARMS)
    rows.append(row)
summary = {'original_identical': sum(r['pilot-v1']['same_text'] for r in rows),
           'amended_identical': sum(r[controller.SELECTOR_VERSION]['same_text'] for r in rows), 'families': len(rows),
           'original_rule_reproduces_records': all(r['original_matches_record'] for r in rows),
           'note': 'Offline replay of practice records only; not evidence of improved test performance.'}
out = ROOT / 'private_artifacts/followup-v1/pilot/selector-replay.json'
out.write_text(json.dumps({'summary': summary, 'rows': rows}, indent=1))
print(json.dumps(summary))
for r in rows: print(r['loop'], {a: r[controller.SELECTOR_VERSION][a]['id'] for a in controller.ARMS})
