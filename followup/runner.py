"""Mac side of one follow-up agent run: relay, tunnel, remote run, collection. Implements controller's runner interface.

For each run: a fresh relay token is written to the owner-private secrets folder; the metered gateway (first study's
BudgetedGateway, admitting each request against the follow-up cap on the original ledger) listens on 127.0.0.1 only;
an SSH reverse tunnel carries it to the server, where a forwarder exposes it only on the solver bridge; the server
runs remote_run.py; the verifier's JUnit file and the agent trace are copied back; the token is removed on both sides.
"""
import json, os, secrets, shlex, subprocess, sys, tarfile, tempfile, threading, time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from capstudy.connection_pilot import read_private_key                         # noqa: E402
from capstudy.deepseek_gateway import BudgetedGateway, OfficialTransport, make_server   # noqa: E402
from capstudy.runner import tree_hash                                          # noqa: E402
from followup.controller import TrialResult, InfraError, EvidencePending, Stop   # noqa: E402
import hashlib                                                                  # noqa: E402
import xml.etree.ElementTree as ET                                              # noqa: E402

COLLECT_TRIES = 3
TEXT_ONLY_REASON = 'Only text and local tool blocks are allowed'      # the relay's own refusal for non-text content


def unsupported_blocks(raw):
    """Content-block types the text-only relay does not accept, found in a request (types only; no content)."""
    try: body = json.loads(raw)
    except Exception: return ['unparseable']
    found = set()
    def walk(content):
        if not isinstance(content, list): return
        for block in content:
            if not isinstance(block, dict): continue
            kind = block.get('type')
            if kind == 'tool_result': walk(block.get('content'))
            elif kind not in ('text', 'tool_use'): found.add(str(kind)[:40])
    for message in body.get('messages', []) if isinstance(body, dict) else []:
        if isinstance(message, dict): walk(message.get('content'))
    return sorted(found)


def log_rejections(gateway, path):
    """Record every request the relay refuses before dispatch: time, its fixed reason text and the unsupported block
    types. Never the request's content."""
    original = gateway.handle
    def handle(route, raw, *args, **kwargs):
        try:
            return original(route, raw, *args, **kwargs)
        except ValueError as exc:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a') as f:
                f.write(json.dumps({'at': time.time(), 'reason': str(exc)[:200], 'unsupported_block_types': unsupported_blocks(raw)}) + '\n')
            raise
    gateway.handle = handle


def image_refusal_confirmed(run_dir, summary):
    """S4 amendment, Mac side: the server found the trace pattern (agent read an image, run ended in a 400) and the
    verifier graded the run; the relay's own log must show that the last thing it did was refuse a request whose only
    unsupported content was an image, with its text-only reason. Anything else stays infrastructure."""
    candidate = summary.get('image_refusal_candidate')
    if not candidate or candidate.get('verifier_status') not in ('pass', 'task_fail'): return False
    log = Path(run_dir) / 'gateway' / 'rejections.jsonl'
    if not log.exists(): return False
    records = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    if not records: return False
    last = records[-1]
    if last.get('reason') != TEXT_ONLY_REASON or last.get('unsupported_block_types') != ['image']: return False
    responses = [f.stat().st_mtime for f in (Path(run_dir) / 'gateway').rglob('response.json')]
    if responses and max(responses) > last['at']: return False            # the relay answered something after it
    return last


def gateway_charges(run_dir):
    total = 0.0
    for g in (Path(run_dir) / 'gateway').rglob('response.json'):
        try: total += float(json.loads(g.read_text()).get('accounted_upper_charge_usd') or 0)
        except Exception: pass
    return total


def gateway_fingerprints(run_dir):
    found = set()
    for g in (Path(run_dir) / 'gateway').rglob('*.json'):
        try: fp = json.loads(g.read_text()).get('system_fingerprint')
        except Exception: fp = None
        if fp: found.add(fp)
    return ','.join(sorted(found))

