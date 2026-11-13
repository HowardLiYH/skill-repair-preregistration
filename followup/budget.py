"""The follow-up's spending ceiling on the original ledger.

The ledger's stored ceiling ($5) is never edited, and no new ledger is created. As in the first study, a recorded
extension raises the effective ceiling for one campaign: the owner's approval is written to a file, its hash is added
to the ledger's metadata under a new key (insert-only), and calls are admitted against the approved cumulative cap.
"""
import hashlib, json, sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src_v2')]
from capstudy.journal import Journal, AdmissionBlocked, money     # noqa: E402

LEDGER = ROOT / 'private_artifacts/deepseek-pilot/ledger.sqlite'
STORED_CEILING, CAMPAIGN = 5, 'deepseek-calibration-pilot'


def approval_record(path, *, stage, api_cap_usd, hosting_cap_usd, approved_by, approved_at, source):
    record = {'kind': 'followup-stage-budget-v1', 'stage': stage, 'api_cap_usd_cumulative': str(money(api_cap_usd)),
              'hosting_cap_usd_cumulative': str(money(hosting_cap_usd)), 'approved_by': approved_by,
              'approved_at': approved_at, 'source': source}
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_text()) != record: raise AdmissionBlocked('A different approval is already recorded')
    else:
        path.write_text(json.dumps(record, indent=1, sort_keys=True) + '\n')
    return path


class FollowupJournal(Journal):
    """The original ledger, admitting calls against the approved follow-up cap instead of the stored $5."""

    def __init__(self, approval_path, *, ledger=LEDGER):
        super().__init__(ledger, STORED_CEILING, campaign_id=CAMPAIGN)
        raw = Path(approval_path).read_bytes(); record = json.loads(raw)
        if record.get('kind') != 'followup-stage-budget-v1': raise AdmissionBlocked('Follow-up budget approval required')
        digest = hashlib.sha256(raw).hexdigest()
        key = 'budget_extension:followup-v1:' + record['stage']
        with self.transaction():
            old = self.connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            value = json.dumps({'approval_path': str(Path(approval_path).resolve()), 'sha256': digest,
                                'api_cap_usd_cumulative': record['api_cap_usd_cumulative']}, sort_keys=True)
            if old is None:
                self.connection.execute('INSERT INTO metadata VALUES(?,?)', (key, value))
            elif json.loads(old['value'])['sha256'] != digest:
                raise AdmissionBlocked('Recorded follow-up approval changed')
        self.budget = money(record['api_cap_usd_cumulative'])
        self.approval = record

    def headroom(self):
        t = self.totals(); return self.budget - t['spent'] - t['reserved']

    def warn_80(self):
        t = self.totals(); return (t['spent'] + t['reserved']) >= self.budget * Decimal('0.8')
