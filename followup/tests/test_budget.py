"""The follow-up budget guard on a copy of a ledger (never the real one)."""
import shutil, sqlite3, sys
from decimal import Decimal
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from followup import budget
from capstudy.journal import Journal, AdmissionBlocked


def ledger_copy(tmp):
    path = tmp / 'ledger.sqlite'
    j = Journal(path, 5, campaign_id=budget.CAMPAIGN)
    job = j.schedule_and_claim({'x': 1}, {'r': 1}, Decimal('4.40'), worker='w')
    resp = tmp / 'r.json'; resp.write_text('{}'); j.complete(job['job_id'], resp, Decimal('4.40')); j.close()
    return path


def test_cap_raises_ceiling_without_touching_stored_value(tmp_path):
    led = ledger_copy(tmp_path)
    appr = budget.approval_record(tmp_path / 'a.json', stage='stage1', api_cap_usd=16, hosting_cap_usd=31,
                                  approved_by='owner', approved_at='2026-11-11', source='thread')
    j = budget.FollowupJournal(appr, ledger=led)
    assert j.budget == Decimal('16') and j.headroom() == Decimal('11.60')
    job = j.schedule_and_claim({'y': 1}, {'r': 2}, Decimal('1'), worker='w')          # would exceed the stored $5
    assert job['state'] == 'running'
    with pytest.raises(AdmissionBlocked):
        j.schedule({'z': 1}, {'r': 3}, Decimal('11'))
    stored = sqlite3.connect(led).execute("select value from metadata where key='budget_usd'").fetchone()[0]
    assert stored == '5'


def test_changed_approval_is_refused(tmp_path):
    led = ledger_copy(tmp_path)
    appr = budget.approval_record(tmp_path / 'a.json', stage='stage1', api_cap_usd=16, hosting_cap_usd=31,
                                  approved_by='owner', approved_at='2026-11-11', source='thread')
    budget.FollowupJournal(appr, ledger=led).close()
    appr.write_text(appr.read_text().replace('16', '60'))
    with pytest.raises(AdmissionBlocked):
        budget.FollowupJournal(appr, ledger=led)