SECRETS = ROOT / 'private_artifacts/secrets'
KEY = SECRETS / 'hetzner_cis5300_ed25519'
REMOTE = '/opt/followup'
GATEWAY_PORT, TUNNEL_PORT = 18081, 18080


class ServerRunner:
    def __init__(self, *, host, known_hosts, journal, out_dir, remote_config, splits_by_task, data_root, remote_artifact_root):
        self.host, self.known_hosts, self.journal = host, Path(known_hosts), journal
        self.out = Path(out_dir); self.out.mkdir(parents=True, exist_ok=True)
        # The same server config and artifact root are used for launch, status checks and recovery.
        self.remote_config, self.split_of, self.data_root = remote_config, splits_by_task, Path(data_root)
        self.remote_artifact_root = remote_artifact_root
        # Same tool list as the first study's frozen solver mapping.
        self.allowed_tools = ('Bash', 'Edit', 'Glob', 'Grep', 'Read', 'Skill', 'Write')
        self.common = ['-i', str(KEY), '-o', f'UserKnownHostsFile="{self.known_hosts}"', '-o', 'StrictHostKeyChecking=yes',
                       '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=30']

    # -------------------------------------------------------------- controller interface
    def instruction(self, family, task):
        return (self.data_root / self.split_of[task] / 'tasks' / family / task / 'instruction.md').read_text()

    def run(self, family, task, files, label):
        trial_id = 'fu-' + secrets.token_hex(10)
        condition = {'full': 'full_skill', 'initial': 'unchanged_rewrite', 'repair': 'feedback_repair',
                     'retry': 'retry_no_feedback'}.get(label.split('/')[1].split('#')[0], 'unchanged_rewrite')
        if label.startswith('calibration/') and label.split('/')[1].split('#')[0] != 'full':
            condition = 'unchanged_rewrite'
        run_dir = self.out / trial_id; run_dir.mkdir()
        (run_dir / 'intent.json').write_text(json.dumps({'family': family, 'task': task, 'label': label, 'condition': condition}))
        try:
            summary = self._run(trial_id, family, task, files, condition, run_dir)
        except (InfraError, EvidencePending):
            raise
        except Exception as exc:                                  # transport, tunnel, gateway or remote failures
            if (run_dir / 'result.json').exists():               # graded: never convert into a replacement
                raise EvidencePending(f'{trial_id} graded; post-grading step failed: {type(exc).__name__}: {str(exc)[:200]}') from exc
            if (run_dir / 'launched').exists():                  # the remote run started: settle its status first
                state = self._remote_state(trial_id)
                if state == 'completed':
                    raise EvidencePending(f'{trial_id} completed remotely but its result was not received; recover it') from exc
                if state != 'not_completed':
                    raise Stop(f'{trial_id}: remote status uncertain after {type(exc).__name__}; reconcile before any new attempt') from exc
            (run_dir / 'incident.json').write_text(json.dumps({'error_type': type(exc).__name__, 'error': str(exc)[:500]}))
            raise InfraError(f'{type(exc).__name__}: {str(exc)[:200]}') from exc
        return self._accept(run_dir, summary)

    def _remote_state(self, trial_id):
        """completed / not_completed (no process left) / uncertain."""
        try:
            out = self._ssh(f"test -f {self.remote_artifact_root}/jobs/{trial_id}/completed.json && echo completed || "
                            f"(pgrep -f 'remote_run.py.*{trial_id}' >/dev/null && echo running || echo not_completed)", check=False).stdout.strip()
        except Exception:
            return 'uncertain'
        return out if out in ('completed', 'not_completed') else 'uncertain'

    # -------------------------------------------------------------- recovery (never a new model run)
    def _find(self, label, task=None):
        def match(d):
            intent = json.loads((d / 'intent.json').read_text())
            return intent['label'] == label and (task is None or intent['task'] == task)
        dirs = [d for d in self.out.iterdir() if (d / 'intent.json').exists() and match(d)]
        if len(dirs) != 1: raise Stop(f'Attempt {label}: {len(dirs)} local records; reconcile by hand before any new attempt')
        return dirs[0]

    def recover(self, label):
        """An attempt dispatched earlier with no recorded outcome. Never a new model run:
        graded locally -> collect its evidence; completed on the server -> read its saved job record there and
        classify it under the current rules (graded -> collect; ungraded -> InfraError for the recorded replacement);
        never launched, or no longer running without a record -> InfraError; still running or unreachable -> Stop."""
        run_dir = self._find(label)
        if (run_dir / 'result.json').exists(): return self._collect(run_dir)
        if (run_dir / 'ungraded.json').exists(): raise InfraError(f'{run_dir.name}: recorded as ungraded')
        if not (run_dir / 'launched').exists(): raise InfraError(f'{run_dir.name}: never launched')
        state = self._remote_state(run_dir.name)
        if state == 'not_completed': raise InfraError(f'{run_dir.name}: remote run ended without a record')
        if state != 'completed': raise Stop(f'Attempt {label} ({run_dir.name}): remote state {state}; reconcile before any new attempt')
        summary = self._remote_summary(run_dir)
        return self._accept(run_dir, summary)

    def _remote_summary(self, run_dir):
        intent = json.loads((run_dir / 'intent.json').read_text())
        out = self._ssh(f"cd {REMOTE}/repo && python3 -B followup/recover_trial.py --config {self.remote_config} "
                        f"--trial-id {run_dir.name} --instance {shlex.quote(intent['task'])} --split {self.split_of[intent['task']]}",
                        timeout=1800, check=False)
        if out.returncode: raise Stop(f'{run_dir.name}: recovery failed: {out.stderr[-300:]}')
        summary = json.loads(out.stdout.strip().splitlines()[-1])
        (run_dir / f'recovered-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}.json').write_text(json.dumps(summary, indent=1))
        return summary

    def _accept(self, run_dir, summary):
        """Apply the Mac-side checks to a server summary: graded -> record and collect; an image-refusal candidate
        becomes graded only with the relay's own recorded image-only rejection; anything else is infrastructure."""
        if summary['status'] not in ('pass', 'task_fail'):
            confirmed = image_refusal_confirmed(run_dir, summary)
            if not confirmed:
                (run_dir / 'ungraded.json').write_text(json.dumps(summary, indent=1))
                raise InfraError(f"Run ended as {summary['status']} ({summary.get('diagnostic')})")
            summary = dict(summary, status=summary['image_refusal_candidate']['verifier_status'],
                           amendment={'rule': 'S4-amendment-2026-11-12-agent-image-text-only-relay', 'original_status': summary['status'],
                                      'trace_evidence': summary['image_refusal_candidate']['trace'], 'relay_evidence': confirmed})
        if not (run_dir / 'meta.json').exists():
            (run_dir / 'meta.json').write_text(json.dumps({'cost_usd': gateway_charges(run_dir), 'cost_basis': 'gateway accounted upper charge',
                                                           'fingerprint': gateway_fingerprints(run_dir)}))
        if not (run_dir / 'result.json').exists(): (run_dir / 'result.json').write_text(json.dumps(summary, indent=1))
        return self._collect(run_dir)

    def amend_recover(self, label, task=None):
        """S4 amendment for a recorded infrastructure failure: re-read its saved job record on the server under the
        current rules; graded only with trace and relay evidence (see _accept). The original records stay."""
        run_dir = self._find(label, task)
        summary = self._remote_summary(run_dir)
        if summary['status'] in ('pass', 'task_fail') and not summary.get('amendment'): return summary, None
        try:
            result = self._accept(run_dir, summary)
        except InfraError:
            return summary, None
        return json.loads((run_dir / 'result.json').read_text()), result

    def recollect(self, label):
        run_dir = self._find(label)
        for name in ('junit.xml', 'claude-code.txt', 'outputs.tar'): (run_dir / name).unlink(missing_ok=True)
        return self._collect(run_dir)

    def _collect(self, run_dir):
        """Copy the graded run's evidence back, retrying the copy only. Raises EvidencePending if it cannot be
        completed: the stage pauses and the evidence is recovered later; the model is never asked again."""
        result = json.loads((run_dir / 'result.json').read_text())
        if result.get('trial_id') != run_dir.name: raise EvidencePending(f'{run_dir.name}: result belongs to {result.get("trial_id")}')
        if (run_dir / 'outputs-recovered.json').exists():  # a later export of the same graded run (result.json is kept as recorded)
            result.update(json.loads((run_dir / 'outputs-recovered.json').read_text()))
        if result.get('outputs_error') and not result.get('outputs_sha256'):
            # Retry the export only (no model run): re-read the saved job record on the server.
            again = self._remote_summary(run_dir)
            fields = {k: again[k] for k in ('outputs_tar', 'outputs_sha256', 'outputs_bytes', 'outputs_error', 'outputs_none_required') if k in again}
            (run_dir / 'outputs-recovered.json').write_text(json.dumps(fields, indent=1))
            result.update(fields)
        last = None
        for _ in range(COLLECT_TRIES):
            try:
                for rel, name in (('verifier/capstudy-junit.xml', 'junit.xml'), ('agent/claude-code.txt', 'claude-code.txt')):
                    if not (run_dir / name).exists():
                        self._scp(f"root@{self.host}:{shlex.quote(result['trial_dir'] + '/' + rel)}", str(run_dir / name))
                ET.parse(run_dir / 'junit.xml')                   # readable and valid XML
                if result.get('outputs_sha256'):
                    if not (run_dir / 'outputs.tar').exists():
                        self._scp(f"root@{self.host}:{shlex.quote(result['outputs_tar'])}", str(run_dir / 'outputs.tar'))
                    if hashlib.sha256((run_dir / 'outputs.tar').read_bytes()).hexdigest() != result['outputs_sha256']:
                        (run_dir / 'outputs.tar').unlink(); raise ValueError('outputs.tar hash mismatch')
                elif not result.get('outputs_none_required'):
                    raise EvidencePending(f"{run_dir.name}: essential outputs were not exported ({result.get('outputs_error')}); stop before cleanup")
                last = None; break
            except EvidencePending:
                raise
            except Exception as exc:
                last = exc
                for name in ('junit.xml',):
                    try: ET.parse(run_dir / name)
                    except Exception: (run_dir / name).unlink(missing_ok=True)
                time.sleep(5)
        if last is not None: raise EvidencePending(f'{run_dir.name}: evidence collection failed: {type(last).__name__}: {str(last)[:200]}')
        meta = json.loads((run_dir / 'meta.json').read_text())
        return TrialResult(status=result['status'], checks_passed=int(result['passed_tests'] or 0),
                           checks_total=int(result['executed_tests'] or 0), cost_usd=meta['cost_usd'],
                           timed_out=bool(result['agent_timed_out']), junit_path=str(run_dir / 'junit.xml'),
                           trace_path=str(run_dir / 'claude-code.txt'), fingerprint=meta['fingerprint'], run_id=run_dir.name)

    # -------------------------------------------------------------- one run
    def _ssh(self, command, timeout=120, check=True):
        return subprocess.run(['ssh', *self.common, f'root@{self.host}', command], capture_output=True, text=True,
                              timeout=timeout, check=check)

    def _scp(self, src, dst, timeout=600):
        subprocess.run(['scp', '-q', *self.common, src, dst], check=True, capture_output=True, timeout=timeout)

    def _run(self, trial_id, family, task, files, condition, run_dir):
        remote_dir = f'{REMOTE}/trials/{trial_id}'
        token_value = 'capstudy-relay-' + secrets.token_urlsafe(48)
        token = SECRETS / f'followup-{trial_id}.relay.token'
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: f.write(token_value + '\n')
        server = tunnel = None; stop = threading.Event()
        spent_before = self.journal.totals()['spent']
        try:
            bundle_args, bundle_sha = '', ''
            self._ssh(f'mkdir -p {remote_dir} && chmod 700 {remote_dir}')
            if condition != 'full_skill':
                bundle = run_dir / 'bundle'
                for rel, text in files.items():
                    p = bundle / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
                bundle_sha = tree_hash(bundle)
                with tarfile.open(run_dir / 'bundle.tar', 'w') as tar: tar.add(bundle, arcname='bundle')
                self._scp(str(run_dir / 'bundle.tar'), f'root@{self.host}:{remote_dir}/bundle.tar')
                self._ssh(f'cd {remote_dir} && tar -xf bundle.tar && rm bundle.tar')
                bundle_args = f' --bundle {remote_dir}/bundle --bundle-sha256 {bundle_sha}'
            self._scp(str(token), f'root@{self.host}:{remote_dir}/relay.token')
            self._ssh(f'chmod 600 {remote_dir}/relay.token')
            gateway = BudgetedGateway(self.journal, run_dir / 'gateway', OfficialTransport(read_private_key(SECRETS / 'deepseek.env'), timeout_seconds=300),
                                      relay_token=token_value, trial_id=trial_id,
                                      allowed_tools=frozenset(self.allowed_tools))
            log_rejections(gateway, run_dir / 'gateway' / 'rejections.jsonl')
            server = make_server(gateway, ('127.0.0.1', GATEWAY_PORT)); server.timeout = 1
            tunnel = subprocess.Popen(['ssh', *self.common, '-N', '-o', 'ExitOnForwardFailure=yes', '-R',
                                       f'127.0.0.1:{TUNNEL_PORT}:127.0.0.1:{GATEWAY_PORT}', f'root@{self.host}'],
                                      stdout=subprocess.DEVNULL, stderr=open(run_dir / 'tunnel.stderr', 'w'))
            time.sleep(3)
            if tunnel.poll() is not None: raise RuntimeError('Reverse tunnel exited')
            split = self.split_of[task]
            command = (f'cd {REMOTE}/repo && python3 -B followup/remote_run.py --config {self.remote_config} '
                       f'--trial-id {trial_id} --instance {shlex.quote(task)} --split {split} --condition {condition} '
                       f'--token-file {remote_dir}/relay.token{bundle_args}')
            # The ledger's SQLite connection belongs to this thread, so the gateway is served here while the
            # remote run waits in a helper thread.
            box = {}
            remote = threading.Thread(target=lambda: box.update(proc=self._ssh(command, timeout=11000, check=False)), daemon=True)
            (run_dir / 'launched').write_text(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            remote.start(); began = time.monotonic()
            while remote.is_alive():
                server.handle_request()
                if tunnel.poll() is not None: raise RuntimeError('Reverse tunnel exited during the run')
                if time.monotonic() - began > 11100: raise TimeoutError('Remote run exceeded its supervisor limit')
            if 'proc' not in box: raise RuntimeError('Remote command did not complete')
            proc = box['proc']
            (run_dir / 'remote.stdout').write_text(proc.stdout); (run_dir / 'remote.stderr').write_text(proc.stderr[-20000:])
            if proc.returncode: raise RuntimeError(f'Remote run failed with exit {proc.returncode}')
            result = json.loads(proc.stdout.strip().splitlines()[-1])
            (run_dir / 'meta.json').write_text(json.dumps({'cost_usd': float(self.journal.totals()['spent'] - spent_before),
                                                           'fingerprint': gateway_fingerprints(run_dir)}))
        finally:
            stop.set()
            if tunnel is not None and tunnel.poll() is None: tunnel.terminate(); tunnel.wait(30)
            if server is not None: server.server_close()
            try: self._ssh(f'rm -f {remote_dir}/relay.token', check=False)
            except Exception: pass
            token.unlink(missing_ok=True)
        return result

