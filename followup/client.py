"""One editor call, recorded in the original spending ledger before and after it is sent.

Keys are read from the owner's private file at call time, passed straight to the HTTPS request and never
written, logged or placed in the environment. No automatic retries. A call whose outcome is unknown stays
reserved in the ledger and blocks further calls until it is reviewed.
"""
import json, sqlite3, sys, hashlib
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src_v2')]
from capstudy.connection_pilot import read_private_key, peak_accounting     # noqa: E402
from capstudy.contracts import write_json_atomic                             # noqa: E402
from capstudy.journal import Journal, AdmissionBlocked                       # noqa: E402
from capstudy.transports import post_json_once, parse_chat_completion, TransportError   # noqa: E402

LEDGER = ROOT / 'private_artifacts/deepseek-pilot/ledger.sqlite'
MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS = 110000, 16000      # together within the model's 131,072-token context
# Upper bound for one call at the published peak tariff ($0.30 / $1.20 per million), for a request that is
# checked to be within MAX_INPUT_TOKENS before it is sent.
RESERVE = (Decimal(MAX_INPUT_TOKENS) * Decimal('.30') + Decimal(MAX_OUTPUT_TOKENS) * Decimal('1.20')) / Decimal(1000000)
DEEPSEEK = {'provider': 'deepseek', 'model': 'deepseek-flash', 'url': 'https://api.deepseek.com/v1/chat/completions',
            'secret_file': ROOT / 'private_artifacts/secrets/deepseek.env',
            'settings': {'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'}}}


def backup_ledger(destination):
    """Consistent copy of the ledger taken before the first follow-up call; returns its SHA-256."""
    destination = Path(destination)
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(f'file:{LEDGER}?mode=ro', uri=True); target = sqlite3.connect(destination)
        with target: source.backup(target)
        source.close(); target.close()
    return hashlib.sha256(destination.read_bytes()).hexdigest()


class LedgerClient:
    def __init__(self, counter, out_dir, *, provider=DEEPSEEK, ledger_budget_usd=5, campaign_id='deepseek-calibration-pilot',
                 worker='followup-step0', post=post_json_once, journal=None):
        self.counter, self.out_dir, self.provider, self.worker, self.post = counter, Path(out_dir), provider, worker, post
        # The ledger's own stored ceiling is used as is (Journal refuses to open with any other value), unless the
        # caller passes the follow-up journal, which admits calls against the owner's recorded cumulative cap.
        self.journal = journal or Journal(LEDGER, ledger_budget_usd, campaign_id=campaign_id)
        self.calls = []

    def close(self): self.journal.close()

    def totals(self):
        t = self.journal.totals(); return {'spent': str(t['spent']), 'reserved': str(t['reserved']), 'unresolved': t['unresolved']}

    def call(self, identity, messages, temperature):
        estimate = int(sum(self.counter.count_file(m['content']) for m in messages) * 1.1) + 64
        if estimate > MAX_INPUT_TOKENS:
            raise AdmissionBlocked(f'Request estimate {estimate} tokens exceeds the reserved input bound')
        payload = {'model': self.provider['model'], 'messages': messages, 'max_tokens': MAX_OUTPUT_TOKENS, 'stream': False,
                   'temperature': temperature, **self.provider['settings']}
        identity = {'study': 'followup-v1', 'provider': self.provider['provider'], **identity}
        job = self.journal.schedule_and_claim(identity, payload, RESERVE, worker=self.worker, reuse_completed=True)
        folder = self.out_dir / 'calls' / job['job_id']
        if job['state'] == 'completed':                      # resume: an identical call already finished
            saved = self.journal.completed_response(job['job_id'])
            self.calls.append({'job_id': job['job_id'], 'charge_usd': job['charge'], 'reused': True})
            return saved['response_text'] or ''
        folder.mkdir(parents=True, exist_ok=True)
        write_json_atomic(folder / 'request.json', {'identity': identity, 'payload': payload, 'input_token_estimate': estimate})
        try:
            raw = self.post(self.provider['url'], payload, read_private_key(self.provider['secret_file']), 300)
        except TransportError as exc:
            write_json_atomic(folder / 'incident.json', {'error': str(exc), 'dispatched': exc.dispatched, 'evidence': exc.evidence})
            self.journal.incident(job['job_id'], str(exc), confirmed_not_dispatched=not exc.dispatched)
            raise
        result = peak_accounting(parse_chat_completion(raw, self.provider['model']))
        write_json_atomic(folder / 'response.json', result)
        usage = (result.get('usage') or {}).get('raw') or {}
        charge = result.get('accounted_charge_usd')
        if (charge is None or not 0 <= usage.get('prompt_tokens', -1) <= MAX_INPUT_TOKENS
                or not 0 <= usage.get('completion_tokens', -1) <= MAX_OUTPUT_TOKENS):
            # Leave the reservation in place for review instead of guessing a charge.
            self.journal.complete(job['job_id'], folder / 'response.json', None, provider_request_id=result.get('request_id'))
            raise AdmissionBlocked('Usage missing or outside the reserved bound; call left for reconciliation')
        self.journal.complete(job['job_id'], folder / 'response.json', charge, provider_request_id=result.get('request_id'))
        self.calls.append({'job_id': job['job_id'], 'charge_usd': charge, 'reused': False,
                           'prompt_tokens': usage['prompt_tokens'], 'completion_tokens': usage['completion_tokens'],
                           'finish': result.get('generation_failure_reason') or 'stop'})
        return result.get('response_text') or ''
